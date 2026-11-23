"""The NLU pipeline: rules → statistical classifier → LLM.

    text ──normalise──► rule patterns ──hit──► NLUResult (source=rules)
                              │ miss
                              ▼
                    TF-IDF + LogReg classifier ──► top intent + probability
                              │ low confidence / actionable intent and LLM online
                              ▼
                    LLM structured NLU (JSON) ──► NLUResult (source=llm)

Each stage is independently testable; the pipeline degrades gracefully when
the LLM is offline.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from ..ai import prompts
from ..ai.llm.base import LLMError, LLMProvider
from ..core.logging_setup import get_logger
from . import rules
from .classifier import IntentClassifier, get_classifier
from .entities import extract_entities, parse_datetime
from .. import v2 as _v2  # noqa: F401  v2 hook: importing it applies the regex-cache performance fix
from .intents import INTENT_DESCRIPTIONS, Intent, NLUResult, intent_from_str

log = get_logger("nlp")

NON_ACTION = {Intent.CONVERSATION, Intent.INFORMATION_REQUEST, Intent.UNKNOWN}
CLASSIFIER_THRESHOLD = 0.45
LLM_ESCALATION_THRESHOLD = 0.6


class NLUPipeline:
    def __init__(self, llm: Optional[LLMProvider] = None, *, use_llm: Callable[[], bool] = lambda: True,
                 classifier: Optional[IntentClassifier] = None, fast_routes: Callable[[], bool] = lambda: False) -> None:
        self.llm = llm
        self.use_llm = use_llm
        self.fast_routes = fast_routes
        self.classifier = classifier or get_classifier()

    # ---------------------------------------------------------------- public
    def split(self, text: str) -> list:
        from ..v2 import nlu as v2nlu  # v2 hook: safer splitting + dependency-aware routing of multi-step goals

        _v2.ensure_regex_cache()
        if not v2nlu.ENABLED:
            return rules.split_requests(text)
        return v2nlu.split(text, rules.split_requests, self.rule_parse, rules._split_app_list, llm_ready=self._llm_ready())

    def rule_parse(self, text: str, now: Optional[datetime] = None) -> Optional[NLUResult]:
        """The deterministic layer only (2.0 overrides + 1.x rules) — no classifier, no model."""
        from ..v2 import nlu as v2nlu

        norm = rules.normalize(text)
        if not norm:
            return None
        if not v2nlu.ENABLED:
            return rules.parse(norm, now)
        return v2nlu.override(norm) or v2nlu.fix(rules.parse(v2nlu.canonicalize(norm), now))

    def parse(self, text: str, *, context: Optional[list[dict]] = None, now: Optional[datetime] = None,
              context_text: str = "") -> NLUResult:
        _v2.ensure_regex_cache()  # v2 hook: scikit-learn's import shrinks the global regex cache to 50 (see genesis/v2)
        norm = rules.normalize(text)
        if not norm:
            return NLUResult(text=text, intent=Intent.UNKNOWN, confidence=0.0)
        from ..v2 import nlu as v2nlu  # v2 hook: guards, canonical phrasings, overrides and post-fixes

        pre = v2nlu.override(norm) if v2nlu.ENABLED else None
        if pre is not None:
            pre.entities = extract_entities(norm, now)
            pre.alternatives = [(pre.intent.value, pre.confidence)]
            return pre
        canon = v2nlu.canonicalize(norm) if v2nlu.ENABLED else norm
        hit = v2nlu.fix(rules.parse(canon, now)) if v2nlu.ENABLED else rules.parse(canon, now)
        if hit is None:  # typo recovery: "opne youtub" → "open youtube" (command words only)
            fixed = rules.correct_typos(canon)
            if fixed != norm:
                hit = rules.parse(fixed, now)
                if hit is not None:
                    hit.source = "rules+typo"
                    hit.confidence = min(hit.confidence, 0.9)
        if hit is not None:
            hit.alternatives = [(hit.intent.value, hit.confidence)]
            return hit

        preds = self.classifier.predict(norm)
        top, prob = preds[0]
        alternatives = [(i.value, p) for i, p in preds]
        log.debug("classifier %s", alternatives)
        bare = v2nlu.bare_app(norm, top, rules.app_lookup) if v2nlu.ENABLED else None  # v2 hook: "chrome" = open it
        if bare is not None:
            bare.alternatives = alternatives
            return bare

        if self._llm_ready() and self.fast_routes():
            # save a model call: questions go straight to the chat model, unfamiliar instructions straight to the planner
            from .routing import is_answer_only, looks_like_request

            if is_answer_only(norm):
                return NLUResult(text=norm, intent=Intent.INFORMATION_REQUEST, confidence=0.9, entities=extract_entities(norm, now),
                                 source="fast-route", alternatives=alternatives)
            if looks_like_request(norm) and (prob < LLM_ESCALATION_THRESHOLD or top not in NON_ACTION) \
                    and top not in (Intent.SHUTDOWN, Intent.RESTART):
                return NLUResult(text=norm, intent=Intent.CONVERSATION, confidence=0.5, entities=extract_entities(norm, now),
                                 source="fast-route", alternatives=alternatives)
        if self._llm_ready() and (prob < LLM_ESCALATION_THRESHOLD or top not in NON_ACTION):
            try:
                res = self._llm_parse(norm, context or [], now, context_text)
                res.alternatives = alternatives
                return res
            except LLMError as exc:
                log.info("LLM NLU unavailable, using classifier: %s", exc)
            except Exception:
                log.exception("LLM NLU failed")

        if prob >= CLASSIFIER_THRESHOLD and top not in (Intent.SHUTDOWN, Intent.RESTART):
            slots = rules.extract_slots_for(top, norm, now)
            return NLUResult(text=norm, intent=top, confidence=prob, slots=slots, entities=extract_entities(norm, now),
                             source="classifier", alternatives=alternatives)
        # Power actions are never inferred statistically (too risky) — they need an explicit rule match.
        return NLUResult(text=norm, intent=Intent.CONVERSATION, confidence=1 - prob if prob < 0.5 else 0.5,
                         entities=extract_entities(norm, now), source="classifier", alternatives=alternatives)

    # --------------------------------------------------------------- helpers
    def _llm_ready(self) -> bool:
        return bool(self.llm is not None and getattr(self.llm, "is_generative", False) and self.use_llm())

    def _llm_parse(self, text: str, context: list[dict], now: Optional[datetime], context_text: str = "") -> NLUResult:
        intents = "; ".join(f"{i.value} ({d})" for i, d in INTENT_DESCRIPTIONS.items() if i != Intent.UNKNOWN)
        # compact context (current app/site/task, results on screen, …) instead of the whole conversation
        ctx = context_text or "\n".join(f"{m['role']}: {m['content'][:200]}" for m in context[-4:]) or "(none)"
        prompt = prompts.NLU_PROMPT.format(intents=intents, context=ctx, text=text)
        assert self.llm is not None
        data = self.llm.chat_json([{"role": "user", "content": prompt}], max_tokens=400)
        if not isinstance(data, dict):
            raise LLMError("NLU output was not an object")
        intent = intent_from_str(data.get("intent"))
        if intent in (Intent.SHUTDOWN, Intent.RESTART):
            # never let a model alone decide on power actions; require the explicit rule path
            intent = Intent.CONVERSATION
        ents = data.get("entities") or {}
        # the model may not set internal markers or confirmation answers (those only come from the user's own reply)
        slots: dict = {k: v for k, v in ents.items() if v not in (None, "", []) and not str(k).startswith("__")
                       and k not in ("confirm_name", "pre_confirmed")}
        # deterministic extractors override/augment model output where they're reliable
        extracted = {k: v for k, v in rules.extract_slots_for(intent, text, now).items() if v not in (None, "")}
        if intent in (Intent.YOUTUBE, Intent.HISTORY, Intent.CODE_ACTION):
            for k, v in extracted.items():  # free-text slots: keep the model's cleaner value when it gave one
                slots.setdefault(k, v)
        else:
            slots.update(extracted)
        if "datetime" in slots and isinstance(slots["datetime"], str):
            try:
                slots["datetime"] = datetime.fromisoformat(slots["datetime"])
            except ValueError:
                parsed = parse_datetime(slots["datetime"], now)
                slots["datetime"] = parsed.value if parsed else None
        if "file_op" in slots and "source" not in slots and slots.get("path"):
            slots["source"] = slots["path"]
        try:
            conf = float(data.get("confidence", 0.7))
        except (TypeError, ValueError):
            conf = 0.7
        return NLUResult(text=text, intent=intent, confidence=max(0.0, min(conf, 1.0)), slots=slots,
                         entities=extract_entities(text, now), source="llm")
