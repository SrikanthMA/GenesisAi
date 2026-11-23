"""Conversation-session state machine.

Genesis is no longer "one wake word = one command". A wake word (or typing,
the hotkey, the mic button) opens a *conversation session*; while the session
is active the user can keep talking without the wake word. The session ends
automatically after a configurable period of inactivity, or when the user says
"go to sleep" / "that's all", and Genesis returns to SLEEPING, where only the
wake-word detector runs.

    SLEEPING ──wake──► WAKING ──► ACTIVE_CONVERSATION ◄──────────────┐
        ▲                              │ user turn                     │
        │                              ▼                               │
        │                          THINKING ─► EXECUTING ─► VERIFYING ─┤
        │                              │                               │
        │                              └──► WAITING_FOR_USER ──answer──┘
        │                                                              │
        └── SLEEPING ◄── RETURNING_TO_SLEEP ◄── inactivity / "sleep" ──┘

The inactivity timer only runs while Genesis is idle *and* not speaking: it
restarts when a response finishes (``response_finished``), and is suspended
while a turn is being processed (``begin_turn``/``end_turn``).
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from .events import AssistantState, EventBus, EventType
from .logging_setup import get_logger

log = get_logger("session")

S = AssistantState
# Allowed transitions of the *session phase*. Activity states (THINKING, EXECUTING…) are
# sub-states of ACTIVE_CONVERSATION and are reported separately via STATE_CHANGED.
_ALLOWED = {
    S.SLEEPING: {S.WAKING},
    S.WAKING: {S.ACTIVE_CONVERSATION, S.SLEEPING},
    S.ACTIVE_CONVERSATION: {S.RETURNING_TO_SLEEP, S.ACTIVE_CONVERSATION},
    S.RETURNING_TO_SLEEP: {S.SLEEPING},
}


@dataclass
class ConversationSession:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    source: str = "wake_word"
    started_at: float = 0.0
    turns: int = 0
    ended_reason: Optional[str] = None


class SessionManager:
    def __init__(self, settings, events: EventBus, clock: Callable[[], float] = time.monotonic) -> None:
        self.settings = settings
        self.events = events
        self.clock = clock
        self._lock = threading.RLock()
        self.phase: AssistantState = S.SLEEPING
        self.session: Optional[ConversationSession] = None
        self.awaiting_reply = False  # Genesis asked a question / needs a yes-no
        self.task_open = False  # multi-step work is still queued
        self._busy = 0  # turns currently being processed
        self._idle_since: float = clock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.history: list[tuple[str, str, str]] = []  # (from, to, reason) — for tests/diagnostics

    # ------------------------------------------------------------ helpers
    @property
    def active(self) -> bool:
        return self.phase == S.ACTIVE_CONVERSATION

    @property
    def sleeping(self) -> bool:
        return self.phase == S.SLEEPING

    def _transition(self, to: AssistantState, reason: str) -> bool:
        with self._lock:
            frm = self.phase
            if to not in _ALLOWED.get(frm, set()):
                log.debug("ignored invalid session transition %s -> %s (%s)", frm.value, to.value, reason)
                return False
            self.phase = to
            self.history.append((frm.value, to.value, reason))
            self.history = self.history[-200:]
        log.info("session_transition", extra={"data": {"from": frm.value, "to": to.value, "reason": reason,
                                                        "session": self.session.id if self.session else None}})
        self.events.emit(EventType.STATE_CHANGED, state=to.value, session=to.value, reason=reason)
        return True

    def timeout_seconds(self) -> float:
        key = "conversation.question_timeout_seconds" if self.awaiting_reply else "conversation.timeout_seconds"
        from ..v2 import voice_ai as v2voice  # v2 hook (2.1): voice chat mode keeps listening longer between turns

        try:
            return v2voice.session_timeout(max(3.0, float(self.settings.get(key, 20))))
        except (TypeError, ValueError):
            return 20.0

    # ------------------------------------------------------------ lifecycle
    def wake(self, source: str = "wake_word") -> bool:
        """Start a conversation session (no-op if one is already active). Returns True if a new session began."""
        with self._lock:
            if self.phase == S.ACTIVE_CONVERSATION:
                self._idle_since = self.clock()
                return False
            if self.phase != S.SLEEPING:
                return False
            self.session = ConversationSession(source=source, started_at=self.clock())
            self.awaiting_reply = False
            self.task_open = False
            self._transition(S.WAKING, source)
            self._transition(S.ACTIVE_CONVERSATION, "session_started")
            self._idle_since = self.clock()
        log.info("session_start", extra={"data": {"session": self.session.id, "source": source}})
        self.events.emit(EventType.SESSION_CHANGED, active=True, session_id=self.session.id, source=source)
        return True

    def sleep(self, reason: str = "inactivity") -> bool:
        with self._lock:
            if self.phase != S.ACTIVE_CONVERSATION or self._busy:
                return False
            sess = self.session
            self._transition(S.RETURNING_TO_SLEEP, reason)
            self._transition(S.SLEEPING, reason)
            if sess:
                sess.ended_reason = reason
            self.awaiting_reply = False
            self.task_open = False
        log.info("session_end", extra={"data": {"session": sess.id if sess else None, "reason": reason,
                                                 "turns": sess.turns if sess else 0}})
        self.events.emit(EventType.SESSION_CHANGED, active=False, session_id=sess.id if sess else None, reason=reason)
        return True

    # ------------------------------------------------------------ turns
    def begin_turn(self, source: str = "text") -> None:
        """A user utterance/message arrived. Wakes the session if needed and pauses the inactivity timer."""
        if self.sleeping:
            self.wake(source)
        with self._lock:
            self._busy += 1
            if self.session:
                self.session.turns += 1

    def end_turn(self, *, awaiting_reply: bool, task_open: bool = False) -> None:
        with self._lock:
            self._busy = max(0, self._busy - 1)
            self.awaiting_reply = awaiting_reply
            self.task_open = task_open
            self._idle_since = self.clock()

    def response_finished(self) -> None:
        """Called when Genesis finished speaking — the inactivity countdown starts now."""
        with self._lock:
            self._idle_since = self.clock()

    def hold(self) -> None:
        """Pause the inactivity countdown while the user is still talking or Genesis is transcribing (the first
        transcription can take a while: it may download the speech model)."""
        with self._lock:
            self._busy += 1

    def release(self) -> None:
        with self._lock:
            self._busy = max(0, self._busy - 1)
            self._idle_since = self.clock()

    def user_activity(self) -> None:
        """Speech started / user is typing — keep the session alive."""
        with self._lock:
            self._idle_since = self.clock()

    def _max_session_exceeded(self) -> bool:
        try:
            cap = float(self.settings.get("conversation.max_session_minutes", 30)) * 60
        except (TypeError, ValueError):
            cap = 1800.0
        return bool(self.session and cap > 0 and self.clock() - self.session.started_at > cap)

    def remaining(self) -> Optional[float]:
        """Seconds until automatic sleep (None = no auto-sleep pending, e.g. while a turn is being processed)."""
        with self._lock:
            if self.phase != S.ACTIVE_CONVERSATION or self._busy:
                return None
            if self._max_session_exceeded():
                return 0.0  # hard cap applies even when auto-sleep is off: the mic never stays "open" forever
            if not self.settings.get("conversation.auto_sleep", True):
                cap = float(self.settings.get("conversation.max_session_minutes", 30) or 30) * 60
                return max(0.0, cap - (self.clock() - self.session.started_at)) if self.session else None
            left = self.timeout_seconds() - (self.clock() - self._idle_since)
            return max(0.0, left)

    def check_timeout(self) -> bool:
        rem = self.remaining()
        if rem is not None and rem <= 0:
            return self.sleep("max_session" if self._max_session_exceeded() else "inactivity")
        return False

    # ------------------------------------------------------------ monitor thread
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="genesis-session", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self.check_timeout()
            except Exception:
                log.exception("session monitor failed")

    def status(self) -> dict:
        return {"phase": self.phase.value, "session_id": self.session.id if self.session else None,
                "awaiting_reply": self.awaiting_reply, "remaining": self.remaining()}


__all__ = ["SessionManager", "ConversationSession"]

