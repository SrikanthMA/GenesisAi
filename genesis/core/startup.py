"""'Start Genesis with Windows' via the per-user Run registry key (no admin rights needed)."""
from __future__ import annotations

import sys
from pathlib import Path

from .paths import PROJECT_ROOT, is_frozen

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "Genesis"


def launch_command() -> str:
    if is_frozen():
        return f'"{sys.executable}" --minimized'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    runner = pyw if pyw.exists() else exe
    return f'"{runner}" -m genesis --minimized'


def set_start_with_windows(enabled: bool) -> tuple[bool, str]:
    if not sys.platform.startswith("win"):
        return True, "not on Windows — preference saved only"
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
            if enabled:
                # the working directory matters when running from source
                cmd = launch_command() if is_frozen() else f'cmd /c "cd /d "{PROJECT_ROOT}" && {launch_command()}"'
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, VALUE_NAME)
                except FileNotFoundError:
                    pass
        return True, "ok"
    except OSError as exc:
        return False, str(exc)


def is_start_with_windows() -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
            return True
    except OSError:
        return False
