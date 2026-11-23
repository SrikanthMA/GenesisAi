"""Google Chrome with your Google account, controlled by Genesis (2.2) - used for Google Colab.

1.x drove Colab in a Playwright-launched Edge window. Google often refuses to sign in inside a browser that was started
*by* automation software, and that window wasn't the user's Google account anyway. 2.2 instead starts the real Google
Chrome installed on the PC, as a normal program, with:

* its own profile folder (``data/chrome-profile``) - you sign in to your Google account there **once**; it stays signed in.
  (Chrome doesn't allow automation of your everyday profile, so a separate one is required; your normal Chrome is untouched.)
* a local control port (127.0.0.1 only) that Genesis connects to with Playwright (``connect_over_cdp``).

Genesis never types or reads your password: you sign in yourself. Settings: ``GENESIS_COLAB_BROWSER=chrome|edge``
(default chrome when Chrome is installed), ``GENESIS_CHROME_PATH``, ``GENESIS_CHROME_PORT`` (default 9333).

It exposes the same small interface the Colab driver already uses (``available``, ``run``, ``page_for``).
"""
from __future__ import annotations

import concurrent.futures
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

from ..core.logging_setup import get_logger

log = get_logger("v2.chrome")
DEFAULT_PORT = 9333


def find_chrome() -> Optional[str]:
    env = (os.getenv("GENESIS_CHROME_PATH") or "").strip().strip('"')
    if env and Path(env).is_file():
        return env
    roots = [os.getenv("ProgramFiles"), os.getenv("ProgramFiles(x86)"), os.getenv("LOCALAPPDATA"), os.getenv("ProgramW6432")]
    for root in roots:
        if root:
            p = Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if p.is_file():
                return str(p)
    return None


def port() -> int:
    try:
        return int(os.getenv("GENESIS_CHROME_PORT", "") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


def launch_args(chrome: str, profile_dir: Path, url: str, cdp_port: int) -> list[str]:
    return [chrome, f"--remote-debugging-port={cdp_port}",
            f"--user-data-dir={profile_dir}", "--no-first-run", "--no-default-browser-check", "--new-window", url]


def _port_open(p: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", p), timeout=timeout):
            return True
    except OSError:
        return False


class ChromeCDP:
    """The user's Google Chrome (dedicated Genesis profile) driven over the DevTools protocol on 127.0.0.1."""

    name = "chrome"

    def __init__(self, profile_dir: Path, chrome_path: Optional[str] = None, cdp_port: Optional[int] = None,
                 popen: Callable[..., Any] = subprocess.Popen) -> None:
        self.profile_dir = Path(profile_dir)
        self.chrome = chrome_path or find_chrome()
        self.port = cdp_port or port()
        self._popen = popen
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="genesis-chrome")
        self._pw = None
        self._browser = None
        self.launch_error: Optional[str] = None

    def available(self) -> bool:
        if not self.chrome or self.launch_error:
            return False
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:
            return False
        return True

    def _call(self, fn: Callable[[], Any], timeout: float = 60.0) -> Any:
        return self._executor.submit(fn).result(timeout=timeout)

    def start_chrome(self, url: str) -> None:
        """Start Chrome with the Genesis profile (no-op when it's already running with the control port)."""
        if _port_open(self.port):
            return
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._popen(launch_args(self.chrome, self.profile_dir, url, self.port), close_fds=True)
        for _ in range(60):  # up to ~15 s for a cold start
            if _port_open(self.port):
                return
            time.sleep(0.25)
        raise RuntimeError("Chrome didn't open its control port")

    def _ensure(self, url: str) -> Any:
        from ..services.browser import BrowserUnavailable

        if self._browser is not None:
            try:
                if self._browser.is_connected():
                    return self._browser
            except Exception:
                pass
            self._browser = None
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserUnavailable("Controlling Chrome needs Playwright: run Update_Genesis.bat (or pip install playwright).") from exc
        try:
            self.start_chrome(url)
            if self._pw is None:
                self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{self.port}", timeout=20_000)
        except Exception as exc:
            self.launch_error = None  # transient: allow a retry next time
            log.warning("couldn't connect to Chrome: %s", str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__)
            raise BrowserUnavailable("I couldn't open Google Chrome for Colab. Close any Chrome window I opened before and "
                                     "try again.") from exc
        return self._browser

    def _context(self, url: str) -> Any:
        b = self._ensure(url)
        return b.contexts[0] if b.contexts else b.new_context()

    def run(self, fn: Callable[[Any], Any], timeout: float = 60.0) -> Any:
        """Run ``fn(page)`` on the Chrome thread (same contract as BrowserController.run)."""
        return self._call(lambda: fn(None), timeout=timeout)

    def page_for(self, url_prefix: str, open_url: str) -> Any:
        """On the Chrome thread: a tab already showing ``url_prefix`` (brought to front), else a tab opened at ``open_url``."""
        ctx = self._context(open_url)
        for p in ctx.pages:
            if (p.url or "").startswith(url_prefix):
                p.bring_to_front()
                return p
        for p in ctx.pages:  # reuse the blank tab Chrome opened at start-up
            if (p.url or "") in ("", "about:blank", "chrome://newtab/", "chrome://new-tab-page/"):
                p.goto(open_url, wait_until="domcontentloaded", timeout=45_000)
                p.bring_to_front()
                return p
        p = ctx.new_page()
        p.goto(open_url, wait_until="domcontentloaded", timeout=45_000)
        p.bring_to_front()
        return p


def colab_browser(svc: Any) -> Optional[ChromeCDP]:
    """Container hook: the browser Colab should use (None = keep 1.x's automated Edge window)."""
    choice = (os.getenv("GENESIS_COLAB_BROWSER", "") or "chrome").strip().lower()
    if choice not in ("chrome", "auto"):
        return None
    chrome = find_chrome()
    if not chrome:
        return None
    profile = Path(os.getenv("GENESIS_CHROME_PROFILE", "") or (svc.config.paths.data_dir / "chrome-profile"))
    return ChromeCDP(profile, chrome)
