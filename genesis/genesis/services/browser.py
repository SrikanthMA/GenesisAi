"""Visible browser automation (Playwright), with a safe fallback.

When Playwright is installed (``pip install playwright`` then
``playwright install chromium``), Genesis drives a *visible* Chromium window
so the user can watch every action. All Playwright calls run on one dedicated
thread (Playwright's sync API is thread-bound).

Without Playwright, navigation falls back to the default browser and reading
falls back to an HTTP fetch; click/fill report that automation is unavailable
instead of pretending to succeed.
"""
from __future__ import annotations

import concurrent.futures
from pathlib import Path
from typing import Any, Callable, Optional

from ..core.logging_setup import get_logger

log = get_logger("browser")


class BrowserUnavailable(RuntimeError):
    pass


class BrowserController:
    def __init__(self, profile_dir: Path) -> None:
        self.profile_dir = profile_dir
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="genesis-browser")
        self._pw = None
        self._ctx = None
        self._page = None

    @staticmethod
    def available() -> bool:
        try:
            import playwright.sync_api  # noqa: F401

            return True
        except ImportError:
            return False

    def _call(self, fn: Callable[[], Any], timeout: float = 60.0) -> Any:
        return self._executor.submit(fn).result(timeout=timeout)

    def _ensure(self) -> Any:
        if self._page is not None and not self._page.is_closed():
            return self._page
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserUnavailable(
                "Browser automation needs Playwright: run 'pip install playwright' and 'playwright install chromium'."
            ) from exc
        if self._pw is None:
            self._pw = sync_playwright().start()
        if self._ctx is None:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            launch_kw = {"headless": False, "viewport": None}
            try:
                self._ctx = self._pw.chromium.launch_persistent_context(str(self.profile_dir), channel="msedge", **launch_kw)
            except Exception:
                self._ctx = self._pw.chromium.launch_persistent_context(str(self.profile_dir), **launch_kw)
        pages = self._ctx.pages
        self._page = pages[0] if pages else self._ctx.new_page()
        return self._page

    # ---------------------------------------------------------------- actions
    def goto(self, url: str) -> dict:
        def _do():
            page = self._ensure()
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            return {"url": page.url, "title": page.title()}
        return self._call(_do)

    def read(self) -> dict:
        def _do():
            page = self._ensure()
            text = page.inner_text("body", timeout=15_000)
            return {"url": page.url, "title": page.title(), "text": text}
        return self._call(_do)

    def current_url(self) -> Optional[str]:
        if self._page is None:
            return None
        try:
            return self._call(lambda: self._page.url, timeout=5)
        except Exception:
            return None

    def click(self, target: str) -> dict:
        def _do():
            page = self._ensure()
            for locator in (page.get_by_role("button", name=target), page.get_by_role("link", name=target),
                            page.get_by_text(target, exact=False)):
                try:
                    if locator.count() > 0:
                        locator.first.click(timeout=8_000)
                        page.wait_for_load_state("domcontentloaded", timeout=15_000)
                        return {"clicked": target, "url": page.url}
                except Exception:
                    continue
            raise BrowserUnavailable(f"I couldn't find anything called '{target}' to click on this page.")
        return self._call(_do)

    def fill(self, target: str, value: str) -> dict:
        def _do():
            page = self._ensure()
            for locator in (page.get_by_label(target), page.get_by_placeholder(target), page.get_by_role("textbox", name=target)):
                try:
                    if locator.count() > 0:
                        locator.first.fill(value, timeout=8_000)
                        return {"field": target}
                except Exception:
                    continue
            raise BrowserUnavailable(f"I couldn't find a field called '{target}' on this page.")
        return self._call(_do)

    def close(self) -> None:
        def _do():
            try:
                if self._ctx:
                    self._ctx.close()
                if self._pw:
                    self._pw.stop()
            finally:
                self._ctx = self._pw = self._page = None
        try:
            self._call(_do, timeout=10)
        except Exception:
            pass
        self._executor.shutdown(wait=False)
