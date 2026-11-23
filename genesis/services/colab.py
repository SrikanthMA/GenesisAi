"""Google Colab automation: open/create notebooks, write code into cells, run them, read output and errors.

Colab runs code on Google's machines, not on this PC, but Genesis still never runs notebook code blindly:
:func:`notebook_scan` flags destructive or suspicious code (deleting Drive files, piping downloads into a shell,
crypto-mining, exfiltrating credentials …) and the run tool then requires explicit confirmation.

Driver: :class:`WebColab` controls colab.research.google.com in Genesis's automated browser (Playwright). You sign in to
Google in that window once; Genesis never handles your password. Without Playwright, Genesis can only open Colab in
your normal browser and says so.
"""
from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from typing import Any, Optional

from ..core.logging_setup import get_logger

log = get_logger("colab")
HOME = "https://colab.research.google.com/"
NEW = "https://colab.research.google.com/#create=true"

_NOTEBOOK_DANGER = [
    (r"(?:^|\n)\s*[!%]\s*rm\s+-[a-z]*r[a-z]*f?\s+(?:/|~|/content/drive\b|\*)", "deletes files or folders (shell rm -rf)"),
    (r"shutil\.rmtree\(\s*['\"]/(?:content/drive|$|root|home)", "deletes a whole folder tree on Drive/the machine"),
    (r"/content/drive\b.*\b(?:os\.remove|unlink|rmtree)|\b(?:os\.remove|unlink|rmtree)\b.*?/content/drive", "deletes files on your Google Drive"),
    (r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:ba)?sh\b", "downloads a script and runs it in a shell"),
    (r"\b(?:xmrig|minerd|cpuminer|ethminer|nicehash|stratum\+tcp)\b", "looks like crypto-currency mining (against Colab's rules)"),
    (r"\.ssh\b|id_rsa|\bcredentials?\.json\b|\.aws[/\\]credentials|google\.colab\s+import\s+auth.*requests\.post", "touches credentials/keys"),
    (r"requests\.(?:post|put)\([^)]*(?:drive|token|secret|password)", "uploads private data to a web address"),
    (r"(?:^|\n)\s*[!%]\s*(?:ngrok|nc\s+-l|ssh\s+-R)|\bpyngrok\b|\bcloudflared\b", "opens a remote-access tunnel"),
    (r"\bfork\s*\(\s*\)\s*while|while\s+True\s*:\s*os\.fork", "fork bomb"),
    (r"(?:os\.system|os\.popen|subprocess\.\w+|get_ipython\(\)\.system)\s*\([^)]*\brm\b[^)]*-[a-z]*r", "deletes files through a shell command"),
    (r"(?:os\.system|subprocess\.\w+)\s*\([^)]*(?:curl|wget)[^)]*\|\s*(?:ba)?sh", "downloads a script and runs it in a shell"),
    (r"\b(?:os\.remove|os\.unlink|os\.rmdir|shutil\.rmtree|Path\([^)]*\)\.unlink)\b", "deletes files"),
    (r"%%(?:bash|sh|script)\b[\s\S]*\brm\s+-[a-z]*r", "deletes files in a shell cell"),
]
# code that reaches outside the notebook (Drive, shell, network) — ask before running it when an AI wrote it
_REACHES_OUT = re.compile(r"/content/drive|drive\.mount|(?:^|\n)\s*[!%]|%%\w+|\bos\.system|\bsubprocess\b|\brequests\.(?:post|put|delete)|"
                          r"\burllib\b|\bsocket\b|\bsmtplib\b|\bpaperspace\b|\bgoogle\.colab\s+import\s+(?:auth|drive)|"
                          r"\.unlink\(|\brmtree\b|\bshutil\.|\bos\.(?:remove|rename|replace|rmdir|chmod)\b|__import__|\bexec\(|\beval\(|"
                          r"\bcompile\(|\bimportlib\b|\.write_(?:text|bytes)\(|\bopen\([^)]*['\"][wax]b?\+?['\"]|\bctypes\b|\bpickle\.loads?\(", re.I)


def reaches_out(code: str) -> bool:
    return bool(_REACHES_OUT.search(code or ""))


def notebook_scan(code: str) -> list[str]:
    found = []
    for rx, why in _NOTEBOOK_DANGER:
        if re.search(rx, code or "", re.I | re.S):
            found.append(why)
    return sorted(set(found))


def error_summary(output: str) -> Optional[dict]:
    """Pull the exception out of a cell's output: {"type","message","line","traceback"}."""
    if not output:
        return None
    matches = list(re.finditer(r"^(?P<type>[A-Za-z_][\w.]*(?:Error|Exception|Interrupt|Exit|Warning))\s*:\s*(?P<msg>.*)$", output, re.M))
    m = matches[-1] if matches else None
    if m is None:
        if "Traceback (most recent call last)" in output:
            return {"type": "Error", "message": output.strip().splitlines()[-1][:300], "line": None, "traceback": output[-2000:]}
        return None
    line = None
    lm = re.findall(r"line (\d+)", output)
    if lm:
        line = int(lm[-1])
    arrow = re.findall(r"^-+>\s*(\d+)", output, re.M)  # IPython style "----> 3 x = 1/0"
    if arrow:
        line = int(arrow[-1])
    return {"type": m.group("type").split(".")[-1], "message": m.group("msg").strip()[:400], "line": line, "traceback": output[-2000:]}


