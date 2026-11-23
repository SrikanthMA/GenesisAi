"""Smarter voice (2.1): accurate transcription, a guard against misheard actions, and a ChatGPT-style voice chat mode.

* **Accurate transcription.** After the wake word, the recorded request is transcribed by Gemini (the same AI model
  Genesis already uses), which understands accents and context far better than the small offline model — e.g. it knows
  "show me an image of a dragon" is about a picture, not an e-mail. If Gemini is unavailable, slow (8 s) or over its
  limit, the offline recogniser is used exactly as before. Only audio recorded *after* "Hey Genesis" (or Ctrl+Alt+G) is
  sent; the wake word itself is always detected offline. Turn it off with ``GENESIS_STT_ENGINE=faster_whisper`` (or
  ``vosk``) or the ``voice.cloud_transcription = off`` setting.
* **Misheard-action guard.** A *spoken* request that only the statistical classifier (not a precise rule) mapped to an
  action that changes or sends something, with low confidence, is treated as conversation: Genesis answers/asks instead
  of acting on a guess.
* **Voice chat mode.** "let's chat" / "start voice chat" → Genesis keeps listening between turns (2 minutes of silence
  instead of 20 s), answers like a conversation partner (short, natural, remembers the last turns) and only runs
  commands you say clearly. "stop voice chat" ends it; it also ends by itself after 10 minutes without speech.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from ..core.logging_setup import get_logger
from ..nlp.intents import Intent
from . import gemini_native

log = get_logger("v2.voice")

# ------------------------------------------------------------------------------------------------- transcription


class SmartSTT:
    """Gemini first (accurate), the local engine as fallback. Same interface as genesis.voice.stt engines."""

    name = "gemini+local"
    CLOUD_TIMEOUT = 8.0
    BACKOFF = {"quota": 600.0, "auth": 1800.0, "not_found": 1800.0, "network": 60.0, "error": 60.0}

    def __init__(self, local: Any, config: Callable[[], Any] | Any, settings: Any, hint: Optional[Callable[[], str]] = None,
                 transcribe_fn: Callable[..., str] = gemini_native.transcribe) -> None:
        self.local = local
        self._config = config
        self.settings = settings
        self.hint = hint or (lambda: "")
        self._transcribe = transcribe_fn
        self._paused_until = 0.0
        self.last_engine = ""
        self.last_language: Optional[str] = None
        self.last_language_probability = 0.0

    @property
    def config(self) -> Any:
        return self._config() if callable(self._config) else self._config

    def cloud_enabled(self) -> bool:
        if str(self.settings.get("voice.cloud_transcription", "auto") if self.settings else "auto").lower() == "off":
            return False
        return bool(gemini_native.gemini_key(self.config)) and time.monotonic() >= self._paused_until

    def transcribe(self, pcm16: bytes, sample_rate: int = 16000) -> str:
        seconds = len(pcm16) / 2 / max(1, sample_rate)
        if self.cloud_enabled() and seconds >= 0.35:
            try:
                hint = ""
                try:
                    hint = self.hint() or ""
                except Exception:
                    pass
                ctx_hint = context_hint()
                hint = "; ".join(x for x in (hint, ctx_hint) if x)
                text = self._transcribe(self.config, pcm16, sample_rate, hint=hint, timeout=self.CLOUD_TIMEOUT)
                if text:
                    LAST_HEARD.update(text=text, engine="gemini", at=time.time())
                    self.last_engine, self.last_language, self.last_language_probability = "gemini", None, 0.0
                    return text
            except gemini_native.GeminiError as exc:
                self._paused_until = time.monotonic() + self.BACKOFF.get(exc.kind, 60.0)
                log.info("cloud transcription unavailable (%s) - using the offline recogniser", exc.kind)
            except Exception as exc:  # never lose the user's words because of the cloud path
                log.info("cloud transcription failed (%s) - using the offline recogniser", type(exc).__name__)
        text = self.local.transcribe(pcm16, sample_rate)
        LAST_HEARD.update(text=text, engine=getattr(self.local, "name", "local"), at=time.time())
        self.last_engine = getattr(self.local, "name", "local")
        self.last_language = getattr(self.local, "last_language", None)
        self.last_language_probability = float(getattr(self.local, "last_language_probability", 0.0) or 0.0)
        return text

    def __getattr__(self, item: str) -> Any:  # anything else (e.g. warm-up hooks) goes to the local engine
        return getattr(self.local, item)


# ------------------------------------------------------------------------------------------------- context (2.2)
LAST_HEARD: dict = {"text": "", "engine": "", "at": 0.0}
_CONTEXT_SVC: dict = {}


def attach(svc: Any) -> None:
    """GenesisCore hook: lets transcription know which window you're in (better names/words in context)."""
    _CONTEXT_SVC["svc"] = svc


