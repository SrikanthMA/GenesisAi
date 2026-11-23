"""Routine scheduler: runs due routines through the normal (safe) pipeline."""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Callable, Optional

from ..core.logging_setup import get_logger
from ..database.repositories import RoutineRepo

log = get_logger("routines")


class RoutineScheduler:
    def __init__(self, repo: RoutineRepo, run: Callable[[dict], None], interval: float = 20.0,
                 now: Callable[[], datetime] = datetime.now) -> None:
        self.repo = repo
        self.run = run
        self.interval = interval
        self.now = now
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    CATCH_UP_MINUTES = 10  # still run a routine if Genesis was busy/asleep at the exact minute (but only once a day)

    def due(self) -> list[dict]:
        now = self.now()
        today = now.strftime("%Y-%m-%d")
        out = []
        for r in self.repo.list():
            if not r["enabled"] or not r.get("time_of_day") or now.weekday() not in (r["days"] or []):
                continue
            try:
                hh, mm = (int(x) for x in r["time_of_day"].split(":"))
            except ValueError:
                continue
            late = (now.hour * 60 + now.minute) - (hh * 60 + mm)
            if not 0 <= late < self.CATCH_UP_MINUTES:
                continue
            if (r.get("last_run") or "")[:10] == today:  # already ran today
                continue
            out.append(r)
        return out

    def check_now(self) -> list[dict]:
        fired = []
        for r in self.due():
            self.repo.mark_run(r["id"], self.now().strftime("%Y-%m-%d %H:%M"))
            log.info("routine due", extra={"data": {"routine": r["id"]}})
            try:
                self.run(r)
            except Exception:
                log.exception("routine failed")
            fired.append(r)
        return fired

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="genesis-routines", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.check_now()
            except Exception:
                log.exception("routine check failed")
