"""Robust ``.env`` loading (Windows-proof) with a secret-free diagnostic report.

Why this exists — each of these silently left Genesis without an API key:

* **Byte-order mark.** Notepad can save UTF-8 *with BOM* (or UTF-16 "Unicode"); python-dotenv then reads the first key as
  ``\\ufeffHF_TOKEN`` and the token is lost.
* **".env.txt".** Windows hides extensions, so "saving as .env" in Notepad often produces ``.env.txt``.
* **Empty OS variables.** ``load_dotenv(override=False)`` never replaces a variable that exists in the environment — even
  an empty ``HF_TOKEN=`` inherited from the shell/VS Code — so the value in ``.env`` was ignored.
* **Editing .env while Genesis runs** had no effect until a restart.

The loader decodes BOM/UTF-16 correctly, finds ``.env`` / ``.env.txt`` in the usual places, fills variables that are
missing **or empty**, can re-apply a changed file (``reload=True``) and returns a report of *which* keys came from where —
never the values.
"""
from __future__ import annotations

import io
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_applied: dict[str, str] = {}  # keys this loader set → the value it set (so a later reload may replace them)


@dataclass
class EnvReport:
    file: Optional[str] = None
    keys: list[str] = field(default_factory=list)  # names only
    warnings: list[str] = field(default_factory=list)
    mtime: float = 0.0
    searched: list[str] = field(default_factory=list)


def _decode(raw: bytes) -> tuple[str, Optional[str]]:
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16"), "The .env file is saved as UTF-16 (“Unicode”); Genesis read it, but UTF-8 is recommended."
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace"), None  # BOM stripped (this used to hide the first key)
    return raw.decode("utf-8", errors="replace"), None


def parse_env_text(text: str) -> dict[str, str]:
    try:
        from dotenv import dotenv_values  # type: ignore

        values = dotenv_values(stream=io.StringIO(text))
        return {k.strip().lstrip("﻿"): (v or "") for k, v in values.items() if k}
    except ImportError:  # minimal fallback parser (KEY=VALUE, comments, quotes, "export ")
        out: dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip().removeprefix("export ").strip()
            v = v.strip()
            if v[:1] in "\"'" and v[-1:] == v[:1]:
                v = v[1:-1]
            elif " #" in v:
                v = v.split(" #", 1)[0].strip()
            out[k] = v
        return out


def candidate_paths(project_root: Path, home: Optional[Path] = None) -> list[Path]:
    dirs = [project_root, Path.cwd(), Path(sys.executable).parent]
    if home:
        dirs.append(home)
    seen, out = set(), []
    for d in dirs:
        try:
            d = d.resolve()
        except OSError:
            continue
        if d in seen:
            continue
        seen.add(d)
        out += [d / ".env", d / ".env.txt"]
    return out


def load_env(project_root: Path, *, env_file: Optional[Path] = None, home: Optional[Path] = None,
             reload: bool = False) -> EnvReport:
    report = EnvReport()
    paths = [env_file] if env_file else candidate_paths(project_root, home)
    report.searched = [str(p) for p in paths if p]
    chosen: Optional[Path] = None
    for p in paths:
        if p and p.is_file():
            chosen = p
            break
    if chosen is None:
        report.warnings.append(f"No .env file found (looked in {', '.join(sorted({str(Path(p).parent) for p in report.searched}))}).")
        return report
    if chosen.name == ".env.txt":
        report.warnings.append("Your settings file is named “.env.txt” (Windows added .txt). Genesis read it, but rename it to “.env”.")
    try:
        raw = chosen.read_bytes()
    except OSError as exc:
        report.warnings.append(f"Couldn't read {chosen.name}: {exc.strerror or exc}")
        return report
    text, warn = _decode(raw)
    if warn:
        report.warnings.append(warn)
    values = parse_env_text(text)
    report.file = str(chosen)
    report.mtime = chosen.stat().st_mtime
    for key, value in values.items():
        value = (value or "").strip()
        current = os.environ.get(key)
        replace = current is None or current.strip() == "" or (reload and _applied.get(key) == current)
        if replace and value != "":
            os.environ[key] = value
            _applied[key] = value
            report.keys.append(key)
        elif replace and reload and key in _applied and value == "":
            os.environ.pop(key, None)  # the key was removed/blanked in .env since the last load
            _applied.pop(key, None)
    if reload:
        for key in [k for k in _applied if k not in values]:
            if os.environ.get(key) == _applied[key]:
                os.environ.pop(key, None)  # deleted from .env since the last load
            _applied.pop(key, None)
    return report


__all__ = ["load_env", "EnvReport", "parse_env_text", "candidate_paths"]
