"""Cancel a running multi-step task. The executor checks the flag between steps (a step that is already running is
allowed to finish — interrupting a tool half-way could leave things in an unknown state)."""
from __future__ import annotations

import threading

_EVENT = "v2_cancel_event"


def token(svc) -> threading.Event:
    ev = svc.extras.get(_EVENT)
    if ev is None:
        ev = svc.extras[_EVENT] = threading.Event()
    return ev


def request(svc) -> None:
    token(svc).set()


def reset(svc) -> None:
    token(svc).clear()


def requested(svc) -> bool:
    ev = svc.extras.get(_EVENT)
    return bool(ev is not None and ev.is_set())
