"""Runtime user settings persisted in SQLite (editable from the Settings page)."""
from __future__ import annotations

import copy
import threading
from typing import Any, Callable

from ..database.repositories import SettingsRepo

DEFAULT_SETTINGS: dict[str, Any] = {
    # voice
    "voice.enabled": True,
    "voice.wake_word_enabled": True,
    "voice.wake_sensitivity": 0.6,  # 0..1 (higher = triggers more easily)
    "voice.tts_enabled": True,
    "voice.tts_rate": -1,  # -10..10 (slightly calmer than default)
    "voice.tts_voice": "",  # substring of a voice name, e.g. "Zira" (blank = best available voice of voice.gender)
    "voice.gender": "female",  # female | male | any — used when voice.tts_voice is blank
    "persona.address": "Boss",  # how Genesis addresses you in acknowledgements ("Done, Boss.") — blank = no address
    "whatsapp.mode": "auto",  # auto | web (WhatsApp Web in Genesis's browser) | desktop (the WhatsApp app)
    "voice.greeting": "I'm listening.",
    "voice.wake_phrase": "hey genesis",  # also accepted: "hi/ok genesis"
    "voice.barge_in": True,  # interrupt Genesis by speaking while it talks
    "voice.end_of_turn_ms": 900,  # silence that ends your sentence (longer sentences get a little more)
    # conversation sessions (wake once, then talk naturally)
    "conversation.enabled": True,  # stay awake for follow-ups after the wake word
    "conversation.auto_sleep": True,  # go back to sleep after inactivity
    "conversation.timeout_seconds": 20,  # inactivity after Genesis finishes responding
    "conversation.question_timeout_seconds": 45,  # longer when Genesis asked you something
    "conversation.max_session_minutes": 30,  # hard cap so the mic never stays open indefinitely
    # ui
    "ui.auto_hide_seconds": 10,
    "ui.always_on_top": True,
    "ui.show_mini_on_start": True,
    # memory
    "memory.enabled": True,  # conversation + long-term memory
    "memory.auto_save_contacts": True,  # save an email address you give Genesis for a named person
    "memory.track_usage": True,  # count which tools/apps you use (no content) to suggest routines
    "memory.suggest_routines": True,
    # screen understanding
    "screen.enabled": True,  # only ever captured when you ask ("what's on my screen?")
    # agent
    "agent.max_retries": 1,  # automatic retries for transient failures
    "agent.max_steps": 12,  # hard cap on steps per plan (prevents runaway execution)
    # knowledge
    "knowledge.enabled": True,
    # llm
    "llm.use_for_nlu": True,
    "llm.use_for_planning": True,
    "llm.fast_routes": True,  # questions → chat model directly; unfamiliar instructions → planner directly (1 model call)
    "performance.prewarm": True,  # index app shortcuts + open the AI model connection in the background at start-up
    "office.open_after_create": True,  # open new Word/Excel/PowerPoint files in Office right away
    # languages (1.6)
    "language.mode": "auto",  # auto = understand any language (detect → translate to English) | english = English only
    "language.reply": "same",  # same = reply and speak in the user's language | english = always reply in English
    "language.show_translation": True,  # show "🌐 French → “open YouTube”" under replies to non-English requests
    # security
    "security.trusted_tools": [],  # MEDIUM-risk tools the user allowed without confirmation
    "security.extra_allowed_roots": [],  # folders beyond the user profile Genesis may touch
    "security.confirm_medium": True,  # ask before MEDIUM-risk actions (HIGH risk always asks)
    # debugging
    "debug.log_content": False,  # include utterance text in logs (off = privacy-preserving)
    "coding.workspace": "",  # where new projects are created ("" = ~/GenesisProjects)
    "coding.open_vscode": True,  # open newly created projects in VS Code
    # system
    "system.start_with_windows": False,
    "search.engine": "google",  # google | bing | duckduckgo
    "user.name": "",
    "voice.stt_hint": "",  # vocabulary hint for the recogniser (maintained by the identity manager)
}


class Settings:
    def __init__(self, repo: SettingsRepo) -> None:
        self._repo = repo
        self._lock = threading.RLock()
        self._cache: dict[str, Any] = copy.deepcopy(DEFAULT_SETTINGS)
        self._cache.update(repo.all())
        self._listeners: list[Callable[[str, Any], None]] = []

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            if key in self._cache:
                return copy.deepcopy(self._cache[key])
            return default

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if key in DEFAULT_SETTINGS and DEFAULT_SETTINGS[key] is not None:
                expected = type(DEFAULT_SETTINGS[key])
                if expected is float and isinstance(value, int):
                    value = float(value)
                if not isinstance(value, expected):
                    raise TypeError(f"setting {key} expects {expected.__name__}")
            self._cache[key] = value
            self._repo.set(key, value)
        for listener in list(self._listeners):
            try:
                listener(key, value)
            except Exception:
                pass

    def all(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._cache)

    def on_change(self, listener: Callable[[str, Any], None]) -> None:
        self._listeners.append(listener)
