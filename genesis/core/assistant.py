"""GenesisCore — the orchestrator that connects every layer.

    text/voice ─► normalise ─► split compound request ─► for each part:
        NLU (rules → classifier → LLM) ─► dialogue (coreference, slot filling)
        ─► planner (rules / LLM) ─► executor (permission → confirm → tool → verify)
        ─► verifier (honest summary) ─► response (text + speech)

The UI and the voice pipeline only ever call :meth:`GenesisCore.handle` and
:meth:`GenesisCore.resolve_confirmation`; everything else is internal.
"""
from __future__ import annotations

import copy
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Union

from ..agent.executor import Executor
from ..agent.plan import Clarify, Plan, Reply, StepStatus
from ..agent.planner import Planner
from ..agent.verifier import summarize
from ..ai import prompts
from ..ai.llm.base import LLMAuthError, LLMError
from ..nlp import followups, rules
from ..nlp.language import Incoming, LanguageService, language_name, multilingual_yes_no
from ..v2 import cancel as v2cancel
from ..v2 import coding_router as v2code
from ..v2 import nlu as v2nlu
from ..v2 import voice_ai as v2voice
from ..nlp.dialogue import DialogueManager, DialogueState
from ..nlp.intents import Intent, NLUResult
from ..nlp.pipeline import NLUPipeline
from ..security.confirmation import PendingConfirmation, parse_confirmation
from ..security.injection import wrap_untrusted
from ..services.routines import RoutineScheduler
from ..services.scheduler import ReminderScheduler
from ..tools.registry import ToolRegistry, build_default_registry
from .container import ServiceContainer
from .events import AssistantState, EventType
from .logging_setup import get_logger
from .paths import PROJECT_ROOT, is_frozen
from . import persona
from .context import ContextEngine
from .session import SessionManager

log = get_logger("core")
from ..nlp.routing import ANSWER_ONLY, COMMAND_START, GOAL_VERBS, REQUEST_START, is_answer_only, looks_like_request  # noqa: E402,F401

# sends/submits that can be postponed ("don't send it yet") and resumed later ("okay, send it")
DEFERRABLE_TOOLS = {"whatsapp_send", "gmail_send_draft"}
DEFER_RE = re.compile(r"\b(yet|later|for now|not now|hold on|wait)\b|^(?:no,?\s+)?(?:don'?t|do not)\s+(?:send|submit|post)\b", re.I)
MAX_REQUESTS_PER_TURN = 8  # runaway guard for compound requests / routines
TURN_TIME_BUDGET = 240.0  # seconds; remaining sub-requests are dropped after this
NON_REPEATABLE = {Intent.MEMORY, Intent.SESSION, Intent.SETTINGS, Intent.UI_CONTROL, Intent.CANCEL, Intent.ROUTINE,
                  Intent.CONVERSATION, Intent.INFORMATION_REQUEST, Intent.UNKNOWN, Intent.IDENTITY}


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
    language: str = "en"  # language of the reply (ISO code)
    spoken: Optional[str] = None  # what to say aloud when it differs from ``text`` (e.g. without the translation note)
    english_text: Optional[str] = None  # the English reply before it was translated (spoken if no voice exists)

    @property
    def speech(self) -> str:
        return speech_text(self.spoken if self.spoken is not None else self.text)

    @property
    def speech_english(self) -> str:
        return speech_text(self.english_text) if self.english_text else self.speech

    def to_dict(self) -> dict:
        return {"text": self.text, "status": self.status, "conversation_id": self.conversation_id, "plans": self.plans,
                "confirmation": self.confirmation, "question": self.question, "ui_action": self.ui_action, "nlu": self.nlu,
                "language": self.language}


