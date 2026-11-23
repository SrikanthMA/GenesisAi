"""Thread-safe bridge from the core's EventBus to Qt signals.

Core/voice threads emit events; Qt delivers these signals to widgets on the
GUI thread (queued connections), so widgets are never touched off-thread.
"""
from __future__ import annotations

import concurrent.futures
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

from ..core.events import Event, EventBus, EventType


class UiBridge(QObject):
    state_changed = Signal(object)
    message = Signal(object)
    plan_created = Signal(object)
    step_updated = Signal(object)
    confirmation_required = Signal(object)
    confirmation_resolved = Signal(object)
    notification = Signal(object)
    reminder_due = Signal(object)
    llm_status = Signal(object)
    voice_status = Signal(object)
    transcript = Signal(object)
    ui_action = Signal(object)
    settings_changed = Signal(object)
    service_changed = Signal(object)
    response_ready = Signal(object)  # AssistantResponse
    task_failed = Signal(str)

    def __init__(self, bus: EventBus) -> None:
        super().__init__()
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="genesis-ui-worker")
        mapping = {
            EventType.STATE_CHANGED: self.state_changed, EventType.MESSAGE: self.message,
            EventType.PLAN_CREATED: self.plan_created, EventType.STEP_UPDATED: self.step_updated,
            EventType.CONFIRMATION_REQUIRED: self.confirmation_required,
            EventType.CONFIRMATION_RESOLVED: self.confirmation_resolved, EventType.NOTIFICATION: self.notification,
            EventType.REMINDER_DUE: self.reminder_due, EventType.LLM_STATUS: self.llm_status,
            EventType.VOICE_STATUS: self.voice_status, EventType.TRANSCRIPT: self.transcript,
            EventType.UI_ACTION: self.ui_action, EventType.SETTINGS_CHANGED: self.settings_changed,
            EventType.SERVICE_CHANGED: self.service_changed,
        }

        def forward(event: Event) -> None:
            sig = mapping.get(event.type)
            if sig is not None:
                sig.emit(dict(event.payload))

        bus.subscribe(forward)

    def run_async(self, fn: Callable[..., Any], *args: Any, on_done: Callable[[Any], None] | None = None) -> None:
        """Run blocking work (core.handle, OAuth, indexing) off the GUI thread."""

        def job() -> None:
            try:
                result = fn(*args)
                if on_done is None:
                    self.response_ready.emit(result)
                else:
                    self.response_ready.emit(("__callback__", on_done, result))
            except Exception as exc:  # surfaced in the UI, never swallowed silently
                self.task_failed.emit(f"{type(exc).__name__}: {exc}")

        self._pool.submit(job)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
