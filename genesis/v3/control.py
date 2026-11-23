"""Emergency stop (3.1): "Genesis, stop everything" halts what Genesis is doing right now.

* stops speech immediately,
* cancels a running mission (it checks ``cancelled()`` between steps and reports what was done so far),
* refuses new automated steps for a few seconds so a half-finished plan can't carry on.

It never undoes actions that already finished; it only stops further ones.
"""
from __future__ import annotations

import threading
import time

_STOP = threading.Event()
_STOPPED_AT = [0.0]
HOLD_S = 5.0


def emergency_stop() -> None:
    _STOP.set()
    _STOPPED_AT[0] = time.time()
    try:  # silence the voice at once
        from .hooks import _VOICE

        vm = _VOICE.get("vm")
        if vm is not None and getattr(vm, "tts", None) is not None:
            vm.tts.stop()
    except Exception:
        pass


def cancelled() -> bool:
    return _STOP.is_set()


def holding() -> bool:
    """True for a few seconds after a stop: automated follow-up steps should not start."""
    return _STOP.is_set() and time.time() - _STOPPED_AT[0] < HOLD_S


def reset() -> None:
    _STOP.clear()
