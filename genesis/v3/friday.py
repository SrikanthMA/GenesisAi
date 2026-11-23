""""Friday mode" (3.0.2): Genesis talks with you like a real assistant.

* **Greets you** when it starts: "Good evening, Boss. Genesis is online."  Wake word answer: "Yes, Boss?"
* **Acknowledges** a task the moment you ask: "On it, Boss." / "Okay Boss, writing that now — give me a minute."
* **Reports back** when it's done: "Done, Boss. I opened Notepad." / "Sorry Boss, I couldn't find that file."
* **Speaks typed requests too** (not only voice ones), so it feels like one continuous conversation.
* **Female voice** by default (the most natural female voice installed in Windows), with a built-in Windows voice as
  fallback if Genesis's own speech engine isn't available - so it never goes silent.

Settings in .env (written by Update_Genesis_3.0.2.bat): ``GENESIS_FRIDAY=true`` (this behaviour; off when absent), ``GENESIS_SPEAK_TYPED=true`` (speak replies to typed
messages). Saying "stop talking" / turning voice replies off in Settings still silences it.
"""
from __future__ import annotations

import os
import random
import re
import subprocess
import threading
import queue
from datetime import datetime
from typing import Optional

ACTION_RE = re.compile(
    r"^\s*(?:hey genesis[, ]+|genesis[, ]+|please |can you |could you |would you |i want you to |go ahead and |now )*"
    r"(open|close|launch|start|stop|search|google|look up|find|play|pause|send|email|mail|message|text|whatsapp|call|"
    r"create|make|build|write|draft|generate|prepare|code|copy|paste|download|install|set|remind|schedule|book|order|"
    r"turn|switch|show|check|run|delete|move|rename|save|summari[sz]e|translate|read|add|remove|clear|take|record|"
    r"lock|restart|shut|mute|unmute|minimi[sz]e|maximi[sz]e|go to|navigate|fix|debug|update|upload)\b", re.I)
LONG_TASK_RE = re.compile(r"\b(ieee|paper|report|essay|document|letter|proposal|assignment|excel|spreadsheet|sheet|"
                          r"wikipedia|code|program|script|app|game|colab|vs ?code|notebook|presentation|slides)\b", re.I)
FAIL_RE = re.compile(r"^\s*(sorry|i couldn'?t|i could not|i can'?t|i cannot|i wasn'?t able|unable|that didn'?t work|"
                     r"something went wrong|error|failed)", re.I)
DONE_ALREADY_RE = re.compile(r"^\s*(done|okay|ok|sure|got it|all set|right|alright|on it|here)\b", re.I)

ACKS = ["On it, Boss.", "Okay Boss, on it.", "Right away, Boss.", "Sure thing, Boss.", "Give me a second, Boss."]
LONG_ACKS = ["On it, Boss. Give me a minute for this one.", "Okay Boss, working on it now. This takes a minute.",
             "Right away, Boss. I'll tell you when it's ready."]


def enabled() -> bool:
    return (os.getenv("GENESIS_FRIDAY") or "").strip().lower() in ("1", "true", "yes", "on")  # set by the 3.0.2 update


def speak_typed() -> bool:
    return enabled() and (os.getenv("GENESIS_SPEAK_TYPED") or "true").strip().lower() not in ("0", "false", "no", "off")


def is_action(text: str) -> bool:
    return bool(ACTION_RE.match(text or ""))


_LAST: dict = {}


def pick(pool: list, rnd=random) -> str:
    """A phrase from ``pool``, never the same one twice running (what makes canned lines sound canned)."""
    key = id(pool)
    choices = [p for p in pool if p != _LAST.get(key)] or pool
    choice = rnd.choice(choices)
    _LAST[key] = choice
    return choice


def ack_for(text: str, rnd: random.Random = random) -> Optional[str]:
    """What to say the moment a task starts (JARVIS-style, by kind of task), or None for questions and small talk."""
    if not enabled() or not is_action(text):
        return None
    from .jarvis_style import for_task

    return for_task(text, rnd)


