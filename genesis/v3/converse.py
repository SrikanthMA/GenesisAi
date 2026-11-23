"""Communication controller (3.2): faster, more natural voice turns.

Ideas adapted (independently re-implemented in Python) from the MIT-licensed JARVIS project (adewaskar/jarvis):

1. **Speak while thinking (streaming).** For a spoken conversation reply, the local model's answer is streamed and each
   complete sentence goes to the voice as soon as it exists, so Genesis starts talking after the first sentence instead
   of after the whole answer. The voice manager then doesn't speak the same text again; it only keeps listening for an
   interruption while the streamed audio plays.
2. **Echo rejection in text.** Genesis's microphone stays open while it speaks (that is what makes barge-in possible),
   so it can hear itself. Besides Genesis's existing loudness calibration, a transcript that mostly repeats what Genesis
   just said (within a short tail after speaking) is dropped. Words like "stop", "wait", "no", "genesis" always get
   through, and short phrases must match completely before they are discarded.
3. **Abbreviation-safe sentence cuts**: "Mr. Stark", "e.g." or "3.5" don't end a sentence.

Genesis already had: wake word, a follow-up window without the wake word, endpointing/VAD, barge-in by voice or wake
word, ducking via TTS stop - those are kept as they were.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Optional

ECHO_TAIL_S = 2.5
OVERRIDE = re.compile(r"\b(stop|wait|cancel|no|hold on|genesis|quiet|shut up|pause|enough)\b", re.I)
STOP_WORDS = set("a an the and or but to of in on for with at by from is are was were be am i me my you your it this "
                 "that so do does did can could would will just okay ok yes yeah boss".split())
ABBREV = re.compile(r"(?:\b(?:mr|mrs|ms|dr|prof|sr|jr|st|vs|etc|e\.g|i\.e|no|fig)\.|\d\.)$", re.I)


def _norm(t: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", (t or "").lower())


def is_echo(heard: str, spoken: str) -> bool:
    if not spoken or not heard:
        return False
    if OVERRIDE.search(heard):
        return False
    words = _norm(heard)
    if not words:
        return True
    mine = set(_norm(spoken))
    content = [w for w in words if w not in STOP_WORDS]
    if len(content) < 2:
        return len(words) >= 2 and all(w in mine for w in words)
    return sum(w in mine for w in content) / len(content) >= 0.6


class SpokenLog:
    """What Genesis said recently, with an estimate of when the audio ended (~14 characters per second)."""

    def __init__(self) -> None:
        self._items: list[tuple[float, str]] = []
        self._lock = threading.Lock()

    def add(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            start = max([t for t, _ in self._items[-1:]] or [time.monotonic()])
            start = max(start, time.monotonic())
            self._items.append((start + len(text) / 14.0, text))
            self._items = self._items[-12:]

    def recent(self, now: Optional[float] = None) -> str:
        now = now or time.monotonic()
        with self._lock:
            return " ".join(t for end, t in self._items if end + ECHO_TAIL_S >= now)


def split_complete(buf: str) -> tuple[list[str], str]:
    """Complete sentences in ``buf`` and the unfinished remainder."""
    out, start = [], 0
    for m in re.finditer(r"[.!?…]+[\"')\]]*(?=\s)|\n{2,}", buf):
        piece = buf[start:m.end()].strip()
        if m.group(0).startswith(".") and ABBREV.search(buf[start:m.end()]):
            continue
        if len(piece) >= 2:
            out.append(piece)
            start = m.end()
    return out, buf[start:]


def speakable(sentence: str) -> str:
    s = re.sub(r"`{1,3}[^`]*`{1,3}", " ", sentence)
    s = re.sub(r"[*_#>|]+", " ", s)
    s = re.sub(r"https?://\S+", "the link", s)
    return " ".join(s.split())


class StreamTurn:
    """One spoken conversation turn whose answer is being streamed into the voice."""

    def __init__(self, speak) -> None:
        self._speak = speak
        self.buffer = ""
        self.spoken: list[str] = []
        self.in_code = False

    def feed(self, piece: str) -> None:
        self.buffer += piece
        if "```" in piece or self.buffer.count("```") % 2 == 1:
            self.in_code = self.buffer.count("```") % 2 == 1
        if self.in_code:
            return  # code isn't read aloud; it's on screen
        done, self.buffer = split_complete(self.buffer)
        for s in done:
            self._say(s)

    def finish(self) -> None:
        rest = self.buffer.strip()
        self.buffer = ""
        if rest and not self.in_code:
            self._say(rest)

    def _say(self, s: str) -> None:
        text = speakable(s)
        if text:
            self.spoken.append(text)
            self._speak(text)


_LOCAL = threading.local()


def begin(speak) -> StreamTurn:
    t = StreamTurn(speak)
    _LOCAL.turn = t
    return t


def current() -> Optional[StreamTurn]:
    return getattr(_LOCAL, "turn", None)


def end() -> Optional[StreamTurn]:
    t = current()
    _LOCAL.turn = None
    return t
