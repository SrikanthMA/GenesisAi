"""Resolve an application name to something launchable.

Resolution order for a name:
    1. catalogue web apps  → URL
    2. catalogue desktop apps → PATH lookup, known install paths, Windows
       "App Paths" registry, Start-menu shortcut (.lnk), protocol URI
    3. unknown names       → fuzzy Start-menu shortcut search
Only resolved executables/shortcuts/URIs are ever launched — never raw text.
"""
from __future__ import annotations

import difflib
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..core.logging_setup import get_logger
from . import app_catalog

log = get_logger("apps")


@dataclass
class ResolvedApp:
    key: str
    display_name: str
    kind: str  # exe | shortcut | uri | url
    target: str
    process_names: list[str]


def _expand(p: str) -> Path:
    return Path(os.path.expandvars(p))


def _start_menu_dirs() -> list[Path]:
    dirs = []
    for env in ("APPDATA", "ProgramData"):
        base = os.getenv(env)
        if base:
            dirs.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return [d for d in dirs if d.exists()]


def _linux_desktop_dirs() -> list[Path]:
    return [p for p in (Path("/usr/share/applications"), Path.home() / ".local/share/applications") if p.exists()]


class AppResolver:
    def __init__(self) -> None:
        self._shortcut_cache: Optional[dict[str, Path]] = None

    # --------------------------------------------------------------- helpers
    def shortcuts(self) -> dict[str, Path]:
        if self._shortcut_cache is None:
            cache: dict[str, Path] = {}
            if sys.platform.startswith("win"):
                for d in _start_menu_dirs():
                    for lnk in d.rglob("*.lnk"):
                        name = lnk.stem.lower()
                        if "uninstall" in name or "readme" in name or "help" == name:
                            continue
                        cache.setdefault(name, lnk)
            else:
                for d in _linux_desktop_dirs():
                    for desk in d.glob("*.desktop"):
                        cache.setdefault(desk.stem.lower(), desk)
            self._shortcut_cache = cache
        return self._shortcut_cache

    def _find_shortcut(self, name: str, cutoff: float = 0.72) -> Optional[Path]:
        sc = self.shortcuts()
        n = name.lower()
        if n in sc:
            return sc[n]
        contains = [k for k in sc if n in k.split() or k.startswith(n)]
        if contains:
            return sc[sorted(contains, key=len)[0]]
        close = difflib.get_close_matches(n, list(sc), n=1, cutoff=cutoff)
        return sc[close[0]] if close else None

    def _app_paths_registry(self, exe: str) -> Optional[str]:
        if not sys.platform.startswith("win"):
            return None
        try:
            import winreg  # type: ignore

            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(hive, rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as k:
                        val, _ = winreg.QueryValueEx(k, None)
                        if val and Path(val.strip('"')).exists():
                            return val.strip('"')
                except OSError:
                    continue
        except ImportError:
            return None
        return None

    # ---------------------------------------------------------------- public
    def resolve(self, name: str) -> Optional[ResolvedApp]:
        found = app_catalog.lookup(name)
        if found:
            key, spec = found
            return self._resolve_spec(key, spec)
        # unknown app → Start-menu / .desktop fuzzy search
        sc = self._find_shortcut(name)
        if sc:
            return ResolvedApp(key=name.lower(), display_name=sc.stem, kind="shortcut", target=str(sc),
                               process_names=[f"{sc.stem}.exe"])
        exe = shutil.which(name) or shutil.which(f"{name}.exe")
        if exe:
            return ResolvedApp(name.lower(), Path(exe).stem, "exe", exe, [Path(exe).name])
        return None

    def _resolve_spec(self, key: str, spec: dict) -> Optional[ResolvedApp]:
        disp = spec.get("name", key)
        procs = spec.get("process", [])
        if spec.get("url"):
            return ResolvedApp(key, disp, "url", spec["url"], procs)
        for exe in spec.get("exe", []):
            found = shutil.which(exe)
            if found:
                if key == "vscode" and found.lower().endswith(("code.cmd", "code")) and sys.platform.startswith("win"):
                    code_exe = Path(found).resolve().parent.parent / "Code.exe"
                    if code_exe.exists():
                        found = str(code_exe)
                return ResolvedApp(key, disp, "exe", found, procs)
        for p in spec.get("paths", []):
            path = _expand(p)
            if path.exists():
                return ResolvedApp(key, disp, "exe", str(path), procs)
        for exe in spec.get("exe", []):
            reg = self._app_paths_registry(exe)
            if reg:
                return ResolvedApp(key, disp, "exe", reg, procs)
        if spec.get("shortcut"):
            sc = self._find_shortcut(spec["shortcut"], cutoff=0.8)
            if sc:
                return ResolvedApp(key, disp, "shortcut", str(sc), procs)
        if spec.get("uri") and sys.platform.startswith("win"):
            return ResolvedApp(key, disp, "uri", spec["uri"], procs)
        return None

    def resolve_all(self, name: str) -> list[ResolvedApp]:
        """Every way we know to launch *name*, best first (used to retry with an alternative method)."""
        out: list[ResolvedApp] = []
        first = self.resolve(name)
        if first:
            out.append(first)
        found = app_catalog.lookup(name)
        if found and not found[1].get("url"):
            key, spec = found
            disp, procs = spec.get("name", key), spec.get("process", [])
            for p in spec.get("paths", []):
                path = _expand(p)
                if path.exists():
                    out.append(ResolvedApp(key, disp, "exe", str(path), procs))
            if spec.get("shortcut"):
                sc = self._find_shortcut(spec["shortcut"], cutoff=0.8)
                if sc:
                    out.append(ResolvedApp(key, disp, "shortcut", str(sc), procs))
            if spec.get("uri") and sys.platform.startswith("win"):
                out.append(ResolvedApp(key, disp, "uri", spec["uri"], procs))
        seen, uniq = set(), []
        for r in out:
            if (r.kind, r.target) not in seen:
                seen.add((r.kind, r.target))
                uniq.append(r)
        return uniq

    def vscode_executable(self) -> Optional[str]:
        r = self.resolve("vscode")
        return r.target if r and r.kind == "exe" else None
