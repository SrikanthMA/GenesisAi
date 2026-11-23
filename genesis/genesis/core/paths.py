"""Filesystem locations used by Genesis.

When running from source (e.g. inside VS Code) data and logs live in the
repository's ``data/`` and ``logs/`` folders. When running as a frozen
executable they live in ``%LOCALAPPDATA%\\Genesis`` (Windows) or
``~/.genesis`` (other platforms). ``GENESIS_HOME`` overrides both.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def is_windows() -> bool:
    return sys.platform.startswith("win")


def default_home() -> Path:
    env = os.getenv("GENESIS_HOME")
    if env:
        return Path(env).expanduser().resolve()
    if is_frozen():
        if is_windows():
            base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
            return Path(base) / "Genesis"
        return Path.home() / ".genesis"
    return PROJECT_ROOT


@dataclass
class AppPaths:
    home: Path

    @property
    def data_dir(self) -> Path:
        return self.home / "data"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def credentials_dir(self) -> Path:
        return self.data_dir / "credentials"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "genesis.db"

    def ensure(self) -> "AppPaths":
        for p in (self.data_dir, self.logs_dir, self.models_dir, self.credentials_dir):
            p.mkdir(parents=True, exist_ok=True)
        return self

    @classmethod
    def default(cls) -> "AppPaths":
        return cls(default_home())


def user_known_folders() -> dict[str, Path]:
    """Well-known user folders (resolved for the current OS)."""
    home = Path.home()
    folders = {
        "home": home,
        "desktop": home / "Desktop",
        "documents": home / "Documents",
        "downloads": home / "Downloads",
        "pictures": home / "Pictures",
        "music": home / "Music",
        "videos": home / "Videos",
    }
    if is_windows():  # honour OneDrive-redirected folders when they exist
        onedrive = os.getenv("OneDrive")
        if onedrive:
            for key in ("desktop", "documents", "pictures"):
                alt = Path(onedrive) / folders[key].name
                if alt.exists() and not folders[key].exists():
                    folders[key] = alt
    return folders
