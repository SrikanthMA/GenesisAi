"""Project memory: what was planned, built, tried and learned - checked against the real files.

Stored in ``<project>/.genesis/project.json``. On resume, file hashes recorded at the last build are compared with the
files on disk: anything that changed outside Genesis (or vanished) is marked as such and the memory is NOT trusted for it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from .workspace import Workspace, sha


class ProjectMemory:
    def __init__(self, ws: Workspace) -> None:
        self.ws = ws
        self.path = ws.state / "project.json"
        self.data: dict = {"spec": None, "status": "new", "files": {}, "known_errors": [], "solutions": [],
                           "decisions": [], "history": [], "updated": 0}
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
            except ValueError:
                pass

    def save(self) -> None:
        self.data["updated"] = time.time()
        self.path.write_text(json.dumps(self.data, indent=1), encoding="utf-8")

    def record_file(self, rel: str) -> None:
        p = self.ws.root / rel
        if p.exists():
            self.data["files"][rel] = sha(p.read_bytes())

    def audit(self) -> dict:
        """Compare memory with reality. Returns {'ok': [...], 'missing': [...], 'changed': [...]} (relative paths)."""
        ok, missing, changed = [], [], []
        for rel, h in self.data["files"].items():
            p = self.ws.root / rel
            if not p.exists():
                missing.append(rel)
            elif sha(p.read_bytes()) != h:
                changed.append(rel)
            else:
                ok.append(rel)
        return {"ok": ok, "missing": missing, "changed": changed}

    def note_error(self, signature: str, summary: str) -> None:
        if not any(e["signature"] == signature for e in self.data["known_errors"]):
            self.data["known_errors"].append({"signature": signature, "summary": summary[:200], "ts": time.time()})
            del self.data["known_errors"][:-40]

    def note_solution(self, signature: str, file: str, summary: str) -> None:
        self.data["solutions"].append({"signature": signature, "file": file, "summary": summary[:200], "ts": time.time()})
        del self.data["solutions"][:-40]

    def decide(self, text: str) -> None:
        self.data["decisions"].append({"text": text[:300], "ts": time.time()})
        del self.data["decisions"][:-40]
