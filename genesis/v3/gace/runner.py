"""Terminal Engineer: runs only validated developer commands, with no shell, a timeout and a scrubbed environment.

What is allowed (everything else is refused with a reason):
* python -m venv .venv            * pip install -r requirements.txt / pip install <plain package names>
* python -m pytest|unittest|compileall, python <file in the workspace>
* npm install / npm test / npm run build|test|lint, node <file>
* javac / java / mvn / gradle build+test, dotnet build/test, go build/test/vet, cargo build/test/check, g++ <files>

Installing packages is the one step that reaches the network, so the engine asks for confirmation before doing it, and
package names are checked (no URLs, no flags like --index-url, no local paths).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.I)
PKG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*(\[[A-Za-z0-9_,\-]+\])?((==|>=|<=|~=|!=|<|>)[A-Za-z0-9_.*\-]+)?$")
NPM_PKG = re.compile(r"^(@[a-z0-9\-~][a-z0-9\-._~]*/)?[a-z0-9\-~][a-z0-9\-._~]*(@[A-Za-z0-9._^~\-]+)?$")
META = re.compile(r"[;&|<>`$\n\r]")


class CommandRefused(Exception):
    pass


@dataclass
class Result:
    argv: list[str]
    code: int
    output: str
    seconds: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out


def venv_python(root: Path) -> Optional[Path]:
    for rel in (".venv/Scripts/python.exe", ".venv/bin/python"):
        p = root / rel
        if p.exists():
            return p
    return None


def validate(argv: list[str], root: Path) -> str:
    """Return 'safe' | 'install' for an allowed command, or raise CommandRefused."""
    if not argv:
        raise CommandRefused("empty command")
    if any(META.search(a) for a in argv):
        raise CommandRefused("shell characters are not allowed")
    exe = Path(argv[0]).name.lower().removesuffix(".exe")
    exe = {"py": "python", "python3": "python"}.get(exe, exe)
    rest = argv[1:]
    if exe in ("python", "pythonw"):
        if rest[:2] == ["-m", "venv"] and len(rest) == 3 and not rest[2].startswith("-") and ".." not in rest[2]:
            return "safe"
        if rest[:1] == ["-m"] and len(rest) >= 2 and rest[1] in ("pytest", "unittest", "compileall", "uvicorn_check"):
            if rest[1] == "pytest" or rest[1] == "unittest" or rest[1] == "compileall":
                return "safe"
        if rest[:1] == ["-m"] and len(rest) >= 2 and rest[1] == "pip":
            return _pip(rest[2:], root)
        if rest and not rest[0].startswith("-") and rest[0].endswith(".py"):
            _inside(rest[0], root)
            return "safe"
        if rest[:1] == ["-c"] and len(rest) == 2 and re.fullmatch(r"import [\w\.]+(; print\('ok'\))?", rest[1]):
            return "safe"
        raise CommandRefused("that python command isn't on the allowed list")
    if exe == "pip":
        return _pip(rest, root)
    if exe == "pytest":
        return "safe"
    if exe == "npm":
        if rest == ["install"] or rest == ["ci"]:
            return "install"
        if rest[:1] == ["install"] and len(rest) >= 2:
            for a in rest[1:]:
                if a in ("--save-dev", "-D", "--save"):
                    continue
                if not NPM_PKG.match(a):
                    raise CommandRefused(f"“{a}” isn't a plain npm package name")
            return "install"
        if rest[:1] == ["test"] or rest[:2] in (["run", "build"], ["run", "test"], ["run", "lint"]):
            return "safe"
        raise CommandRefused("npm is allowed only for install, test and run build/test/lint")
    if exe == "node" and rest and rest[0].endswith((".js", ".mjs", ".cjs")):
        _inside(rest[0], root)
        return "safe"
    if exe in ("mvn", "gradle", "dotnet", "go", "cargo"):
        if rest and rest[0] in ("test", "compile", "package", "build", "check", "vet"):
            return "safe"
        raise CommandRefused(f"{exe} is allowed only for build/test commands")
    if exe == "javac" and rest and all(a.endswith(".java") or a in ("-d",) or "/" not in a for a in rest):
        return "safe"
    if exe == "java" and rest:
        return "safe"
    if exe in ("g++", "gcc", "clang++") and rest and not any(a.startswith(("-wl", "-Wl", "-fplugin")) for a in rest):
        for a in rest:
            if not a.startswith("-"):
                _inside(a, root)
        return "safe"
    raise CommandRefused(f"“{argv[0]}” isn't on the allowed list")


def _inside(rel: str, root: Path) -> None:
    p = (root / rel).resolve()
    try:
        p.relative_to(root.resolve())
    except ValueError:
        raise CommandRefused("that file is outside the workspace") from None


def _pip(args: list[str], root: Path) -> str:
    if args[:1] != ["install"]:
        raise CommandRefused("pip is allowed only for install")
    rest = args[1:]
    if rest[:1] == ["-r"] and len(rest) == 2:
        _inside(rest[1], root)
        return "install"
    if not rest:
        raise CommandRefused("pip install needs package names")
    for a in rest:
        if not PKG.match(a):
            raise CommandRefused(f"“{a}” isn't a plain package name (no URLs, paths or options)")
    return "install"


def clean_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def run(argv: list[str], root: Path, timeout: int = 300, on_line=None) -> Result:
    validate(argv, root)
    t0 = time.time()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(argv, cwd=str(root), capture_output=True, text=True, timeout=timeout, shell=False,
                              env=clean_env(), encoding="utf-8", errors="replace", creationflags=flags)
        out = (proc.stdout or "") + (proc.stderr or "")
        res = Result(argv, proc.returncode, out[-20000:], time.time() - t0)
    except subprocess.TimeoutExpired as exc:
        out = ((exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")) + \
              f"\nTimed out after {timeout}s."
        res = Result(argv, -1, out[-20000:], time.time() - t0, True)
    except FileNotFoundError:
        res = Result(argv, 127, f"{argv[0]} is not installed or not on PATH.", time.time() - t0)
    if on_line:
        on_line(" ".join(argv), res)
    return res