def context_hint() -> str:
    svc = _CONTEXT_SVC.get("svc")
    if svc is None:
        return ""
    try:
        from . import window_context

        t = window_context.current_target(svc)
    except Exception:
        return ""
    if t is None:
        return ""
    return f"the user is looking at {t.page or t.title} in {t.app}"


def wrap_stt(local: Any, config: Any, settings: Any, hint: Optional[Callable[[], str]] = None) -> Any:
    """Called by the voice manager (v2 hook). Explicit local engines in .env stay local."""
    engine = str(getattr(config, "stt_engine", "auto") or "auto").lower()
    if engine not in ("auto", "gemini", "cloud"):
        return local
    return SmartSTT(local, config, settings, hint)


# ------------------------------------------------------------------------------------------------- voice chat mode
class _ChatMode:
    IDLE_LIMIT = 600.0  # ends by itself after 10 minutes without a turn

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = False
        self._last = 0.0

    def start(self) -> None:
        with self._lock:
            self._active, self._last = True, time.monotonic()

    def stop(self) -> None:
        with self._lock:
            self._active = False

    def touch(self) -> None:
        with self._lock:
            if self._active:
                self._last = time.monotonic()

    @property
    def active(self) -> bool:
        with self._lock:
            if self._active and time.monotonic() - self._last > self.IDLE_LIMIT:
                self._active = False
            return self._active


CHAT = _ChatMode()
CHAT_TIMEOUT_S = 120.0

CHAT_PROMPT = ("\n\nYou are in a live VOICE CHAT with the user, like a phone call: everything you write is spoken aloud. "
               "Talk like a warm, smart friend: natural spoken sentences, usually one to three, no lists, no markdown, "
               "no emojis, no URLs. Answer directly, then keep the conversation going when it fits (a short follow-up "
               "question or a relevant thought). Remember what was said earlier in this chat. If the user asks you to "
               "do something on the computer, say briefly what you'll do.")


def session_timeout(base: float) -> float:
    """SessionManager hook: stay awake longer between turns while voice chat is on."""
    return max(base, CHAT_TIMEOUT_S) if CHAT.active else base


def chat_prompt_addition() -> str:
    return CHAT_PROMPT if CHAT.active else ""


def history_turns(default: int) -> int:
    return max(default, 14) if CHAT.active else default


# ------------------------------------------------------------------------------------------------- routing guard
_PRECISE = ("rules", "rules+typo", "v2-", "followup", "follow-up", "dialogue", "context", "screen")
_SAFE_INTENTS = {Intent.CONVERSATION, Intent.INFORMATION_REQUEST, Intent.UNKNOWN, Intent.IDENTITY, Intent.SESSION,
                 Intent.CANCEL, Intent.OPEN_APPLICATION, Intent.OPEN_URL, Intent.OPEN_FOLDER, Intent.SEARCH_WEB,
                 Intent.YOUTUBE, Intent.MEDIA, Intent.IMAGE, Intent.UI_CONTROL, Intent.RESEARCH, Intent.KNOWLEDGE,
                 Intent.HISTORY, Intent.SCREEN, Intent.FOLLOW_UP, Intent.CORRECTION}
VOICE_MIN_CONFIDENCE = 0.85


def route(nlu: Any, source: str) -> Any:
    """Assistant hook after NLU: don't act on a guess that came from speech."""
    if source == "voice":
        CHAT.touch()
    if nlu is None or nlu.intent in _SAFE_INTENTS:
        return nlu
    precise = str(getattr(nlu, "source", "") or "").startswith(_PRECISE)
    if precise:
        return nlu
    if CHAT.active or (source == "voice" and float(getattr(nlu, "confidence", 0.0) or 0.0) < VOICE_MIN_CONFIDENCE):
        log.info("voice guard: %s (%s, %.2f) treated as conversation", nlu.intent.value, nlu.source, nlu.confidence)
        nlu.slots = {k: v for k, v in (nlu.slots or {}).items() if k == "raw"}
        nlu.intent = Intent.CONVERSATION
        nlu.source = f"{nlu.source}+voice-guard"
    return nlu

from ..v3.hooks import patch_v2_voice as _v3_hook; _v3_hook(globals())  # v3 hook (Genesis 3.0)
