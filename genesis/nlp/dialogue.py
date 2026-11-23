"""Dialogue management: state tracking, coreference and slot filling.

The dialogue state tracker keeps, per conversation:

* a short-term history window (for the LLM and for context),
* the most recent referents — person, file, folder, app, project, e-mail draft —
  so that "him", "it", "that file" and "send it" resolve correctly,
* an open *frame* (intent + slots) when Genesis asked a clarifying question,
* a queue of remaining sub-requests from compound utterances.

Example::

    user:    Draft an email to Rahul.
    genesis: What should I say?              (frame=draft_email, missing=message)
    user:    Tell him I'll reach at 5.
    → slot message="I'll reach at 5", recipient=Rahul (from the frame / "him")
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from . import rules
from .entities import EMAIL_RE, find_filenames, find_folder, find_paths, parse_datetime
from .intents import Intent, NLUResult

PERSON_PRONOUNS = {"him", "her", "them", "he", "she", "they", "that person", "this person"}
FILE_PRONOUNS = {"it", "this", "that", "this file", "that file", "the file", "the document", "that document", "the pdf",
                 "this document", "the same file"}


@dataclass
class Referents:
    person: Optional[dict] = None  # {"name":..., "email":...}
    file: Optional[str] = None
    folder: Optional[str] = None
    app: Optional[str] = None
    url: Optional[str] = None
    project: Optional[str] = None
    project_path: Optional[str] = None
    draft: Optional[dict] = None  # {"draft_id","to","subject"}


@dataclass
class Focus:
    """Something the conversation is currently 'about' (for "it", "that", "open it")."""

    kind: str  # file | folder | app | url | email | draft | project | person | issue | reminder
    value: dict
    label: str = ""


@dataclass
class ListContext:
    """The most recent enumerable result ("the second one", "the latest one", "the other one")."""

    kind: str  # file | email | web | issue | reminder | contact
    items: list[dict]
    newest_first: bool = False
    selected: Optional[int] = None
    verb: str = "open"


@dataclass
class DialogueState:
    conversation_id: str
    history: deque = field(default_factory=lambda: deque(maxlen=24))
    ref: Referents = field(default_factory=Referents)
    frame: Optional[NLUResult] = None
    pending_slot: Optional[str] = None
    pending_question: Optional[str] = None
    queue: list = field(default_factory=list)
    pending_confirmation_id: Optional[str] = None
    session_tasks: list[dict] = field(default_factory=list)
    # --- conversational task context (short-term, never persisted) ---
    focus: list[Focus] = field(default_factory=list)  # most recent first
    last_list: Optional[ListContext] = None
    last_request: Optional[NLUResult] = None  # last executed actionable request (for "do that again")
    last_failed: Optional[NLUResult] = None  # for "try again"
    last_memory: Optional[tuple] = None  # ("fact", id) | ("contact", id) | ("project", name) for "forget that"
    folder_history: list[str] = field(default_factory=list)  # for "go back"
    pending_suggestion: Optional[dict] = None  # e.g. routine suggestion awaiting "yes"
    pending_yesno: Optional[dict] = None  # a simple yes/no question Genesis asked (e.g. "did you mean Srikanth?")
    turn_source: str = "text"  # text | voice | routine — for the current turn
    language: str = "en"  # language of the conversation (sticky across short answers like names or "yes")
    chat_language: Optional[str] = None  # set for one turn when the chat model should answer directly in that language
    last_name_heard: Optional[str] = None
    undo_stack: list = field(default_factory=list)  # [{"tool","args","description"} | {"irreversible": msg}]
    last_correction: Optional[str] = None
    current_task: Optional[dict] = None  # {"request","status","intent"} of the latest plan
    # a send/submit the user postponed ("don't send it yet") → "okay, send it" later runs exactly this, with its preview
    deferred_action: Optional[dict] = None  # {"tool","args","description","at"}

    def push_focus(self, kind: str, value: dict, label: str = "") -> None:
        key = (kind, value.get("id") or value.get("path") or value.get("url") or value.get("key") or label)
        self.focus = [f for f in self.focus
                      if (f.kind, f.value.get("id") or f.value.get("path") or f.value.get("url") or f.value.get("key") or f.label) != key]
        self.focus.insert(0, Focus(kind, value, label))
        del self.focus[12:]

    def top_focus(self, kinds: Optional[set] = None, skip: int = 0) -> Optional[Focus]:
        hits = [f for f in self.focus if kinds is None or f.kind in kinds]
        return hits[skip] if len(hits) > skip else None

    def add_turn(self, role: str, content: str) -> None:
        self.history.append({"role": role, "content": content})

    def clear_frame(self) -> None:
        self.frame = None
        self.pending_slot = None
        self.pending_question = None


# File-operation slot requirements depend on the operation.
FILE_OP_SLOTS = {
    "rename": [("source", "Which file should I rename?"), ("new_name", "What should I rename it to?")],
    "move": [("source", "Which file should I move?"), ("destination", "Where should I move it?")],
    "copy": [("source", "Which file should I copy?"), ("destination", "Where should I copy it to?")],
    "delete": [("source", "Which file should I delete?")],
    "read": [("source", "Which file should I read?")],
    "open": [("source", "Which file should I open?")],
    "create_file": [("name", "What should the file be called?")],
    "create_folder": [("name", "What should the folder be called?")],
}


class DialogueManager:
    def __init__(self) -> None:
        self._states: dict[str, DialogueState] = {}

    def state(self, conversation_id: str) -> DialogueState:
        if conversation_id not in self._states:
            self._states[conversation_id] = DialogueState(conversation_id)
        return self._states[conversation_id]

    def reset(self, conversation_id: str) -> None:
        self._states.pop(conversation_id, None)

    # ----------------------------------------------------------- references
    def resolve_references(self, nlu: NLUResult, st: DialogueState) -> NLUResult:
        s = nlu.slots
        low = nlu.text.lower()
        ref = st.ref
        if nlu.intent in (Intent.DRAFT_EMAIL, Intent.SEND_EMAIL):
            rec = (s.get("recipient") or "").lower()
            if (rec in PERSON_PRONOUNS or (not rec and re.search(r"\b(him|her|them)\b", low))) and ref.person:
                s["recipient"] = ref.person.get("name")
                if ref.person.get("email") and not s.get("email"):
                    s["email"] = ref.person["email"]
            if s.get("attach_last_file") and ref.file:
                s.setdefault("attachments", [ref.file])
            if s.get("use_last_draft") and ref.draft:
                s["draft_id"] = ref.draft.get("draft_id")
        if nlu.intent == Intent.FILE_OPERATION:
            src = (s.get("source") or "").lower()
            if src in FILE_PRONOUNS and ref.file:
                s["source"] = ref.file
            elif src in FILE_PRONOUNS:
                s.pop("source", None)
            dst = (s.get("destination") or "").lower()
            if dst in {"there", "that folder", "the folder", "it"} and ref.folder:
                s["destination"] = ref.folder
        if nlu.intent == Intent.CODE_ACTION:
            if s.get("code_action") in {"check_file", "explain", "open_file"} and not s.get("path") and ref.file \
                    and (s.get("code_action") != "explain" or not s.get("error_text")):
                s["path"] = ref.file
            if s.get("code_action") == "open_project" and not s.get("project") and ref.project:
                s["project"] = ref.project
        if nlu.intent == Intent.MEMORY and s.get("memory_op") == "add_contact" and not s.get("contact_name") and ref.person:
            s["contact_name"] = ref.person.get("name")
        if nlu.intent == Intent.OPEN_APPLICATION and (s.get("app") in {"it", "that"}) and ref.app:
            s["app"] = ref.app
        return nlu

    def missing_slots(self, nlu: NLUResult) -> list[tuple[str, str]]:
        missing = nlu.missing_slots()
        if nlu.intent == Intent.FILE_OPERATION:
            op = nlu.slots.get("file_op")
            missing = [(slot, q) for slot, q in FILE_OP_SLOTS.get(op, []) if not nlu.slots.get(slot)]
        if nlu.intent == Intent.SEND_EMAIL and nlu.slots.get("use_last_draft"):
            return [] if nlu.slots.get("draft_id") else [("draft_id", "__no_draft__")]
        return missing

    # -------------------------------------------------------- slot filling
    def ask(self, st: DialogueState, nlu: NLUResult, slot: str, question: str) -> str:
        st.frame = nlu
        st.pending_slot = slot
        st.pending_question = question
        return question

    def fill_pending_slot(self, st: DialogueState, answer: str, now: Optional[datetime] = None) -> Optional[NLUResult]:
        """Apply the user's answer to the open frame. Returns ``None`` if the answer is a new request."""
        if st.frame is None or st.pending_slot is None:
            return None
        text = rules.normalize(answer)
        slot = st.pending_slot
        if slot in ("confirm_name", "repo") and not text:
            text = answer.strip().strip(".!?")  # a repository literally called "genesis" is a name, not the wake word
        # a brand-new, unrelated command abandons the frame (except for free-text slots)
        if slot not in {"message", "reminder_text", "content", "cal_time", "cal_when", "text"}:
            probe = rules.parse(text, now)
            if probe is not None and probe.intent not in (Intent.CONVERSATION, Intent.UNKNOWN) and probe.intent != st.frame.intent \
                    and len(text.split()) > 1:
                st.clear_frame()
                return None
        if probe_cancel(text):
            st.clear_frame()
            return NLUResult(text=answer, intent=Intent.CANCEL, confidence=1.0, source="dialogue")

        value: Any = text
        frame = st.frame
        if slot == "text":  # a WhatsApp message: "tell him I'm running late" → "I'm running late"
            value = strip_message_prefix(text)
        elif slot == "reminder_text":  # "What should I remind you about?" → "to drink water" → "drink water"
            value = re.sub(r"^(?:to|about|that)\s+", "", text, flags=re.I).strip() or text
        elif slot == "message":
            value = strip_message_prefix(text)
            # "tell him ..." also confirms the recipient
            if re.search(r"\b(him|her|them)\b", text, re.I) and not frame.slots.get("recipient") and st.ref.person:
                frame.slots["recipient"] = st.ref.person.get("name")
        elif slot in {"recipient"}:
            em = EMAIL_RE.search(text)
            if em:
                frame.slots["email"] = em.group(0)
            value = re.sub(r"^(?:to|send it to|it'?s for|for)\s+", "", text, flags=re.I).strip()
            if value.lower() in PERSON_PRONOUNS and st.ref.person:
                value = st.ref.person.get("name")
        elif slot == "email":
            if re.fullmatch(r"(skip|leave it( blank| empty)?|none|no|don'?t know|i don'?t know)", text, re.I):
                value = "__blank__"
            else:
                em = EMAIL_RE.search(text)
                if not em:
                    st.pending_question = "That doesn't look like an e-mail address. What is it? (or say 'leave it blank')"
                    return NLUResult(text=answer, intent=Intent.UNKNOWN, confidence=0.0, source="dialogue",
                                     slots={"__reask__": st.pending_question})
                value = em.group(0)
        elif slot in ("cal_time", "cal_when"):
            from .entities import date_time_parts

            day, tm = date_time_parts(text, now)
            if tm is None and (slot == "cal_time" or day is None):
                st.pending_question = "Sorry, what time? (for example “4 PM” or “tomorrow at 10”)"
                return NLUResult(text=answer, intent=Intent.UNKNOWN, confidence=0.0, source="dialogue",
                                 slots={"__reask__": st.pending_question})
            if day is not None:
                frame.slots["date"] = day.isoformat()
            if tm is not None:
                frame.slots["time"] = f"{tm[0]:02d}:{tm[1]:02d}"
            value = frame.slots.get("time") or ""
        elif slot == "context_file":
            paths = find_paths(text) or find_filenames(text)
            value = paths[0].value if paths else text.strip(" \"'")
        elif slot == "datetime":
            parsed = parse_datetime(text, now)
            if not parsed:
                st.pending_question = "Sorry, I didn't catch the time. When should I remind you? (e.g. 'tomorrow at 9 am')"
                return NLUResult(text=answer, intent=Intent.UNKNOWN, confidence=0.0, source="dialogue",
                                 slots={"__reask__": st.pending_question})
            value = parsed.value
        elif slot in {"source", "destination"}:
            paths = find_paths(text) or find_filenames(text)
            folder = find_folder(text)
            if slot == "destination" and folder and len(text.split()) <= 3:
                value = folder.value
            elif paths:
                value = paths[0].value
            else:
                value = rules._strip_article(text)
        elif slot in {"new_name", "name"}:
            value = re.sub(r"^(?:call it|name it|rename it to|to|as)\s+", "", text, flags=re.I).strip(" \"'")
        elif slot == "app":
            value = rules.app_slot("open " + text) or text.lower()
        elif slot == "folder":
            f = find_folder(text)
            value = f.value if f else text
        elif slot == "query":
            value = re.sub(r"^(?:for|about)\s+", "", text, flags=re.I)
        frame.slots[slot] = value
        frame.source = "dialogue"
        st.clear_frame()
        return frame

    # --------------------------------------------------------- bookkeeping
    def remember_referents(self, st: DialogueState, nlu: Optional[NLUResult], data: dict[str, Any]) -> None:
        """Update referents from slots and tool results (so later pronouns resolve)."""
        if nlu is not None:
            s = nlu.slots
            if s.get("recipient") and s.get("recipient") not in PERSON_PRONOUNS and s.get("recipient") != "me":
                st.ref.person = {"name": s["recipient"], "email": s.get("email") or data.get("to")}
            if nlu.intent in (Intent.OPEN_APPLICATION, Intent.CLOSE_APPLICATION) and s.get("app"):
                st.ref.app = s["app"]
        if data.get("path"):
            p = str(data["path"])
            if data.get("is_dir"):
                st.ref.folder = p
            else:
                st.ref.file = p
        if data.get("folder_path"):
            st.ref.folder = str(data["folder_path"])
        if data.get("draft_id") or data.get("compose_web"):
            st.ref.draft = {"draft_id": data.get("draft_id"), "to": data.get("to"), "subject": data.get("subject"),
                            "body": data.get("body"), "thread_id": data.get("thread_id"),
                            "in_reply_to": data.get("in_reply_to"), "web": bool(data.get("compose_web"))}
        if data.get("project_path"):
            st.ref.project = data.get("project")
            st.ref.project_path = str(data["project_path"])
        if data.get("url"):
            st.ref.url = data["url"]

    def _record_run(self, st: DialogueState, data: dict) -> None:
        _record_run_impl(st, data)

    # ------------------------------------------------------ task context
    def update_context(self, st: DialogueState, tool: str, data: dict[str, Any], args: Optional[dict] = None) -> None:
        """Record what a successful tool call produced, so follow-ups ("open it", "the second one") resolve."""
        args = args or {}
        if tool in ("whatsapp_draft", "whatsapp_send", "gmail_create_draft", "gmail_update_draft", "gmail_send_draft"):
            st.deferred_action = None  # a newer draft/send replaces anything the user postponed earlier
        undo = undo_for(tool, data, args)
        if undo is not None:
            st.undo_stack.append(undo)
            st.undo_stack = st.undo_stack[-15:]
        if tool in ("open_application", "close_application"):
            key = data.get("app") or args.get("app")
            if key:
                st.push_focus("app", {"key": key, "url": data.get("url")}, str(key))
        elif tool in ("open_folder",):
            prev = st.top_focus({"folder"})
            if args.get("__back__"):
                pass  # navigating back must not push the folder we are leaving back onto the history
            elif prev is not None and prev.value.get("path") and prev.value.get("path") != data.get("path") and \
                    (not st.folder_history or st.folder_history[-1] != prev.value["path"]):
                st.folder_history.append(prev.value["path"])
                st.folder_history = st.folder_history[-20:]
            st.push_focus("folder", {"path": data.get("path")}, _name(data.get("path")))
        elif tool in ("open_file", "read_file", "create_file", "rename_path", "move_path", "copy_path", "vscode_open_file",
                      "create_folder"):
            if data.get("path"):
                st.push_focus("folder" if data.get("is_dir") else "file", {"path": data["path"]}, _name(data["path"]))
        elif tool == "search_files" and data.get("results"):
            st.last_list = ListContext("file", [{"path": p, "label": _name(p)} for p in data["results"]],
                                       newest_first=args.get("sort") == "newest")
            st.push_focus("file", {"path": data["results"][0]}, _name(data["results"][0]))
            st.last_list.selected = 0
        elif tool in ("open_url", "browser_open"):
            if data.get("url"):
                st.push_focus("url", {"url": data["url"], "genesis_browser": tool == "browser_open"}, data.get("title") or data["url"])
        elif tool == "web_search" and data.get("results"):
            st.last_list = ListContext("web", [{"url": r.get("url"), "label": r.get("title", "")} for r in data["results"]])
        elif tool == "gmail_search" and data.get("messages"):
            st.last_list = ListContext("email", [dict(m, label=m.get("subject", "")) for m in data["messages"]], newest_first=True)
        elif tool in ("gmail_open_message", "gmail_read_message"):
            st.push_focus("email", dict(data), data.get("subject", ""))
            if data.get("from_email"):
                st.ref.person = {"name": data.get("from_name") or data["from_email"], "email": data["from_email"]}
        elif tool in ("gmail_create_draft", "gmail_update_draft", "gmail_compose_web"):
            st.push_focus("draft", dict(st.ref.draft or {}, **{k: data.get(k) for k in ("draft_id", "subject", "to", "url")}),
                          data.get("subject", "draft"))
        elif tool == "vscode_open_project" and data.get("project_path"):
            st.push_focus("project", {"path": data["project_path"], "key": data.get("project")}, data.get("project", ""))
        elif tool in ("diagnose_project", "check_code_file") and data.get("issues"):
            st.last_list = ListContext("issue", [dict(i, label=i.get("message", "")) for i in data["issues"]])
        elif tool == "list_reminders" and data.get("reminders"):
            st.last_list = ListContext("reminder", [dict(r, label=r.get("text", "")) for r in data["reminders"]])
        elif tool in ("add_contact",) and data.get("contact_id"):
            st.last_memory = ("contact", data["contact_id"])
        elif tool == "remember" and data.get("memory_id"):
            st.last_memory = ("fact", data["memory_id"])
        elif tool == "remember_project" and data.get("project"):
            st.last_memory = ("project", data["project"])
            st.push_focus("project", {"path": data.get("path"), "key": data["project"]}, data["project"])
        elif tool == "apply_patch" and data.get("backup"):
            st.push_focus("patch", {"path": data["path"], "backup": data["backup"]}, _name(data["path"]))
        elif tool == "create_routine" and data.get("routine_id"):
            st.last_memory = ("routine", data["routine_id"])
        elif tool == "remember_preference" and data.get("key"):
            st.last_memory = ("preference", f"{data['key']}|{data.get('memory_id', '')}")
        # ---- 1.2 capabilities
        elif tool == "youtube_search" and data.get("results"):
            st.last_list = ListContext("video", [dict(r, label=r.get("title", "")) for r in data["results"]], verb="play")
            st.push_focus("app", {"key": "youtube"}, "YouTube")
        elif tool == "youtube_play" and data.get("url"):
            st.push_focus("media", {"url": data["url"], "video_id": data.get("video_id"), "title": data.get("title", ""),
                                    "key": "youtube"}, data.get("title") or "video")
        elif tool == "list_calendar_events" and data.get("events"):
            st.last_list = ListContext("event", [dict(e, label=e.get("title", "")) for e in data["events"]], verb="open")
        elif tool in ("create_calendar_event", "update_calendar_event", "find_calendar_event") and data.get("event_id"):
            st.push_focus("event", dict(data), data.get("title", "event"))
        elif tool == "search_history" and data.get("items"):
            st.last_list = ListContext("history", [dict(it) for it in data["items"]])
        elif tool == "browser_list_elements" and data.get("elements"):
            st.last_list = ListContext("element", [dict(e, label=e.get("name", "")) for e in data["elements"]], verb="click")
        elif tool == "list_screen_elements" and data.get("elements"):
            els = [dict(e, label=e.get("name", ""), source="screen") for e in data["elements"]
                   if e.get("control_type") in ("Button", "Hyperlink", "MenuItem", "TabItem", "CheckBox", "ListItem")]
            st.last_list = ListContext("element", els, verb="click")
        elif tool in ("browser_find", "find_screen_element") and data.get("element"):
            st.push_focus("element", dict(data["element"]), data["element"].get("name", ""))
        elif tool in ("browser_open", "browser_click", "browser_back") and data.get("url"):
            st.push_focus("url", {"url": data["url"]}, data.get("title") or data["url"])
        elif tool == "list_tasks" and data.get("tasks"):
            st.last_list = ListContext("task", [dict(t, label=t.get("text", "")) for t in data["tasks"]], verb="complete")
        elif tool == "find_notes" and data.get("notes"):
            st.last_list = ListContext("note", [dict(n, label=n.get("title", "")) for n in data["notes"]], verb="open")
        elif tool == "apply_code_change" and data.get("project_path"):
            st.push_focus("project", {"path": data["project_path"]}, _name(data["project_path"]))
            st.ref.project_path = data["project_path"]
        elif tool == "list_windows" and data.get("windows"):
            st.last_list = ListContext("window", [dict(w, label=w.get("title", "")) for w in data["windows"]], verb="focus")
        elif tool == "manage_window" and data.get("title"):
            st.push_focus("window", {"id": data.get("window_id"), "title": data["title"]}, data["title"])
        elif tool == "take_screenshot" and data.get("path"):
            st.push_focus("file", {"path": data["path"]}, _name(data["path"]))
        elif tool == "write_code_files" and data.get("project_path"):
            st.push_focus("project", {"path": data["project_path"], "key": data.get("project"), "entry": data.get("path")},
                          data.get("project", ""))
            st.push_focus("file", {"path": data["path"]}, _name(data["path"]))
            st.ref.project_path = data["project_path"]
        elif tool == "run_program":
            self._record_run(st, data)
        # ---- 1.4 desktop agent
        elif tool == "whatsapp_open":
            st.push_focus("app", {"key": "whatsapp"}, "WhatsApp")
        elif tool == "whatsapp_read" and data.get("chat"):
            st.push_focus("chat", {"chat": data["chat"], "key": "wa:" + data["chat"].lower(), "last": data.get("last")}, data["chat"])
            st.ref.person = {"name": data["chat"]}
        elif tool == "whatsapp_unread" and data.get("chats"):
            st.last_list = ListContext("wa_chat", [dict(c, label=c.get("chat", "")) for c in data["chats"]], verb="open")
        elif tool == "whatsapp_draft" and data.get("chat"):
            st.push_focus("wa_draft", {"chat": data["chat"], "text": data.get("text", ""), "key": "wa:" + data["chat"].lower()},
                          data["chat"])
        elif tool == "whatsapp_send" and data.get("chat"):
            st.push_focus("chat", {"chat": data["chat"], "key": "wa:" + data["chat"].lower()}, data["chat"])
        elif tool.startswith("colab_"):
            st.push_focus("notebook", {"key": "colab", "url": data.get("url", "")}, "Colab notebook")
        elif tool in ("image_search", "image_generate") and data.get("image_path"):
            st.push_focus("image", {"path": data["image_path"], "key": data["image_path"]}, data.get("query") or "picture")
            st.last_list = ListContext("image", [dict(i, label=i.get("title", "")) for i in data.get("images", [])], verb="open")
        elif tool == "computer_observe" and data.get("elements"):
            st.last_list = ListContext("element", [dict(e, label=e.get("name", "")) for e in data["elements"]], verb="click")
        elif tool in ("github_open", "github_create_repo") and (data.get("repo") or data.get("full_name")):
            full = data.get("full_name") or data.get("repo")
            st.push_focus("repo", {"repo": full, "key": "gh:" + full.lower(), "url": data.get("url", "")}, full)
        elif tool == "github_list_repos" and data.get("repos"):
            st.last_list = ListContext("repo", [dict(r, label=r.get("name", "")) for r in data["repos"]], verb="open")
        elif tool in ("office_create", "office_write") and data.get("app"):
            st.push_focus("document", {"app": data["app"], "path": data.get("path", ""), "key": "office:" + data["app"]},
                          data.get("document") or data["app"])
        elif tool in ("fill_form", "form_set_field"):
            prev = st.top_focus({"form"})
            labels = [f.get("label", "") for f in (data.get("form") or {}).get("fields", [])] if tool == "fill_form" \
                else list((prev.value.get("labels") if prev is not None else None) or [])
            st.push_focus("form", {"key": "form", "labels": [x for x in labels if x][:60]}, "the form")


