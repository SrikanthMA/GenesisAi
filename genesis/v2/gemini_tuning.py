"""Gemini through the OpenAI-compatible endpoint, tuned for an assistant (2.2.2).

Gemini 2.5+ "Flash" models think before answering, and the thinking counts against ``max_tokens``. Genesis often asks
for short answers (5-300 tokens), so a thinking model could use the whole budget thinking and return an empty reply -
which is why the setup tool picked the smaller "Flash-Lite" model. Sending ``reasoning_effort: "none"`` switches thinking
off, so the stronger Flash model answers fully and quickly. If a model doesn't accept the setting, Genesis remembers that
and asks again without it.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable

from ..core.logging_setup import get_logger

log = get_logger("v2.gemini")
_UNSUPPORTED: set[str] = set()

# Free Gemini keys have a separate per-minute and per-day allowance FOR EACH MODEL. When one model says "too many
# requests", the same question goes to the next model instead of failing (2.2.3). GENESIS_GEMINI_FALLBACKS overrides.
FALLBACK_MODELS = ("gemini-flash-lite-latest", "gemini-flash-latest", "gemini-2.5-flash-lite", "gemini-2.5-flash",
                   "gemini-2.0-flash")
COOLDOWN = {"rate_limited": 60.0, "quota": 3600.0, "not_found": 86400.0}
_cool_until: dict[str, float] = {}
_lock = threading.RLock()


def _fallbacks() -> list[str]:
    env = (os.getenv("GENESIS_GEMINI_FALLBACKS") or "").strip()
    return [m.strip() for m in env.split(",") if m.strip()] if env else list(FALLBACK_MODELS)


def cooling(model: str) -> bool:
    return time.monotonic() < _cool_until.get(model, 0.0)


def reset() -> None:
    _cool_until.clear()


def chat_with_failover(call: Callable[..., str], provider: Any, messages: Any, **kw: Any) -> str:
    """Ask the configured model; if Google rate-limits it (or it no longer exists), ask the next Gemini model."""
    from ..ai.llm.base import LLMModelError, LLMUnavailable

    with _lock:
        primary = provider.model
        order = [primary] + [m for m in _fallbacks() if m != primary]
        ready = [m for m in order if not cooling(m)] or order  # everything cooling down: try them all again
        last: Exception | None = None
        try:
            for m in ready:
                provider.model = m
                try:
                    out = call(messages, **kw)
                    if m != primary:
                        log.info("[LLM] %s was busy - answered by %s", primary, m)
                    return out
                except LLMModelError as exc:
                    _cool_until[m] = time.monotonic() + COOLDOWN["not_found"]
                    last = exc
                except LLMUnavailable as exc:
                    kind = getattr(exc, "kind", "")
                    if kind not in ("rate_limited", "quota"):
                        raise
                    _cool_until[m] = time.monotonic() + COOLDOWN.get(kind, 60.0)
                    last = exc
        finally:
            provider.model = primary
        raise last  # type: ignore[misc]


def tune(provider: Any, payload: dict) -> None:
    if getattr(provider, "name", "") != "gemini":
        return
    model = str(payload.get("model") or "")
    if "flash" in model.lower() and model not in _UNSUPPORTED:
        payload["reasoning_effort"] = "none"


def rejected(provider: Any, payload: dict, resp: Any) -> bool:
    """True when Gemini refused the request because of ``reasoning_effort`` (the caller then retries without it)."""
    if "reasoning_effort" not in payload or getattr(resp, "status_code", 200) != 400:
        return False
    # Google often answers just "Request contains an invalid argument." without naming the field, so ANY 400 while the
    # setting was sent means: ask again without it (and don't send it to this model again).
    _UNSUPPORTED.add(str(payload.get("model") or ""))
    payload.pop("reasoning_effort", None)
    return True
