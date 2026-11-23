"""Voice conversation engine.

    SLEEPING: microphone frames go ONLY to the local wake-word detector.
        │  "Hey Genesis[, open Gmail]"
        ▼
    ACTIVE CONVERSATION (no wake word needed):
        listen ─► speech start ─► end of turn ─► STT ─► core.handle() ─► speak reply
          ▲                                                        │
          └──────── continue listening (timer restarts after the reply finished) ◄┘
        • the user can interrupt while Genesis speaks (wake word, or clear speech over the playback — barge-in)
        • "go to sleep" / "that's all" ends the session
        • no speech for `conversation.timeout_seconds` (longer after a question) ─► back to SLEEPING
    SLEEPING again — the wake word keeps working; no restart needed.

The inactivity countdown is owned by :class:`genesis.core.session.SessionManager`, shared with typed input,
so voice and text follow the same rules. The microphone is fully closed whenever listening is disabled.
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
Handler = Callable[[str], tuple]  # → (speech, expects_reply[, reply_language, english_speech])


class VoiceManager:
    def __init__(self, config, settings, events: EventBus, handler: Handler, *, mic: Optional[Microphone] = None,
                 wake: Optional[WakeWordDetector] = None, stt: Optional[STTEngine] = None, tts: Optional[TTSEngine] = None,
                 session=None) -> None:
        self.config = config
        self.settings = settings
        self.events = events
        self.handler = handler
        self.mic = mic or Microphone(config.audio_input_device)
        self._wake = wake
        self._stt = stt
        self.tts: TTSEngine = tts or NullTTS()
        self._tts_given = tts is not None
        if session is None:
            from ..core.session import SessionManager

            session = SessionManager(settings, events)
        self.session = session
        self._running = threading.Event()
        self._wakeup = threading.Event()
        self._ptt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.wake_error: Optional[str] = None
        self.mic_error: Optional[str] = None
        self.interruptions = 0
        self._held = False
        self.heard_language: Optional[str] = None  # language speech recognition heard in the last utterance
        self.heard_language_probability: float = 0.0

    # ------------------------------------------------------------- control
    @property
    def listening_enabled(self) -> bool:
        return bool(self.settings.get("voice.enabled", True) and self.settings.get("voice.wake_word_enabled", True))

    def start(self) -> None:
        if not self._tts_given:
            self.tts = create_tts(self.config.tts_engine)
            self.tts.set_rate(int(self.settings.get("voice.tts_rate", 0)))
            chosen = self.settings.get("voice.tts_voice", "") or f"gender:{self.settings.get('voice.gender', 'female') or 'female'}"
            self.tts.set_voice(chosen)  # "gender:female" → the most natural installed female voice
        self.tts.on_start = lambda: self.events.emit(EventType.STATE_CHANGED, state=AssistantState.SPEAKING.value)
        self.tts.on_done = lambda: self.events.emit(EventType.STATE_CHANGED, state=self._resting().value)
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
                "tts": self.tts.name, "session": self.session.phase.value}

    def _emit_status(self) -> None:
        self.events.emit(EventType.VOICE_STATUS, **self.status())

    def _resting(self) -> AssistantState:
        return AssistantState.ACTIVE_CONVERSATION if self.session.active else AssistantState.SLEEPING

    # ---------------------------------------------------------------- engines
    def _ensure_wake(self) -> Optional[WakeWordDetector]:
        if self._wake is None and self.wake_error is None:
            try:
                self._wake = create_wake_detector(self.config, float(self.settings.get("voice.wake_sensitivity", 0.6)),
                                                  str(self.settings.get("voice.wake_phrase", "hey genesis") or "hey genesis"))
            except (WakeWordError, Exception) as exc:
                self.wake_error = str(exc)
                log.warning("wake word unavailable: %s", exc)
                self.events.emit(EventType.NOTIFICATION, title="Wake word unavailable",
                                 message=f"{exc} You can still click the microphone button to talk.")
                self._emit_status()
        return self._wake

    def _ensure_stt(self) -> STTEngine:
        if self._stt is None:
            self._stt = create_stt(self.config, getattr(self._wake, "model", None),
                                   hint=lambda: self.settings.get("voice.stt_hint", "") or "",
                                   multilingual=lambda: str(self.settings.get("language.mode", "auto")) != "english")
            from ..v2 import voice_ai as v2voice  # v2 hook (2.1): accurate cloud transcription, offline fallback

            self._stt = v2voice.wrap_stt(self._stt, self.config, self.settings,
                                         hint=lambda: self.settings.get("voice.stt_hint", "") or "")
        return self._stt

    # ---------------------------------------------------------------- main loop
    def _loop(self) -> None:
        while self._running.is_set():
            try:
                self._tick()
            except Exception:  # never let the voice thread die
                log.exception("voice loop error")
                time.sleep(1.0)

    def _tick(self) -> None:
        want_mic = self.listening_enabled or self._ptt.is_set()
        if not want_mic:
            if self.mic.is_open:
                self.mic.close()
                self._emit_status()
            self._wakeup.wait(1.0)
            self._wakeup.clear()
            return
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
                return
        if self._ptt.is_set():
            self._ptt.clear()
            self.session.wake("push_to_talk")
            self._conversation(greet=False)
            return
        wake = self._ensure_wake() if self.listening_enabled else None
        frame = self.mic.read(timeout=0.3)
        if frame is None:
            return
        if wake is not None and wake.process(frame):
            log.info("wake_word_detected", extra={"data": {"engine": getattr(wake, "name", "?"),
                                                            "while_speaking": self.tts.speaking}})
            if self.tts.speaking:
                self.tts.stop()  # barge-in
            self.session.wake("wake_word")
            self.events.emit(EventType.UI_ACTION, action="wake")
            self._conversation(greet=True)

    # ------------------------------------------------------------ conversation
    def _conversation(self, greet: bool) -> None:
        """Run turns until the session ends (inactivity, 'go to sleep', or disabled)."""
        carry: list[bytes] = []
        first = True
        while self._running.is_set() and (self.listening_enabled or first):
            if first and greet:
                carry = self._peek_speech(0.45)  # "Hey Genesis, open Gmail" in one breath → no greeting
                if not carry and self.settings.get("voice.tts_enabled", True):
                    self.tts.speak(self.settings.get("voice.greeting", "I'm listening."))
                    wait_until_quiet(self.tts, 5)
                    self.mic.flush()
                self.session.response_finished()
            first = False
            if not self.session.active:
                self.session.wake("voice")
            text = self._record_and_transcribe(carry)
            carry = []
            if text is None:
                if self.session.check_timeout() or not self.session.active:
                    self._back_to_sleep()
                    return
                if not self.settings.get("conversation.enabled", True):
                    self.session.sleep("single_command_mode")
                    self._back_to_sleep()
                    return
                continue
            out = self.handler(text)
            speech, lang, english = out[0], (out[2] if len(out) > 2 else None), (out[3] if len(out) > 3 else None)
            can_speak = getattr(self.tts, "can_speak", None)
            if lang and lang != "en" and (can_speak is None or not can_speak(lang)):
                speech, lang = english or speech, None  # no voice for that language installed → say it in English
                from ..v2.voice import no_voice_notice  # v2 hook: say so once instead of switching silently

                no_voice_notice(self, out[2])
            interrupted = self._speak_interruptible(speech, lang)
            self.session.response_finished()
            if interrupted:
                carry = interrupted
                continue
            if not self.session.active:  # user said "go to sleep" / "that's all"
                self._back_to_sleep()
                return
            if not self.settings.get("conversation.enabled", True) and not self.session.awaiting_reply:
                self.session.sleep("single_command_mode")
                self._back_to_sleep()
                return

    def _back_to_sleep(self) -> None:
        self.mic.flush()
        if self._wake:
            self._wake.reset()
        self.events.emit(EventType.STATE_CHANGED, state=AssistantState.SLEEPING.value)

    def _peek_speech(self, seconds: float) -> list[bytes]:
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

    def _endpoint_config(self, no_speech_s: Optional[float]) -> EndpointConfig:
        silence = int(self.settings.get("voice.end_of_turn_ms", 900) or 900)
        no_speech = int(max(3.0, min(no_speech_s if no_speech_s is not None else 60.0, 120.0)) * 1000)
        return EndpointConfig(silence_ms=max(400, min(silence, 3000)), no_speech_ms=no_speech)

    def _record_and_transcribe(self, carry: Optional[list[bytes]] = None) -> Optional[str]:
        """Listen for one utterance. Returns None when nobody spoke before the session's inactivity deadline."""
        remaining = self.session.remaining()
        ep = Endpointer(self._endpoint_config(remaining))
        self.events.emit(EventType.STATE_CHANGED, state=(AssistantState.LISTENING if carry else self._resting()).value)
        try:
            return self._record_and_transcribe_inner(ep, carry)
        finally:
            if self._held:
                self._held = False
                self.session.release()

    def _record_and_transcribe_inner(self, ep, carry) -> Optional[str]:
        announced = False
        for f in carry or []:
            ep.feed(f)
        wall_deadline = time.monotonic() + ep.cfg.no_speech_ms / 1000 + ep.cfg.max_ms / 1000 + 2
        t0 = time.monotonic()
        while not ep.done and self._running.is_set():
            if not ep.started and time.monotonic() > wall_deadline - ep.cfg.max_ms / 1000:
                ep.done = ep.timed_out = True  # wall-clock guard (e.g. audio device stalled)
                break
            if time.monotonic() > wall_deadline:
                break
            f = self.mic.read(timeout=0.5)
            if f is None:
                continue
            ep.feed(f)
            if ep.started and not announced:
                announced = True
                self.session.user_activity()
                if not self._held:  # no auto-sleep while you're talking or we're transcribing
                    self._held = True
                    self.session.hold()
                self.events.emit(EventType.STATE_CHANGED, state=AssistantState.LISTENING.value)
                log.info("utterance_start", extra={"data": {"session": self._sid()}})
        if ep.timed_out or not ep.started:
            log.info("no_speech", extra={"data": {"session": self._sid(), "waited_ms": int((time.monotonic() - t0) * 1000)}})
            return None
        log.info("utterance_end", extra={"data": {"session": self._sid(), "speech_ms": ep.speech_ms}})
        self.events.emit(EventType.STATE_CHANGED, state=AssistantState.THINKING.value, detail="Transcribing…")
        try:
            stt = self._ensure_stt()
            text = stt.transcribe(ep.audio())
            self.heard_language = getattr(stt, "last_language", None)
            self.heard_language_probability = float(getattr(stt, "last_language_probability", 0.0) or 0.0)
        except (STTError, Exception) as exc:
            log.warning("speech recognition failed: %s", exc)
            self.events.emit(EventType.NOTIFICATION, title="Speech recognition failed", message=str(exc))
            self.events.emit(EventType.STATE_CHANGED, state=AssistantState.ERROR.value, message=str(exc))
            return None
        text = normalize(text)
        self.events.emit(EventType.TRANSCRIPT, text=text, final=True)
        log.info("utterance_transcribed (%s)", self.heard_language or "?",
                 extra={"data": {"session": self._sid(), "chars": len(text), "language": self.heard_language}})
        if not text:
            self.events.emit(EventType.STATE_CHANGED, state=self._resting().value)
            return None
        return text

    def _speak_interruptible(self, speech: str, lang: Optional[str] = None) -> list[bytes]:
        """Speak; if the user starts talking over Genesis (barge-in) stop and return the captured frames."""
        if not speech or not self.settings.get("voice.tts_enabled", True):
            return []
        if lang:
            self.tts.speak(speech, lang=lang)
        else:
            self.tts.speak(speech)
        barge = bool(self.settings.get("voice.barge_in", True))
        wake = self._wake
        buf: list[bytes] = []
        echo_levels: list[float] = []
        loud_run = 0
        started = time.monotonic()
        # give the TTS thread a moment to start (loading a voice for another language can take a while)
        while not self.tts.speaking and time.monotonic() - started < 1.5:
            time.sleep(0.02)
        started = time.monotonic()  # calibrate on our own voice from the moment it is actually playing
        while self.tts.speaking and self._running.is_set():
            f = self.mic.read(timeout=0.1)
            if f is None:
                continue
            buf.append(f)
            buf = buf[-24:]  # keep ~0.75 s so the start of the interruption isn't lost
            if wake is not None and wake.process(f):
                return self._interrupted(buf, "wake_word")
            if not barge:
                continue
            level = frame_rms(f)
            if time.monotonic() - started < 0.6:
                echo_levels.append(level)  # calibrate: how loud our own voice is in the mic
                continue
            # the loud parts of our own voice (90th percentile), not the average — syllables are much louder than gaps
            echo = sorted(echo_levels)[int(0.9 * (len(echo_levels) - 1))] if echo_levels else 0.0
            threshold = max(1400.0, echo * 2.2)
            loud_run = loud_run + 1 if level > threshold else 0
            if loud_run * FRAME_MS >= 256:  # ~quarter second of clear speech over the playback
                return self._interrupted(buf, "speech")
        wait_until_quiet(self.tts, 60)
        self.mic.flush()
        return []

    def _interrupted(self, frames: list[bytes], how: str) -> list[bytes]:
        self.tts.stop()
        self.interruptions += 1
        log.info("barge_in (%s)", "you said “Hey Genesis”" if how == "wake_word" else "speech over the reply",
                 extra={"data": {"session": self._sid(), "trigger": how}})
        self.events.emit(EventType.STATE_CHANGED, state=AssistantState.LISTENING.value, detail="Interrupted")
        return frames if how == "speech" else []

    def _sid(self) -> Optional[str]:
        return self.session.session.id if self.session.session else None

from ..v3.hooks import patch_voice_manager as _v3_hook; _v3_hook(globals())  # v3 hook (Genesis 3.0)