def undo_for(tool: str, data: dict, args: dict) -> Optional[dict]:
    """The inverse of a successful action (run later through the normal permission checks), or why it can't be undone."""
    from pathlib import Path as _P

    if args.get("__undo__"):
        return None  # undoing an undo doesn't push another entry

    def u(t: str, a: dict, d: str) -> dict:
        return {"tool": t, "args": a, "description": d}

    if tool in ("create_file", "create_folder", "copy_path") and data.get("path"):
        return u("delete_path", {"path": data["path"]}, f"Delete {_name(data['path'])} (move it to the Recycle Bin)")
    if tool == "rename_path" and data.get("old_path"):
        return u("rename_path", {"path": data["path"], "new_name": _P(data["old_path"]).name}, f"Rename it back to {_name(data['old_path'])}")
    if tool == "move_path" and data.get("old_path"):
        return u("move_path", {"path": data["path"], "destination": str(_P(data["old_path"]).parent)},
                 f"Move {_name(data['path'])} back")
    if tool == "create_calendar_event" and data.get("event_id"):
        return u("delete_calendar_event", {"event_id": data["event_id"], "title": data.get("title", ""), "start": data.get("start", "")},
                 f"Remove “{data.get('title', 'the event')}” from your calendar")
    if tool == "update_calendar_event" and data.get("event_id") and args.get("current_start"):
        return u("update_calendar_event", {"event_id": data["event_id"], "title": data.get("title", ""),
                                           "new_start": args["current_start"]}, "Move the event back")
    if tool == "create_reminder" and data.get("reminder_id"):
        return u("cancel_reminder", {"reminder_id": int(data["reminder_id"]), "text": data.get("text", "")}, "Cancel that reminder")
    if tool == "open_application" and data.get("app") and data.get("kind") != "url":
        return u("close_application", {"app": data["app"]}, f"Close {data['app']}")
    if tool == "set_volume" and args.get("action") == "set":
        return None  # the previous level isn't known, so there's nothing honest to restore
    if tool == "set_volume":
        inverse = {"volume_up": "volume_down", "volume_down": "volume_up", "mute": "mute"}[args.get("action", "mute")]
        return u("set_volume", {"action": inverse, "steps": args.get("steps", 5)}, "Put the volume back")
    if tool == "media_control" and args.get("action") in ("pause", "resume"):
        return u("media_control", {"action": "resume" if args["action"] == "pause" else "pause"}, "Undo that")
    if tool == "apply_patch" and data.get("backup"):
        return u("restore_backup", {"path": data["path"], "backup": data["backup"]}, "Undo my change")
    if tool == "apply_code_change" and data.get("backups"):
        return u("restore_code_change", {"backups": data["backups"]}, "Undo my changes")
    if tool == "add_task" and data.get("task_id"):
        return u("delete_task", {"ref": str(data.get("text") or data["task_id"]).strip()[:300]}, "Remove that task")
    if tool == "add_note" and data.get("note_id"):
        return u("delete_note", {"ref": str(data["note_id"])}, "Delete that note")
    if tool == "complete_task" and data.get("task_id"):
        return None
    if tool in ("remember", "add_contact", "remember_project", "remember_preference", "create_routine"):
        return {"memory": True, "description": "Forget that"}
    if tool == "gmail_send_draft":
        return {"irreversible": "That e-mail has already been sent — Gmail can't unsend it from here."}
    if tool == "delete_path":
        return {"irreversible": "It's in the Recycle Bin — you can restore it from there (right-click → Restore)."}
    if tool == "gmail_create_draft":
        return {"irreversible": "I can't delete Gmail drafts — just discard it in Gmail."}
    if tool in ("shutdown_computer", "restart_computer"):
        return {"irreversible": "Say “cancel the shutdown” to stop it."}
    return None


