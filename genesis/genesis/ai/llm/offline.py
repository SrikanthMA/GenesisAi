"""Offline, rule-based conversational fallback.

Used when no LLM is configured or the network is down. It is a small
pattern/reflection responder in the tradition of ELIZA (Weizenbaum, 1966) —
honest about being in offline mode, and never pretends to know facts.
"""
from __future__ import annotations

import random
import re
from typing import Optional

from .base import LLMProvider, LLMUnavailable, Message

_REFLECTIONS = {
    "i": "you", "me": "you", "my": "your", "am": "are", "i'm": "you're", "i've": "you've",
    "you": "I", "your": "my", "yours": "mine", "are": "am", "myself": "yourself",
}

_RULES: list[tuple[str, list[str]]] = [
    (r"^(hi|hello|hey|good (morning|afternoon|evening))\b.*", [
        "Hello! I'm Genesis. I can open apps, manage files, draft e-mails, set reminders and more. What can I do for you?",
    ]),
    (r".*\b(who|what) are you\b.*", [
        "I'm Genesis, a desktop assistant running on your computer. My cloud language model is offline right now, "
        "but local actions like opening apps, folders, files and reminders still work.",
    ]),
    (r".*\bhow are you\b.*", ["I'm running smoothly, thanks for asking. What would you like to do?"]),
    (r".*\bthank(s| you)\b.*", ["You're welcome!", "Happy to help."]),
    (r".*\b(what can you do|help)\b.*", [
        "I can open applications and folders, search the web, find and organise files, draft Gmail e-mails, "
        "set reminders, open your projects in VS Code, and control power settings (with confirmation).",
    ]),
    (r"i feel (.*)", ["Why do you feel {0}?", "How long have you felt {0}?"]),
    (r"i need (.*)", ["Why do you need {0}?", "Would it really help you to get {0}?"]),
    (r"i want (.*)", ["What would it mean to you if you got {0}?"]),
    (r"because (.*)", ["Is that the real reason?"]),
]


def _reflect(fragment: str) -> str:
    return " ".join(_REFLECTIONS.get(w, w) for w in fragment.lower().split())


class OfflineProvider(LLMProvider):
    name = "offline"
    is_remote = False
    is_generative = False

    def __init__(self, reason: Optional[str] = None, seed: Optional[int] = None) -> None:
        self.reason = reason
        self._rng = random.Random(seed)

    def chat(self, messages: list[Message], *, temperature: Optional[float] = None, max_tokens: int = 700,
             json_mode: bool = False) -> str:
        if json_mode:
            raise LLMUnavailable("Structured generation needs a language model; running offline.")
        user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        text = user.strip().rstrip("?!.").strip()
        for pattern, responses in _RULES:
            m = re.match(pattern, text, re.I)
            if m:
                reply = self._rng.choice(responses)
                groups = [_reflect(g) for g in m.groups() if g] if m.groups() else []
                try:
                    return reply.format(*groups)
                except (IndexError, KeyError):
                    return reply
        note = f" ({self.reason})" if self.reason else ""
        return (
            "My language model isn't available right now" + note + ", so I can't answer open-ended questions. "
            "I can still do local tasks — try 'open Downloads', 'open VS Code' or 'remind me in 10 minutes to stretch'."
        )
