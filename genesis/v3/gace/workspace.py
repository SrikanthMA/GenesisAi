"""The approved workspace: every file operation goes through here.

* Paths are resolved and must stay inside the workspace (``..``, absolute paths and symlinks that escape are refused).
* Secrets (.env, keys, tokens) are never written or read by the engine.
* Checkpoints are zip snapshots of the project's text files (no git needed); ``rollback`` restores files that were
  changed or deleted and removes only files that Genesis itself created since (listed in the journal) - never yours.
* Deleting needs ``confirm=True``.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import time
import zipfile
from pathlib import Path
from typing import Optional

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".genesis", "dist", "build", ".pytest_cache", ".mypy_cache"}
SECRET = re.compile(r"(^\.env(?!\.example)|secret|credential|\.pem$|\.key$|id_rsa|\.pfx$|token)", re.I)
MAX_BYTES = 400_000
STATE = ".genesis"


class WorkspaceError(Exception):
    pass


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


class Workspace:
    def __init__(self, root: Path, approved: list[Path]) -> None:
        self.root = Path(root).resolve()
        ok = False
        for a in approved:
            try:
                self.root.relative_to(Path(a).resolve())
                ok = True
                break
            except ValueError:
                continue
        if not ok:
            raise WorkspaceError(f"{self.root} is not inside an approved workspace folder.")
        if self.root.parent == self.root:
            raise WorkspaceError("A drive root can't be a workspace.")
        self.root.mkdir(parents=True, exist_ok=True)
        self.state = self.root / STATE
        self.state.mkdir(exist_ok=True)
        self.journal_path = self.state / "journal.jsonl"

    # ------------------------------------------------------------------ paths
    def resolve(self, rel: str) -> Path:
        rel = (rel or "").replace("\\", "/").strip()
        if not rel or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel) or ".." in Path(rel).parts:
            raise WorkspaceError(f"“{rel}” is outside the workspace.")
        p = (self.root / rel).resolve()
        try:
            p.relative_to(self.root)
        except ValueError:
            raise WorkspaceError(f"“{rel}” is outside the workspace.") from None
        if SECRET.search(p.name):
            raise WorkspaceError(f"“{p.name}” looks like a secrets file; I never read or write those.")
        if STATE in p.relative_to(self.root).parts:
            raise WorkspaceError("The .genesis folder is Genesis's own bookkeeping.")
        return p

    def rel(self, p: Path) -> str:
        return p.resolve().relative_to(self.root).as_posix()

    def files(self) -> list[str]:
        out = []
        for p in sorted(self.root.rglob("*")):
            if p.is_file() and not (set(p.relative_to(self.root).parts) & SKIP_DIRS):
                out.append(p.relative_to(self.root).as_posix())
        return out

    # ------------------------------------------------------------------ files
    def read(self, rel: str) -> str:
        p = self.resolve(rel)
        if p.stat().st_size > MAX_BYTES:
            raise WorkspaceError(f"{rel} is too large to read.")
        return p.read_text(encoding="utf-8", errors="replace")

    def write(self, rel: str, content: str, who: str = "genesis") -> dict:
        p = self.resolve(rel)
        data = content.encode("utf-8")
        if len(data) > MAX_BYTES:
            raise WorkspaceError(f"{rel} is larger than {MAX_BYTES // 1000} KB.")
        existed = p.exists()
        old = p.read_text(encoding="utf-8", errors="replace") if existed else ""
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8", newline="\n")
        entry = {"ts": time.time(), "op": "modify" if existed else "create", "path": self.rel(p), "by": who,
                 "sha": sha(data)}
        self._journal(entry)
        return {**entry, "diff": self.diff_text(rel, old, content)}

    def delete(self, rel: str, confirm: bool = False) -> None:
        if not confirm:
            raise WorkspaceError("Deleting needs your confirmation.")
        p = self.resolve(rel)
        if p.is_file():
            p.unlink()
            self._journal({"ts": time.time(), "op": "delete", "path": self.rel(p), "by": "genesis"})

    @staticmethod
    def diff_text(rel: str, old: str, new: str) -> str:
        return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{rel}", f"b/{rel}", n=2))

    def _journal(self, entry: dict) -> None:
        with self.journal_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def journal(self) -> list[dict]:
        if not self.journal_path.exists():
            return []
        out = []
        for ln in self.journal_path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
        return out

    # ------------------------------------------------------------------ checkpoints
    def checkpoint(self, label: str) -> str:
        cid = time.strftime("%Y%m%d-%H%M%S") + "-" + re.sub(r"[^a-z0-9]+", "-", label.lower())[:30].strip("-")
        d = self.state / "checkpoints"
        d.mkdir(exist_ok=True)
        n, base = 1, cid
        while (d / f"{cid}.zip").exists():
            n += 1
            cid = f"{base}-{n}"
        names = []
        with zipfile.ZipFile(d / f"{cid}.zip", "w", zipfile.ZIP_DEFLATED) as z:
            for rel in self.files():
                p = self.root / rel
                if p.stat().st_size <= MAX_BYTES and not SECRET.search(p.name):
                    z.write(p, rel)
                    names.append(rel)
            z.writestr("MANIFEST.json", json.dumps({"label": label, "ts": time.time(), "files": names,
                                                    "journal_len": len(self.journal())}))
        return cid

    def checkpoints(self) -> list[str]:
        d = self.state / "checkpoints"
        return sorted(p.stem for p in d.glob("*.zip")) if d.exists() else []

    def rollback(self, cid: str, confirm: bool = False) -> dict:
        if not confirm:
            raise WorkspaceError("Rolling back needs your confirmation.")
        zp = self.state / "checkpoints" / f"{cid}.zip"
        if not zp.exists():
            raise WorkspaceError(f"No checkpoint called {cid}.")
        restored, removed = [], []
        with zipfile.ZipFile(zp) as z:
            man = json.loads(z.read("MANIFEST.json"))
            had = set(man["files"])
            for rel in man["files"]:
                p = self.resolve(rel)
                data = z.read(rel)
                if not p.exists() or p.read_bytes() != data:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(data)
                    restored.append(rel)
            made_by_genesis = {e["path"] for e in self.journal()[man.get("journal_len", 0):] if e["op"] == "create"}
            for rel in sorted(made_by_genesis - had):
                p = self.root / rel
                if p.is_file():
                    p.unlink()
                    removed.append(rel)
        self._journal({"ts": time.time(), "op": "rollback", "path": cid, "by": "user"})
        return {"restored": restored, "removed": removed}
