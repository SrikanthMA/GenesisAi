"""Voice pipeline state machine.

    ┌────────────┐ wake word ┌───────────┐ end of speech ┌──────────────┐
    │ WAKE-LISTEN│──────────►│ RECORDING │──────────────►│ TRANSCRIBING │
    └────────────┘           └───────────┘               └──────┬───────┘
          ▲        (barge-in: wake word while speaking            │ text
          │         stops TTS and starts recording)               ▼
    ┌─────┴──────┐ reply needs an answer (confirmation) ┌──────────────────┐
    │  SPEAKING  │◄─────────────────────────────────────│ core.handle(text)│
    └────────────┘                                      └──────────────────┘

The microphone is physically closed whenever listening is disabled (privacy),
and the UI shows a live indicator while it is open.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from ..core.events import AssistantState, EventBus, EventType
from ..core.logging_setup import get_logger
from ..nlp.rules import normalize
from .audio import FRAME_MS, Microphone, MicrophoneError
from .stt import STTEngine, STTError, create_stt
from .tts import NullTTS, TTSEngine, create_tts, wait_until_quiet
from .vad import EndpointConfig, Endpointer, frame_rms
from .wakeword import WakeWordDetector, WakeWordError, create_wake_detector

log = get_logger("voice")

# handler(text) -> (text_to_speak, expects_reply)
Handler = Callable[[str], tuple[str, bool]]


class VoiceManager:
    def __init__(self, config, settings, events: EventBus, handler: Handler, *, mic: Optional[Microphone] = None,
                 wake: Optional[WakeWordDetector] = None, stt: Optional[STTEngine] = None, tts: Optional[TTSEngine] = None) -> None:
        self.config = config
        self.settings = settings
        self.events = events
        self.handler = handler
        self.mic = mic or Microphone(config.audio_input_device)
        self._wake = wake
        self._stt = stt
        self.tts: TTSEngine = tts or NullTTS()
        self._tts_given = tts is not None
        self._running = threading.Event()
        self._wakeup = threading.Event()
        self._ptt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.wake_error: Optional[str] = None
        self.mic_error: Optional[str] = None

    # ------------------------------------------------------------- control
    @property
    def listening_enabled(self) -> bool:
        return bool(self.settings.get("voice.enabled", True) and self.settings.get("voice.wake_word_enabled", True))

    def start(self) -> None:
        if not self._tts_given:
            self.tts = create_tts(self.config.tts_engine)
            self.tts.set_rate(int(self.settings.get("voice.tts_rate", 0)))
            self.tts.set_voice(self.settings.get("voice.tts_voice", ""))
        self.tts.on_start = lambda: self.events.emit(EventType.STATE_CHANGED, state=AssistantState.SPEAKING.value)
        self.tts.on_done = lambda: self.events.emit(EventType.STATE_CHANGED, state=AssistantState.IDLE.value)
        self._running.set()
        self._thread = threading.Thread(target=self._loop, name="genesis-voice", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()
        self._wakeup.set()
        self.tts.close()
        self.mic.close()
        if self._wake:
            self._wake.close()

    def refresh(self) -> None:
        """Settings changed (listening toggled, etc.)."""
        self._wakeup.set()

    def push_to_talk(self) -> None:
        self.tts.stop()
        self._ptt.set()
        self._wakeup.set()

    def interrupt(self) -> None:
        self.tts.stop()

    def speak(self, text: str) -> None:
        if text and self.settings.get("voice.tts_enabled", True):
            self.tts.speak(text)

    def status(self) -> dict:
        return {"mic_open": self.mic.is_open, "listening": self.listening_enabled, "wake_engine": getattr(self._wake, "name", None),
                "wake_error": self.wake_error, "mic_error": self.mic_error, "stt": getattr(self._stt, "name", None),
                "tts": self.tts.name}

    def _emit_status(self) -> None:
        self.events.emit(EventType.VOICE_STATUS, **self.status())

    # ---------------------------------------------------------------- loop
    def _ensure_wake(self) -> Optional[WakeWordDetector]:
        if self._wake is None and self.wake_error is None:
            try:
                self._wake = create_wake_detector(self.config, float(self.settings.get("voice.wake_sensitivity", 0.6)))
            except (WakeWordError, Exception) as exc:
                self.wake_error = str(exc)
                log.warning("wake word unavailable: %s", exc)
                self.events.emit(EventType.NOTIFICATION, title="Wake word unavailable",
                                 message=f"{exc} You can still click the microphone button to talk.")
                self._emit_status()
        return self._wake

    def _ensure_stt(self) -> STTEngine:
        if self._stt is None:
            self._stt = create_stt(self.config, getattr(self._wake, "model", None))
        return self._stt

    def _loop(self) -> None:
        while self._running.is_set():
            want_mic = self.listening_enabled or self._ptt.is_set()
            if not want_mic:
                if self.mic.is_open:
                    self.mic.close()
                    self._emit_status()
                self._wakeup.wait(1.0)
                self._wakeup.clear()
                continue
            if not self.mic.is_open:
                try:
                    self.mic.open()
                    self.mic_error = None
                    self._emit_status()
                except MicrophoneError as exc:
                    self.mic_error = str(exc)
                    self._ptt.clear()
                    self._emit_status()
                    self.events.emit(EventType.NOTIFICATION, title="Microphone unavailable", message=str(exc))
                    self._wakeup.wait(15.0)
                    self._wakeup.clear()
                    continue
            if self._ptt.is_set():
                self._ptt.clear()
                self._converse(greet=False)
                continue
            wake = self._ensure_wake() if self.listening_enabled else None
            frame = self.mic.read(timeout=0.3)
            if frame is None:
                continue
            if wake is not None and wake.process(frame):
                log.info("wake word detected")
                if self.tts.speaking:
                    self.tts.stop()  # barge-in
                self.events.emit(EventType.UI_ACTION, action="wake")
                self._converse(greet=True)

    def _converse(self, greet: bool) -> None:
        """Record one utterance, send it to the core, speak the answer; keep listening while a reply is expected."""
        expects_reply = True
        first = True
        while expects_reply and self._running.is_set():
            carry: list[bytes] = []
            if first and greet:
                carry = self._peek_speech(0.45)
                if not carry and self.settings.get("voice.tts_enabled", True):
                    self.tts.speak(self.settings.get("voice.greeting", "I'm listening."))
                    wait_until_quiet(self.tts, 5)
                    self.mic.flush()
            first = False
            text = self._record_and_transcribe(carry)
            if text is None:
                return
            speech, expects_reply = self.handler(text)
            if speech and self.settings.get("voice.tts_enabled", True):
                self.tts.speak(speech)
                wait_until_quiet(self.tts, 60)
                self.mic.flush()

    def _peek_speech(self, seconds: float) -> list[bytes]:
        """Did the user keep talking right after the wake word ("Hey Genesis, open Chrome")?"""
        needed = max(1, int(seconds * 1000 / FRAME_MS))
        frames, loud = [], 0
        end = time.monotonic() + seconds * 2
        while len(frames) < needed and time.monotonic() < end:
            f = self.mic.read(timeout=0.2)
            if f:
                frames.append(f)
                if frame_rms(f) > 600:
                    loud += 1
        return frames if loud * FRAME_MS >= 96 else []

    def _record_and_transcribe(self, carry: Optional[list[bytes]] = None) -> Optional[str]:
        self.events.emit(EventType.STATE_CHANGED, state=AssistantState.LISTENING.value)
        ep = Endpointer(EndpointConfig())
        for f in carry or []:
            ep.feed(f)
        while not ep.done and self._running.is_set():
            f = self.mic.read(timeout=0.5)
            if f is not None:
                ep.feed(f)
        if ep.timed_out:
            self.events.emit(EventType.TRANSCRIPT, text="", final=True, note="I didn't hear anything.")
            self.events.emit(EventType.STATE_CHANGED, state=AssistantState.IDLE.value)
            return None
        self.events.emit(EventType.STATE_CHANGED, state=AssistantState.THINKING.value, detail="Transcribing…")
        try:
            text = self._ensure_stt().transcribe(ep.audio())
        except (STTError, Exception) as exc:
            log.warning("speech recognition failed: %s", exc)
            self.events.emit(EventType.NOTIFICATION, title="Speech recognition failed", message=str(exc))
            self.events.emit(EventType.STATE_CHANGED, state=AssistantState.ERROR.value, message=str(exc))
            return None
        text = normalize(text)
        self.events.emit(EventType.TRANSCRIPT, text=text, final=True)
        if not text:
            self.events.emit(EventType.STATE_CHANGED, state=AssistantState.IDLE.value)
            return None
        return text
