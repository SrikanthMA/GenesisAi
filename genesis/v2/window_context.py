"""Which window is the user working in? (2.2)

Genesis looks at the foreground window (title + program). If that is Genesis's own window - you typed the request into
Genesis - it uses the window you were in just before (Windows keeps windows in most-recently-used order). The window is
classified so coding requests go to the right place:

* ``colab``     - a Google Colab notebook (in any browser)
* ``code_web``  - an online compiler / code playground / coding site (OnlineGDB, Programiz, Replit, LeetCode, ...)
* ``ide``       - a code editor program (VS Code, PyCharm, ...)
* ``editor``    - a text editor (Notepad, Notepad++, Word, ...)
* ``browser``   - any other web page;  ``terminal`` - never typed into;  ``other``
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..core.logging_setup import get_logger

log = get_logger("v2.window")

BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "opera_gx.exe", "vivaldi.exe", "arc.exe",
            "iexplore.exe", "chromium.exe"}
CODE_SITES = re.compile(
    r"compiler|interpreter|\bonline (?:ide|editor|python|java|c\+\+|ide)|onlinegdb|programiz|replit|repl\.it|jdoodle|ideone|"
    r"onecompiler|codechef|leetcode|hackerrank|hackerearth|geeksforgeeks|\bgfg\b|codeforces|codepen|jsfiddle|stackblitz|"
    r"codesandbox|godbolt|compiler explorer|\btryit\b|w3schools|playground|paiza|kaggle|jupyter|vscode\.dev|github\.dev|"
    r"sololearn|code editor|trinket|glitch|codewars|codingame|exercism|python tutor|pythontutor|rextester|tio\.run|"
    r"online-python|online python|online java|online c\b|coderpad|codeshare|code\.org|scratch|edabit|atcoder|topcoder",
    re.I)
COLAB = re.compile(r"\bcolab(?:oratory)?\b|colab\.research\.google", re.I)
LANGS = [("python", r"\bpython\d?\b|\.py\b|\bpy\b|jupyter|colab|ipynb"), ("cpp", r"c\+\+|\bcpp\b|\.cpp\b"),
         ("csharp", r"c#|\bcsharp\b|\.cs\b"), ("java", r"\bjava\b(?!script)|\.java\b"),
         ("javascript", r"javascript|\bnode(?:\.?js)?\b|\.js\b|\bjs\b"), ("typescript", r"typescript|\.ts\b"),
         ("c", r"\bc (?:compiler|online|programming|language|program|code)\b|\bin c\b|\bonline c\b|\.c\b|\bgcc\b"), ("go", r"\bgolang\b|\bgo (?:playground|online)\b|\.go\b"),
         ("rust", r"\brust\b|\.rs\b"), ("kotlin", r"\bkotlin\b|\.kt\b"), ("php", r"\bphp\b"), ("ruby", r"\bruby\b|\.rb\b"),
         ("swift", r"\bswift\b"), ("r", r"\br (?:compiler|online|programming)\b"), ("sql", r"\bsql\b|mysql|postgres|sqlite"),
         ("html", r"\bhtml\b|codepen|jsfiddle")]
LANG_NAMES = {"python": "Python", "cpp": "C++", "csharp": "C#", "java": "Java", "javascript": "JavaScript", "typescript": "TypeScript",
              "c": "C", "go": "Go", "rust": "Rust", "kotlin": "Kotlin", "php": "PHP", "ruby": "Ruby", "swift": "Swift", "r": "R",
              "sql": "SQL", "html": "HTML/CSS/JavaScript"}


@dataclass
class Target:
    title: str = ""
    process: str = ""
    handle: int = 0
    pid: int = 0
    kind: str = "other"
    language: Optional[str] = None
    via: str = "foreground"  # foreground | previous (Genesis itself was in front)
    extra: dict = field(default_factory=dict)

    @property
    def app(self) -> str:
        p = (self.process or "").lower()
        return {"chrome.exe": "Google Chrome", "msedge.exe": "Microsoft Edge", "firefox.exe": "Firefox", "brave.exe": "Brave",
                "code.exe": "VS Code", "pycharm64.exe": "PyCharm", "notepad.exe": "Notepad", "notepad++.exe": "Notepad++",
                "winword.exe": "Word", "idea64.exe": "IntelliJ IDEA", "devenv.exe": "Visual Studio"}.get(p, self.process or "an app")

    @property
    def page(self) -> str:
        """The window title without the trailing browser name."""
        return re.sub(r"\s+[-–—]\s+(?:google chrome|microsoft​? edge|mozilla firefox|brave|opera|vivaldi)$", "",
                      self.title or "", flags=re.I).strip()

    def describe(self) -> str:
        what = {"colab": "a Google Colab notebook", "code_web": "an online code editor", "ide": "a code editor",
                "editor": "a text editor", "browser": "a web page", "terminal": "a terminal", "other": "an app"}[self.kind]
        lang = f" ({LANG_NAMES.get(self.language, self.language)})" if self.language else ""
        return f"“{self.page or self.title}” in {self.app} - {what}{lang}"


def guess_language(*texts: str) -> Optional[str]:
    blob = " ".join(t for t in texts if t)
    for lang, rx in LANGS:
        if re.search(rx, blob, re.I):
            return lang
    return None


def classify(title: str, process: str) -> tuple[str, Optional[str]]:
    from ..services.computer import EDITOR_PROCESSES, IDE_PROCESSES, TERMINAL_PROCESSES

    p, t = (process or "").lower(), title or ""
    if p in TERMINAL_PROCESSES:
        return "terminal", None
    if p in BROWSERS:
        if COLAB.search(t):
            return "colab", "python"
        if CODE_SITES.search(t):
            return "code_web", guess_language(t)
        return "browser", None
    if p in IDE_PROCESSES:
        return "ide", guess_language(t)
    if p in EDITOR_PROCESSES:
        return "editor", guess_language(t)
    return "other", None


def _is_genesis(title: str, pid: int) -> bool:
    return pid == os.getpid() or (title or "").strip().lower().startswith("genesis")


def _process_of(hwnd: int) -> tuple[int, str]:
    """(pid, program name) of a window handle - Windows only; (0, "") elsewhere."""
    try:
        import ctypes

        pid = ctypes.c_ulong(0)
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))  # type: ignore[attr-defined]
        import psutil

        return int(pid.value), psutil.Process(pid.value).name()
    except Exception:
        return 0, ""


def current_target(svc: Any) -> Optional[Target]:
    """The window the user means by "here" (None when it can't be determined, e.g. not on Windows)."""
    fake = getattr(svc, "extras", {}).get("v2_window_target") if hasattr(svc, "extras") else None
    if fake is not None:  # tests / diagnostics
        return fake
    comp = getattr(svc, "computer", None)
    try:
        if comp is None or not comp.available():
            return None
        win = comp.driver.foreground()
    except Exception:
        return None
    title, proc, handle, pid, via = win.title, win.process, win.handle, win.pid, "foreground"
    if _is_genesis(title, pid) or not title:
        try:
            wins = svc.backend.list_windows()  # most recently used first
        except Exception:
            wins = []
        for w in wins:
            wt = (w.get("title") or "").strip()
            if wt and not wt.lower().startswith("genesis") and w.get("id") != handle:
                wpid, wproc = w.get("pid", 0), w.get("process", "")
                if not wproc:
                    wpid, wproc = _process_of(int(w.get("id") or 0))
                if wpid == os.getpid():
                    continue
                title, proc, handle, pid, via = wt, wproc, w.get("id", 0), wpid, "previous"
                break
        else:
            return None
    kind, lang = classify(title, proc)
    return Target(title=title, process=proc, handle=int(handle or 0), pid=int(pid or 0), kind=kind, language=lang, via=via)


def bring_to_front(svc: Any, target: Target) -> bool:
    """Make sure keystrokes go to ``target`` (it is usually already in front when you spoke the command)."""
    comp = getattr(svc, "computer", None)
    try:
        fg = comp.driver.foreground()
        if fg.handle and fg.handle == target.handle:
            return True
        if target.handle and svc.backend is not None:
            svc.backend.window_action(target.handle, "focus")
            import time

            time.sleep(0.4)
            return True
    except Exception as exc:
        log.info("couldn't focus the target window: %s", exc)
    return False
