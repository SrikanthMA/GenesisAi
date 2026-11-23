"""Microphone and speaker mute (3.5).

* **Mic mute** really stops capture: the audio stream is closed, wake-word detection is switched off, push-to-talk is
  ignored, and ``Microphone.read`` returns nothing even if a frame were still queued. Nothing is recorded or sent to a
  transcription service while muted. The state is saved, so it stays muted after a restart until the user turns it on.
* **Speaker mute** silences Genesis (current speech stops, new speech is dropped) without touching the microphone.
* The two are independent. ``snapshot()`` reports the *actual* state (is the stream really closed?) so the UI never
  shows a state that isn't true.
* Ctrl+Shift+M (changeable with ``GENESIS_MUTE_HOTKEY``) toggles the microphone from anywhere on Windows.
"""
from __future__ import annotations

import functools
import os
import threading
import time
from typing import Callable, Optional

from ..core.logging_setup import get_logger

log = get_logger("v3.privacy")

MIC_KEY = "privacy.mic_muted"
SPK_KEY = "privacy.speaker_muted"
DEFAULT_HOTKEY = "ctrl+shift+m"


class Gate:
    """One shared object that holds the two mute states and applies them to the voice system."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.mic_muted = False
        self.speaker_muted = False
        self.vm = None
        self.settings = None
        self.listeners: list[Callable[[dict], None]] = []
        self.changes: list[dict] = []

    # ------------------------------------------------------------------ wiring
    def preload(self, vm) -> None:
        with self._lock:
            self.vm = vm
            self.settings = getattr(vm, "settings", None)
            if self.settings is not None:
                try:
                    self.mic_muted = bool(self.settings.get(MIC_KEY, False))
                    self.speaker_muted = bool(self.settings.get(SPK_KEY, False))
                except Exception:
                    pass
            guard_mic(self, getattr(vm, "mic", None))  # closed to the world before the voice thread starts
            guard_tts(self, getattr(vm, "tts", None))

    def attach(self, vm) -> None:
        """Connect to the VoiceManager; restore the saved state (a muted mic stays muted after a restart)."""
        with self._lock:
            self.vm = vm
            self.settings = getattr(vm, "settings", None)
            if self.settings is not None:
                try:
                    self.mic_muted = bool(self.settings.get(MIC_KEY, False))
                    self.speaker_muted = bool(self.settings.get(SPK_KEY, False))
                except Exception:
                    pass
            guard_mic(self, getattr(vm, "mic", None))
            guard_tts(self, getattr(vm, "tts", None))
        if self.mic_muted:
            self._apply_mic()
        if self.speaker_muted:
            self._apply_speaker()

    # ------------------------------------------------------------------ state changes
    def set_mic(self, muted: bool, source: str = "ui") -> dict:
        with self._lock:
            self.mic_muted = bool(muted)
            self._save(MIC_KEY, self.mic_muted)
            self._apply_mic()
            self._note("mic", self.mic_muted, source)
        return self.snapshot()

    def set_speaker(self, muted: bool, source: str = "ui") -> dict:
        with self._lock:
            self.speaker_muted = bool(muted)
            self._save(SPK_KEY, self.speaker_muted)
            self._apply_speaker()
            self._note("speaker", self.speaker_muted, source)
        return self.snapshot()

    def toggle_mic(self, source: str = "hotkey") -> dict:
        return self.set_mic(not self.mic_muted, source)

    def toggle_speaker(self, source: str = "ui") -> dict:
        return self.set_speaker(not self.speaker_muted, source)

    # ------------------------------------------------------------------ reporting
    def snapshot(self) -> dict:
        vm = self.vm
        mic = getattr(vm, "mic", None)
        try:
            capturing = bool(mic.is_open) if mic is not None else False
        except Exception:
            capturing = False
        tts = getattr(vm, "tts", None)
        try:
            speaking = bool(getattr(tts, "speaking", False))
        except Exception:
            speaking = False
        return {"mic_muted": self.mic_muted, "speaker_muted": self.speaker_muted,
                "mic_capturing": capturing, "speaking": speaking,
                # the indicator is "muted" only when it is true in reality: flagged AND the stream really is closed
                "mic_really_off": self.mic_muted and not capturing}

    # ------------------------------------------------------------------ internals
    def _save(self, key: str, value: bool) -> None:
        if self.settings is not None:
            try:
                self.settings.set(key, bool(value))
            except Exception:
                log.exception("could not save %s", key)

    def _apply_mic(self) -> None:
        vm = self.vm
        if vm is None:
            return
        if self.mic_muted:
            try:
                mic = getattr(vm, "mic", None)
                if mic is not None:
                    mic.close()  # stops capture and flushes whatever was buffered
                w = getattr(vm, "_wake", None)
                if w is not None and hasattr(w, "reset"):
                    w.reset()  # forget any half-heard wake word
                for ev in ("_ptt",):
                    e = getattr(vm, ev, None)
                    if e is not None:
                        e.clear()
            except Exception:
                log.exception("mic mute: close failed")
        try:
            vm.refresh()  # the voice loop re-reads listening_enabled right away
        except Exception:
            pass

    def _apply_speaker(self) -> None:
        if self.speaker_muted:
            try:
                tts = getattr(self.vm, "tts", None)
                if tts is not None:
                    tts.stop()
            except Exception:
                pass

    def _note(self, what: str, muted: bool, source: str) -> None:
        entry = {"ts": time.time(), "what": what, "muted": muted, "source": source}
        self.changes.append(entry)
        del self.changes[:-50]
        log.info("privacy: %s %s (%s)", what, "muted" if muted else "on", source)
        snap = self.snapshot()
        for fn in list(self.listeners):
            try:
                fn(snap)
            except Exception:
                pass


GATE = Gate()


def guard_mic(gate: Gate, mic) -> None:
    """Make ``Microphone.read``/``open`` refuse to work while muted (belt and braces on top of closing the stream)."""
    if mic is None or getattr(mic, "_v3_guarded", False):
        return
    orig_read, orig_open = getattr(mic, "read", None), getattr(mic, "open", None)
    if not (callable(orig_read) and callable(orig_open)):
        return

    @functools.wraps(orig_read)
    def read(*a, **k):
        if gate.mic_muted:
            try:
                mic.flush()
            except Exception:
                pass
            time.sleep(min(float(k.get("timeout", a[0] if a else 0.3) or 0.3), 0.3))
            return None
        return orig_read(*a, **k)

    @functools.wraps(orig_open)
    def open_(*a, **k):
        if gate.mic_muted:
            return None  # never open the stream while muted
        return orig_open(*a, **k)

    try:
        mic.read, mic.open = read, open_
        mic._v3_guarded = True
    except Exception:  # a read-only object: the VoiceManager-level guards still hold
        pass


def guard_tts(gate: Gate, tts) -> None:
    if tts is None or getattr(tts, "_v3_guarded", False):
        return
    orig = getattr(tts, "speak", None)
    if not callable(orig):
        return

    @functools.wraps(orig)
    def speak(*a, **k):
        if gate.speaker_muted:
            return None
        return orig(*a, **k)

    try:
        tts.speak = speak
        tts._v3_guarded = True
    except Exception:
        pass


def patch_voice_manager(cls) -> None:
    """Wire the gate into VoiceManager (called from the v3 hook)."""
    if cls.__dict__.get("_v3_privacy"):
        return
    cls._v3_privacy = True
    orig_prop = next((c.__dict__["listening_enabled"] for c in cls.__mro__ if "listening_enabled" in c.__dict__), None)
    orig_ptt, orig_speak, orig_start = (getattr(cls, n, None) for n in ("push_to_talk", "speak", "start"))
    orig_tick, orig_init, orig_status = (getattr(cls, n, None) for n in ("_tick", "__init__", "status"))

    if isinstance(orig_prop, property):
        cls.listening_enabled = property(lambda self: (not GATE.mic_muted) and bool(orig_prop.fget(self)))

    if callable(orig_init):
        @functools.wraps(orig_init)
        def __init__(self, *a, **k):
            orig_init(self, *a, **k)
            GATE.preload(self)  # saved mute state applies before anything can be heard or said

        cls.__init__ = __init__

    if callable(orig_ptt):
        @functools.wraps(orig_ptt)
        def push_to_talk(self, *a, **k):
            if GATE.mic_muted:
                return None  # a muted microphone can't be woken by a button either
            return orig_ptt(self, *a, **k)

        cls.push_to_talk = push_to_talk

    if callable(orig_speak):
        @functools.wraps(orig_speak)
        def speak(self, text, *a, **k):
            if GATE.speaker_muted:
                return None
            return orig_speak(self, text, *a, **k)

        cls.speak = speak

    if callable(orig_start):
        @functools.wraps(orig_start)
        def start(self, *a, **k):
            res = orig_start(self, *a, **k)  # creates the real TTS engine
            GATE.attach(self)
            return res

        cls.start = start

    if callable(orig_tick):
        @functools.wraps(orig_tick)
        def _tick(self):
            if GATE.mic_muted and getattr(self.mic, "is_open", False):
                self.mic.close()
            return orig_tick(self)

        cls._tick = _tick

    if callable(orig_status):
        @functools.wraps(orig_status)
        def status(self):
            d = orig_status(self)
            d.update(mic_muted=GATE.mic_muted, speaker_muted=GATE.speaker_muted)
            return d

        cls.status = status


def start_hotkey(spec: Optional[str] = None):
    """Register Ctrl+Shift+M (Windows). Returns the hotkey object, or None where unsupported."""
    try:
        from ..ui.hotkey import GlobalHotkey

        hk = GlobalHotkey(spec or os.getenv("GENESIS_MUTE_HOTKEY") or DEFAULT_HOTKEY, lambda: GATE.toggle_mic("hotkey"))
        return hk if hk.start() else None
    except Exception:
        log.exception("mute hotkey failed")
        return None
