"""Operating mode (3.0).

``GENESIS_MODE=local`` (set by Setup_Local_AI.bat) means: conversations run on this PC through Ollama, and Genesis makes
**no** request to an external AI service — not for chat, not for speech transcription, not for image generation —
unless ``GENESIS_EXTERNAL_AI_ENABLED=true``. Even then an external model is only used as a fallback that is reported in
the reply ("answered by Gemini (external) - the local model was unavailable"), never silently.

``GENESIS_MODE=hybrid`` (or unset) keeps 2.x behaviour.
"""
from __future__ import annotations

import os


def _flag(name: str, default: bool = False) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def mode() -> str:
    m = (os.getenv("GENESIS_MODE") or "").strip().lower()
    return m if m in ("local", "hybrid") else "hybrid"


def is_local() -> bool:
    return mode() == "local"


def external_ai_allowed() -> bool:
    """May Genesis send anything to an external AI service (chat, transcription, image generation)?"""
    if is_local():
        return _flag("GENESIS_EXTERNAL_AI_ENABLED", False)
    return True  # hybrid: 2.x behaviour (the configured provider is external by choice)


def external_fallback_enabled() -> bool:
    """May the local provider fall back to an external model? Only when explicitly switched on, in any mode."""
    return _flag("GENESIS_EXTERNAL_AI_ENABLED", False)


def describe() -> dict:
    return {"mode": mode(), "external_ai_allowed": external_ai_allowed(),
            "memory": (os.getenv("GENESIS_MEMORY_MODE") or "local").strip().lower()}
