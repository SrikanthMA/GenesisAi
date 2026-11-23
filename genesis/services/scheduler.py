"""Background reminder scheduler (local, persisted in SQLite)."""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Callable, Optional

from ..core.logging_setup import get_logger
from ..database.repositories import ReminderRepo

log = get_logger("scheduler")


def to_utc_iso(local_dt: datetime) -> str:
    if local_dt.tzinfo is None:
        local_dt = local_dt.astimezone()  # interpret naive as local time
    return local_dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_utc_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()  # local aware


class ReminderScheduler:
    def __init__(self, repo: ReminderRepo, on_due: Callable[[dict], None], interval: float = 10.0) -> None:
        self.repo = repo
        self.on_due = on_due
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def add(self, text: str, due_local: datetime) -> int:
        return self.repo.add(text, to_utc_iso(due_local))

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="genesis-reminders", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def check_now(self) -> list[dict]:
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        fired = []
        for r in self.repo.due(now_iso):
            self.repo.mark(r["id"], "fired")
            r["late_minutes"] = max(0, int((datetime.now(timezone.utc) - from_utc_iso(r["due_at"])).total_seconds() // 60))
            try:
                self.on_due(r)
            except Exception:
                log.exception("reminder callback failed")
            fired.append(r)
        return fired

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_now()
            except Exception:
                log.exception("reminder check failed")
            self._stop.wait(self.interval)
