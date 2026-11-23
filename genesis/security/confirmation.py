"""Confirmation manager: pending actions that need an explicit yes/no."""
from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from ..core.logging_setup import get_logger

log = get_logger("security")

_YES = re.compile(
    r"^(yes|yeah|yep|yup|sure|ok|okay|confirm(ed)?|do it|go ahead|proceed|continue|affirmative|please do|"
    r"yes please|yes,? (do it|go ahead|continue|proceed|send it|submit it|shut it down)|(?:ok(?:ay)?,? )?(?:send|submit) it(?: now)?|"
    r"approved?)[.! ]*$",
    re.I,
)
_NO = re.compile(
    r"^(no|nope|nah|cancel|stop|don'?t|do not|abort|never ?mind|negative|no thanks|no,? (cancel|stop|don'?t)|"
    r"wait|hold on|not now|not yet|(?:no,? )?(?:don'?t|do not) (?:send|submit|post|do|click|delete|run)(?: it| that| this| them)?"
    r"(?: yet| now| please)?|(?:no,? )?(?:leave it|keep it) (?:as a )?(?:draft|unsent)|(?:just )?save it as a draft)[.! ]*$",
    re.I,
)


def parse_confirmation(text: str) -> Optional[bool]:
    """Return True for an explicit yes, False for an explicit no, None otherwise.

    Deliberately strict: anything that is not a clear yes is NOT a yes.
    """
    t = (text or "").strip().lower()
    t = re.sub(r"^(hey |ok |okay )?genesis[, ]*", "", t).strip()
    if not t:
        return None
    if _NO.match(t):
        return False
    if _YES.match(t):
        return True
    return None


@dataclass
class PendingConfirmation:
    id: str
    prompt: str
    tool: str
    risk: str
    args_preview: dict
    payload: Any  # opaque executor state needed to resume
    created_at: float = field(default_factory=time.time)
    ttl_seconds: float = 300.0

    @property
    def expired(self) -> bool:
        return time.time() - self.created_at > self.ttl_seconds

    def public(self) -> dict:
        return {"id": self.id, "prompt": self.prompt, "tool": self.tool, "risk": self.risk, "args": self.args_preview}


class ConfirmationManager:
    def __init__(self) -> None:
        self._pending: dict[str, PendingConfirmation] = {}
        self._lock = threading.RLock()

    def create(self, *, prompt: str, tool: str, risk: str, args_preview: dict, payload: Any,
               ttl_seconds: float = 300.0) -> PendingConfirmation:
        pc = PendingConfirmation(uuid.uuid4().hex[:12], prompt, tool, risk, args_preview, payload, ttl_seconds=ttl_seconds)
        with self._lock:
            self._pending[pc.id] = pc
        log.info("confirmation_requested", extra={"data": {"id": pc.id, "tool": tool, "risk": risk}})
        return pc

    def get(self, cid: str) -> Optional[PendingConfirmation]:
        with self._lock:
            pc = self._pending.get(cid)
            if pc and pc.expired:
                del self._pending[cid]
                log.info("confirmation_expired", extra={"data": {"id": cid}})
                return None
            return pc

    def pop(self, cid: str) -> Optional[PendingConfirmation]:
        with self._lock:
            pc = self.get(cid)
            if pc:
                del self._pending[cid]
            return pc

    def latest(self) -> Optional[PendingConfirmation]:
        with self._lock:
            live = [p for p in list(self._pending.values()) if self.get(p.id)]
            return max(live, key=lambda p: p.created_at) if live else None

    def cancel_all(self) -> None:
        with self._lock:
            self._pending.clear()
