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
from datetime import datetime
from typing import TYPE_CHECKING, Union

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
LLM_EXCLUDED_TOOLS = {"shutdown_computer", "restart_computer", "sleep_computer", "gmail_send_draft", "delete_path"}
CODE_EXTS = (".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cpp", ".cs", ".go", ".rs", ".html", ".css", ".json", ".md")


class Planner:
    def __init__(self, registry: ToolRegistry, services: "ServiceContainer") -> None:
        self.registry = registry
        self.svc = services

    # ================================================================ rules
    def build(self, nlu: NLUResult, st: DialogueState) -> Decision:
        s = nlu.slots
        i = nlu.intent
        plan = Plan(request=nlu.text, intent=i.value)
        base_dir = st.ref.project_path

        if i == Intent.OPEN_APPLICATION:
            plan.add("open_application", {"app": s["app"]}, f"Open {s['app']}")
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
            plan.add("gmail_search", {"query": " ".join(q) or "in:inbox", "max_results": 5}, "Check your e-mail")
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
                       "mute": ("set_volume", {"action": "mute"}), "cancel_shutdown": ("cancel_shutdown", {})}
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
                plan.add("browser_read_page", args, "Read the page")
            elif op == "click":
                plan.add("browser_click", {"target": s["target"]}, f"Click “{s['target']}”")
            elif op == "fill":
                plan.add("browser_fill", {"target": s["target"], "value": s["value"]}, f"Fill “{s['target']}”")
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
            else:
                return Reply("", use_llm=True)
        elif i == Intent.CANCEL:
            return Reply("Okay, cancelled.")
        else:
            return Reply("", use_llm=True)
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

    # ================================================================== LLM
    def plan_with_llm(self, text: str) -> Decision:
        llm = self.svc.llm
        if not getattr(llm, "is_generative", False):
            return Reply("", use_llm=True)
        catalog = self.registry.catalog_for_llm(exclude=LLM_EXCLUDED_TOOLS)
        try:
            data = llm.chat_json([{"role": "user", "content": prompts.PLANNER_PROMPT.format(tools=catalog, text=text)}],
                                 max_tokens=700)
        except LLMError as exc:
            log.info("LLM planning failed: %s", exc)
            return Reply("", use_llm=True)
        steps = data.get("steps") if isinstance(data, dict) else None
        if not steps:
            reply = data.get("reply") if isinstance(data, dict) else None
            return Reply(reply or "", use_llm=not reply)
        plan = Plan(request=text, intent="llm_plan", source="llm")
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
        return plan


TEMPLATE_RE = re.compile(r"\{\{\s*steps\.(\d+)\.(data\.[\w]+|message)\s*\}\}")
