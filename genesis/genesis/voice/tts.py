"""Text-to-speech with interruption (barge-in) support.

* ``SapiTTS`` (Windows default) – Microsoft SAPI 5 through COM. Speech is
  asynchronous and can be purged instantly, which is what makes “interrupt
  Genesis while it's talking” work.
* ``Pyttsx3TTS`` – cross-platform fallback.
* ``NullTTS`` – text only.
All engines speak on their own worker thread so the UI never blocks.
"""
from __future__ import annotations

import queue
import sys
import threading
import time
from abc import ABC, abstractmethod
from typing import Callable, Optional

from ..core.logging_setup import get_logger

log = get_logger("voice.tts")


class TTSEngine(ABC):
    name = "base"

    def __init__(self) -> None:
        self.on_start: Optional[Callable[[], None]] = None
        self.on_done: Optional[Callable[[], None]] = None

    @abstractmethod
    def speak(self, text: str) -> None:
        ...

    @abstractmethod
    def stop(self) -> None:
        ...

    @property
    @abstractmethod
    def speaking(self) -> bool:
        ...

    def set_rate(self, rate: int) -> None:
        pass

    def set_voice(self, name_fragment: str) -> None:
        pass

    def voices(self) -> list[str]:
        return []

    def close(self) -> None:
        self.stop()


class NullTTS(TTSEngine):
    name = "none"

    def speak(self, text: str) -> None:
        pass

    def stop(self) -> None:
        pass

    @property
    def speaking(self) -> bool:
        return False


class _ThreadedTTS(TTSEngine):
    """Runs a backend on one dedicated thread (COM/pyttsx3 objects are thread-affine)."""

    def __init__(self) -> None:
        super().__init__()
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._speaking = threading.Event()
        self._interrupt = threading.Event()
        self._ready = threading.Event()
        self._error: Optional[BaseException] = None
        self._thread = threading.Thread(target=self._run, name=f"genesis-tts-{self.name}", daemon=True)
        self._thread.start()
        self._ready.wait(10)
        if self._error:
            raise self._error

    @property
    def speaking(self) -> bool:
        return self._speaking.is_set()

    def speak(self, text: str) -> None:
        if text and text.strip():
            self._interrupt.clear()
            self._q.put(("speak", text))

    def stop(self) -> None:
        self._interrupt.set()
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        self._q.put(("stop", None))

    def set_rate(self, rate: int) -> None:
        self._q.put(("rate", rate))

    def set_voice(self, name_fragment: str) -> None:
        self._q.put(("voice", name_fragment))

    def close(self) -> None:
        self.stop()
        self._q.put(("quit", None))

    def _run(self) -> None:
        try:
            self._init_backend()
        except BaseException as exc:  # surfaced to the constructor
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        while True:
            cmd, arg = self._q.get()
            try:
                if cmd == "quit":
                    break
                if cmd == "speak":
                    self._speaking.set()
                    if self.on_start:
                        self.on_start()
                    self._do_speak(arg)
                    self._speaking.clear()
                    if self.on_done:
                        self.on_done()
                elif cmd == "stop":
                    self._do_stop()
                    self._speaking.clear()
                elif cmd == "rate":
                    self._do_rate(arg)
                elif cmd == "voice":
                    self._do_voice(arg)
            except Exception:
                log.exception("TTS error")
                self._speaking.clear()

    def _init_backend(self) -> None: ...
    def _do_speak(self, text: str) -> None: ...
    def _do_stop(self) -> None: ...
    def _do_rate(self, rate: int) -> None: ...
    def _do_voice(self, frag: str) -> None: ...


class SapiTTS(_ThreadedTTS):
    name = "sapi"
    SVSF_ASYNC = 1
    SVSF_PURGE = 2

    def _init_backend(self) -> None:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore

        pythoncom.CoInitialize()
        self._voice = win32com.client.Dispatch("SAPI.SpVoice")
        try:  # cache names here: the COM object must only be used from this thread
            self._voice_names = [v.GetDescription() for v in self._voice.GetVoices()]
        except Exception:
            self._voice_names = []

    def _do_speak(self, text: str) -> None:
        self._voice.Speak(text, self.SVSF_ASYNC)
        while not self._interrupt.is_set():
            if self._voice.WaitUntilDone(50):  # returns True when finished
                return
        self._voice.Speak("", self.SVSF_ASYNC | self.SVSF_PURGE)

    def _do_stop(self) -> None:
        self._voice.Speak("", self.SVSF_ASYNC | self.SVSF_PURGE)

    def _do_rate(self, rate: int) -> None:
        self._voice.Rate = max(-10, min(10, int(rate)))

    def _do_voice(self, frag: str) -> None:
        if not frag:
            return
        for v in self._voice.GetVoices():
            if frag.lower() in v.GetDescription().lower():
                self._voice.Voice = v
                return

    def voices(self) -> list[str]:
        return list(getattr(self, "_voice_names", []))


class Pyttsx3TTS(_ThreadedTTS):
    name = "pyttsx3"

    def _init_backend(self) -> None:
        import pyttsx3  # type: ignore

        self._engine = pyttsx3.init()

    def _do_speak(self, text: str) -> None:
        # speak sentence by sentence so interruption takes effect quickly
        import re

        for sentence in re.split(r"(?<=[.!?])\s+", text):
            if self._interrupt.is_set():
                break
            self._engine.say(sentence)
            self._engine.runAndWait()

    def _do_stop(self) -> None:
        try:
            self._engine.stop()
        except Exception:
            pass

    def _do_rate(self, rate: int) -> None:
        self._engine.setProperty("rate", 185 + int(rate) * 12)

    def _do_voice(self, frag: str) -> None:
        for v in self._engine.getProperty("voices"):
            if frag and frag.lower() in (v.name or "").lower():
                self._engine.setProperty("voice", v.id)
                return


def create_tts(engine: str = "auto") -> TTSEngine:
    if engine == "none":
        return NullTTS()
    candidates = ["sapi", "pyttsx3"] if sys.platform.startswith("win") else ["pyttsx3"]
    if engine in ("sapi", "pyttsx3"):
        candidates = [engine] + [c for c in candidates if c != engine]
    for c in candidates:
        try:
            return SapiTTS() if c == "sapi" else Pyttsx3TTS()
        except BaseException as exc:
            log.warning("TTS engine %s unavailable: %s", c, exc)
    log.warning("no TTS engine available; replies will be text only")
    return NullTTS()


def wait_until_quiet(tts: TTSEngine, timeout: float = 30.0) -> None:
    end = time.time() + timeout
    while tts.speaking and time.time() < end:
        time.sleep(0.05)