def speech_text(text: str, max_chars: int = 400) -> str:
    """Make text pleasant for TTS: drop URLs, markdown, emoji; keep it short."""
    from ..v2 import speech as v2speech  # v2 hook (2.2.2): no code, file names, settings or asides read aloud

    text = v2speech.natural(text or "") or (text or "")
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
        self.nlu = NLUPipeline(services.llm, use_llm=lambda: bool(services.settings.get("llm.use_for_nlu", True)),
                               fast_routes=lambda: bool(services.settings.get("llm.fast_routes", True))
                               and bool(services.settings.get("llm.use_for_planning", True)))
        self.dialogue = DialogueManager()
        self.planner = Planner(self.registry, services)
        self.executor = Executor(self.registry, services)
        self.scheduler = ReminderScheduler(services.repos.reminders, self._on_reminder_due)
        services.scheduler = self.scheduler
        self._lock = threading.RLock()
        self.conversation_id: Optional[str] = None
        self.state = AssistantState.SLEEPING
        self.session = SessionManager(services.settings, services.events)
        self.routines = RoutineScheduler(services.repos.routines, self._run_routine)
        self.context = ContextEngine(services, self.session)
        self._recent_actions: list[tuple[str, str, float]] = []  # (signature, request text, time) for routine suggestions
        v2voice.attach(services)  # v2 hook (2.2): voice transcription knows which window you're in
        from ..v2 import tasks as v2tasks  # v2 hook: persistent task journal (listens to plan/step events)

        v2tasks.attach(services)
        self.language = LanguageService(services.llm, services.settings,
                                        english_parser=lambda t: rules.parse(rules.normalize(t)) is not None)

    # =============================================================== life-cycle
    def start(self) -> None:
        if not is_frozen() and self.svc.repos.projects.get("genesis") is None:
            self.svc.repos.projects.set("genesis", str(PROJECT_ROOT))  # so "open my Genesis project" works out of the box
        self.ensure_conversation()
        self.scheduler.start()
        self.session.start()
        self.routines.start()
        if self.svc.settings.get("performance.prewarm", True):
            threading.Thread(target=self._prewarm, name="genesis-prewarm", daemon=True).start()
        log.info("Genesis core started", extra={"data": self.svc.config.describe()})
        self._set_state(AssistantState.SLEEPING)

    def _prewarm(self) -> None:
        """Do the one-off slow work in the background at start-up, so the first real request doesn't pay for it:
        index Start-menu shortcuts (app lookup) and open the HTTPS connection to the AI model (TLS handshake)."""
        try:
            shortcuts = getattr(self.svc.apps, "shortcuts", None)
            if callable(shortcuts):
                shortcuts()
        except Exception:
            log.debug("shortcut prewarm failed", exc_info=True)
        try:
            from ..ai.llm.openai_compat import OpenAICompatibleProvider

            prov = getattr(self.svc.llm, "primary", None)
            if isinstance(prov, OpenAICompatibleProvider):
                prov.warm()
        except Exception:
            log.debug("LLM connection prewarm failed", exc_info=True)

    def stop(self) -> None:
        self.scheduler.stop()
        self.session.stop()
        self.routines.stop()
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
    def handle(self, text: str, source: str = "text", lang_hint: Optional[str] = None,
               lang_confidence: float = 0.0) -> AssistantResponse:
        """One turn. ``lang_hint``/``lang_confidence``: the language speech recognition heard (voice turns)."""
        interactive = source != "routine"
        with self._lock:
            v2cancel.reset(self.svc)  # v2 hook: a new turn starts uncancelled
            cid = self.ensure_conversation()
            st = self.dialogue.state(cid)
            incoming = self._understand_language(text, st, source, lang_hint, lang_confidence)
            work = incoming.english  # the pipeline below always works on English
            clean = rules.normalize(work)
            if not clean and st.pending_slot in ("confirm_name", "repo") and work.strip():
                clean = work.strip().strip(".!?")  # answering "genesis" to "type the repository's name" isn't the wake word
            st.turn_source = source
            if interactive:
                self.session.begin_turn(source)  # wakes the conversation session if Genesis was sleeping
            try:
                if not clean:  # just the wake word
                    who = self.svc.identity.get().call_name if self.svc.identity else ""
                    greeting = self.svc.settings.get("voice.greeting", "I'm listening.")
                    resp = AssistantResponse(f"Yes, {who}? {greeting}" if who else greeting, "chat", cid)
                    self._set_state(self._resting_state())
                    return resp
                self._record("user", text.strip(), {"source": source})
                self._log_utterance(source, clean)
                self._set_state(AssistantState.THINKING)
                target = self.language.reply_language(incoming.lang)
                st.chat_language = target if target != "en" else None
                try:
                    if incoming.error:
                        resp = self._language_unavailable(incoming, cid)
                    else:
                        resp = self._process(clean, st)
                except Exception:
                    log.exception("unhandled error while processing request")
                    resp = AssistantResponse("Sorry — something went wrong on my side while handling that. "
                                             "The details are in logs/errors.log.", "error", cid)
                finally:
                    chat_lang, st.chat_language = st.chat_language, None
                resp.text = persona.styled(resp.text, self.svc.settings)
                if not incoming.error:
                    self._localize(resp, incoming, chat_lang)
                self._finish(resp)
                return resp
            finally:
                if interactive:
                    awaiting = "resp" in locals() and resp.status in ("awaiting_confirmation", "awaiting_input")
                    self.session.end_turn(awaiting_reply=awaiting, task_open=awaiting or bool(st.queue))
                    if "resp" in locals() and resp.ui_action == "sleep":
                        self.session.sleep("user_request")

    # ============================================================== languages
    # the user's own words — a message body, a typed repository name, an address or a name — are never translated
    _VERBATIM_SLOTS = ("confirm_name", "repo", "text", "message", "content", "email", "recipient")

    def _understand_language(self, text: str, st: DialogueState, source: str, hint: Optional[str],
                             hint_confidence: float) -> Incoming:
        """Detect the language of ``text`` and return its English version (see nlp/language.py)."""
        if source == "routine" or not self.language.enabled:
            return Incoming(text, text, "en")
        if st.pending_slot in self._VERBATIM_SLOTS and st.frame is not None:
            return Incoming(text, text, st.language)  # a message body or a typed repository name stays exactly as said
        if st.pending_confirmation_id or st.pending_yesno:
            yn = multilingual_yes_no(text)
            if yn is not None:  # "oui" / "nahi" / "はい" — understood offline, no model call
                det = self.language.detect(text, hint, hint_confidence)
                lang = det.lang if det.lang not in ("und",) else st.language
                return Incoming(text, "yes" if yn else "no", lang)
        inc = self.language.understand(text, hint=hint, hint_confidence=hint_confidence, previous=st.language)
        st.language = inc.lang
        if inc.translated:
            log.info("language: %s → translated to English", language_name(inc.lang),
                     extra={"data": {"lang": inc.lang, "translated": True}})
        return inc

    def _language_unavailable(self, inc: Incoming, cid: str) -> AssistantResponse:
        name = language_name(inc.lang)
        return AssistantResponse(f"I can tell you're speaking {name}, but I need my AI model to translate it, and it isn't "
                                 "available right now. Commands in English work without it — or say “check your AI "
                                 "connection”.", "failed", cid)

    def _localize(self, resp: AssistantResponse, inc: Incoming, chat_lang: Optional[str]) -> None:
        """Translate the reply into the user's language (and add a small note showing what Genesis understood)."""
        lang = self.language.reply_language(inc.lang or "en")
        if lang == "en":
            resp.language = "en"
        elif resp.status == "chat" and chat_lang:
            resp.language = lang  # the chat model already answered in the user's language
        else:
            english = resp.text
            local = self.language.localize(english, lang)
            if local != english:
                resp.english_text, resp.text, resp.language = english, local, lang
        if inc.translated and (self.svc.settings.get("language.show_translation", True)
                               or resp.status == "awaiting_confirmation"):  # v2 hook: never approve a guess unseen
            resp.spoken = resp.text
            resp.text = f"{resp.text}\n\n🌐 {language_name(inc.lang)} → “{inc.english}”"

    def _resting_state(self) -> AssistantState:
        return AssistantState.ACTIVE_CONVERSATION if self.session.active else AssistantState.SLEEPING

    def _log_utterance(self, source: str, text: str) -> None:
        data = {"source": source, "chars": len(text), "session": self.session.session.id if self.session.session else None}
        if self.svc.settings.get("debug.log_content", False):
            data["text"] = text
        log.info("utterance", extra={"data": data})

    def request_cancel(self) -> None:
        """2.0: stop the running task after its current step (safe to call from any thread — doesn't take the turn lock)."""
        v2cancel.request(self.svc)
        log.info("cancel requested")

    def resolve_confirmation(self, confirmation_id: str, approved: bool) -> AssistantResponse:
        """Called by UI buttons (Confirm / Cancel)."""
        with self._lock:
            v2cancel.reset(self.svc)  # v2 hook
            cid = self.ensure_conversation()
            st = self.dialogue.state(cid)
            pc = self.svc.confirmations.get(confirmation_id)
            if pc is None:
                resp = AssistantResponse("That confirmation has expired, so I didn't do anything.", "done", cid)
                st.pending_confirmation_id = None
            else:
                self._record("user", "Yes, continue." if approved else "No, cancel.", {"source": "button"})
                resp = self._finish_confirmation(st, pc, approved)
            resp.text = persona.styled(resp.text, self.svc.settings)
            self._localize(resp, Incoming("", "", st.language), None)
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
            self._set_state(self._resting_state(), success=resp.status in ("done", "chat"))

    # ============================================================== pipeline
    def _process(self, text: str, st: DialogueState) -> AssistantResponse:
        cid = st.conversation_id
        if st.pending_yesno:
            handled = self._answer_yesno(text, st)
            if handled is not None:
                return handled
        if st.pending_confirmation_id:
            pc = self.svc.confirmations.get(st.pending_confirmation_id)
            if pc is None:
                st.pending_confirmation_id = None
            else:
                answer = parse_confirmation(text)
                if answer is not None:
                    return self._finish_confirmation(st, pc, answer, text)
                probe = rules.parse(followups.strip_interjections(text) or text)
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
        queue = list(items)[:MAX_REQUESTS_PER_TURN]
        if len(items) > MAX_REQUESTS_PER_TURN:
            outputs.append(f"That's a lot at once — I'll do the first {MAX_REQUESTS_PER_TURN} steps.")
        ui_action: Optional[str] = None
        started = time.monotonic()
        expansions = 0
        while queue:
            if time.monotonic() - started > TURN_TIME_BUDGET:
                outputs.append("That's taking too long, so I stopped before the remaining steps.")
                queue.clear()
                break
            item = queue.pop(0)
            if isinstance(item, str):
                if re.match(r"^(?:and\s+)?attach\b", item.strip(), re.I):
                    attach_next = True
                    continue
                fu = followups.resolve(item, st)
                if fu is not None and fu.question:
                    st.queue = queue
                    outputs.append(fu.question)
                    return AssistantResponse("\n".join(outputs), "awaiting_input", cid, plans=plans, question=fu.question,
                                             nlu=nlu_dump)
                if fu is not None and fu.reply:
                    outputs.append(fu.reply)
                    continue
                if fu is not None and fu.nlu is not None:
                    nlu = fu.nlu
                else:
                    # v2 hook (2.1): "stop taking notes" must not lose its "stop" to the interjection filter
                    nlu = v2nlu.raw_command(item) or self.nlu.parse(followups.strip_interjections(item) or item,
                                                                    context=list(st.history),
                                                                    context_text=self.context.snapshot(st).to_prompt())
                    nlu = v2voice.route(nlu, getattr(st, "turn_source", "text"))  # v2 hook (2.1): no actions on a guess
                    nlu = v2code.route(nlu, self.svc)  # v2 hook (2.2): coding goes to Colab / the window you're in
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
            nlu_log = {"intent": nlu.intent.value, "confidence": round(nlu.confidence, 3), "source": nlu.source,
                       "slots": sorted(nlu.slots)}
            if self.svc.settings.get("debug.log_content", False):
                nlu_log = nlu.to_dict()
            log.info("nlu: %s (%s, %.2f) — %d ms into the turn", nlu.intent.value, nlu.source, nlu.confidence,
                     int((time.monotonic() - started) * 1000), extra={"data": nlu_log})

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

            self._set_state(AssistantState.PLANNING, detail=nlu.intent.value.replace("_", " "))
            decision = self.planner.build(nlu, st)
            if isinstance(decision, Reply) and decision.use_llm and decision.data and decision.data.get("plan_goal"):
                self._set_state(AssistantState.PLANNING, detail="Working out the steps…")
                decision = self.planner.plan_with_llm(decision.data["plan_goal"], self.context.snapshot(st).to_prompt())
            elif isinstance(decision, Reply) and decision.use_llm and nlu.source != "rules" and looks_like_request(nlu.text) \
                    and self.svc.settings.get("llm.use_for_planning", True):
                # a task Genesis has no template for: let the LLM work out the steps from the tool registry
                self._set_state(AssistantState.PLANNING, detail="Working out the steps…")
                decision = self.planner.plan_with_llm(nlu.text, self.context.snapshot(st).to_prompt())

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
                cmds = decision.data.get("run_commands") if decision.data else None
                if cmds:
                    if expansions >= 1:  # prevents routines from (recursively) starting routines
                        outputs.append("A routine can't start another routine.")
                        continue
                    expansions += 1
                    queue = self.nlu.split(cmds)[:MAX_REQUESTS_PER_TURN] + queue
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
            # only a follow-up Genesis itself resolved from the conversation may carry the user's earlier approval (never LLM output)
            pre = 0 if (nlu.intent == Intent.FOLLOW_UP and nlu.source == "context" and nlu.slots.get("pre_confirmed")
                        and len(plan.steps) == 1 and plan.steps[0].tool in DEFERRABLE_TOOLS and not plan.untrusted) else None
            outcome = self.executor.run(plan, task_id=task_id, confirmed_index=pre)
            if outcome.status == "failed" and plan.source == "llm" and not plan.replanned:
                alt = self._replan(plan, st)  # observe → correct: one alternative approach, never a loop
                if alt is not None:
                    self._after_plan(plan, nlu, st, outcome.status)
                    plans.append(plan.to_dict())
                    outputs.append(summarize(plan).text + "\nThat didn't work, so I tried another way.")
                    plan, task_id = alt, alt.id
                    self.svc.repos.tasks.create(task_id, cid, plan.request, plan.to_dict())
                    self.svc.events.emit(EventType.PLAN_CREATED, plan=plan.to_dict())
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
            if report.success:
                tip = self._learn_workflow(nlu, st)
                if tip:
                    outputs.append(tip)
            if not report.success:
                worst = "partial" if report.partial or worst == "done" and outputs[:-1] else "failed"
                if queue:
                    outputs.append("I didn't continue with the rest of your request.")
                break
        text = "\n".join(o for o in outputs if o) or "Okay."
        return AssistantResponse(text, worst, cid, plans=plans, ui_action=ui_action, nlu=nlu_dump)

    def _answer_yesno(self, text: str, st: DialogueState) -> Optional[AssistantResponse]:
        """Answer to a simple yes/no question Genesis asked (not a permission confirmation)."""
        q, st.pending_yesno = st.pending_yesno, None
        answer = parse_confirmation(text)
        if answer is None or q is None:
            return None  # not an answer → treat as a new request
        cid = st.conversation_id
        idm = self.svc.identity
        if q["kind"] == "name_variant":
            if answer:
                idm.learn_variant(q["heard"])
                return AssistantResponse(f"Great — I'll remember that “{q['heard']}” means you, {q['form']}.", "done", cid)
            idm.reject_variant(q["heard"])
            return AssistantResponse("Okay, noted. If I've got your name wrong, tell me “my name is …”.", "done", cid)
        if q["kind"] == "set_name":
            if answer:
                ident = idm.set_name(q["name"])
                return AssistantResponse(f"Nice to meet you, {ident.call_name}! I'll remember your name.", "done", cid)
            return AssistantResponse("Okay — I won't change it. Tell me “my name is …” whenever you like.", "done", cid)
        return None

    def _replan(self, plan: Plan, st: DialogueState) -> Optional[Plan]:
        failed = next((s for s in plan.steps if s.status in (StepStatus.FAILED, StepStatus.BLOCKED)), None)
        if failed is None or failed.status == StepStatus.BLOCKED:
            return None  # permission problems are never "worked around"
        self._set_state(AssistantState.RETRYING, detail="Trying another way…")
        done = ", ".join(s.tool for s in plan.steps if s.status == StepStatus.SUCCEEDED) or "none"
        alt = self.planner.plan_with_llm(plan.request, self.context.snapshot(st).to_prompt(),
                                         failure={"failed_tool": failed.tool, "error": (failed.result.message if failed.result else "")[:300],
                                                  "done": done})
        if isinstance(alt, Plan) and alt.steps:
            alt.replanned = True
            return alt
        return None

    # ---------------------------------------------------------- confirmation
    def _cancel_confirmation(self, st: DialogueState, pc: PendingConfirmation, defer: bool = False) -> Plan:
        self.svc.confirmations.pop(pc.id)
        st.pending_confirmation_id = None
        plan: Plan = pc.payload["plan"]
        idx = pc.payload["index"]
        step = plan.steps[idx]
        if step.tool == "whatsapp_send":
            for f in st.focus:
                if f.kind == "wa_draft":
                    f.value["declined"] = not defer  # a declined send needs a fresh confirmation before it can go
        if step.tool in DEFERRABLE_TOOLS and step.resolved_args is not None and defer:
            st.deferred_action = {"tool": step.tool, "args": dict(step.resolved_args), "description": step.description,
                                  "at": time.time()}
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

    def _finish_confirmation(self, st: DialogueState, pc: PendingConfirmation, approved: bool, text: str = "") -> AssistantResponse:
        cid = st.conversation_id
        if not approved:
            # "don't send it yet" postpones (→ "okay, send it" later); a plain "no" / "cancel" / the Cancel button cancels
            plan = self._cancel_confirmation(st, pc, defer=bool(DEFER_RE.search(text or "")))
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
        track = self.svc.memory.enabled and self.svc.settings.get("memory.track_usage", True)
        for step in plan.steps:
            if step.status == StepStatus.FAILED and step.tool == "run_program" and step.result:
                # a program that crashed is still useful context: "fix it", "why is this error happening", "try again"
                self.dialogue.update_context(st, step.tool, dict(step.result.data), step.resolved_args or step.args)
            if step.status == StepStatus.SUCCEEDED and step.result:
                data = dict(step.result.data)
                if step.tool == "web_search":
                    data.pop("url", None)
                self.dialogue.remember_referents(st, nlu, data)
                ctx_args = dict(step.resolved_args or step.args or {})
                if nlu is not None and nlu.slots.get("back"):
                    ctx_args["__back__"] = True  # context-only marker; never passed to the tool
                if nlu is not None and nlu.slots.get("undo"):
                    ctx_args["__undo__"] = True
                self.dialogue.update_context(st, step.tool, dict(step.result.data), ctx_args)
                if track:
                    try:
                        self.svc.repos.usage.bump(f"tool:{step.tool}")
                        if step.tool == "open_application" and data.get("app"):
                            self.svc.repos.usage.bump(f"app:{data['app']}")
                    except Exception:
                        pass
        if nlu is not None and nlu.intent not in NON_REPEATABLE and status != "awaiting_confirmation":
            if plan.succeeded or status == "completed":
                st.last_request = copy.deepcopy(nlu)
            elif status in ("failed", "blocked"):
                st.last_failed = copy.deepcopy(nlu)
        if nlu is not None:
            self.dialogue.remember_referents(st, nlu, {})
        report = summarize(plan)
        st.current_task = {"request": plan.request, "status": status, "intent": plan.intent, "reason": plan.reason,
                           "steps": [s.description[:1].lower() + s.description[1:] for s in plan.steps
                                     if s.status == StepStatus.SUCCEEDED and not s.hidden and not s.is_recovery]
                           or [s.description[:1].lower() + s.description[1:] for s in plan.steps if not s.is_recovery][:1],
                           "recovered": " ".join(s.recovery_reason for s in plan.steps if s.is_recovery and s.status == StepStatus.SUCCEEDED)}
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
        ident = self.svc.identity.get() if self.svc.identity else None
        who = f"{ident.canonical} (call them {ident.call_name})" if ident and ident.canonical else ""
        spoken = getattr(st, "turn_source", "text") == "voice"
        brevity = ("\n\nThis reply will be spoken aloud: answer in one to three short sentences unless the user asks for "
                   "detail, a list or code." if spoken else "")
        if st.chat_language:
            name = ("Hindi written in Latin letters (Hinglish)" if st.chat_language == "hi-Latn"
                    else language_name(st.chat_language))
            brevity += f"\n\nThe user is speaking {name}. Write your whole answer in {name}."
        brevity += v2voice.chat_prompt_addition()  # v2 hook (2.1): voice chat mode talks like a conversation partner
        messages = [{"role": "system", "content": prompts.system_prompt(who, memory) + brevity +
                     "\n\nWhat's going on right now:\n" + self.context.snapshot(st, turns=0).to_prompt()}]
        messages += [m for m in list(st.history)[-v2voice.history_turns(7):-1]]  # compact: the last few turns only
        messages.append({"role": "user", "content": text})
        try:
            return self.svc.llm.chat(messages, max_tokens=300 if spoken else 600)  # shorter spoken answers arrive sooner
        except LLMAuthError:
            return "My language model rejected the configured API token. Please check HF_TOKEN in your .env file."
        except LLMError as exc:
            log.warning("chat failed: %s", exc)
            return "I couldn't reach my language model just now. Local commands still work — try again in a moment."

    # ------------------------------------------------------ routines & learning
    def _run_routine(self, routine: dict) -> None:
        """Scheduled routine: runs through the normal pipeline (permissions/confirmations still apply)."""
        self.svc.notify("Genesis routine", f"Running “{routine['name']}”")
        resp = self.handle(routine["commands"], source="routine")
        if resp.status in ("awaiting_confirmation", "awaiting_input"):
            self.svc.notify("Genesis routine needs you", resp.text[:200])

    @staticmethod
    def _signature(nlu: NLUResult) -> Optional[str]:
        s = nlu.slots
        key = s.get("app") or s.get("project") or s.get("folder") or s.get("url") or s.get("query")
        if nlu.intent in NON_REPEATABLE or nlu.intent in (Intent.FOLLOW_UP, Intent.REVISE_DRAFT, Intent.REPLY_EMAIL) or not key:
            return None
        return f"{nlu.intent.value}:{str(key).lower()[:60]}"

    def _learn_workflow(self, nlu: NLUResult, st: DialogueState) -> Optional[str]:
        """Count repeated command sequences (names only, no content) and *offer* to save them as a routine."""
        if not (self.svc.memory.enabled and self.svc.settings.get("memory.track_usage", True)):
            return None
        sig = self._signature(nlu)
        if not sig:
            return None
        now = time.monotonic()
        self._recent_actions = [a for a in self._recent_actions if now - a[2] < 300][-4:]
        if self._recent_actions and self._recent_actions[-1][0] == sig:
            return None
        self._recent_actions.append((sig, nlu.text, now))
        if len(self._recent_actions) < 2:
            return None
        seq = self._recent_actions[-2:]
        pattern = " → ".join(a[0] for a in seq)
        try:
            count = self.svc.repos.usage.bump_sequence(pattern)
            row = self.svc.repos.usage.sequence(pattern)
        except Exception:
            return None
        if count >= 3 and row and not row["suggested"] and self.svc.settings.get("memory.suggest_routines", True):
            self.svc.repos.usage.mark_suggested(pattern)
            commands = " and then ".join(a[1] for a in seq)
            st.pending_suggestion = {"commands": commands, "name": seq[-1][1][:40]}
            return (f"By the way, you often do “{seq[0][1]}” and then “{seq[1][1]}”. Say “save that as a routine” "
                    "and I'll do both with one command.")
        return None

    def _on_reminder_due(self, reminder: dict) -> None:
        late = reminder.get("late_minutes", 0)
        text = f"Reminder: {reminder['text']}" + (f" (this was due {late} minutes ago)" if late >= 2 else "")
        self.svc.events.emit(EventType.REMINDER_DUE, id=reminder["id"], text=text)
        self.svc.notify("Genesis reminder", text)

from ..v3.hooks import patch_assistant as _v3_hook; _v3_hook(globals())  # v3 hook (Genesis 3.0)