class ColabError(RuntimeError):
    def __init__(self, message: str, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind  # unavailable | signin | no_notebook | timeout | error


class ColabDriver(ABC):
    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def open(self) -> dict: ...  # {"signed_in", "title", "url"}

    @abstractmethod
    def new_notebook(self) -> dict: ...  # {"title", "url"}

    @abstractmethod
    def set_code(self, code: str, new_cell: bool) -> int: ...  # returns the cell index (1-based)

    @abstractmethod
    def run(self, timeout: float) -> dict: ...  # {"output", "finished"}

    @abstractmethod
    def cell_code(self) -> str: ...

    @abstractmethod
    def notebook(self) -> dict: ...  # {"title","url","cells": int}


_JS_INFO = """() => {
  const signin = !!Array.from(document.querySelectorAll('a,button')).find(e => /^\\s*sign in\\s*$/i.test(e.innerText || ''));
  const cells = document.querySelectorAll('.cell.code').length;
  const title = (document.querySelector('#doc-name, .notebook-name input') || {}).value || document.title || '';
  return {signed_in: !signin, cells: cells, title: title.replace(/ - Colab$/, ''), url: location.href};
}"""
_JS_FOCUSED_OUTPUT = """() => {
  const cell = document.querySelector('.cell.code.focused') || Array.from(document.querySelectorAll('.cell.code')).pop();
  if (!cell) return {output: '', running: false};
  const out = cell.querySelector('.output, .outputview, colab-static-output-renderer');
  let text = out ? (out.innerText || '') : '';
  const running = !!cell.querySelector('.running, [running], colab-run-button[status="running"]') || cell.classList.contains('running');
  return {output: text, running: running};
}"""
_JS_CELL_CODE = """() => {
  const cell = document.querySelector('.cell.code.focused') || Array.from(document.querySelectorAll('.cell.code')).pop();
  if (!cell) return '';
  const lines = cell.querySelectorAll('.view-line');
  return Array.from(lines).map(l => l.innerText.replace(/\\u00a0/g, ' ')).join('\\n');
}"""


class WebColab(ColabDriver):
    def __init__(self, browser: Any) -> None:
        self.browser = browser

    def available(self) -> bool:
        return bool(self.browser is not None and self.browser.available())

    def _page(self, fn, timeout: float = 90.0, url: str = HOME):
        return self.browser.run(lambda _p: fn(self.browser.page_for(HOME, url)), timeout=timeout)

    def open(self) -> dict:
        return self._page(lambda page: (page.wait_for_timeout(1500), page.evaluate(_JS_INFO))[1])

    def new_notebook(self) -> dict:
        def fn(page):
            page.goto(NEW, wait_until="domcontentloaded", timeout=45_000)
            for _ in range(40):
                info = page.evaluate(_JS_INFO)
                if not info["signed_in"]:
                    raise ColabError("Please sign in to Google in the Colab window I opened, then ask again. "
                                     "(I never type your password.)", "signin")
                if info["cells"] > 0:
                    return info
                page.wait_for_timeout(500)
            raise ColabError("Colab didn't create the notebook in time.", "timeout")
        return self._page(fn, url=NEW)

    def _focus_cell(self, page, new_cell: bool) -> None:
        cells = page.locator(".cell.code")
        if cells.count() == 0:
            raise ColabError("There's no notebook open in Colab yet.", "no_notebook")
        self._click_editor(cells.last)
        if new_cell and self.cell_code_on(page).strip():
            page.keyboard.press("Escape")
            page.keyboard.press("Control+M")
            page.keyboard.press("B")  # insert a code cell below
            page.wait_for_timeout(400)
            self._click_editor(page.locator(".cell.code").last)

    @staticmethod
    def _click_editor(cell) -> None:
        """Put the keyboard focus inside the cell's code editor (Monaco keeps a hidden <textarea> for input)."""
        cell.locator(".monaco-editor, .editor").first.click()
        area = cell.locator("textarea")
        if area.count() > 0:
            area.first.focus()

    @staticmethod
    def cell_code_on(page) -> str:
        return page.evaluate(_JS_CELL_CODE) or ""

    def set_code(self, code: str, new_cell: bool) -> int:
        def fn(page):
            self._focus_cell(page, new_cell)
            page.keyboard.press("Control+A")
            page.keyboard.press("Delete")
            page.keyboard.insert_text(code)  # inserted as-is (no auto-indent / auto-closing brackets)
            page.wait_for_timeout(200)
            return page.locator(".cell.code").count()
        return self._page(fn)

    def run(self, timeout: float) -> dict:
        def fn(page):
            page.keyboard.press("Control+Enter")
            t0 = time.time()
            idle = 0
            state = {"output": "", "running": True}
            while time.time() - t0 < timeout:
                page.wait_for_timeout(500)
                state = page.evaluate(_JS_FOCUSED_OUTPUT)
                idle = 0 if state["running"] else idle + 1
                if idle >= 3 and time.time() - t0 > 1.5:
                    return {"output": state["output"], "finished": True}
            return {"output": state["output"], "finished": False}
        return self._page(fn, timeout=timeout + 30)

    def cell_code(self) -> str:
        return self._page(self.cell_code_on)

    def notebook(self) -> dict:
        return self._page(lambda page: page.evaluate(_JS_INFO))


class ColabService:
    def __init__(self, browser: Any = None, backend: Any = None, *, driver: Optional[ColabDriver] = None) -> None:
        self.backend = backend
        self.driver = driver or WebColab(browser)
        self.last_code = ""
        self.last_run: Optional[dict] = None
        self.ai_written = False  # the current cell's code was generated by the model (not typed/dictated by the user)

    def automation_available(self) -> bool:
        return self.driver.available()

    def _call(self, fn, *args):
        try:
            return fn(*args)
        except ColabError:
            raise
        except Exception as exc:
            from .browser import BrowserUnavailable

            log.warning("colab driver error: %s", type(exc).__name__)
            if isinstance(exc, BrowserUnavailable):
                raise ColabError(str(exc), "unavailable") from exc
            raise ColabError(f"Colab didn't respond as expected ({type(exc).__name__}). Please check the Colab window.", "error") from exc

    def _need(self) -> None:
        if not self.driver.available():
            raise ColabError("To write and run Colab cells for you I need browser automation: pip install playwright, then "
                             "playwright install chromium. I can still open Colab in your browser.", "unavailable")

    def open(self) -> dict:
        if self.driver.available():
            try:
                return dict(self._call(self.driver.open), automated=True, opened=True)
            except ColabError as exc:
                if exc.kind != "unavailable":
                    raise
        ok = self.backend.open_url(HOME) if self.backend is not None else False
        return {"signed_in": None, "automated": False, "opened": bool(ok), "url": HOME}

    def create_notebook(self) -> dict:
        self._need()
        return self._call(self.driver.new_notebook)

    def write(self, code: str, new_cell: bool = True, ai_written: bool = False) -> dict:
        self._need()
        self.ai_written = ai_written
        idx = self._call(self.driver.set_code, code, new_cell)
        typed = self._call(self.driver.cell_code)
        self.last_code = code
        return {"cell": idx, "verified": _same_code(typed, code), "chars": len(code)}

    def run(self, timeout: float = 120.0) -> dict:
        self._need()
        code = self._call(self.driver.cell_code)
        res = self._call(self.driver.run, timeout)
        err = error_summary(res.get("output", ""))
        self.last_code = code or self.last_code
        self.last_run = {"code": self.last_code, "output": res.get("output", ""), "finished": res.get("finished", False),
                         "error": err, "status": "error" if err else ("ok" if res.get("finished") else "timeout")}
        return dict(self.last_run)


def _same_code(a: str, b: str) -> bool:
    norm = lambda s: [ln.rstrip() for ln in (s or "").replace("\r\n", "\n").strip().splitlines()]  # noqa: E731
    return norm(a) == norm(b)


class FakeColabDriver(ColabDriver):
    """In-memory Colab used by tests: executes nothing; outputs are scripted per code snippet."""

    def __init__(self, outputs: Optional[dict[str, str]] = None, signed_in: bool = True) -> None:
        self.outputs = outputs or {}
        self.signed_in = signed_in
        self.cells: list[str] = []
        self.title = ""
        self.runs: list[str] = []

    def available(self) -> bool:
        return True

    def open(self) -> dict:
        return {"signed_in": self.signed_in, "title": self.title, "url": HOME, "cells": len(self.cells)}

    def new_notebook(self) -> dict:
        if not self.signed_in:
            raise ColabError("Please sign in to Google in the Colab window I opened, then ask again.", "signin")
        self.title, self.cells = "Untitled0.ipynb", [""]
        return {"title": self.title, "url": HOME + "drive/abc", "cells": 1}

    def set_code(self, code: str, new_cell: bool) -> int:
        if not self.cells:
            raise ColabError("There's no notebook open in Colab yet.", "no_notebook")
        if new_cell and self.cells[-1].strip():
            self.cells.append("")
        self.cells[-1] = code
        return len(self.cells)

    def run(self, timeout: float) -> dict:
        code = self.cells[-1] if self.cells else ""
        self.runs.append(code)
        for key, out in self.outputs.items():
            if key in code:
                return {"output": out, "finished": True}
        return {"output": "", "finished": True}

    def cell_code(self) -> str:
        return self.cells[-1] if self.cells else ""

    def notebook(self) -> dict:
        return {"title": self.title, "url": HOME, "cells": len(self.cells)}
