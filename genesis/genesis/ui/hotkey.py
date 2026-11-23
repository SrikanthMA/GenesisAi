"""Global hotkey (Windows RegisterHotKey) to summon Genesis from anywhere."""
from __future__ import annotations

import sys
import threading
from typing import Callable, Optional

from ..core.logging_setup import get_logger

log = get_logger("hotkey")

MODS = {"alt": 0x0001, "ctrl": 0x0002, "control": 0x0002, "shift": 0x0004, "win": 0x0008}
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012


def parse_hotkey(spec: str) -> Optional[tuple[int, int]]:
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        return None
    mods = 0
    for p in parts[:-1]:
        if p not in MODS:
            return None
        mods |= MODS[p]
    key = parts[-1]
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key == "space":
        vk = 0x20
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    else:
        return None
    return mods, vk


class GlobalHotkey:
    def __init__(self, spec: str, callback: Callable[[], None]) -> None:
        self.spec = spec
        self.callback = callback
        self._thread: Optional[threading.Thread] = None
        self._tid = 0

    def start(self) -> bool:
        if not sys.platform.startswith("win"):
            return False
        parsed = parse_hotkey(self.spec)
        if not parsed:
            log.warning("invalid hotkey %r", self.spec)
            return False
        self._thread = threading.Thread(target=self._run, args=parsed, name="genesis-hotkey", daemon=True)
        self._thread.start()
        return True

    def _run(self, mods: int, vk: int) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        self._tid = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, vk):
            log.warning("could not register hotkey %s (already in use?)", self.spec)
            return
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    try:
                        self.callback()
                    except Exception:
                        log.exception("hotkey callback failed")
        finally:
            user32.UnregisterHotKey(None, 1)

    def stop(self) -> None:
        if self._tid and sys.platform.startswith("win"):
            import ctypes

            ctypes.windll.user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)  # type: ignore[attr-defined]