def greeting(now: Optional[datetime] = None) -> str:
    h = (now or datetime.now()).hour
    part = "Good morning" if 5 <= h < 12 else "Good afternoon" if 12 <= h < 17 else "Good evening" if 17 <= h < 23 else "Hello"
    return f"{part}, Boss. Genesis is online."


def shorten(speech: str, limit: int = 320) -> str:
    """Keep spoken replies short: the first sentences, with the rest left on screen."""
    s = " ".join((speech or "").split())
    s = re.sub(r"```.*?```", " I've put the code on screen. ", s)
    s = re.sub(r"https?://\S+", "the link", s)
    s = re.sub(r"[A-Za-z]:\\[^\s,;]+", "the file", s)
    if len(s) <= limit:
        return s.strip()
    parts = re.split(r"(?<=[.!?])\s+", s)
    out = ""
    for p in parts:
        if len(out) + len(p) > limit:
            break
        out += (" " if out else "") + p
    return (out or s[:limit].rsplit(" ", 1)[0] + "…").strip() + " The rest is on screen, Boss."


def report(speech: str, *, acted: bool, status: str = "") -> str:
    """The spoken reply: short, success stated plainly (often with a final "Boss"), failure fronted with "I'm afraid".
    Answers to questions are only shortened - their wording is the model's."""
    s = shorten(speech)
    if not enabled() or not s:
        return s
    if status in ("awaiting_confirmation", "awaiting_input"):
        return s if "boss" in s.lower()[:40] else "Boss, " + s[:1].lower() + s[1:]
    from .jarvis_style import report as jreport

    if status in ("failed", "error") or FAIL_RE.match(s) or acted:
        return jreport(s)
    return s


# ------------------------------------------------------------------ fallback voice (Windows built-in, female)
PS_SPEAK = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "try { $s.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::Female) } catch {}; $s.Rate = 1; "
            "$t = [Console]::In.ReadToEnd(); if ($t.Trim()) { $s.Speak($t) }")


class FallbackSpeaker:
    """Speaks with Windows's built-in voice through PowerShell (text goes in on stdin, never on the command line)."""

    name = "windows-fallback"

    def __init__(self, runner=None) -> None:
        self._q: "queue.Queue[Optional[str]]" = queue.Queue()
        self._proc: Optional[subprocess.Popen] = None
        self._busy = False
        self._run = runner or self._powershell
        self._t = threading.Thread(target=self._loop, daemon=True, name="genesis-fallback-tts")
        self._t.start()

    def _powershell(self, text: str) -> None:
        kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
        self._proc = subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_SPEAK],
                                      stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
        try:
            self._proc.communicate(text.encode("utf-8"), timeout=120)
        except subprocess.TimeoutExpired:
            self._proc.kill()

    def _loop(self) -> None:
        while True:
            text = self._q.get()
            if text is None:
                return
            self._busy = True
            try:
                self._run(text)
            except Exception:
                pass
            finally:
                self._busy = False

    # TTSEngine-compatible surface
    @property
    def speaking(self) -> bool:
        return self._busy or not self._q.empty()

    def speak(self, text: str, lang: Optional[str] = None) -> None:
        if text and text.strip():
            self._q.put(text.strip())

    def can_speak(self, lang: Optional[str]) -> bool:
        return not lang or lang.startswith("en")

    def stop(self) -> None:
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.kill()
            except Exception:
                pass

    def set_rate(self, rate: int) -> None:
        pass

    def set_voice(self, name_fragment: str) -> None:
        pass

    def voices(self) -> list[str]:
        return ["Windows (female)"]

    def close(self) -> None:
        self.stop()
        self._q.put(None)


_FALLBACK: Optional[FallbackSpeaker] = None


def fallback_speaker() -> FallbackSpeaker:
    global _FALLBACK
    if _FALLBACK is None:
        _FALLBACK = FallbackSpeaker()
    return _FALLBACK
