"""GenesisCore — the orchestrator that connects every layer.

    text/voice ─► normalise ─► split compound request ─► for each part:
        NLU (rules → classifier → LLM) ─► dialogue (coreference, slot filling)
        ─► planner (rules / LLM) ─► executor (permission → confirm → tool → verify)
        ─► verifier (honest summary) ─► response (text + speech)

The UI and the voice pipeline only ever call :meth:`GenesisCore.handle` and
:meth:`GenesisCore.resolve_confirmation`; everything else is internal.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any, Optional, Union

from ..agent.executor import Executor
from ..agent.plan import Clarify, Plan, Reply, StepStatus
from ..agent.planner import Planner
from ..agent.verifier import summarize
from ..ai import prompts
from ..ai.llm.base import LLMAuthError, LLMError
from ..nlp import rules
from ..nlp.dialogue import DialogueManager, DialogueState
from ..nlp.intents import Intent, NLUResult
from ..nlp.pipeline import NLUPipeline
from ..security.confirmation import PendingConfirmation, parse_confirmation
from ..security.injection import wrap_untrusted
from ..services.scheduler import ReminderScheduler
from ..tools.registry import ToolRegistry, build_default_registry
from .container import ServiceContainer
from .events import AssistantState, EventType
from .logging_setup import get_logger
from .paths import PROJECT_ROOT, is_frozen

log = get_logger("core")
COMMAND_START = re.compile(rf"^(?:please\s+)?{rules.ACTION_VERBS}\b", re.I)


@dataclass
class AssistantResponse:
    text: str
    status: str  # done | partial | failed | awaiting_confirmation | awaiting_input | chat | error
    conversation_id: str
    plans: list[dict] = field(default_factory=list)
    confirmation: Optional[dict] = None
    question: Optional[str] = None
    ui_action: Optional[str] = None
    nlu: list[dict] = field(default_factory=list)

    @property
    def speech(self) -> str:
        return speech_text(self.text)

    def to_dict(self) -> dict:
        return {"text": self.text, "status": self.status, "conversation_id": self.conversation_id, "plans": self.plans,
                "confirmation": self.confirmation, "question": self.question, "ui_action": self.ui_action, "nlu": self.nlu}


def speech_text(text: str, max_chars: int = 400) -> str:
    """Make text pleasant for TTS: drop URLs, markdown, emoji; keep it short."""
    t = re.sub(r"https?://\S+", "", text or "")
    t = re.sub(r"[*_`#>•]", "", t)
    t = re.sub("[\U0001F300-\U0001FAFF☀-➿️‍]", "", t)
    t = re.sub(r"\n\s*\d+\.\s", ". ", t)
    t = re.sub(r"\s*\n+\s*", ". ", t).replace("..", ".")
    t = re.sub(r"\s{2,}", " ", t).strip(" .") + "."
    t = re.sub(r"\s+([.,!?])", r"\1", t)
    if len(t) > max_chars:
        cut = t.rfind(". ", 0, max_chars)
        t = t[: cut + 1 if cut > 80 else max_chars] + " I've put the details on screen."
    return t


class GenesisCore:
    def __init__(self, services: ServiceContainer, registry: Optional[ToolRegistry] = None) -> None:
        self.svc = services
        self.registry = registry or build_default_registry()
        self.nlu = NLUPipeline(services.llm, use_llm=lambda: bool(services.settings.get("llm.use_for_nlu", True)))
        self.dialogue = DialogueManager()
        self.planner = Planner(self.registry, services)
        self.executor = Executor(self.registry, services)
        self.scheduler = ReminderScheduler(services.repos.reminders, self._on_reminder_due)
        services.scheduler = self.scheduler
        self._lock = threading.RLock()
        self.conversation_id: Optional[str] = None
        self.state = AssistantState.IDLE

    # =============================================================== life-cycle
    def start(self) -> None:
        if not is_frozen() and self.svc.repos.projects.get("genesis") is None:
            self.svc.repos.projects.set("genesis", str(PROJECT_ROOT))  # so "open my Genesis project" works out of the box
        self.ensure_conversation()
        self.scheduler.start()
        log.info("Genesis core started", extra={"data": self.svc.config.describe()})

    def stop(self) -> None:
        self.scheduler.stop()
        try:
            self.svc.browser.close()
        except Exception:
            pass

    def _set_state(self, state: AssistantState, **kw: Any) -> None:
        self.state = state
        self.svc.events.emit(EventType.STATE_CHANGED, state=state.value, **kw)

    # =========================================================== conversations
    def ensure_conversation(self) -> str:
        if self.conversation_id and self.svc.repos.conversations.exists(self.conversation_id):
            return self.conversation_id
        recent = self.svc.repos.conversations.list(limit=1)
        if recent and recent[0].get("message_count", 0) == 0:
            self.conversation_id = recent[0]["id"]
        else:
            self.conversation_id = self.svc.repos.conversations.create()
        return self.conversation_id

    def new_conversation(self) -> str:
        with self._lock:
            self.svc.confirmations.cancel_all()
            self.conversation_id = self.svc.repos.conversations.create()
            return self.conversation_id

    def switch_conversation(self, conversation_id: str) -> None:
        with self._lock:
            if self.svc.repos.conversations.exists(conversation_id):
                self.conversation_id = conversation_id
                st = self.dialogue.state(conversation_id)
                if not st.history:
                    for m in self.svc.repos.conversations.messages(conversation_id, limit=24):
                        if m["role"] in ("user", "assistant"):
                            st.add_turn(m["role"], m["content"])

    def clear_conversation(self) -> None:
        with self._lock:
            cid = self.ensure_conversation()
            self.svc.repos.conversations.clear_messages(cid)
            self.dialogue.reset(cid)
            self.svc.confirmations.cancel_all()

    def _record(self, role: str, text: str, metadata: Optional[dict] = None) -> None:
        cid = self.ensure_conversation()
        self.dialogue.state(cid).add_turn(role, text)
        if self.svc.memory.enabled:
            self.svc.repos.conversations.add_message(cid, role, text, metadata)
            if role == "user":
                conv = self.svc.repos.conversations.list(limit=50)
                row = next((c for c in conv if c["id"] == cid), None)
                if row and row["title"] == "New conversation":
                    self.svc.repos.conversations.rename(cid, text[:60])
        self.svc.events.emit(EventType.MESSAGE, role=role, text=text, conversation_id=cid, metadata=metadata or {})

    # ================================================================ public
    def handle(self, text: str, source: str = "text") -> AssistantResponse:
        with self._lock:
            cid = self.ensure_conversation()
            st = self.dialogue.state(cid)
            clean = rules.normalize(text)
            if not clean:
                resp = AssistantResponse(self.svc.settings.get("voice.greeting", "I'm listening."), "chat", cid)
                self._set_state(AssistantState.IDLE)
                return resp
            self._record("user", text.strip(), {"source": source})
            self._set_state(AssistantState.THINKING)
            try:
                resp = self._process(clean, st)
            except Exception:
                log.exception("unhandled error while processing request")
                resp = AssistantResponse("Sorry — something went wrong on my side while handling that. "
                                         "The details are in logs/errors.log.", "error", cid)
            self._finish(resp)
            return resp

    def resolve_confirmation(self, confirmation_id: str, approved: bool) -> AssistantResponse:
        """Called by UI buttons (Confirm / Cancel)."""
        with self._lock:
            cid = self.ensure_conversation()
            st = self.dialogue.state(cid)
            pc = self.svc.confirmations.get(confirmation_id)
            if pc is None:
                resp = AssistantResponse("That confirmation has expired, so I didn't do anything.", "done", cid)
                st.pending_confirmation_id = None
            else:
                self._record("user", "Yes, continue." if approved else "No, cancel.", {"source": "button"})
                resp = self._finish_confirmation(st, pc, approved)
            self._finish(resp)
            return resp

    def _finish(self, resp: AssistantResponse) -> None:
        meta = {"status": resp.status, "plans": resp.plans, "confirmation": resp.confirmation}
        if resp.ui_action == "new_conversation":
            self.new_conversation()
        elif resp.ui_action == "clear_conversation":
            self.clear_conversation()
        self._record("assistant", resp.text, meta)
        if resp.ui_action:
            self.svc.events.emit(EventType.UI_ACTION, action=resp.ui_action)
        if resp.status == "awaiting_confirmation":
            self._set_state(AssistantState.WAITING_FOR_CONFIRMATION, confirmation=resp.confirmation)
            self.svc.events.emit(EventType.CONFIRMATION_REQUIRED, **(resp.confirmation or {}))
        elif resp.status == "awaiting_input":
            self._set_state(AssistantState.WAITING_FOR_INPUT, question=resp.question)
        elif resp.status in ("error", "failed"):
            self._set_state(AssistantState.ERROR, message=resp.text)
        else:
            self._set_state(AssistantState.IDLE, success=resp.status in ("done", "chat"))

    # ============================================================== pipeline
    def _process(self, text: str, st: DialogueState) -> AssistantResponse:
        cid = st.conversation_id
        if st.pending_confirmation_id:
            pc = self.svc.confirmations.get(st.pending_confirmation_id)
            if pc is None:
                st.pending_confirmation_id = None
            else:
                answer = parse_confirmation(text)
                if answer is not None:
                    return self._finish_confirmation(st, pc, answer)
                probe = rules.parse(text)
                if probe is None or probe.intent in (Intent.CONVERSATION, Intent.UNKNOWN):
                    return AssistantResponse(f"Please answer yes or no. {pc.prompt}", "awaiting_confirmation", cid,
                                             confirmation=pc.public())
                self._cancel_confirmation(st, pc)  # a new command replaces the pending one

        if st.frame is not None:
            filled = self.dialogue.fill_pending_slot(st, text)
            if filled is not None:
                if "__reask__" in filled.slots:
                    return AssistantResponse(filled.slots["__reask__"], "awaiting_input", cid, question=filled.slots["__reask__"])
                if filled.intent == Intent.CANCEL:
                    st.queue.clear()
                    return AssistantResponse("Okay, cancelled.", "done", cid)
                queue, st.queue = list(st.queue), []
                return self._run_requests([filled, *queue], st)
        st.queue = []
        return self._run_requests(self.nlu.split(text), st)

    def _run_requests(self, items: list[Union[str, NLUResult]], st: DialogueState,
                      outputs: Optional[list[str]] = None, plans: Optional[list[dict]] = None) -> AssistantResponse:
        cid = st.conversation_id
        outputs = outputs if outputs is not None else []
        plans = plans if plans is not None else []
        nlu_dump: list[dict] = []
        attach_next = False
        worst = "done"
        queue = list(items)
        ui_action: Optional[str] = None
        while queue:
            item = queue.pop(0)
            if isinstance(item, str):
                if re.match(r"^(?:and\s+)?attach\b", item.strip(), re.I):
                    attach_next = True
                    continue
                nlu = self.nlu.parse(item, context=list(st.history))
            else:
                nlu = item
            if nlu.intent == Intent.OPEN_APPLICATION and nlu.slots.get("app") == "vscode" and queue and isinstance(queue[0], str):
                nxt = rules.parse(queue[0])
                if nxt is not None and nxt.intent == Intent.CODE_ACTION and nxt.slots.get("code_action") in ("open_project", "open_file"):
                    continue  # opening the project/file launches VS Code anyway — avoid an extra empty window
            if attach_next and nlu.intent in (Intent.DRAFT_EMAIL, Intent.SEND_EMAIL):
                nlu.slots["attach_last_file"] = True
                attach_next = False
            nlu = self.dialogue.resolve_references(nlu, st)
            nlu_dump.append(nlu.to_dict())
            log.info("nlu", extra={"data": nlu.to_dict()})

            missing = self.dialogue.missing_slots(nlu)
            if missing:
                slot, question = missing[0]
                if question == "__no_draft__":
                    outputs.append("There's no Gmail draft from this conversation for me to send.")
                    continue
                self.dialogue.ask(st, nlu, slot, question)
                st.queue = queue
                outputs.append(question)
                return AssistantResponse("\n".join(outputs), "awaiting_input", cid, plans=plans, question=question, nlu=nlu_dump)

            decision = self.planner.build(nlu, st)
            if isinstance(decision, Reply) and decision.use_llm and nlu.source != "rules" and COMMAND_START.match(nlu.text) \
                    and self.svc.settings.get("llm.use_for_planning", True):
                decision = self.planner.plan_with_llm(nlu.text)

            if isinstance(decision, Clarify):
                st.pending_slot = decision.slot
                st.pending_question = decision.question
                if st.frame is None:
                    st.frame = nlu
                st.queue = queue
                outputs.append(decision.question)
                return AssistantResponse("\n".join(outputs), "awaiting_input", cid, plans=plans, question=decision.question,
                                         nlu=nlu_dump)
            if isinstance(decision, Reply):
                text = self._chat(nlu.text, st) if decision.use_llm else decision.text
                if text:
                    outputs.append(text)
                ui_action = decision.ui_action or ui_action
                if decision.use_llm and worst == "done":
                    worst = "chat" if len(items) == 1 else worst
                continue

            plan: Plan = decision
            task_id = plan.id
            self.svc.repos.tasks.create(task_id, cid, plan.request, plan.to_dict())
            self.svc.events.emit(EventType.PLAN_CREATED, plan=plan.to_dict())
            self._set_state(AssistantState.EXECUTING, detail=plan.steps[0].description if plan.steps else "")
            outcome = self.executor.run(plan, task_id=task_id)
            self._after_plan(plan, nlu, st, outcome.status)
            plans.append(plan.to_dict())
            if outcome.status == "awaiting_confirmation" and outcome.confirmation is not None:
                pc = outcome.confirmation
                pc.payload.update({"queue": queue, "nlu": nlu})
                st.pending_confirmation_id = pc.id
                done_so_far = summarize(plan)
                prefix = [done_so_far.text] if any(s.status == StepStatus.SUCCEEDED and not s.hidden for s in plan.steps) else []
                outputs += prefix + [pc.prompt]
                return AssistantResponse("\n".join(outputs), "awaiting_confirmation", cid, plans=plans, confirmation=pc.public(),
                                         nlu=nlu_dump)
            report = summarize(plan)
            outputs.append(self._post_process(plan, nlu, report.text))
            if not report.success:
                worst = "partial" if report.partial or worst == "done" and outputs[:-1] else "failed"
                if queue:
                    outputs.append("I didn't continue with the rest of your request.")
                break
        text = "\n".join(o for o in outputs if o) or "Okay."
        return AssistantResponse(text, worst, cid, plans=plans, ui_action=ui_action, nlu=nlu_dump)

    # ---------------------------------------------------------- confirmation
    def _cancel_confirmation(self, st: DialogueState, pc: PendingConfirmation) -> Plan:
        self.svc.confirmations.pop(pc.id)
        st.pending_confirmation_id = None
        plan: Plan = pc.payload["plan"]
        idx = pc.payload["index"]
        plan.steps[idx].status = StepStatus.CANCELLED
        for later in plan.steps[idx + 1:]:
            if later.status == StepStatus.PENDING:
                later.status = StepStatus.SKIPPED
        for s in plan.steps:
            self.svc.events.emit(EventType.STEP_UPDATED, plan_id=plan.id, step=s.to_dict())
        self.svc.repos.tool_executions.record(task_id=pc.payload.get("task_id"), tool=pc.tool, args=pc.args_preview, risk=pc.risk,
                                              status="cancelled_by_user", confirmed=False, message="User declined")
        self.svc.repos.tasks.update(plan.id, "cancelled", plan=plan.to_dict())
        self.svc.events.emit(EventType.CONFIRMATION_RESOLVED, id=pc.id, approved=False)
        return plan

    def _finish_confirmation(self, st: DialogueState, pc: PendingConfirmation, approved: bool) -> AssistantResponse:
        cid = st.conversation_id
        if not approved:
            plan = self._cancel_confirmation(st, pc)
            step = plan.steps[pc.payload["index"]]
            return AssistantResponse(f"Okay, I won't {step.description[:1].lower() + step.description[1:]}. Cancelled.", "done", cid,
                                     plans=[plan.to_dict()])
        self.svc.confirmations.pop(pc.id)
        st.pending_confirmation_id = None
        self.svc.events.emit(EventType.CONFIRMATION_RESOLVED, id=pc.id, approved=True)
        plan: Plan = pc.payload["plan"]
        nlu: NLUResult = pc.payload.get("nlu")
        outcome = self.executor.run(plan, start=pc.payload["index"], confirmed_index=pc.payload["index"], task_id=plan.id)
        self._after_plan(plan, nlu, st, outcome.status)
        if outcome.status == "awaiting_confirmation" and outcome.confirmation is not None:
            nxt = outcome.confirmation
            nxt.payload.update({"queue": pc.payload.get("queue", []), "nlu": nlu})
            st.pending_confirmation_id = nxt.id
            return AssistantResponse(nxt.prompt, "awaiting_confirmation", cid, plans=[plan.to_dict()], confirmation=nxt.public())
        report = summarize(plan)
        outputs = [self._post_process(plan, nlu, report.text)]
        queue = pc.payload.get("queue") or []
        if report.success and queue:
            return self._run_requests(queue, st, outputs=outputs, plans=[plan.to_dict()])
        status = "done" if report.success else ("partial" if report.partial else "failed")
        return AssistantResponse("\n".join(outputs), status, cid, plans=[plan.to_dict()])

    # ------------------------------------------------------------- helpers
    def _after_plan(self, plan: Plan, nlu: Optional[NLUResult], st: DialogueState, status: str) -> None:
        for step in plan.steps:
            if step.status == StepStatus.SUCCEEDED and step.result:
                data = dict(step.result.data)
                if step.tool == "web_search":
                    data.pop("url", None)
                self.dialogue.remember_referents(st, nlu, data)
        if nlu is not None:
            self.dialogue.remember_referents(st, nlu, {})
        report = summarize(plan)
        self.svc.memory.record_task(plan.request, status, report.text[:200])
        self.svc.repos.tasks.update(plan.id, status, plan=plan.to_dict(), result={"summary": report.text})

    def _post_process(self, plan: Plan, nlu: Optional[NLUResult], text: str) -> str:
        """Optional LLM summaries of content the tools fetched (wrapped as untrusted)."""
        if not getattr(self.svc.llm, "is_generative", False) or nlu is None:
            return text
        req = nlu.text.lower()
        for step in plan.steps:
            if step.status != StepStatus.SUCCEEDED or not step.result:
                continue
            content = None
            if step.tool == "read_file" and re.search(r"\b(summari[sz]e|explain|what'?s in|tell me about|read)\b", req):
                content = step.result.data.get("content")
                task = "Summarise this document for the user in 3-5 sentences."
            elif step.tool == "browser_read_page":
                content = step.result.data.get("content")
                task = "Summarise this web page for the user in 3-5 sentences."
            elif step.tool == "web_search" and step.result.data.get("results"):
                content = "\n".join(f"- {r['title']}: {r['snippet']} ({r['url']})" for r in step.result.data["results"])
                task = f"Using these search results, answer the user's request “{nlu.text}” in 2-4 sentences."
            if content:
                try:
                    summary = self.svc.llm.chat([
                        {"role": "system", "content": prompts.system_prompt()},
                        {"role": "user", "content": f"{task}\n\n{wrap_untrusted(content, step.tool)}"},
                    ], max_tokens=350, temperature=0.3)
                    if summary:
                        text = f"{text}\n\n{summary.strip()}"
                except LLMError:
                    pass
        return text

    def _chat(self, text: str, st: DialogueState) -> str:
        memory = self.svc.memory.context_for_prompt() if self.svc.memory.enabled else ""
        messages = [{"role": "system", "content": prompts.system_prompt(self.svc.settings.get("user.name", ""), memory)}]
        messages += [m for m in list(st.history)[-12:-1]]
        messages.append({"role": "user", "content": text})
        try:
            return self.svc.llm.chat(messages, max_tokens=600)
        except LLMAuthError:
            return "My language model rejected the configured API token. Please check HF_TOKEN in your .env file."
        except LLMError as exc:
            log.warning("chat failed: %s", exc)
            return "I couldn't reach my language model just now. Local commands still work — try again in a moment."

    def _on_reminder_due(self, reminder: dict) -> None:
        late = reminder.get("late_minutes", 0)
        text = f"Reminder: {reminder['text']}" + (f" (this was due {late} minutes ago)" if late >= 2 else "")
        self.svc.events.emit(EventType.REMINDER_DUE, id=reminder["id"], text=text)
        self.svc.notify("Genesis reminder", text)
