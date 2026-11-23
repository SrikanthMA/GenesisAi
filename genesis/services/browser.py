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
import os
import re
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
        self.launch_error: Optional[str] = None  # set when the automated browser failed to start (then treated as unavailable)

    def available(self) -> bool:
        if self.launch_error:
            return False
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
            # visible by default so you can watch; GENESIS_BROWSER_HEADLESS=1 hides it (servers/CI)
            launch_kw = {"headless": os.getenv("GENESIS_BROWSER_HEADLESS", "") == "1", "viewport": None}
            try:
                self._ctx = self._pw.chromium.launch_persistent_context(str(self.profile_dir), channel="msedge", **launch_kw)
            except Exception:
                try:
                    self._ctx = self._pw.chromium.launch_persistent_context(str(self.profile_dir), **launch_kw)
                except Exception as exc:
                    self.launch_error = str(exc).splitlines()[0][:200]
                    log.warning("automated browser could not start: %s", self.launch_error)
                    raise BrowserUnavailable("My automated browser couldn't start. Run “playwright install chromium” "
                                             "(or install Microsoft Edge) and try again.") from exc
        pages = self._ctx.pages
        self._page = pages[0] if pages else self._ctx.new_page()
        return self._page

    def run(self, fn: Callable[[Any], Any], timeout: float = 60.0) -> Any:
        """Run ``fn(page)`` on the browser thread (used by the WhatsApp / Colab drivers)."""
        return self._call(lambda: fn(self._ensure()), timeout=timeout)

    def page_for(self, url_prefix: str, open_url: str) -> Any:
        """Must be called on the browser thread: a tab already showing ``url_prefix`` (reused), else navigate to ``open_url``."""
        page = self._ensure()
        for p in self._ctx.pages:
            if (p.url or "").startswith(url_prefix):
                p.bring_to_front()
                self._page = p
                return p
        if (page.url or "").startswith(("about:blank", "chrome://newtab", "edge://newtab")) or page.url == "":
            page.goto(open_url, wait_until="domcontentloaded", timeout=45_000)
            return page
        page = self._ctx.new_page()
        page.goto(open_url, wait_until="domcontentloaded", timeout=45_000)
        self._page = page
        return page

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

    # ---------------------------------------------------------------- semantic page model
    _ELEMENTS_JS = """
    (limit) => {
      const sel = 'a[href],button,input:not([type=hidden]),textarea,select,[role=button],[role=link],[role=tab],[role=menuitem],' +
                  '[role=checkbox],[role=option],summary,h1,h2,h3';
      const out = [];
      let i = 0;
      for (const el of document.querySelectorAll(sel)) {
        const r = el.getBoundingClientRect();
        const st = window.getComputedStyle(el);
        if (r.width < 2 || r.height < 2 || st.visibility === 'hidden' || st.display === 'none') continue;
        if (r.bottom < 0 || r.top > window.innerHeight * 3) continue;
        const tag = el.tagName.toLowerCase();
        const role = el.getAttribute('role') || (tag === 'a' ? 'link' : tag === 'button' || tag === 'summary' ? 'button' :
                     /^h[1-3]$/.test(tag) ? 'heading' : tag === 'select' ? 'select' : 'field');
        const name = (el.getAttribute('aria-label') || el.innerText || el.getAttribute('placeholder') || el.value ||
                      el.getAttribute('title') || el.getAttribute('name') || '').trim().replace(/\s+/g, ' ').slice(0, 120);
        if (!name) continue;
        i += 1;
        el.setAttribute('data-genesis-id', String(i));
        out.push({id: i, role: role, name: name, href: el.getAttribute('href') || '', in_view: r.top >= 0 && r.top < window.innerHeight});
        if (out.length >= limit) break;
      }
      return out;
    }"""

    def elements(self, limit: int = 80) -> dict:
        """Visible, named interactive elements (links, buttons, fields, headings) — the page as the agent sees it."""
        def _do():
            page = self._ensure()
            items = page.evaluate(self._ELEMENTS_JS, limit)
            return {"url": page.url, "title": page.title(), "elements": items}
        return self._call(_do)

    def click_element(self, element_id: int) -> dict:
        def _do():
            page = self._ensure()
            loc = page.locator(f'[data-genesis-id="{int(element_id)}"]')
            if loc.count() == 0:
                raise BrowserUnavailable("That element isn't on the page any more (it may have changed) — let me look again.")
            loc.first.click(timeout=8_000)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15_000)
            except Exception:
                pass
            return {"url": page.url, "title": page.title()}
        return self._call(_do)

    def scroll(self, direction: str = "down", amount: int = 1) -> dict:
        def _do():
            page = self._ensure()
            dy = {"down": 1, "up": -1}.get(direction, 1) * 700 * max(1, min(amount, 10))
            if direction in ("top", "bottom"):
                page.evaluate("d => window.scrollTo(0, d === 'top' ? 0 : document.body.scrollHeight)", direction)
            else:
                page.mouse.wheel(0, dy)
            pos = page.evaluate("() => [window.scrollY, document.body.scrollHeight, window.innerHeight]")
            return {"url": page.url, "scroll_y": pos[0], "at_bottom": pos[0] + pos[2] >= pos[1] - 5}
        return self._call(_do)

    def press(self, key: str) -> dict:
        def _do():
            page = self._ensure()
            page.keyboard.press(key)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10_000)
            except Exception:
                pass
            return {"url": page.url, "title": page.title()}
        return self._call(_do)

    def go_back(self) -> dict:
        def _do():
            page = self._ensure()
            page.go_back(wait_until="domcontentloaded", timeout=15_000)
            return {"url": page.url, "title": page.title()}
        return self._call(_do)

    # ---------------------------------------------------------------- forms
    _FIELDS_JS = """
    () => {
      const labelOf = (el) => {
        if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
        if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) return l.innerText; }
        const wrap = el.closest('label'); if (wrap) return wrap.innerText;
        const by = el.getAttribute('aria-labelledby');
        if (by) { const l = document.getElementById(by.split(' ')[0]); if (l) return l.innerText; }
        let p = el.parentElement;
        for (let i = 0; i < 3 && p; i++, p = p.parentElement) {
          const t = Array.from(p.querySelectorAll('label, legend, [class*="label"], [class*="question"]'))
                      .map(x => x.innerText).find(x => x && x.trim().length > 1);
          if (t) return t;
        }
        return el.getAttribute('placeholder') || el.getAttribute('name') || '';
      };
      const out = [];
      let i = 0;
      for (const el of document.querySelectorAll('input, textarea, select')) {
        const type = el.tagName === 'INPUT' ? (el.getAttribute('type') || 'text').toLowerCase() : el.tagName.toLowerCase();
        if (['hidden', 'submit', 'button', 'image', 'reset', 'file'].includes(type)) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 2 || r.height < 2 || el.disabled) continue;
        i += 1;
        el.setAttribute('data-genesis-field', String(i));
        const opts = el.tagName === 'SELECT' ? Array.from(el.options).map(o => o.text.trim()).filter(Boolean).slice(0, 40) : [];
        out.push({id: i, label: labelOf(el).trim().replace(/\s+/g, ' ').slice(0, 160), type: type, name: el.getAttribute('name') || '',
                  required: el.required || el.getAttribute('aria-required') === 'true', value: (el.type === 'password') ? '' :
                  (el.type === 'checkbox' || el.type === 'radio') ? String(el.checked) : (el.value || '').slice(0, 300), options: opts});
        if (out.length >= 80) break;
      }
      return out;
    }"""

    def form_fields(self) -> dict:
        def _do():
            page = self._ensure()
            return {"url": page.url, "title": page.title(), "fields": page.evaluate(self._FIELDS_JS)}
        return self._call(_do)

    def fill_field(self, field_id: int, value: str, ftype: str = "text") -> None:
        def _do():
            page = self._ensure()
            loc = page.locator(f'[data-genesis-field="{int(field_id)}"]')
            if loc.count() == 0:
                raise BrowserUnavailable("That field isn't on the page any more.")
            el = loc.first
            if ftype == "select":
                try:
                    el.select_option(label=value, timeout=5_000)
                except Exception:
                    el.select_option(value=value, timeout=5_000)
            elif ftype in ("checkbox", "radio"):
                el.set_checked(str(value).lower() in ("true", "yes", "1", "on"), timeout=5_000)
            else:
                el.fill(value, timeout=8_000)
        self._call(_do)

    def field_value(self, field_id: int) -> str:
        def _do():
            loc = self._ensure().locator(f'[data-genesis-field="{int(field_id)}"]')
            return loc.first.input_value(timeout=3_000) if loc.count() else ""
        return self._call(_do)

    def submit_form(self) -> dict:
        def _do():
            page = self._ensure()
            before = page.url
            btn = None
            for loc in (page.locator('button[type="submit"], input[type="submit"]'),
                        page.get_by_role("button", name=re.compile(r"^(submit|apply|send|continue|next|finish)", re.I))):
                try:
                    if loc.count() > 0:
                        btn = loc.first
                        break
                except Exception:
                    continue
            if btn is None:
                raise BrowserUnavailable("I can't find the form's Submit button.")
            label = (btn.inner_text(timeout=2_000) or btn.get_attribute("value") or "Submit").strip()
            btn.click(timeout=8_000)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15_000)
            except Exception:
                pass
            page.wait_for_timeout(800)
            body = page.inner_text("body", timeout=8_000)[:3000]
            return {"button": label, "url_before": before, "url": page.url, "title": page.title(), "text": body}
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
