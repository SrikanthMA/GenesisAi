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
from .intents import INTENT_DESCRIPTIONS, Intent, NLUResult, intent_from_str

log = get_logger("nlp")

NON_ACTION = {Intent.CONVERSATION, Intent.INFORMATION_REQUEST, Intent.UNKNOWN}
CLASSIFIER_THRESHOLD = 0.45
LLM_ESCALATION_THRESHOLD = 0.6


class NLUPipeline:
    def __init__(self, llm: Optional[LLMProvider] = None, *, use_llm: Callable[[], bool] = lambda: True,
                 classifier: Optional[IntentClassifier] = None) -> None:
        self.llm = llm
        self.use_llm = use_llm
        self.classifier = classifier or get_classifier()

    # ---------------------------------------------------------------- public
    def split(self, text: str) -> list[str]:
        return rules.split_requests(text)

    def parse(self, text: str, *, context: Optional[list[dict]] = None, now: Optional[datetime] = None) -> NLUResult:
        norm = rules.normalize(text)
        if not norm:
            return NLUResult(text=text, intent=Intent.UNKNOWN, confidence=0.0)
        hit = rules.parse(norm, now)
        if hit is not None:
            hit.alternatives = [(hit.intent.value, hit.confidence)]
            return hit

        preds = self.classifier.predict(norm)
        top, prob = preds[0]
        alternatives = [(i.value, p) for i, p in preds]
        log.debug("classifier %s", alternatives)

        if self._llm_ready() and (prob < LLM_ESCALATION_THRESHOLD or top not in NON_ACTION):
            try:
                res = self._llm_parse(norm, context or [], now)
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

    def _llm_parse(self, text: str, context: list[dict], now: Optional[datetime]) -> NLUResult:
        intents = "; ".join(f"{i.value} ({d})" for i, d in INTENT_DESCRIPTIONS.items() if i != Intent.UNKNOWN)
        ctx = "\n".join(f"{m['role']}: {m['content'][:200]}" for m in context[-6:]) or "(none)"
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
        slots: dict = {k: v for k, v in ents.items() if v not in (None, "", [])}
        # deterministic extractors override/augment model output where they're reliable
        slots.update({k: v for k, v in rules.extract_slots_for(intent, text, now).items() if v not in (None, "")})
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
