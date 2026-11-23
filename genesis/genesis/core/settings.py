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
    "voice.tts_rate": 0,  # SAPI rate -10..10
    "voice.tts_voice": "",  # substring of a voice name, e.g. "Zira"
    "voice.greeting": "I'm listening.",
    # ui
    "ui.auto_hide_seconds": 10,
    "ui.always_on_top": True,
    "ui.show_mini_on_start": True,
    # memory
    "memory.enabled": True,  # conversation + long-term memory
    "memory.auto_save_contacts": True,  # save an email address you give Genesis for a named person
    # knowledge
    "knowledge.enabled": True,
    # llm
    "llm.use_for_nlu": True,
    "llm.use_for_planning": True,
    # security
    "security.trusted_tools": [],  # MEDIUM-risk tools the user allowed without confirmation
    "security.extra_allowed_roots": [],  # folders beyond the user profile Genesis may touch
    # system
    "system.start_with_windows": False,
    "search.engine": "google",  # google | bing | duckduckgo
    "user.name": "",
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
