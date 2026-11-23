"""Operating-system backend.

All OS side effects go through a :class:`PlatformBackend`. Commands are always
launched with an argument *list* (never through a shell), so user or model
text can never be interpreted as shell syntax.

Implementations: Windows (primary), macOS, Linux, plus :class:`RecordingBackend`
for tests and demos. ``GENESIS_SYSTEM_DRY_RUN=1`` wraps power actions so that
shutdown/restart/sleep/lock are simulated.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Optional

from ..core.logging_setup import get_logger

log = get_logger("platform")

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None


class PlatformError(RuntimeError):
    pass


class PlatformBackend:
    name = "generic"
    dry_run_power = False

    # -------------------------------------------------------------- launching
    def launch(self, argv: list[str], cwd: Optional[str] = None) -> int:
        if not argv or not isinstance(argv, list):
            raise PlatformError("launch() requires an argument list")
        kwargs: dict = {"cwd": cwd, "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                        "close_fds": True}
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(argv, shell=False, **kwargs)  # noqa: S603 - argv list, no shell
        except FileNotFoundError as exc:
            raise PlatformError(f"Program not found: {argv[0]}") from exc
        except OSError as exc:
            raise PlatformError(f"Could not start {Path(argv[0]).name}: {exc}") from exc
        return proc.pid

    def open_path(self, path: str) -> None:
        raise NotImplementedError

    def open_uri(self, uri: str) -> None:
        self.open_path(uri)

    def open_url(self, url: str) -> bool:
        return bool(webbrowser.open(url, new=2))

    def reveal_in_file_manager(self, path: str) -> None:
        self.open_path(str(Path(path).parent))

    # -------------------------------------------------------------- processes
    def find_processes(self, names: list[str]) -> list:
        if psutil is None:
            return []
        wanted = {n.lower() for n in names}
        wanted |= {n.lower()[:-4] for n in names if n.lower().endswith(".exe")}
        found = []
        for p in psutil.process_iter(["name", "pid"]):
            try:
                if (p.info.get("name") or "").lower() in wanted:
                    found.append(p)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return found

    def terminate_processes(self, names: list[str], timeout: float = 5.0) -> int:
        procs = self.find_processes(names)
        if psutil is None:
            raise PlatformError("psutil is required to close applications")
        for p in procs:
            try:
                p.terminate()  # SIGTERM on POSIX (polite); Windows overrides this method with a WM_CLOSE request
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        gone, alive = psutil.wait_procs(procs, timeout=timeout)
        return len(gone)

    def process_alive(self, pid: int) -> bool:
        if psutil is None:
            return True
        try:
            return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except psutil.Error:
            return False

    # ------------------------------------------------------------------ power
    def shutdown(self, delay: int = 10) -> None:
        raise NotImplementedError

    def restart(self, delay: int = 10) -> None:
        raise NotImplementedError

    def lock(self) -> None:
        raise NotImplementedError

    def sleep(self) -> None:
        raise NotImplementedError

    def cancel_shutdown(self) -> None:
        raise NotImplementedError

    # ------------------------------------------------------------------ misc
    def media_key(self, key: str, times: int = 1) -> None:
        raise PlatformError("Volume control is not supported on this platform.")

    # ------------------------------------------------------------------ windows
    def list_windows(self) -> list[dict]:
        """Visible top-level windows: [{"id", "title", "active"}]."""
        raise PlatformError("Window management is only supported on Windows.")

    def window_action(self, window_id: int, action: str) -> None:
        """action: focus | minimize | maximize | restore"""
        raise PlatformError("Window management is only supported on Windows.")

    def minimize_all(self) -> None:
        raise PlatformError("Window management is only supported on Windows.")

    def system_info(self) -> dict:
        info: dict = {"platform": sys.platform}
        if psutil is None:
            return info
        info["cpu_percent"] = psutil.cpu_percent(interval=0.3)
        vm = psutil.virtual_memory()
        info["memory_percent"] = vm.percent
        info["memory_total_gb"] = round(vm.total / 1e9, 1)
        try:
            du = psutil.disk_usage(str(Path.home().anchor or "/"))
            info["disk_free_gb"] = round(du.free / 1e9, 1)
            info["disk_percent"] = du.percent
        except OSError:
            pass
        try:
            bat = psutil.sensors_battery()
            if bat is not None:
                info["battery_percent"] = round(bat.percent)
                info["plugged_in"] = bool(bat.power_plugged)
        except Exception:
            pass
        info["uptime_hours"] = round((time.time() - psutil.boot_time()) / 3600, 1)
        return info


class WindowsBackend(PlatformBackend):
    name = "windows"

    def _system32(self, exe: str) -> str:
        root = os.getenv("SystemRoot", r"C:\Windows")
        return str(Path(root) / "System32" / exe)

    def open_path(self, path: str) -> None:
        try:
            os.startfile(path)  # type: ignore[attr-defined]  # uses ShellExecute, no shell parsing
        except OSError as exc:
            raise PlatformError(f"Windows could not open '{path}': {exc.strerror or exc}") from exc

    def reveal_in_file_manager(self, path: str) -> None:
        self.launch([str(Path(os.getenv("SystemRoot", r"C:\Windows")) / "explorer.exe"), f"/select,{path}"])

    def shutdown(self, delay: int = 10) -> None:
        self.launch([self._system32("shutdown.exe"), "/s", "/t", str(int(delay)), "/c", "Shutdown requested via Genesis"])

    def restart(self, delay: int = 10) -> None:
        self.launch([self._system32("shutdown.exe"), "/r", "/t", str(int(delay)), "/c", "Restart requested via Genesis"])

    def cancel_shutdown(self) -> None:
        self.launch([self._system32("shutdown.exe"), "/a"])

    def terminate_processes(self, names: list[str], timeout: float = 6.0) -> int:
        """Ask apps to close like clicking X (taskkill without /F sends WM_CLOSE); never force-kills."""
        procs = self.find_processes(names)
        if not procs:
            return 0
        images = sorted({(p.info.get("name") or "") for p in procs if p.info.get("name")})
        for image in images:
            subprocess.run([self._system32("taskkill.exe"), "/IM", image], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, shell=False, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if psutil is None:
            return len(procs)
        gone, _alive = psutil.wait_procs(procs, timeout=timeout)
        return len(gone)

    def lock(self) -> None:
        import ctypes

        if not ctypes.windll.user32.LockWorkStation():  # type: ignore[attr-defined]
            raise PlatformError("Windows refused to lock the workstation.")

    def sleep(self) -> None:
        import ctypes

        # SetSuspendState(hibernate=False, force=False, wakeupEventsDisabled=False)
        if not ctypes.windll.powrprof.SetSuspendState(0, 0, 0):  # type: ignore[attr-defined]
            raise PlatformError("Windows refused to enter sleep (it may be disabled by policy).")

    def media_key(self, key: str, times: int = 1) -> None:
        import ctypes

        codes = {"volume_up": 0xAF, "volume_down": 0xAE, "mute": 0xAD, "play_pause": 0xB3, "next": 0xB0, "prev": 0xB1}
        vk = codes.get(key)
        if vk is None:
            raise PlatformError(f"Unknown media key {key}")
        for _ in range(max(1, min(times, 50))):
            ctypes.windll.user32.keybd_event(vk, 0, 0, 0)  # type: ignore[attr-defined]
            ctypes.windll.user32.keybd_event(vk, 0, 2, 0)  # type: ignore[attr-defined]

    def list_windows(self) -> list[dict]:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        out: list[dict] = []
        fg = user32.GetForegroundWindow()
        proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def cb(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):  # skip hidden + owned (GW_OWNER=4)
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value.strip()
            if title and title not in ("Program Manager", "Settings", "Windows Input Experience"):
                out.append({"id": int(hwnd), "title": title, "active": hwnd == fg})
            return True

        user32.EnumWindows(proto(cb), 0)
        return out

    def window_action(self, window_id: int, action: str) -> None:
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        if not user32.IsWindow(window_id):
            raise PlatformError("That window no longer exists.")
        show = {"minimize": 6, "maximize": 3, "restore": 9, "focus": 9}  # SW_MINIMIZE, SW_MAXIMIZE, SW_RESTORE
        if action not in show:
            raise PlatformError(f"Unknown window action {action}")
        if action == "focus" and not user32.IsIconic(window_id):
            user32.ShowWindow(window_id, 5)  # SW_SHOW
        else:
            user32.ShowWindow(window_id, show[action])
        if action in ("focus", "maximize", "restore"):
            # Windows only lets the foreground process change focus; a zero-length ALT tap satisfies that rule
            user32.keybd_event(0x12, 0, 0, 0)
            user32.keybd_event(0x12, 0, 2, 0)
            user32.SetForegroundWindow(window_id)

    def minimize_all(self) -> None:
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        for vk, flag in ((0x5B, 0), (0x44, 0), (0x44, 2), (0x5B, 2)):  # Win + D (show desktop)
            user32.keybd_event(vk, 0, flag, 0)


class MacBackend(PlatformBackend):
    name = "macos"

    def open_path(self, path: str) -> None:
        self.launch(["/usr/bin/open", path])

    def shutdown(self, delay: int = 10) -> None:
        self.launch(["/usr/bin/osascript", "-e", 'tell app "System Events" to shut down'])

    def restart(self, delay: int = 10) -> None:
        self.launch(["/usr/bin/osascript", "-e", 'tell app "System Events" to restart'])

    def lock(self) -> None:
        self.launch(["/usr/bin/pmset", "displaysleepnow"])

    def sleep(self) -> None:
        self.launch(["/usr/bin/pmset", "sleepnow"])

    def cancel_shutdown(self) -> None:
        raise PlatformError("Cancelling a shutdown is not supported on macOS.")


class LinuxBackend(PlatformBackend):
    name = "linux"

    def open_path(self, path: str) -> None:
        opener = shutil.which("xdg-open") or shutil.which("gio")
        if not opener:
            raise PlatformError("No desktop opener (xdg-open) available.")
        self.launch([opener, path] if opener.endswith("xdg-open") else [opener, "open", path])

    def shutdown(self, delay: int = 10) -> None:
        self.launch(["systemctl", "poweroff"])

    def restart(self, delay: int = 10) -> None:
        self.launch(["systemctl", "reboot"])

    def lock(self) -> None:
        self.launch(["loginctl", "lock-session"])

    def sleep(self) -> None:
        self.launch(["systemctl", "suspend"])

    def cancel_shutdown(self) -> None:
        self.launch(["shutdown", "-c"])


class RecordingBackend(PlatformBackend):
    """Records every action instead of performing it (tests / dry-run demos)."""

    name = "recording"

    def __init__(self, existing_processes: Optional[list[str]] = None, fail_launch: bool = False) -> None:
        self.actions: list[tuple] = []
        self.existing = [p.lower() for p in (existing_processes or [])]
        self.fail_launch = fail_launch
        self._next_pid = 1000
        self.windows: list[dict] = [{"id": 101, "title": "Inbox - Gmail - Google Chrome", "active": True},
                                    {"id": 102, "title": "genesis - Visual Studio Code", "active": False},
                                    {"id": 103, "title": "Untitled - Notepad", "active": False}]

    def launch(self, argv: list[str], cwd: Optional[str] = None) -> int:
        if self.fail_launch:
            raise PlatformError(f"Program not found: {argv[0]}")
        if not isinstance(argv, list):
            raise PlatformError("launch() requires an argument list")
        self.actions.append(("launch", tuple(argv), cwd))
        self._next_pid += 1
        return self._next_pid

    def open_path(self, path: str) -> None:
        self.actions.append(("open_path", path))

    def open_uri(self, uri: str) -> None:
        self.actions.append(("open_uri", uri))

    def open_url(self, url: str) -> bool:
        self.actions.append(("open_url", url))
        return True

    def reveal_in_file_manager(self, path: str) -> None:
        self.actions.append(("reveal", path))

    def find_processes(self, names: list[str]) -> list:
        return [n for n in names if n.lower() in self.existing]

    def terminate_processes(self, names: list[str], timeout: float = 5.0) -> int:
        hit = self.find_processes(names)
        self.actions.append(("terminate", tuple(hit)))
        self.existing = [p for p in self.existing if p not in {h.lower() for h in hit}]
        return len(hit)

    def process_alive(self, pid: int) -> bool:
        return True

    def shutdown(self, delay: int = 10) -> None:
        self.actions.append(("shutdown", delay))

    def restart(self, delay: int = 10) -> None:
        self.actions.append(("restart", delay))

    def lock(self) -> None:
        self.actions.append(("lock",))

    def sleep(self) -> None:
        self.actions.append(("sleep",))

    def cancel_shutdown(self) -> None:
        self.actions.append(("cancel_shutdown",))

    def media_key(self, key: str, times: int = 1) -> None:
        self.actions.append(("media_key", key, times))

    def system_info(self) -> dict:
        return {"platform": "test", "cpu_percent": 12.0, "memory_percent": 40.0, "battery_percent": 80, "plugged_in": True,
                "disk_free_gb": 100.0}

    def list_windows(self) -> list[dict]:
        return [dict(w) for w in self.windows]

    def window_action(self, window_id: int, action: str) -> None:
        if not any(w["id"] == window_id for w in self.windows):
            raise PlatformError("That window no longer exists.")
        self.actions.append(("window", window_id, action))
        if action in ("focus", "maximize", "restore"):
            for w in self.windows:
                w["active"] = w["id"] == window_id

    def minimize_all(self) -> None:
        self.actions.append(("minimize_all",))

    def names_of(self, kind: str) -> list[tuple]:
        return [a for a in self.actions if a[0] == kind]


class DryRunPowerBackend(PlatformBackend):
    """Delegates everything to *inner* except power actions, which are only logged."""

    dry_run_power = True

    def __init__(self, inner: PlatformBackend) -> None:
        self.inner = inner
        self.name = f"{inner.name}+dry-run"
        self.simulated: list[str] = []

    def __getattr__(self, item):  # delegate the rest
        return getattr(self.inner, item)

    def launch(self, argv, cwd=None):
        return self.inner.launch(argv, cwd)

    def open_path(self, path):
        return self.inner.open_path(path)

    def open_uri(self, uri):
        return self.inner.open_uri(uri)

    def open_url(self, url):
        return self.inner.open_url(url)

    def reveal_in_file_manager(self, path):
        return self.inner.reveal_in_file_manager(path)

    def find_processes(self, names):
        return self.inner.find_processes(names)

    def terminate_processes(self, names, timeout=5.0):
        return self.inner.terminate_processes(names, timeout)

    def process_alive(self, pid):
        return self.inner.process_alive(pid)

    def media_key(self, key, times=1):
        return self.inner.media_key(key, times)

    def system_info(self):
        return self.inner.system_info()

    def list_windows(self):
        return self.inner.list_windows()

    def window_action(self, window_id, action):
        return self.inner.window_action(window_id, action)

    def minimize_all(self):
        return self.inner.minimize_all()

    def _sim(self, what: str) -> None:
        log.warning("DRY RUN: %s simulated (GENESIS_SYSTEM_DRY_RUN=1)", what)
        self.simulated.append(what)

    def shutdown(self, delay: int = 10) -> None:
        self._sim("shutdown")

    def restart(self, delay: int = 10) -> None:
        self._sim("restart")

    def lock(self) -> None:
        self._sim("lock")

    def sleep(self) -> None:
        self._sim("sleep")

    def cancel_shutdown(self) -> None:
        self._sim("cancel_shutdown")


def create_backend(dry_run_power: bool = False) -> PlatformBackend:
    if sys.platform.startswith("win"):
        backend: PlatformBackend = WindowsBackend()
    elif sys.platform == "darwin":
        backend = MacBackend()
    else:
        backend = LinuxBackend()
    return DryRunPowerBackend(backend) if dry_run_power else backend
