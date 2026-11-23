"""The digital twin (3.0.1): Genesis knows *you* and can answer *as* you.

* **Profile** - your saved memories and preferences ("my favourite pizza is a Farmhouse from Domino's") are given to
  Suzanne with every conversation, most relevant first, so "which pizza should I order?" gets *your* answer.
* **Twin mode** - "answer as me: …", "reply like me to …", "what would I say to …": Suzanne answers in the first person,
  as you, using your facts and the way you write (short samples of your own past messages).
* **Learning** - "my favourite pizza is …", "my usual coffee is …", "I always order …" are saved as preferences
  (deterministically, no model needed).

It never invents facts about you: anything not in your saved memories is answered with "I'd have to check" (twin mode)
or by asking you (normal mode).
"""
from __future__ import annotations

import re
from typing import Callable, Optional

STOP = set("a an the and or but to of in on for with at by from is are was were be am i me my you your it this that "
           "what which who how when where do does did can could should would will want like get have has".split())

TWIN_PROMPT = (
    "Right now you are acting as the user's DIGITAL TWIN: answer exactly as the user would, in the first person "
    "(\"I\"), in their own voice and style - same tone, length, slang and language mix as the samples of their "
    "writing below. Use only the facts about them listed below; for anything about them that isn't listed, say "
    "something like \"not sure, I'd have to check\" instead of making it up. Don't mention that you are an AI or a "
    "twin; just give the answer they would give."
)

PROFILE_HEADER = ("What you know about the user (from their saved memories - use these naturally when relevant; never "
                  "claim to know anything about them that isn't here):")

_PREF = re.compile(
    r"^(?:remember(?: that)?\s+)?my (favou?rite|usual|go-?to|preferred|regular) ([\w' -]{2,40}?) (?:is|are|=) (.{2,200})$",
    re.I)
_ALWAYS = re.compile(r"^(?:remember(?: that)?\s+)?i (always|usually|normally) (order|get|have|eat|drink|buy|use|take) "
                     r"(.{2,200})$", re.I)
_LIKE = re.compile(r"^remember(?: that)?\s+i (love|like|prefer|hate|dislike|can't stand|am allergic to) (.{2,200})$",
                   re.I)


def preference_from(text: str) -> Optional[str]:
    """A statement about the user's preferences, normalised for saving, or None."""
    t = " ".join((text or "").strip().rstrip(".!").split())
    m = _PREF.match(t)
    if m:
        return f"User's {m.group(1).lower()} {m.group(2).strip().lower()}: {m.group(3).strip()}"
    m = _ALWAYS.match(t)
    if m:
        return f"User {m.group(1).lower()} {m.group(2).lower()}s {m.group(3).strip()}"
    m = _LIKE.match(t)
    if m:
        verb = {"love": "loves", "like": "likes", "prefer": "prefers", "hate": "hates", "dislike": "dislikes",
                "can't stand": "can't stand", "am allergic to": "is allergic to"}[m.group(1).lower()]
        return f"User {verb} {m.group(2).strip()}"
    return None


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", (text or "").lower()) if w not in STOP and len(w) > 2}


def relevant(facts: list[str], query: str, limit: int = 20) -> list[str]:
    """Facts sharing words with the query first (pizza -> pizza facts), then the most recent ones."""
    q = _words(query)
    scored = sorted(enumerate(facts), key=lambda p: (-len(q & _words(p[1])), p[0]))
    return [f for _, f in scored[:limit]]


def facts_about(item: str, facts: list[str]) -> list[str]:
    words = _words(item)
    stems = {w.rstrip("s") for w in words}
    return [f for f in facts if words & _words(f) or stems & {w.rstrip("s") for w in _words(f)}]


TWIN_RE = re.compile(r"^\s*(?:(?:please\s+)?(?:answer|reply|respond|write|talk|text)(?: (?:this|that|to (?:this|that|it)))?"
                     r" (?:as|like) me\b[:,]?|as me[:,]|what would i (?:say|reply|answer|write)\b[:,]?|"
                     r"(?:be|act as) my (?:digital )?twin\b[:,]?)\s*", re.I)


def is_twin_request(text: str) -> bool:
    return bool(TWIN_RE.match(text or ""))


class Profile:
    """Reads the user's saved memories and writing samples from Genesis (set up by the container hook)."""

    def __init__(self, facts: Callable[[], list[str]], samples: Callable[[], list[str]] = lambda: []) -> None:
        self._facts = facts
        self._samples = samples

    def facts(self) -> list[str]:
        try:
            return [f for f in (self._facts() or []) if f]
        except Exception:
            return []

    def samples(self) -> list[str]:
        try:
            return [s for s in (self._samples() or []) if s]
        except Exception:
            return []

    def context(self, query: str, limit: int = 20) -> str:
        fs = relevant(self.facts(), query, limit)
        return (PROFILE_HEADER + "\n" + "\n".join(f"- {f}" for f in fs)) if fs else ""

    def twin_prompt(self, query: str) -> str:
        parts = [TWIN_PROMPT]
        ctx = self.context(query, 30)
        parts.append(ctx or "You know no saved facts about the user yet.")
        sm = self.samples()
        if sm:
            parts.append("Samples of how the user writes (match this style):\n" + "\n".join(f"> {s}" for s in sm))
        return "\n\n".join(parts)


def from_services(svc) -> Optional[Profile]:
    """Build a Profile from Genesis's own memory (facts + preferences) and recent conversations."""
    repos = getattr(svc, "repos", None)
    if repos is None:
        return None

    def facts() -> list[str]:
        out: list[str] = []
        mem = getattr(repos, "memory", None)
        if mem is not None:
            out += [str(m.get("content") or "") for m in mem.list()[:200]]
        prefs = getattr(repos, "preferences", None)
        if prefs is not None:
            try:
                for k, v in prefs.all().items():
                    if not str(k).startswith(("ui.", "voice.", "system.", "security.", "_")) and isinstance(v, (str, int, float)):
                        out.append(f"Preference {k}: {v}")
            except Exception:
                pass
        manager = getattr(svc, "memory", None)
        if manager is not None and getattr(manager, "enabled", True) is False:
            return []  # the user switched memory off
        return out

    def samples() -> list[str]:
        conv = getattr(repos, "conversations", None)
        if conv is None:
            return []
        out: list[str] = []
        try:
            for c in conv.list(8):
                for m in conv.messages(c["id"], limit=40):
                    txt = " ".join(str(m.get("content") or "").split())
                    if (m.get("role") == "user" and 12 <= len(txt) <= 220 and not is_twin_request(txt)
                            and not txt.lower().startswith(("open ", "close ", "check ", "show "))):
                        out.append(txt)
        except Exception:
            return out[:12]
        return out[-12:]

    return Profile(facts, samples)
