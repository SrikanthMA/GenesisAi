"""Planner: turns an understood request (intent + slots) into a tool plan.

Two strategies:

* **Rule-based planning** (default): deterministic intent → tool-sequence
  templates. Fast, offline, predictable — ideal for the high-frequency and
  safety-critical commands.
* **LLM planning** (fallback): for requests the NLU couldn't map to a known
  intent, the LLM proposes a JSON plan over the tool catalogue. Every proposed
  step is validated against the registry and the tool's JSON schema before it
  is accepted; power tools are excluded from LLM plans entirely.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Union

from ..ai import prompts
from ..ai.llm.base import LLMError
from ..core.logging_setup import get_logger
from ..nlp.dialogue import DialogueState
from ..nlp.intents import Intent, NLUResult
from ..security.validators import ValidationError
from ..tools.registry import ToolRegistry
from .plan import Clarify, Plan, Reply

if TYPE_CHECKING:  # pragma: no cover
    from ..core.container import ServiceContainer

log = get_logger("agent.planner")
Decision = Union[Plan, Clarify, Reply]
LLM_EXCLUDED_TOOLS = {"shutdown_computer", "restart_computer", "sleep_computer", "gmail_send_draft", "delete_path",
                      "apply_patch", "restore_backup", "run_tests", "click_screen_element", "create_routine", "delete_routine",
                      "forget_item", "browser_fill", "browser_click", "delete_calendar_event", "write_code_files", "run_program",
                      "apply_code_change", "restore_code_change", "run_project_command", "delete_task", "delete_note", "browser_press",
                      "set_identity", "github_delete_repo"}
CODE_EXTS = (".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cpp", ".cs", ".go", ".rs", ".html", ".css", ".json", ".md")


class Planner:
    def __init__(self, registry: ToolRegistry, services: "ServiceContainer") -> None:
        self.registry = registry
        self.svc = services

    # ================================================================ rules
    def build(self, nlu: NLUResult, st: DialogueState) -> Decision:
        """Turn understanding into a plan. A malformed interpretation (e.g. an AI-model reading with a missing slot) never
        crashes the turn: it falls back to letting the model plan/answer the request directly."""
        if nlu.slots.get("v2_reply"):  # v2 hook: a fixed answer decided by the 2.0 layer (guards, task journal)
            return Reply(str(nlu.slots["v2_reply"]))
        if nlu.slots.get("v2_resume"):  # v2 hook: "continue where you stopped"
            return self._v2_resume(nlu, st)
        try:
            return self._build(nlu, st)
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            log.warning("planner could not use the %s interpretation (%s: %s) — falling back to the model",
                        nlu.source, type(exc).__name__, exc)
            return Reply("", use_llm=True)

    def _build(self, nlu: NLUResult, st: DialogueState) -> Decision:
        s = nlu.slots
        i = nlu.intent
        plan = Plan(request=nlu.text, intent=i.value)
        base_dir = st.ref.project_path

        if i == Intent.OPEN_APPLICATION:
            app, reason = self._preferred_app(s["app"], nlu.text)
            if app == "__default_browser__":
                plan.add("open_url", {"url": "https://www.google.com/"}, "Open your browser")
            else:
                plan.add("open_application", {"app": app}, f"Open {app}")
            if reason:
                plan.reason = reason
        elif i == Intent.CLOSE_APPLICATION:
            plan.add("close_application", {"app": s["app"]}, f"Close {s['app']}")
        elif i == Intent.OPEN_FOLDER:
            plan.add("open_folder", {"folder": s["folder"]}, f"Open {s['folder']}")
        elif i == Intent.OPEN_URL:
            plan.add("open_url", {"url": s["url"]}, f"Open {s['url']}")
        elif i == Intent.SEARCH_WEB:
            plan.add("web_search", {"query": s["query"]}, f"Search the web for “{s['query']}”")
        elif i in (Intent.DRAFT_EMAIL, Intent.SEND_EMAIL):
            return self._email(nlu, st, plan)
        elif i == Intent.READ_EMAIL:
            q = []
            if s.get("from"):
                q.append(f"from:{s['from']}")
            if s.get("unread"):
                q.append("is:unread")
            if s.get("query"):
                q.append(s["query"])
            if s.get("attachments"):
                q.append("has:attachment")
            plan.add("gmail_search", {"query": " ".join(q) or "in:inbox", "max_results": 5}, "Check your e-mail",
                     hidden=bool(s.get("open_latest")))
            if s.get("open_latest"):
                plan.add("gmail_open_message", {"message_id": "{{steps.1.data.first_id}}"}, "Open the latest one", trusted_input=True)
        elif i == Intent.CREATE_REMINDER:
            due = s["datetime"]
            if isinstance(due, datetime) and due <= datetime.now():
                st.frame = nlu
                return Clarify("That time has already passed. When should I remind you?", "datetime")
            plan.add("create_reminder", {"text": s["reminder_text"], "due_at": due.isoformat() if isinstance(due, datetime) else str(due)},
                     "Create a reminder")
        elif i == Intent.FILE_OPERATION:
            return self._files(nlu, st, plan, base_dir)
        elif i == Intent.SYSTEM_CONTROL:
            action = s.get("system_action")
            mapping = {"lock": ("lock_computer", {}), "sleep": ("sleep_computer", {}), "info": ("system_info", {}),
                       "volume_up": ("set_volume", {"action": "volume_up"}), "volume_down": ("set_volume", {"action": "volume_down"}),
                       "mute": ("set_volume", {"action": "mute"}), "cancel_shutdown": ("cancel_shutdown", {}),
                       "volume_set": ("set_volume", {"action": "set", "level": int(s.get("level", 50))})}
            if action not in mapping:
                return Reply("I'm not sure which system action you want.")
            tool, args = mapping[action]
            plan.add(tool, args, tool.replace("_", " ").capitalize())
        elif i == Intent.SHUTDOWN:
            plan.add("shutdown_computer", {"delay_seconds": 10}, "Shut down the computer")
        elif i == Intent.RESTART:
            plan.add("restart_computer", {"delay_seconds": 10}, "Restart the computer")
        elif i == Intent.BROWSER_ACTION:
            op = s.get("browser_op")
            if op == "read":
                args = {"url": s["url"]} if s.get("url") else {}
                f = st.top_focus({"url"})
                if not args and f is not None:
                    args = {"url": f.value["url"]}
                if not args and not (self.svc.browser.available() and self.svc.browser.current_url()):
                    # not Genesis's browser → look at the screen (captured once, only now)
                    plan.add("analyze_screen", {"question": f"{nlu.text}. Summarise the page or document shown on screen."},
                             "Look at the screen")
                    return plan
                plan.add("browser_read_page", args, "Read the page")
                if re.search(r"\bsummar", nlu.text, re.I):
                    plan.add("llm_analyze", {"instruction": "Summarise this web page for the user in 3-5 sentences.",
                                             "content": "{{steps.1.data.content}}"}, "Summarise it", optional=True, trusted_input=True)
            elif op == "click":
                if self.svc.browser.current_url():
                    plan.add("browser_click", {"target": s["target"]}, f"Click “{s['target']}”")
                elif self._computer_ok():  # the active window, through the accessibility tree (never blind coordinates)
                    plan.add("computer_click", {"target": s["target"]}, f"Click “{s['target']}”")
                else:
                    plan.add("click_screen_element", {"name": s["target"]}, f"Click “{s['target']}”")
            elif op == "fill":
                if self.svc.browser.current_url() or not self._computer_ok():
                    plan.add("browser_fill", {"target": s["target"], "value": s["value"]}, f"Fill “{s['target']}”")
                else:
                    plan.add("computer_type", {"text": s["value"], "field": s["target"], "replace": True}, f"Type into “{s['target']}”")
            elif op in ("elements", "find", "click_nth", "scroll", "press"):
                in_browser = bool(self.svc.browser.available() and self.svc.browser.current_url())
                desk = not in_browser and self._computer_ok()
                if op == "elements":
                    if in_browser:
                        plan.add("browser_list_elements", {"kind": s.get("kind", "all")}, "Look at the page")
                    elif desk:
                        role = {"buttons": "button", "links": "link", "fields": "field"}.get(s.get("kind", "all"), "all")
                        plan.add("computer_observe", {"role": role}, "Look at the active window")
                    else:
                        plan.add("list_screen_elements", {}, "Look at the active window")
                elif op == "find":
                    plan.add("browser_find" if in_browser else "find_screen_element", {"description": s["description"]},
                             f"Find {s['description']}")
                elif op == "click_nth":
                    if in_browser:
                        plan.add("browser_click", {"nth": int(s["nth"]), "kind": s.get("kind", "button")}, "Click it")
                    elif desk:
                        plan.add("computer_click", {"nth": int(s["nth"]), "role": s.get("kind", "button")}, "Click it")
                    else:
                        plan.add("click_screen_element", {"nth": int(s["nth"]), "kind": s.get("kind", "button")
                                                          if s.get("kind") in ("button", "link") else "any"}, "Click it")
                elif not in_browser and desk:
                    if op == "scroll":
                        d = s.get("direction", "down")
                        plan.add("computer_press" if d in ("top", "bottom") else "computer_scroll",
                                 {"keys": "ctrl+home" if d == "top" else "ctrl+end"} if d in ("top", "bottom") else {"direction": d},
                                 "Scroll")
                    else:
                        plan.add("computer_press", {"keys": s["key"].lower()}, f"Press {s['key']}")
                elif not in_browser:
                    return Reply("Scrolling and key presses work in Genesis's own browser window — open the page with "
                                 "“open <site> in your browser” first.")
                elif op == "scroll":
                    plan.add("browser_scroll", {"direction": s.get("direction", "down")}, "Scroll")
                else:
                    plan.add("browser_press", {"key": s["key"]}, f"Press {s['key']}")
            else:
                return Reply("What should I do on the page?")
        elif i == Intent.CODE_ACTION:
            return self._code(nlu, st, plan, base_dir)
        elif i == Intent.MEMORY:
            op = s.get("memory_op")
            if op == "add_contact" and s.get("contact_name"):
                plan.add("add_contact", {"name": s["contact_name"], "alias": s.get("contact_alias"), "email": s.get("email")},
                         "Save contact")
            elif op in ("add", "add_contact"):
                plan.add("remember", {"text": s.get("memory_text") or nlu.text}, "Remember")
            elif op == "forget":
                plan.add("forget", {"text": s["memory_text"]}, "Forget")
            elif op == "list":
                plan.add("list_memory", {}, "Show memory")
            elif op == "remember_project":
                path = s.get("path") or ""
                if re.fullmatch(r"(this|that|the current|my current)?\s*(folder|directory|project)?|here|there", path.strip().lower()):
                    f = st.top_focus({"folder", "project"}) or st.top_focus({"file"})
                    if f is None:
                        st.frame = nlu
                        return Clarify("Which folder is it in? Give me the path, or open the folder first.", "path")
                    path = f.value["path"] if f.kind != "file" else str(Path(f.value["path"]).parent)
                plan.add("remember_project", {"name": s["project"], "path": path}, f"Remember the {s['project']} project")
            elif op == "recall":
                plan.add("recall", {"topic": s["topic"]}, "Recall")
            elif op == "preference":
                plan.add("remember_preference", {"key": s["key"], "value": s["value"], "text": s.get("memory_text", "")},
                         "Remember your preference")
            elif op == "instruction":
                plan.add("remember", {"text": s["memory_text"][:1].upper() + s["memory_text"][1:], "kind": "instruction"},
                         "Remember your instruction")
            elif op == "explain":
                task = st.current_task or {}
                if not task:
                    return Reply("I haven't done anything yet in this conversation.")
                why = task.get("reason") or "that's what the request asked for"
                steps = ", then ".join(task.get("steps", [])[:4])
                extra = f" {task['recovered']}" if task.get("recovered") else ""
                return Reply(f"You asked “{task.get('request')}”, so I {steps or 'did that'} — {why}.{extra}")
            elif op == "forget_last":
                if not st.last_memory:
                    return Reply("There's nothing I saved in this conversation to forget. You can delete anything on the Memory page.")
                kind, ref = st.last_memory
                plan.add("forget_item", {"kind": kind, "ref": str(ref)}, f"Forget that {kind}")
            elif op == "clear_conversation":
                return Reply("Cleared this conversation.", ui_action="clear_conversation")
            elif op == "new_conversation":
                return Reply("Started a new conversation.", ui_action="new_conversation")
            else:
                return Reply("What would you like me to remember or forget?")
        elif i == Intent.KNOWLEDGE:
            if s.get("knowledge_op") == "ingest":
                path = s.get("path") or st.ref.file
                if not path:
                    st.frame = nlu
                    return Clarify("Which file or folder should I add to your knowledge base?", "path")
                plan.add("knowledge_ingest", {"path": path}, "Index document")
            else:
                plan.add("knowledge_query", {"question": s.get("query") or nlu.text}, "Search your documents")
        elif i == Intent.SETTINGS and s.get("language_info"):
            return Reply("I understand and answer in many languages — English, Hindi, Kannada, Tamil, Telugu, French, "
                         "Spanish, German, Japanese, Chinese, Arabic and more. Just talk to me in your language; I translate it, "
                         "do the task and reply in the same language. Say “always reply in English” if you prefer English replies.")
        elif i == Intent.SETTINGS and (s.get("language_reply") or s.get("language_mode")):
            args = {k: v for k, v in (("reply", s.get("language_reply")), ("mode", s.get("language_mode"))) if v}
            plan.add("set_language", args, "Change my language setting")
        elif i == Intent.SETTINGS:
            plan.add("set_setting", {"setting": s["setting"], "value": bool(s["value"])}, f"Change {s['setting']}")
        elif i == Intent.UI_CONTROL:
            action = s.get("ui_action", "open_chat")
            text = {"open_chat": "Opening the chat window.", "hide": "Okay.", "open_settings": "Opening settings."}.get(action, "Okay.")
            return Reply(text, ui_action=action)
        elif i == Intent.INFORMATION_REQUEST:
            topic = s.get("topic")
            if topic == "datetime":
                now = datetime.now()
                return Reply(f"It's {now.strftime('%I:%M %p').lstrip('0')} on {now.strftime('%A, %d %B %Y')}.")
            if topic == "reminders":
                plan.add("list_reminders", {}, "List reminders")
            elif topic == "ai_status":
                plan.add("check_ai_connection", {}, "Check my AI connection")
            elif topic == "connection":
                plan.add("diagnose_connection", {"url": s["url"]} if s.get("url") else {}, "Check the connection")
            else:
                return Reply("", use_llm=True)
        elif i == Intent.CANCEL:
            return Reply("Okay, cancelled.")
        elif i == Intent.FOLLOW_UP:
            for step in s.get("steps") or [{"tool": s["tool"], "args": s.get("args"), "description": s.get("description")}]:
                plan.add(step["tool"], dict(step.get("args") or {}), step.get("description") or step["tool"],
                         trusted_input=step["tool"] in ("llm_analyze",))
        elif i == Intent.REPLY_EMAIL:
            return self._reply(nlu, st, plan)
        elif i == Intent.REVISE_DRAFT:
            return self._revise(nlu, st, plan)
        elif i == Intent.SESSION:
            return Reply("Okay — I'll be here if you need me. Just say “Hey Genesis”.", ui_action="sleep")
        elif i == Intent.ROUTINE:
            return self._routine(nlu, st, plan)
        elif i == Intent.SCREEN:
            op = s.get("screen_op", "analyze")
            if op == "read":
                plan.add("read_screen_text", {}, "Read the screen")
            elif op == "elements":
                plan.add("list_screen_elements", {}, "List on-screen controls")
            else:
                plan.add("analyze_screen", {"question": s.get("question") or nlu.text}, "Look at the screen")
        elif i == Intent.RESEARCH and s.get("operation") in ("answer", "news"):
            question = s.get("question") or s["query"]
            query = s["query"]
            if s.get("about_error"):
                err = st.top_focus({"error"})
                if err is None:
                    return Reply("Which error? Paste the error message, run the program first, or say “what's on my screen?”.")
                query = f"{err.value.get('message', '')} {err.value.get('language', '')}".strip()[:300]
                question = f"Why does this error happen and how do I fix it? Error: {err.value.get('message', '')}"
            plan.add("web_search", {"query": query, "read_pages": 3}, f"Search the web for “{query[:60]}”")
            plan.add("llm_analyze", {"instruction": f"Answer this for the user in 3-6 sentences using ONLY the search results, and "
                                                    f"cite the most useful link(s): {question}",
                                     "content": "Search results: {{steps.1.data.results}}", "allow_own_knowledge": True},
                     "Read the results and answer",
                     optional=True, trusted_input=True)
        elif i == Intent.RESEARCH and s.get("operation") == "list":
            n = s.get("count", "5")
            prefs = "; ".join(f["content"] for f in self.svc.memory.facts()[:10]) if self.svc.memory.enabled else ""
            plan.add("web_search", {"query": s["query"], "read_pages": 3}, f"Search for {s['query']}")
            plan.add("llm_analyze", {"instruction": f"The user asked: “{s.get('question') or nlu.text}”. From these search results pick "
                                                    f"the {n} best matches. For each give the name, the key requirements/details, and the "
                                                    "link. Only use what the results say; flag anything uncertain. "
                                                    + (f"User context: {prefs}" if prefs else ""),
                                     "content": "Search results: {{steps.1.data.results}}", "allow_own_knowledge": False},
                     f"Pick the best {n}", optional=True, trusted_input=True)
            plan.reason = "I searched the web and compared the results for you (nothing is applied for or sent without asking)"
        elif i == Intent.GOAL:
            return self._goal(nlu, st, plan)
        elif i == Intent.TASKS:
            op = s.get("tasks_op")
            if op == "add":
                args = {"text": s["text"]}
                if s.get("due"):
                    args["due"] = s["due"]
                plan.add("add_task", args, "Add to your to-do list")
            elif op == "complete":
                plan.add("complete_task", {"ref": s["ref"]}, "Mark it done")
            elif op == "delete":
                plan.add("delete_task", {"ref": s["ref"]}, "Remove it from your to-do list")
            else:
                plan.add("list_tasks", {}, "Show your to-do list")
        elif i == Intent.NOTES:
            op = s.get("notes_op")
            if op == "add":
                plan.add("add_note", {"content": s["content"]}, "Save the note")
            elif op == "delete":
                plan.add("delete_note", {"ref": s["ref"]}, "Delete the note")
            else:
                plan.add("find_notes", {"query": s.get("query", "")}, "Find your notes")
        elif i == Intent.RESEARCH:
            n = s.get("count", "3")
            plan.add("web_search", {"query": s["query"]}, f"Search for {s['query']}")
            op = s.get("operation", "compare")
            instruction = (f"From these search results, pick the {n} most relevant {s['query']}. For each give the name, "
                           f"key requirements/features and link, then {op} them in a short table-like list and give a one-line recommendation.")
            plan.add("llm_analyze", {"instruction": instruction, "content": "Search results: {{steps.1.data.results}}"},
                     f"{op.capitalize()} the results", trusted_input=True)
        elif i == Intent.YOUTUBE and re.fullmatch(r"(some |my )?(music|songs?|tunes)", (s.get("query") or "").lower()) and \
                self.svc.memory.get_preference("pref.music") == "spotify":
            plan.add("open_application", {"app": "spotify"}, "Open Spotify")
            plan.add("media_control", {"action": "resume"}, "Start playing", optional=True)
            plan.reason = "you told me you prefer Spotify for music"
        elif i == Intent.YOUTUBE:
            q = s.get("query") or ""
            if s.get("youtube_op") == "search":
                plan.add("youtube_search", {"query": q, "latest": bool(s.get("latest"))}, f"Search YouTube for “{q}”")
            else:
                plan.add("youtube_play", {"query": q, "latest": bool(s.get("latest"))},
                         f"Play {'the latest ' if s.get('latest') else ''}“{q}” on YouTube")
        elif i == Intent.MEDIA:
            a = s.get("action", "play_pause")
            plan.add("media_control", {"action": a}, {"pause": "Pause", "resume": "Resume", "next": "Skip", "previous": "Go back"}
                     .get(a, "Media control"))
        elif i == Intent.WINDOW:
            return self._window(nlu, st, plan)
        elif i == Intent.CALENDAR:
            return self._calendar(nlu, st, plan)
        elif i == Intent.IDENTITY:
            return self._identity(nlu, st, plan)
        elif i == Intent.HISTORY:
            plan.add("search_history", {"query": s.get("query") or "", "when": s.get("when", "any")}, "Search our past conversations")
        elif i == Intent.WHATSAPP:
            return self._whatsapp(nlu, st, plan)
        elif i == Intent.COLAB:
            return self._colab(nlu, st, plan)
        elif i == Intent.IMAGE:
            if s.get("image_op") == "generate":
                prompt = s.get("prompt") or s.get("query") or nlu.text
                plan.add("image_generate", {"prompt": prompt}, "Create the image")
            else:
                q = s.get("query") or nlu.text
                plan.add("image_search", {"query": q, "count": int(s.get("count") or 1)}, f"Find a picture of {q}")
        elif i == Intent.COMPUTER:
            return self._computer(nlu, st, plan)
        elif i == Intent.GITHUB:
            return self._github(nlu, st, plan)
        elif i == Intent.OFFICE:
            return self._office(nlu, st, plan)
        elif i == Intent.FORM:
            op = s.get("form_op", "fill")
            if op == "submit":
                plan.add("form_submit", {}, "Submit the form")
            elif op == "set":
                plan.add("form_set_field", {"field": s.get("field") or "that", "value": s["value"]}, "Change that answer")
            else:
                plan.add("fill_form", {"source": s.get("source") or "resume"}, "Fill in the form from your résumé")
        else:
            return Reply("", use_llm=True)
        return plan

    # ------------------------------------------------------------ 1.4 desktop agent
    def _computer_ok(self) -> bool:
        c = getattr(self.svc, "computer", None)
        return bool(c is not None and c.available())

    def _whatsapp(self, nlu: NLUResult, st: DialogueState, plan: Plan) -> Decision:
        from ..services.whatsapp import second_person

        s = nlu.slots
        op = s.get("wa_op", "open")
        wa = getattr(self.svc, "whatsapp", None)
        automated = bool(wa is not None and wa.automation_available())
        if op == "open":
            if automated:
                plan.add("whatsapp_open", {}, "Open WhatsApp")
            else:
                plan.add("open_application", {"app": "whatsapp"}, "Open WhatsApp")
            return plan
        contact = None
        if op in ("send", "draft") and not s.get("explicit") and s.get("chat"):
            try:
                contact = self.svc.repos.contacts.find(s["chat"])
            except Exception:
                contact = None
        if op in ("send", "draft") and not s.get("explicit") and s.get("chat") and (not automated or (contact or {}).get("email")):
            # "send a message to Rahul saying …" (no "WhatsApp"/"text") to someone you e-mail, or without WhatsApp automation
            # → the e-mail path, as before 1.4
            email = NLUResult(text=nlu.text, intent=Intent.DRAFT_EMAIL if op == "draft" else Intent.SEND_EMAIL, confidence=nlu.confidence,
                              slots={"recipient": s["chat"], "message": s.get("text")}, source=nlu.source)
            if not s.get("text"):
                st.frame = email
                return Clarify("What should I say?", "message")
            return self._email(email, st, plan)
        if not automated:
            return Reply("I can open WhatsApp for you, but to read or send messages myself I need browser automation "
                         "(pip install playwright, then playwright install chromium) — or the WhatsApp app on Windows. "
                         "Say “open WhatsApp” to use it yourself.")
        chat = s.get("chat")
        if chat and chat.lower() in ("him", "her", "them"):
            f = st.top_focus({"chat"})
            chat = f.value.get("chat") if f else None
        if op == "unread":
            plan.add("whatsapp_unread", {}, "Check unread WhatsApp messages")
            return plan
        if op == "read":
            if not chat:
                st.frame = nlu
                return Clarify("Whose messages should I read?", "chat")
            plan.add("whatsapp_read", {"chat": chat, "count": int(s.get("count") or 5), "incoming_only": bool(s.get("incoming_only"))},
                     f"Read your WhatsApp chat with {chat}")
            return plan
        if not chat:
            st.frame = nlu
            return Clarify("Who should I send it to?", "chat")
        text = s.get("text")
        if not text:
            st.frame = nlu
            return Clarify(f"What should the message to {chat} say?", "text")
        text = second_person(text)
        plan.add("whatsapp_draft", {"chat": chat, "text": text}, f"Draft the message to {chat}", hidden=op == "send")
        if op == "send":
            plan.add("whatsapp_send", {"chat": chat, "text": text}, f"Send the message to {chat}")
        return plan

    def _colab(self, nlu: NLUResult, st: DialogueState, plan: Plan) -> Decision:
        s = nlu.slots
        op = s.get("colab_op", "open")
        colab = getattr(self.svc, "colab", None)
        automated = bool(colab is not None and colab.automation_available())
        if op == "open" or not automated:
            plan.add("colab_open", {}, "Open Google Colab")
            if not automated and op != "open":
                plan.reason = "browser automation isn't installed"
            return plan
        if op == "new":
            plan.add("colab_new_notebook", {}, "Create a new notebook")
        elif op == "write":
            plan.add("colab_write_code", {"task": s.get("task") or nlu.text} if not s.get("code") else {"code": s["code"]},
                     "Write the code into the notebook")
            if s.get("run_after"):
                plan.add("colab_run", {}, "Run the cell")
        elif op == "run":
            plan.add("colab_run", {}, "Run the cell")
        elif op == "explain":
            plan.add("colab_explain_error", {}, "Explain the error")
        elif op == "fix":
            plan.add("colab_fix", {}, "Fix the code")
        return plan

    # ------------------------------------------------------------ 1.5 GitHub + Office
    def _github(self, nlu: NLUResult, st: DialogueState, plan: Plan) -> Decision:
        s = nlu.slots
        op = s.get("github_op", "open")
        if op == "open":
            plan.add("github_open", {"target": s.get("target") or "home"}, "Open GitHub")
        elif op == "list":
            plan.add("github_list_repos", {"limit": 10}, "List your repositories")
        elif op == "create":
            if not s.get("repo"):
                st.frame = nlu
                return Clarify("What should the new repository be called?", "repo")
            plan.add("github_create_repo", {"name": s["repo"], "private": bool(s.get("private", True))},
                     f"Create the repository {s['repo']}")
        elif op == "delete":
            repo = s.get("repo")
            if not repo:
                f = st.top_focus({"repo"})
                repo = f.value.get("repo") if f is not None else None
            if not repo:
                st.frame = nlu
                return Clarify("Which repository should I delete?", "repo")
            s["repo"] = repo
            if not s.get("__confirm_asked__"):
                s.pop("confirm_name", None)  # only the user's answer to our question counts as the typed name
            if not s.get("confirm_name"):
                s["__confirm_asked__"] = True
                st.frame = nlu
                return Clarify(f"Deleting “{repo}” on GitHub is permanent — its code, issues and history will be gone. To confirm, "
                               "type or say the repository's name. (Say “cancel” to keep it.)", "confirm_name")
            norm = lambda x: re.sub(r"[^a-z0-9]", "", str(x).lower().split("/")[-1])  # noqa: E731
            if norm(s["confirm_name"]) != norm(repo):
                return Reply(f"“{s['confirm_name']}” doesn't match “{repo}”, so I haven't deleted anything.")
            plan.add("github_delete_repo", {"repo": repo, "confirm_name": str(s["confirm_name"])},
                     f"Delete the GitHub repository {repo}")
        else:
            return Reply("", use_llm=True)
        return plan

    def _office(self, nlu: NLUResult, st: DialogueState, plan: Plan) -> Decision:
        s = nlu.slots
        kind = s.get("app") or "word"
        args = {"app": kind}
        for k in ("request", "text", "slides", "title"):
            if s.get(k):
                args[k] = int(s[k]) if k == "slides" else str(s[k])
        name = {"word": "Word", "excel": "Excel", "powerpoint": "PowerPoint"}[kind]
        if s.get("office_op") == "write":
            plan.add("office_write", args, f"Write into {name}")
        else:
            plan.add("office_create", args, f"Create the {name} file")
        return plan

    def _computer(self, nlu: NLUResult, st: DialogueState, plan: Plan) -> Decision:
        s = nlu.slots
        op = s.get("computer_op")
        in_browser = bool(self.svc.browser.available() and self.svc.browser.current_url())
        if op == "press":
            if in_browser and "+" not in s["keys"]:
                plan.add("browser_press", {"key": s["keys"].title()}, f"Press {s['keys']}")
            else:
                plan.add("computer_press", {"keys": s["keys"], "times": int(s.get("times") or 1)}, f"Press {s['keys']}")
        elif op == "type":
            args = {"text": s["text"]}
            if s.get("field"):
                args["field"] = s["field"]
            if in_browser and s.get("field"):
                plan.add("browser_fill", {"target": s["field"], "value": s["text"]}, f"Type into {s['field']}")
            else:
                plan.add("computer_type", args, "Type it")
        elif op == "copy":
            plan.add("computer_copy", {"text": s["text"]} if s.get("text") else {}, "Copy")
        elif op == "paste":
            plan.add("computer_paste", {}, "Paste")
        elif op == "select_all":
            plan.add("computer_select", {}, "Select all")
        elif op == "click":
            plan.add("computer_click", {"target": s["target"], "button": s.get("button", "left"), "double": bool(s.get("double"))},
                     f"Click “{s['target']}”")
        elif op == "drag":
            plan.add("computer_drag", {"source": s["source"], "destination": s["destination"]}, "Drag it")
        elif op == "observe":
            plan.add("computer_observe", {}, "Look at the active window")
        elif op == "task":
            plan.add("computer_task", {"goal": s.get("goal") or nlu.text}, "Do it in the app")
        else:
            return Reply("", use_llm=True)
        return plan

    def _v2_resume(self, nlu: NLUResult, st: DialogueState) -> Decision:
        """Rebuild the remaining steps of the latest unfinished task with the same protection as the original plan."""
        from ..v2 import policy as v2policy

        journal = self.svc.extras.get("v2_journal")
        if journal is None:
            return Reply("Task history isn't available in this session.")
        slots = journal.resume_slots()
        if slots.get("v2_reply"):
            return Reply(slots["v2_reply"])
        plan = Plan(request=nlu.text, intent="resume", source=slots.get("v2_source") or "rules",
                    reason=slots.get("description", ""))
        plan.untrusted = bool(slots.get("v2_untrusted"))
        for step in slots["steps"]:
            if self.registry.get(step["tool"]) is None:
                return Reply("I can't continue that task — one of its steps isn't available any more.")
            plan.add(step["tool"], dict(step.get("args") or {}), step.get("description") or step["tool"])
        if plan.source == "llm":
            problem = v2policy.check_llm_plan(plan, self.registry)
            if problem:
                return Reply("I can't safely continue that task automatically. Please ask me again.")
        journal.mark_resumed(slots["v2_resumed"], by=plan.id)
        return plan

    def _goal(self, nlu: NLUResult, st: DialogueState, plan: Plan) -> Decision:
        """A goal rather than a command: reuse a matching routine, else let the LLM plan it, else a minimal offline plan."""
        goal = nlu.slots.get("goal", "")
        words = [w for w in re.findall(r"[a-z]{3,}", goal.lower()) if w not in ("the", "for", "and", "some", "work")]
        for r in self.svc.repos.routines.list():
            if any(w in r["name"].lower() for w in words):
                return Reply(f"Running your “{r['name']}” routine for that.", data={"run_commands": r["commands"]})
        if getattr(self.svc.llm, "is_generative", False) and self.svc.settings.get("llm.use_for_planning", True):
            return Reply("", use_llm=True, data={"plan_goal": nlu.slots.get("request") or nlu.text})
        if re.search(r"\b(cod(e|ing)|program(ming)?|develop(ment)?|dev)\b", goal, re.I):
            project = st.ref.project_path or next((p["path"] for p in self.svc.repos.projects.list()), None)
            if self.svc.apps.vscode_executable():
                plan.add("vscode_open_project", {"path": project} if project else {"project": "genesis"}, "Open your project in VS Code",
                         optional=True)
            plan.add("open_terminal", {"path": project} if project else {}, "Open a terminal", optional=True)
            plan.reason = "that's the usual coding setup (the language model is offline, so I kept it simple)"
            return plan
        return Reply(f"To plan “{goal}” step by step I need the language model, which is offline. Tell me the steps, or save them "
                     f"as a routine: “create a routine called {words[0] if words else 'setup'} that opens …”.")

    def _preferred_app(self, app: str, text: str) -> tuple[str, str]:
        """Apply the user's stored preferences to generic requests ("open my email", "open a browser")."""
        low = text.lower()
        pref = self.svc.memory.get_preference
        if re.search(r"\b(e-?mail|mail|inbox)\b", low) and not re.search(r"\b(gmail|outlook)\b", low) and pref("pref.email"):
            return pref("pref.email"), f"you told me you prefer {pref('pref.email').capitalize()}"
        if app in ("browser", "web browser", "internet") or re.fullmatch(r"(open|launch|start)( my| the| a)? (web )?browser", low):
            if pref("pref.browser"):
                return pref("pref.browser"), f"you told me you prefer {pref('pref.browser').capitalize()}"
            return "__default_browser__", ""
        if app in ("editor", "code editor", "ide") and pref("pref.editor"):
            return pref("pref.editor"), "that's your preferred editor"
        return app, ""

    # --------------------------------------------------------------- identity
    def _identity(self, nlu: NLUResult, st: DialogueState, plan: Plan) -> Decision:
        from ..memory.identity import ACCEPT, CONFIRM

        s = nlu.slots
        op = s.get("identity_op")
        idm = self.svc.identity
        ident = idm.get()
        name = (s.get("name") or "").strip()
        if op == "whoami":
            return Reply(idm.describe())
        if op == "forget":
            plan.add("set_identity", {"action": "forget"}, "Forget your name")
            return plan
        if op == "not_my_name":
            heard = st.last_name_heard
            if heard:
                idm.reject_variant(heard)
            st.last_name_heard = None
            return Reply("Sorry about that! Tell me “my name is …” (and “call me …” for a short form) and I'll get it right.")
        if op in ("set_preferred", "add_nickname"):
            plan.add("set_identity", {"action": op, "name": name}, "Remember what to call you")
            return plan
        if op == "learn_variant":
            plan.add("set_identity", {"action": "learn_variant", "name": name}, "Learn how your name sounds")
            return plan
        # set_name / introduce
        voice = st.turn_source == "voice"
        m = idm.match(name) if ident.canonical else None
        st.last_name_heard = name
        if m is None:  # nothing stored yet
            if op == "introduce" and voice:
                st.pending_yesno = {"kind": "set_name", "name": name}
                return Reply(f"Nice to meet you! Is your name {name}?")
            plan.add("set_identity", {"action": "set_name", "name": name}, "Remember your name")
            return plan
        if m.score >= ACCEPT:
            if name.lower() != m.form.lower():
                if s.get("spelling") and not voice:  # "it's spelled …" typed: the user is fixing the spelling itself
                    plan.add("set_identity", {"action": "fix_spelling", "name": name}, "Fix the spelling of your name")
                    return plan
                idm.learn_variant(name)  # confidently the same name, just spelled differently by the recogniser
            return Reply(f"Hi {ident.call_name}! I know — you're {ident.canonical}." if op == "set_name" else f"Hi {ident.call_name}!")
        if m.score >= CONFIRM:
            st.pending_yesno = {"kind": "name_variant", "heard": name, "form": ident.call_name}
            return Reply(f"Just to check — did you mean {ident.call_name}?")
        # a clearly different name
        if op == "set_name" and not voice:
            plan.add("set_identity", {"action": "set_name", "name": name}, "Update your name")
            return plan
        st.pending_yesno = {"kind": "set_name", "name": name}
        return Reply(f"I know you as {ident.canonical}. Should I change your name to {name}?")

    # --------------------------------------------------------------- windows
    def _window(self, nlu: NLUResult, st: DialogueState, plan: Plan) -> Decision:
        s = nlu.slots
        op = s.get("window_op")
        if op not in ("screenshot", "show_desktop", "list", "minimize", "maximize", "restore", "focus", "close"):
            low = nlu.text.lower()  # e.g. the AI model said "window" without saying what to do with it
            op = next((o for o, rx in (("minimize", r"\bminimi[sz]e\b"), ("maximize", r"\bmaximi[sz]e|full ?screen\b"),
                                       ("restore", r"\brestore\b"), ("close", r"\bclose\b"),
                                       ("focus", r"\b(switch|go|bring|focus)\b"), ("list", r"\b(which|what|list)\b.*\bwindows?\b"),
                                       ("screenshot", r"\bscreen ?shot\b"), ("show_desktop", r"\bshow (the )?desktop\b"))
                       if re.search(rx, low)), None)
            if op is None:
                return Reply("", use_llm=True)
        if op == "screenshot":
            plan.add("take_screenshot", {"open_after": bool(s.get("open_after"))}, "Take a screenshot")
        elif op == "show_desktop":
            plan.add("show_desktop", {}, "Show the desktop")
        elif op == "list":
            plan.add("list_windows", {}, "List open windows")
        else:
            name = s.get("window") or ""
            args = {"action": op}
            if not name:
                f = st.top_focus({"window", "app"})
                if f is not None and f.kind == "window" and f.value.get("id"):
                    args["window_id"] = int(f.value["id"])
                    name = f.value.get("title", "")
                elif f is not None:
                    name = f.value.get("key", "")
                    args["window"] = name
            else:
                args["window"] = name
            plan.add("manage_window", args, f"{op.capitalize()} {name or 'the window'}".strip())
        return plan

    # --------------------------------------------------------------- calendar
    def _calendar(self, nlu: NLUResult, st: DialogueState, plan: Plan) -> Decision:
        s = nlu.slots
        op = s.get("calendar_op")
        if op == "list":
            if s.get("range_days"):
                start = datetime.fromisoformat(s["date"]) if s.get("date") else datetime.now()
                if s.get("label") == "next week":
                    start = start + timedelta(days=7 - start.weekday())
                start = start.replace(hour=0, minute=0, second=0, microsecond=0)
                args = {"start": start.isoformat(), "end": (start + timedelta(days=int(s["range_days"]))).isoformat(),
                        "label": s.get("label") or "this week"}
            else:
                args = {"day": s["date"]} if s.get("date") else {}
                if s.get("date"):
                    d = datetime.fromisoformat(s["date"]).date()
                    delta = (d - datetime.now().date()).days
                    args["label"] = {0: "today", 1: "tomorrow"}.get(delta, "on " + d.strftime("%A %d %B"))
            if s.get("query"):
                args["query"] = s["query"]
            plan.add("list_calendar_events", args, "Check your calendar")
            return plan
        if op in ("move", "delete"):
            find = {k2: s[k1] for k1, k2 in (("find_date", "day"), ("find_time", "time"), ("query", "query")) if s.get(k1)}
            if not find:
                f = st.top_focus({"event"})
                if f is None:
                    return Reply("Which event? Tell me its time or name, e.g. “move my 3 PM meeting to 5”.")
                ev = f.value
                plan.add("find_calendar_event", {"day": (ev.get("start") or "")[:10], "query": ev.get("title", "")}, "Find the event")
            else:
                plan.add("find_calendar_event", find, "Find the event")
            if op == "delete":
                plan.add("delete_calendar_event", {"event_id": "{{steps.1.data.event_id}}", "title": "{{steps.1.data.title}}",
                                                   "start": "{{steps.1.data.start}}"}, "Cancel the event")
            else:
                upd = {"event_id": "{{steps.1.data.event_id}}", "title": "{{steps.1.data.title}}",
                       "current_start": "{{steps.1.data.start}}"}
                if s.get("new_time"):
                    upd["new_time"] = s["new_time"]
                if s.get("new_date"):
                    upd["new_date"] = s["new_date"]
                plan.add("update_calendar_event", upd, "Move the event")
            return plan
        # create
        title = s.get("title") or "Meeting"
        if not s.get("date") and not s.get("time"):
            st.frame = nlu
            return Clarify(f"When should I schedule “{title}”?", "cal_when")
        if not s.get("time"):
            st.frame = nlu
            return Clarify("What time should I schedule it?", "cal_time")
        day = datetime.fromisoformat(s["date"]).date() if s.get("date") else datetime.now().date()
        hh, mm = (int(x) for x in s["time"].split(":"))
        start = datetime.combine(day, datetime.min.time()).replace(hour=hh, minute=mm)
        if start <= datetime.now():
            if s.get("date"):
                st.frame = nlu
                nlu.slots.pop("time", None)
                return Clarify("That time has already passed. What time should I schedule it?", "cal_time")
            start += timedelta(days=1)
        plan.add("create_calendar_event", {"title": title, "start": start.isoformat(), "duration_minutes": int(s.get("duration") or 30)},
                 f"Add “{title}” to your calendar")
        return plan

    # --------------------------------------------------------------- e-mail
    def _email(self, nlu: NLUResult, st: DialogueState, plan: Plan) -> Decision:
        s = nlu.slots
        if s.get("use_last_draft"):
            d = st.ref.draft
            if not d or not d.get("draft_id"):
                return Reply("There's no Gmail draft from this conversation for me to send. "
                             "If you're using the Gmail compose window, press Send there.")
            plan.add("gmail_send_draft", {"draft_id": d["draft_id"], "to": d.get("to") or [], "subject": d.get("subject") or ""},
                     "Send the draft")
            return plan

        recipient = (s.get("recipient") or "").strip()
        email = s.get("email")
        display = recipient
        contact = None
        if email == "__blank__":
            email = None
            to: list[str] = []
        else:
            if not email and recipient.lower() == "me":
                email = self.svc.google.account_email()
            if not email:
                contact = self.svc.memory.find_contact(recipient)
                if contact:
                    display = contact["name"]
                    email = contact.get("email")
            if not email:
                st.frame = nlu
                label = recipient if not recipient.lower().startswith("my ") else "your " + recipient[3:]
                return Clarify(f"I don't have an e-mail address for {label}. What is it? (or say “leave it blank”)", "email")
            to = [email]

        if not contact and s.get("email") and s.get("email") != "__blank__" and "@" not in recipient \
                and self.svc.memory.enabled and self.svc.settings.get("memory.auto_save_contacts", True):
            alias = recipient[3:].lower() if recipient.lower().startswith("my ") else None
            name = recipient if not alias else recipient[3:].capitalize()
            plan.add("add_contact", {"name": name, "alias": alias, "email": s["email"]}, f"Save {name}'s e-mail for next time",
                     optional=True)
            display = name

        n = len(plan.steps)
        compose_args = {"recipient_name": display or "", "message": s["message"]}
        if s.get("subject"):
            compose_args["subject"] = s["subject"]
        plan.add("compose_email", compose_args, "Write the e-mail", hidden=True)
        c = n + 1
        attachments = s.get("attachments") or []
        if self.svc.gmail.connected:
            draft_args = {"to": to, "subject": f"{{{{steps.{c}.data.subject}}}}", "body": f"{{{{steps.{c}.data.body}}}}",
                          "open_after": True}
            if attachments:
                draft_args["attachments"] = attachments
            plan.add("gmail_create_draft", draft_args, "Create the Gmail draft", hidden=nlu.intent == Intent.SEND_EMAIL)
            if nlu.intent == Intent.SEND_EMAIL:
                d = c + 1
                plan.add("gmail_send_draft", {"draft_id": f"{{{{steps.{d}.data.draft_id}}}}", "to": to,
                                              "subject": f"{{{{steps.{c}.data.subject}}}}"}, "Send the e-mail")
        else:
            plan.add("gmail_compose_web", {"to": to, "subject": f"{{{{steps.{c}.data.subject}}}}",
                                           "body": f"{{{{steps.{c}.data.body}}}}"}, "Open a pre-filled Gmail compose window")
        return plan

    # ---------------------------------------------------------------- files
    def _files(self, nlu: NLUResult, st: DialogueState, plan: Plan, base_dir) -> Decision:
        s = nlu.slots
        op = s.get("file_op")
        bd = {"base_dir": base_dir} if base_dir else {}
        if op == "search":
            args = {k: s[k] for k in ("query", "extension", "folder", "sort") if s.get(k)}
            plan.add("search_files", args, "Search your files")
        elif op == "open":
            plan.add("open_file", {"path": s["source"], **bd}, f"Open {s['source']}")
        elif op == "read":
            plan.add("read_file", {"path": s["source"], **bd}, f"Read {s['source']}")
        elif op == "create_file":
            args = {"name": s["name"], **bd}
            if s.get("location"):
                args["location"] = s["location"]
            if s.get("content"):
                args["content"] = s["content"]
            plan.add("create_file", args, f"Create {s['name']}")
            if s["name"].lower().endswith(CODE_EXTS) and self.svc.apps.vscode_executable():
                plan.add("vscode_open_file", {"path": "{{steps.1.data.path}}"}, "Open it in VS Code", optional=True)
        elif op == "create_folder":
            args = {"name": s["name"], **bd}
            if s.get("location"):
                args["location"] = s["location"]
            plan.add("create_folder", args, f"Create folder {s['name']}")
        elif op == "rename":
            plan.add("rename_path", {"path": s["source"], "new_name": s["new_name"], **bd}, f"Rename to {s['new_name']}")
        elif op in ("move", "copy"):
            plan.add(f"{op}_path", {"path": s["source"], "destination": s["destination"], **bd}, f"{op.capitalize()} {s['source']}")
        elif op == "delete":
            plan.add("delete_path", {"path": s["source"], **bd}, f"Delete {s['source']}")
        else:
            return Reply("What should I do with the file?")
        return plan

    # ----------------------------------------------------------------- code
    def _code(self, nlu: NLUResult, st: DialogueState, plan: Plan, base_dir) -> Decision:
        s = nlu.slots
        action = s.get("code_action")
        bd = {"base_dir": base_dir} if base_dir else {}
        if action == "diagnose":
            args = {"focus": s["focus"]} if s.get("focus") else {}
            if s.get("project"):
                args["project"] = s["project"]
            elif st.ref.project_path:
                args["path"] = st.ref.project_path
            else:
                st.frame = nlu
                return Clarify("Which project? Say “open my <name> project” first, or tell me its name.", "project")
            plan.add("diagnose_project", args, "Inspect the project")
            return plan
        if action == "create_program":
            return self._create_program(nlu, st, plan)
        if action in ("modify", "run_command"):
            root = None
            if s.get("project"):
                saved = self.svc.repos.projects.get(s["project"])
                root = saved["path"] if saved else None
            root = root or st.ref.project_path
            if not root:
                f = st.top_focus({"project", "folder"})
                root = f.value.get("path") if f else None
            if not root:
                st.frame = nlu
                return Clarify("Which project? Say “open my <name> project” first, or tell me its folder.", "project")
            if action == "run_command":
                plan.add("run_project_command", {"command": s["command"], "path": root}, f"Run {s['command']}")
                return plan
            plan.add("plan_code_change", {"request": s.get("request") or nlu.text, "path": root}, "Work out the change")
            plan.add("apply_code_change", {k: f"{{{{steps.1.data.{k}}}}}" for k in ("project_path", "summary", "changes", "created",
                                                                                      "files", "plus", "minus", "risky")},
                     "Apply the change", confirm_group=1)
            from ..tools.code import detect_test_command
            from pathlib import Path as _P

            try:
                has_tests = detect_test_command(_P(root)) is not None
            except Exception:
                has_tests = False
            if has_tests:
                plan.add("run_tests", {"path": root}, "run the tests", confirm_group=1, optional=True)
            plan.reason = "I read the relevant files, planned the edits and showed you the diff before changing anything"
            return plan
        if action == "run":
            target = s.get("path")
            if not target:
                f = st.top_focus({"project", "file"})
                if f is None:
                    return Reply("Run what? Tell me the file, e.g. “run main.py”, or ask me to create a program first.")
                target = f.value.get("path")
            if not target:
                return Reply("Which program should I run?")
            if not Path(target).is_absolute() and base_dir:
                target = str(Path(base_dir) / target)
            plan.add("run_program", {"path": target}, f"Run {Path(target).name}")
            return plan
        if action == "fix":
            issue = s.get("issue") or {}
            if issue.get("kind") == "deps":
                return Reply("That one isn't a code change — the dependencies need installing. Open a terminal in the project "
                             "and run “npm install”, then try again.")
            if not issue.get("path"):
                return Reply("Which problem should I fix? Say “check why … isn't working” first.")
            plan.add("propose_fix", {"path": issue["path"], "issue": issue.get("message", ""), "line": int(issue.get("line") or 0)},
                     "Work out a fix", hidden=False)
            plan.add("apply_patch", {"path": "{{steps.1.data.path}}", "new_content": "{{steps.1.data.new_content}}",
                                     "summary": "{{steps.1.data.summary}}", "base_sha": "{{steps.1.data.base_sha}}"},
                     "Apply the fix", confirm_group=1)
            if issue.get("kind") == "runtime" and issue.get("entry"):
                plan.add("run_program", {"path": issue["entry"], "auto_fix": False}, "Run it again", confirm_group=1, optional=True)
                return plan
            root = st.ref.project_path
            if root:
                plan.add("run_tests", {"path": root}, "Run the tests", confirm_group=1, optional=True)
            return plan
        if action == "open_project":
            project = s.get("project") or st.ref.project
            if not project:
                st.frame = nlu
                return Clarify("Which project should I open?", "project")
            plan.add("vscode_open_project", {"project": project}, f"Open the {project} project in VS Code")
        elif action == "open_file":
            plan.add("vscode_open_file", {"path": s["path"], **bd}, f"Open {s['path']} in VS Code")
        elif action == "check_file":
            if not s.get("path"):
                st.frame = nlu
                return Clarify("Which file should I check?", "path")
            plan.add("check_code_file", {"path": s["path"], **bd}, f"Check {s['path']} for errors")
            if getattr(self.svc.llm, "is_generative", False):
                plan.add("explain_code", {"path": s["path"], "issues": "{{steps.1.message}}", **bd}, "Explain the problems",
                         optional=True)
        elif action == "explain":
            args = {k: s[k] for k in ("error_text", "path") if s.get(k)}
            if not args:
                st.frame = nlu
                return Clarify("Paste the error, or tell me which file to explain.", "error_text")
            if "path" in args:
                args.update(bd)
            plan.add("explain_code", args, "Explain")
        else:
            return Reply("What would you like me to do in VS Code?")
        return plan

    def _create_program(self, nlu: NLUResult, st: DialogueState, plan: Plan) -> Decision:
        s = nlu.slots
        lang = s.get("language") or self.svc.memory.get_preference("coding.language") or "python"
        args = {"task": s.get("task") or nlu.text, "language": lang}
        if s.get("uses_context_file"):
            f = st.top_focus({"file"})
            if f is None:
                st.frame = nlu
                return Clarify("Which file should the program use? Give me its name or path.", "context_file")
            args["context_file"] = f.value["path"]
        elif s.get("context_file"):
            args["context_file"] = s["context_file"]
        prefs = [fct["content"] for fct in self.svc.memory.facts()[:20]
                 if re.search(r"\b(code|coding|program|style|comment|indent|tabs|spaces|type hints|language)\b", fct["content"], re.I)]
        if prefs:
            args["preferences"] = "; ".join(prefs)[:450]
        plan.add("generate_code", args, "Write the code")
        plan.add("write_code_files", {"name": "{{steps.1.data.name}}", "files": "{{steps.1.data.files}}", "entry": "{{steps.1.data.entry}}",
                                      "language": "{{steps.1.data.language}}", "summary": "{{steps.1.data.summary}}",
                                      "task": args["task"]}, "Create the project files", confirm_group=1)
        if s.get("open_vscode") or (self.svc.apps.vscode_executable() and self.svc.settings.get("coding.open_vscode", True)):
            plan.add("vscode_open_project", {"path": "{{steps.2.data.project_path}}"}, "Open it in VS Code", optional=True)
        if s.get("run", True):
            plan.add("run_program", {"path": "{{steps.2.data.path}}", "task": args["task"]}, "run it (fixing errors if needed)",
                     confirm_group=1)
        return plan

    # --------------------------------------------------------------- reply / revise
    def _compose_and_save(self, plan: Plan, *, to: list, recipient: str, message: str, subject: str, thread_id=None,
                          in_reply_to=None) -> Plan:
        n = len(plan.steps)
        plan.add("compose_email", {"recipient_name": recipient or "", "message": message, "subject": subject},
                 "Write the reply", hidden=True)
        c = n + 1
        if self.svc.gmail.connected:
            args = {"to": to, "subject": f"{{{{steps.{c}.data.subject}}}}", "body": f"{{{{steps.{c}.data.body}}}}", "open_after": True}
            if thread_id:
                args["thread_id"] = thread_id
            if in_reply_to:
                args["in_reply_to"] = in_reply_to
            plan.add("gmail_create_draft", args, "Save the reply as a Gmail draft")
        else:
            plan.add("gmail_compose_web", {"to": to, "subject": f"{{{{steps.{c}.data.subject}}}}", "body": f"{{{{steps.{c}.data.body}}}}"},
                     "Open a pre-filled reply in Gmail")
        return plan

    def _reply(self, nlu: NLUResult, st: DialogueState, plan: Plan) -> Decision:
        s = nlu.slots
        if not s.get("to_email"):
            return Reply("I don't know who sent that e-mail, so I can't reply to it.")
        subj = s.get("subject") or ""
        subj = subj if subj.lower().startswith("re:") else f"Re: {subj}".strip()
        return self._compose_and_save(plan, to=[s["to_email"]], recipient=s.get("recipient") or "", message=s["message"],
                                      subject=subj, thread_id=s.get("thread_id"), in_reply_to=s.get("in_reply_to"))

    def _revise(self, nlu: NLUResult, st: DialogueState, plan: Plan) -> Decision:
        s = nlu.slots
        d = st.ref.draft or {}
        if not d.get("body"):
            return Reply("I don't have the text of that draft any more — edit it in Gmail, or ask me to draft it again.")
        args = {"body": d["body"], "subject": d.get("subject") or "", "revision": s["revision"]}
        for k_src, k_dst in (("find", "find"), ("replace", "replace"), ("addition", "addition"), ("style", "style"),
                             ("subject", "new_subject")):
            if s.get(k_src):
                args[k_dst] = s[k_src]
        plan.add("revise_text", args, "Revise the e-mail")
        to = d.get("to") or []
        if d.get("draft_id") and self.svc.gmail.connected:
            upd = {"draft_id": d["draft_id"], "to": to, "subject": "{{steps.1.data.subject}}", "body": "{{steps.1.data.body}}"}
            if d.get("thread_id"):
                upd["thread_id"] = d["thread_id"]
            if d.get("in_reply_to"):
                upd["in_reply_to"] = d["in_reply_to"]
            plan.add("gmail_update_draft", upd, "Update the Gmail draft")
        else:
            plan.add("gmail_compose_web", {"to": to, "subject": "{{steps.1.data.subject}}", "body": "{{steps.1.data.body}}"},
                     "Open the revised e-mail in Gmail")
        return plan

    # --------------------------------------------------------------- routines
    def _routine(self, nlu: NLUResult, st: DialogueState, plan: Plan) -> Decision:
        s = nlu.slots
        op = s.get("routine_op")
        if op == "list":
            plan.add("list_routines", {}, "List routines")
        elif op == "delete":
            plan.add("delete_routine", {"name": s["name"]}, f"Delete the {s['name']} routine")
        elif op == "run":
            r = self.svc.repos.routines.find(s["name"])
            if not r:
                return Reply(f"I don't have a routine called “{s['name']}”. Say “list my routines” to see them.")
            return Reply(f"Running your “{r['name']}” routine.", data={"run_commands": r["commands"]})
        elif op == "save_suggestion":
            sug = st.pending_suggestion
            if not sug:
                return Reply("Save what as a routine? Tell me the steps, e.g. “create a routine called work that opens VS Code "
                             "and my Genesis project”.")
            name = s.get("name") or sug.get("name") or "my routine"
            plan.add("create_routine", {"name": name, "commands": sug["commands"]}, f"Save the {name} routine")
        else:
            args = {"name": s["name"], "commands": s["commands"]}
            if s.get("days"):
                args["days"] = s["days"]
            if s.get("time"):
                args["time_of_day"] = s["time"]
            plan.add("create_routine", args, f"Save the routine “{s['name']}”")
        return plan

    # ================================================================== LLM
    def plan_with_llm(self, text: str, context_text: str = "", *, failure: Optional[dict] = None) -> Decision:
        llm = self.svc.llm
        if not getattr(llm, "is_generative", False):
            return Reply("", use_llm=True)
        catalog = self.registry.catalog_for_llm(exclude=LLM_EXCLUDED_TOOLS)
        prompt = prompts.PLANNER_PROMPT.format(tools=catalog, text=text, context=context_text or "(nothing yet)")
        if failure:
            prompt += "\n\n" + prompts.REPLAN_PROMPT.format(text=text, **failure)
        try:
            data = llm.chat_json([{"role": "user", "content": prompt}], max_tokens=900)
        except LLMError as exc:
            log.info("LLM planning failed: %s", exc)
            return Reply("", use_llm=True)
        steps = data.get("steps") if isinstance(data, dict) else None
        if not steps:
            reply = data.get("reply") if isinstance(data, dict) else None
            return Reply(reply or "", use_llm=not reply)
        plan = Plan(request=text, intent="llm_plan", source="llm", reason=str(data.get("reason") or "")[:200])
        for raw in steps[:8]:
            if not isinstance(raw, dict):
                return Reply("", use_llm=True)
            tool_name = str(raw.get("tool", ""))
            tool = self.registry.get(tool_name)
            if tool is None or tool_name in LLM_EXCLUDED_TOOLS:
                log.warning("LLM proposed unknown/forbidden tool %r — plan rejected", tool_name)
                return Reply("", use_llm=True)
            args = raw.get("args") or {}
            if not isinstance(args, dict):
                return Reply("", use_llm=True)
            templated = {k: v for k, v in args.items() if isinstance(v, str) and "{{" in v}
            try:
                concrete = {k: v for k, v in args.items() if k not in templated}
                tool.validate({**concrete, **{k: "x" for k in templated}})
            except ValidationError as exc:
                log.warning("LLM plan step failed validation: %s", exc)
                return Reply("", use_llm=True)
            plan.add(tool_name, args, str(raw.get("description") or tool_name)[:120])
        from ..v2 import policy as v2policy  # v2 hook: step references must point backwards; bounded consequential steps

        problem = v2policy.check_llm_plan(plan, self.registry)
        if problem:
            log.warning("LLM plan rejected: %s", problem)
            if "in one go" in problem:
                return Reply(f"That needs {problem.split(' steps')[0]} steps that change things on your computer — more than I'll "
                             "do in one go. Could you split it into smaller requests?")
            return Reply("", use_llm=True)
        return plan


TEMPLATE_RE = re.compile(r"\{\{\s*steps\.(\d+)\.(data\.[\w]+|message)\s*\}\}")
