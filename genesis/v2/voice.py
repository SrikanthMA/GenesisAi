"""Honest voice fallbacks."""
from __future__ import annotations

from ..core.events import EventType
from ..core.logging_setup import get_logger

log = get_logger("v2.voice")


def no_voice_notice(manager, lang: str) -> str:
    """The first time a reply can't be spoken in the user's language, tell the user why (a notification, once per
    language per run) instead of silently switching to English."""
    from ..nlp.language import language_name

    warned = manager.__dict__.setdefault("_v2_voice_warned", set())
    if lang in warned:
        return ""
    warned.add(lang)
    msg = (f"No {language_name(lang)} voice is installed, so I'm reading replies in English. Add one in Windows Settings → "
           "Time & language → Speech → Add voices.")
    log.info("no voice for %s — speaking English", lang)
    manager.events.emit(EventType.NOTIFICATION, title="Voice not available", message=msg)
    return msg
