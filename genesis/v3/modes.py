"""Conversation modes and natural-conversation rules (3.1).

Modes change *how* Genesis talks, not what it can do. The user picks one ("switch to coding mode"); Genesis never
switches by itself, it only adapts a single reply when you're clearly upset (supportive tone for that turn).

* casual       - natural everyday conversation (default)
* coding       - technical development help, precise, code shown in full
* study        - step-by-step explanations, checks understanding, offers practice
* productivity - concise and action-oriented
* supportive   - empathetic; listens first, no rushing to solutions
* mission      - structured multi-step work: plan, progress, results

Also here: the rules that make replies sound less scripted (no stock openers, no repeating the last few openings, no
"As an AI", length matched to the situation) and a light distress detector.
"""
from __future__ import annotations

import re
from collections import deque
from typing import Optional

MODES = {
    "casual": ("Casual", "Talk like a relaxed, quick-witted friend. Keep it short - usually one or two sentences - and "
                         "match the user's energy. A little humour is welcome when it fits."),
    "coding": ("Coding", "You are in coding mode: be a precise senior developer. Give complete, working code in fenced "
                         "blocks, explain briefly why, point out risks and edge cases. Detail is welcome here."),
    "study": ("Study", "You are in study mode: teach step by step, use a simple example, check understanding with one short "
                       "question, and offer a practice problem when it helps."),
    "productivity": ("Productivity", "You are in productivity mode: be concise and action-oriented. Lead with the answer "
                                     "or next action; skip small talk."),
    "supportive": ("Supportive", "You are in supportive mode: listen first, acknowledge feelings warmly and specifically, "
                                 "ask gentle open questions, and don't rush to fix things unless asked."),
    "mission": ("Mission", "You are in mission mode: work in clear steps, state what you're doing and what you found, and "
                           "report verified results only."),
}
DEFAULT_MODE = "casual"

NATURAL_RULES = (
    "How to sound natural: answer the actual message first; vary your wording; never open with stock phrases like "
    "\"Certainly!\", \"Of course!\", \"Great question\", \"As an AI\" or \"I'm here to help\". Don't repeat a greeting "
    "you already used. Casual messages get short replies; technical questions get detail. Use earlier messages in the "
    "conversation to understand follow-ups, incomplete sentences and words like \"it\", \"that one\" or \"yesterday's\". "
    "If something refers to information you don't have (a file, a project, yesterday's work), say you'll check or ask - "
    "never guess. Ask a clarifying question only when you really can't tell what's meant."
)

DISTRESS = re.compile(
    r"\b(frustrat\w*|stress\w*|sad|upset|depress\w*|anxious|anxiety|lonely|angry|annoyed|tired of|fed up|exhausted|"
    r"overwhelm\w*|hate (my|this)|bad day|worst day|crying|can'?t (take|handle|cope)|burn(ed|t)? out|hopeless)\b", re.I)
SUPPORTIVE_TURN = ("The user sounds upset right now. Respond with empathy first: acknowledge how they feel in your own "
                   "words and gently ask what happened. Don't jump to solutions or change the subject. Keep it short.")

MODE_CMD = re.compile(r"^(?:switch|change|go|turn|set)(?: (?:to|into|on))?(?: the)? (casual|coding|code|study|studying|"
                      r"productivity|productive|work|supportive|support|mission)(?: mode)?$|^(casual|coding|study|"
                      r"productivity|supportive|mission) mode(?: on)?$", re.I)
ALIASES = {"code": "coding", "studying": "study", "productive": "productivity", "work": "productivity",
           "support": "supportive"}


def parse_mode(text: str) -> Optional[str]:
    m = MODE_CMD.match(" ".join((text or "").strip().rstrip(".!").split()))
    if not m:
        return None
    name = (m.group(1) or m.group(2)).lower()
    return ALIASES.get(name, name)


def is_distressed(text: str) -> bool:
    return bool(DISTRESS.search(text or ""))


class Conversation:
    """Per-session conversational state: current mode and the openings of recent replies (to avoid repeating them)."""

    def __init__(self, store=None) -> None:
        self.store = store
        self._openings: deque[str] = deque(maxlen=6)
        self._mode = None

    @property
    def mode(self) -> str:
        if self._mode is None:
            saved = self.store.get("conversation.mode") if self.store is not None else None
            self._mode = saved if saved in MODES else DEFAULT_MODE
        return self._mode

    def set_mode(self, mode: str) -> str:
        mode = ALIASES.get(mode, mode)
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode}")
        self._mode = mode
        if self.store is not None:
            self.store.set("conversation.mode", mode)
        return mode

    def remember_reply(self, text: str) -> None:
        first = re.split(r"(?<=[.!?])\s", (text or "").strip(), maxsplit=1)[0]
        opening = " ".join(first.split()[:5])
        if opening:
            self._openings.append(opening)

    def instructions(self, user_text: str) -> str:
        parts = [MODES[self.mode][1], NATURAL_RULES]
        if self._openings:
            parts.append("Recent replies started with: " + " | ".join(f"\"{o}\"" for o in self._openings) +
                         ". Start this one differently.")
        if is_distressed(user_text) and self.mode not in ("coding", "mission"):
            parts.append(SUPPORTIVE_TURN)
        return "\n\n".join(parts)
