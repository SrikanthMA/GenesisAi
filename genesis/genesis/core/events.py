"""A small thread-safe publish/subscribe bus.

The core never imports the UI. The UI subscribes to events and marshals them
onto the Qt thread (see :mod:`genesis.ui.bridge`).
"""
from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from .logging_setup import get_logger

log = get_logger("events")


class AssistantState(str, Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    THINKING = "THINKING"
    EXECUTING = "EXECUTING"
    SPEAKING = "SPEAKING"
    WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
    WAITING_FOR_INPUT = "WAITING_FOR_INPUT"
    ERROR = "ERROR"


class EventType(str, Enum):
    STATE_CHANGED = "state_changed"
    MESSAGE = "message"  # a chat message (user or assistant)
    PLAN_CREATED = "plan_created"
    STEP_UPDATED = "step_updated"
    CONFIRMATION_REQUIRED = "confirmation_required"
    CONFIRMATION_RESOLVED = "confirmation_resolved"
    NOTIFICATION = "notification"
    REMINDER_DUE = "reminder_due"
    LLM_STATUS = "llm_status"
    VOICE_STATUS = "voice_status"  # mic open/closed, engine errors
    TRANSCRIPT = "transcript"  # partial / final speech transcripts
    UI_ACTION = "ui_action"  # e.g. open_chat, hide
    SETTINGS_CHANGED = "settings_changed"
    SERVICE_CHANGED = "service_changed"


@dataclass
class Event:
    type: EventType
    payload: dict[str, Any] = field(default_factory=dict)


Handler = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[EventType | None, list[Handler]] = defaultdict(list)
        self._lock = threading.RLock()

    def subscribe(self, handler: Handler, event_type: EventType | None = None) -> Callable[[], None]:
        with self._lock:
            self._subs[event_type].append(handler)

        def unsubscribe() -> None:
            with self._lock:
                if handler in self._subs[event_type]:
                    self._subs[event_type].remove(handler)

        return unsubscribe

    def emit(self, event_type: EventType, **payload: Any) -> None:
        event = Event(event_type, payload)
        with self._lock:
            handlers = list(self._subs.get(event_type, [])) + list(self._subs.get(None, []))
        for h in handlers:
            try:
                h(event)
            except Exception:  # a broken subscriber must never break the core
                log.exception("event handler failed for %s", event_type.value)
