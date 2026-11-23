"""Code runner for the coding agent.

* **Language registry** — each language says how to run a file (argv only, never a shell). Adding a language
  means adding one :class:`Language` entry.
* **Safe execution** — ``shell=False``; working directory = the project folder; wall-clock timeout; output is
  captured and truncated; API keys/tokens are removed from the child's environment so generated code can't read
  Genesis's secrets.
* **Static safety scan** — generated code is checked for destructive or sensitive operations (deleting trees,
  spawning shells, registry edits, credential files, keyloggers …). Findings are shown to the user and make
  running the code a HIGH-risk action that always needs an explicit "yes".
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..core.logging_setup import get_logger

log = get_logger("coderunner")

MAX_OUTPUT = 8000
SECRET_ENV = re.compile(r"(TOKEN|SECRET|PASSWORD|API_KEY|APIKEY|ACCESS_KEY|PRIVATE|CREDENTIAL|GOOGLE_CLIENT|HF_|OPENAI|PICOVOICE)",
                        re.I)


def _python() -> Optional[str]:
    if not getattr(sys, "frozen", False):
        return sys.executable
    return shutil.which("python") or shutil.which("py")


def _node_supports_strip_types(node: str) -> bool:
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10, shell=False).stdout.strip()
        major, minor = (int(x) for x in out.lstrip("v").split(".")[:2])
        return (major, minor) >= (22, 6)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _ts_command(file: Path) -> Optional[list[str]]:
    tsx = shutil.which("tsx")
    if tsx:
        return [tsx, str(file)]
    node = shutil.which("node")
    if node and _node_supports_strip_types(node):
        return [node, "--experimental-strip-types", "--no-warnings", str(file)]
    return None


@dataclass
class Language:
    key: str
    name: str
    extensions: tuple[str, ...]
    command: Callable[[Path], Optional[list[str]]]  # argv to run the entry file, or None if the runtime is missing
    install_hint: str
    runs_in_browser: bool = False
    aliases: tuple[str, ...] = ()


LANGUAGES: dict[str, Language] = {}


def register(lang: Language) -> None:
    LANGUAGES[lang.key] = lang


register(Language("python", "Python", (".py",), lambda f: [p, "-u", str(f)] if (p := _python()) else None,
                  "Install Python from python.org.", aliases=("py", "python3")))
register(Language("javascript", "JavaScript", (".js", ".mjs", ".cjs"),
                  lambda f: [n, str(f)] if (n := shutil.which("node")) else None,
                  "Install Node.js from nodejs.org.", aliases=("js", "node", "nodejs")))
register(Language("typescript", "TypeScript", (".ts",), _ts_command,
                  "Install Node.js 22.6+ (or run: npm install -g tsx).", aliases=("ts",)))
register(Language("java", "Java", (".java",), lambda f: [j, str(f)] if (j := shutil.which("java")) else None,
                  "Install a JDK 11+ (e.g. from adoptium.net).", aliases=()))
register(Language("html", "HTML/CSS/JS web page", (".html", ".htm"), lambda f: None, "", runs_in_browser=True,
                  aliases=("html/css", "web page", "website", "css", "web")))


def language_for(name_or_file: str) -> Optional[Language]:
    t = (name_or_file or "").lower().strip()
    if t in LANGUAGES:
        return LANGUAGES[t]
    for lang in LANGUAGES.values():
        if t in lang.aliases or t == lang.name.lower() or (t.startswith(".") and t in lang.extensions):
            return lang
    suffix = Path(t).suffix
    for lang in LANGUAGES.values():
        if suffix and suffix in lang.extensions:
            return lang
    return None


# ------------------------------------------------------------------ safety scan
_DANGER = [
    (r"\bshutil\.rmtree\b|\brmdir\s+/s\b|\brm\s+-rf?\b|\bdel\s+/[sq]\b|\bfs\.rm(?:Sync)?\([^)]*recursive|Files\.walk\([^)]*\)\s*\.sorted.*delete",
     "deletes folders recursively"),
    (r"\bos\.(?:remove|unlink|rmdir)\b|\bPath\([^)]*\)\.unlink\b|\bfs\.unlink(?:Sync)?\b|\bFiles\.delete\b", "deletes files"),
    (r"\bsubprocess\b|\bos\.system\b|\bos\.popen\b|\bchild_process\b|\bRuntime\.getRuntime\(\)\.exec\b|\bProcessBuilder\b",
     "starts other programs / shell commands"),
    (r"\bwinreg\b|\breg\s+(?:add|delete)\b", "edits the Windows registry"),
    (r"\.ssh\b|id_rsa|\bLogin Data\b|\bCookies\b.*sqlite|credential|\bkeyring\b|\.aws[/\\]credentials", "touches credentials"),
    (r"\bpynput\b|\bkeyboard\.on_press\b|\bGetAsyncKeyState\b|\bSetWindowsHookEx", "captures keyboard/mouse input"),
    (r"\bformat\s+[a-z]:|\bdiskpart\b|\bshutdown\s+/|\bbcdedit\b", "system-level disk/power commands"),
    (r"\bctypes\.windll\b", "calls Windows system APIs directly"),
    (r"\bsocket\.bind\b.*0\.0\.0\.0|\bhost\s*=\s*['\"]0\.0\.0\.0", "listens on all network interfaces"),
]


def safety_scan(files: dict[str, str]) -> list[str]:
    findings = []
    for name, content in files.items():
        for rx, why in _DANGER:
            if re.search(rx, content or "", re.I):
                findings.append(f"{name}: {why}")
    return sorted(set(findings))


# ------------------------------------------------------------------ running
@dataclass
class RunResult:
    command: list[str]
    returncode: Optional[int]
    stdout: str
    stderr: str
    timed_out: bool = False
    duration_ms: int = 0
    server_like: bool = False
    extras: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    cwd: str = ""

    def display_command(self) -> str:
        """Short, copy-pasteable form: 'python -u main.py' rather than absolute interpreter/file paths."""
        if not self.command:
            return ""
        parts = []
        for i, a in enumerate(self.command):
            if i == 0:
                name = Path(a).stem.lower()
                parts.append("python" if name.startswith("python") else name)
            elif self.cwd and a.startswith(self.cwd):
                parts.append(str(Path(a).relative_to(self.cwd)))
            else:
                parts.append(a)
        return " ".join(parts)


_SERVER_HINT = re.compile(r"\b(Flask|FastAPI|uvicorn|app\.run\(|http\.server|serve_forever|express\(\)|\.listen\(|createServer|"
                          r"HttpServer\.create|SpringApplication)", re.I)


def looks_like_server(files: dict[str, str]) -> bool:
    return any(_SERVER_HINT.search(c or "") for c in files.values())


def _clip(text: str) -> str:
    text = text or ""
    return text if len(text) <= MAX_OUTPUT else text[: MAX_OUTPUT // 2] + "\n…(output truncated)…\n" + text[-MAX_OUTPUT // 2:]


def _child_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env


class CodeRunner:
    def __init__(self, runner: Callable = subprocess.run) -> None:
        self._run = runner

    def command_for(self, entry: Path, language: Optional[str] = None) -> tuple[Optional[Language], Optional[list[str]]]:
        lang = language_for(language) if language else None
        lang = lang or language_for(entry.name)
        if lang is None:
            return None, None
        return lang, lang.command(entry)

    def run(self, argv: list[str], cwd: Path, *, timeout: float = 30.0, stdin: str = "", server_like: bool = False) -> RunResult:
        if not argv or not isinstance(argv, list):
            raise ValueError("argv must be a non-empty list")
        t0 = time.perf_counter()
        try:
            proc = self._run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, shell=False,
                             input=stdin or None, env=_child_env(), encoding="utf-8", errors="replace",
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            res = RunResult(argv, proc.returncode, _clip(proc.stdout), _clip(proc.stderr))
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            err = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            res = RunResult(argv, None, _clip(out), _clip(err), timed_out=True, server_like=server_like)
        res.cwd = str(cwd)
        res.duration_ms = int((time.perf_counter() - t0) * 1000)
        log.info("program run", extra={"data": {"cmd": res.display_command(), "rc": res.returncode, "timed_out": res.timed_out,
                                                 "ms": res.duration_ms}})
        return res


# ------------------------------------------------------------------ error parsing
_PY_TB = re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d+)')
_JS_ERR = re.compile(r"(?P<path>[\w./\\:-]+\.(?:js|mjs|cjs|ts)):(?P<line>\d+)")
_JAVA_ERR = re.compile(r"(?P<path>[\w./\\:-]+\.java):(?P<line>\d+): error")
_MISSING_MOD = re.compile(r"(?:ModuleNotFoundError: No module named ['\"](?P<py>[\w.]+)['\"]|Cannot find module ['\"](?P<js>[^'\"]+)['\"])")


def parse_error(stderr: str, project: Path) -> Optional[dict]:
    """Locate the failing file/line in a traceback (only files inside the project)."""
    last = None
    for rx in (_PY_TB, _JS_ERR, _JAVA_ERR):
        for m in rx.finditer(stderr or ""):
            p = Path(m.group("path"))
            try:
                p = p if p.is_absolute() else (project / p)
                p.resolve().relative_to(project.resolve())
            except (ValueError, OSError):
                continue
            last = {"path": str(p.resolve()), "line": int(m.group("line"))}
    lines = [ln for ln in (stderr or "").strip().splitlines() if ln.strip()]
    message = lines[-1].strip() if lines else "The program failed."
    if last:
        return {**last, "message": message[:300], "severity": "error", "kind": "runtime"}
    return {"path": "", "line": 0, "message": message[:300], "severity": "error", "kind": "runtime"} if lines else None


def missing_module(stderr: str) -> Optional[str]:
    m = _MISSING_MOD.search(stderr or "")
    if not m:
        return None
    name = m.group("py") or m.group("js") or ""
    return None if name.startswith((".", "/")) else name.split(".")[0]
