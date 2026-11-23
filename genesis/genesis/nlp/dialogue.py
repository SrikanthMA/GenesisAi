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
class DialogueState:
    conversation_id: str
    history: deque = field(default_factory=lambda: deque(maxlen=24))
    ref: Referents = field(default_factory=Referents)
    frame: Optional[NLUResult] = None
    pending_slot: Optional[str] = None
    pending_question: Optional[str] = None
    queue: list[str] = field(default_factory=list)
    pending_confirmation_id: Optional[str] = None
    session_tasks: list[dict] = field(default_factory=list)

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
        # a brand-new, unrelated command abandons the frame (except for free-text slots)
        if slot not in {"message", "reminder_text", "content"}:
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
        if slot == "message":
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
        if data.get("draft_id"):
            st.ref.draft = {"draft_id": data["draft_id"], "to": data.get("to"), "subject": data.get("subject")}
        if data.get("project_path"):
            st.ref.project = data.get("project")
            st.ref.project_path = str(data["project_path"])
        if data.get("url"):
            st.ref.url = data["url"]


def probe_cancel(text: str) -> bool:
    return bool(re.fullmatch(r"(cancel|never ?mind|forget it|stop|abort)( that| it)?( please)?", text.strip().lower()))


def strip_message_prefix(text: str) -> str:
    t = text.strip()
    t = re.sub(r"^(?:please\s+)?(?:tell|say to|inform|let)\s+(?:him|her|them)(?:\s+know)?(?:\s+that)?[,:]?\s*", "", t, flags=re.I)
    t = re.sub(r"^(?:say|saying|write|just say)(?:\s+that)?[,:]?\s*", "", t, flags=re.I)
    t = re.sub(r"^that\s+", "", t, flags=re.I)
    return t.strip().strip("\"“”")