def _record_run_impl(st: "DialogueState", data: dict) -> None:
    if data.get("path"):
        st.push_focus("file", {"path": data["path"]}, _name(data["path"]))
    issues = [i for i in (data.get("issues") or []) if i]
    if not data.get("passed") and issues:
        issue = dict(issues[0], entry=data.get("path"))
        if not issue.get("path"):
            issue["path"] = data.get("path")
        st.last_list = ListContext("issue", [dict(issue, label=issue.get("message", ""))])
        from ..services.coderunner import language_for

        lang = language_for(str(data.get("path") or ""))
        st.push_focus("error", {"message": issue.get("message", ""), "path": issue.get("path"), "line": issue.get("line"),
                                "language": lang.name if lang else ""}, issue.get("message", "error"))


def _name(path: Any) -> str:
    from pathlib import Path as _P

    return _P(str(path)).name if path else ""


def probe_cancel(text: str) -> bool:
    return bool(re.fullmatch(r"(cancel|never ?mind|forget it|stop|abort)( that| it)?( please)?", text.strip().lower()))


def strip_message_prefix(text: str) -> str:
    t = text.strip()
    t = re.sub(r"^(?:please\s+)?(?:tell|say to|inform|let)\s+(?:him|her|them)(?:\s+know)?(?:\s+that)?[,:]?\s*", "", t, flags=re.I)
    t = re.sub(r"^(?:say|saying|write|just say)(?:\s+that)?[,:]?\s*", "", t, flags=re.I)
    t = re.sub(r"^that\s+", "", t, flags=re.I)
    return t.strip().strip("\"“”")
