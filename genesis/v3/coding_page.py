"""The Coding page and the mute bar of the Genesis window (3.5).

Describe what to build, press Build, and watch: stage, files created / modified, commands run, errors found, fixes
applied, tests passed / failed. Every button calls a real tool (gace_build, gace_resume, gace_rollback); "Stop" is
Genesis's emergency stop. The mute bar shows the *actual* microphone and speaker state (it re-reads it every 400 ms)."""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QListWidget, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                               QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import coding, control, privacy

STAGES = ["UNDERSTAND", "PLAN", "BUILD", "SETUP", "TEST", "DIAGNOSE", "REPAIR", "REPORT"]


class MuteBar(QWidget):
    """Microphone and speaker buttons. Red + "muted" only when the capture really is off."""

    def __init__(self, compact: bool = False) -> None:
        super().__init__()
        self.compact = compact
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.mic = QPushButton()
        self.spk = QPushButton()
        for b in (self.mic, self.spk):
            b.setCheckable(False)
            b.setMinimumHeight(30)
        self.mic.setToolTip("Microphone mute (Ctrl+Shift+M). Muted = no capture, no wake word, nothing recorded or sent.")
        self.spk.setToolTip("Speaker mute: Genesis stops speaking; the microphone is not affected.")
        self.mic.clicked.connect(lambda: privacy.GATE.toggle_mic("button"))
        self.spk.clicked.connect(lambda: privacy.GATE.toggle_speaker("button"))
        lay.addWidget(self.mic)
        lay.addWidget(self.spk)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(400)
        self.refresh()

    def refresh(self) -> None:
        s = privacy.GATE.snapshot()
        if s["mic_muted"]:
            self.mic.setText(("🎤✕" if self.compact else "🎤 MUTED") + ("" if s["mic_really_off"] else " …"))
            self.mic.setStyleSheet("background:#7a1f1f;color:white;font-weight:600;")
        else:
            self.mic.setText("🎤" if self.compact else "🎤 Mic on")
            self.mic.setStyleSheet("")
        self.spk.setText(("🔇" if self.compact else "🔇 Speaker muted") if s["speaker_muted"] else ("🔊" if self.compact else "🔊 Speaker on"))
        self.spk.setStyleSheet("background:#7a5a1f;color:white;font-weight:600;" if s["speaker_muted"] else "")


class _Sig(QObject):
    event = Signal(object)
    done = Signal(object)


class CodingPage(QWidget):
    def __init__(self, core) -> None:
        super().__init__()
        self.core, self.svc = core, core.svc
        self.setObjectName("Page")
        self._busy = False
        self._workspace = ""
        self.counts = {"files": 0, "modified": 0, "errors": 0, "fixes": 0, "passed": 0, "failed": 0}
        self.sig = _Sig()
        self.sig.event.connect(self._on_event)
        self.sig.done.connect(self._on_done)
        coding.LISTENERS.append(lambda e: self.sig.event.emit(e))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 22, 28, 18)
        lay.setSpacing(8)
        top = QHBoxLayout()
        h = QLabel("Coding mode")
        h.setObjectName("H1")
        top.addWidget(h)
        top.addStretch(1)
        top.addWidget(MuteBar())
        lay.addLayout(top)
        sub = QLabel("Describe what to build. Genesis plans it, writes the files in your coding workspace, runs the tests, fixes what "
                     "fails and tells you exactly what was verified. It only touches that folder and keeps checkpoints for rollback.")
        sub.setObjectName("Muted")
        sub.setWordWrap(True)
        lay.addWidget(sub)

        self.name = QLineEdit()
        self.name.setPlaceholderText("Project name (optional)")
        lay.addWidget(self.name)
        self.request = QPlainTextEdit()
        self.request.setPlaceholderText("e.g. Build a complete personal finance tracker with a FastAPI backend, SQLite, authentication and tests")
        self.request.setMaximumHeight(90)
        lay.addWidget(self.request)

        row = QHBoxLayout()
        self.b_build = QPushButton("Build project")
        self.b_build.setToolTip("Plan, write, install (asks first), test, repair, report")
        self.b_resume = QPushButton("Continue project")
        self.b_resume.setToolTip("Write only missing files and test again; your own changes are kept")
        self.b_roll = QPushButton("Roll back")
        self.b_roll.setToolTip("Restore the latest checkpoint")
        self.b_stop = QPushButton("Stop")
        self.b_stop.setToolTip("Emergency stop: halts the build after the current step")
        self.b_open = QPushButton("Open folder")
        self.b_build.clicked.connect(lambda: self._start("gace_build"))
        self.b_resume.clicked.connect(lambda: self._start("gace_resume"))
        self.b_roll.clicked.connect(lambda: self._start("gace_rollback"))
        self.b_stop.clicked.connect(control.emergency_stop)
        self.b_open.clicked.connect(self._open_folder)
        for b in (self.b_build, self.b_resume, self.b_roll, self.b_stop, self.b_open):
            row.addWidget(b)
        lay.addLayout(row)

        self.bar = QProgressBar()
        self.bar.setRange(0, len(STAGES))
        self.bar.setFormat("Idle")
        lay.addWidget(self.bar)
        self.counter = QLabel("")
        self.counter.setObjectName("Muted")
        lay.addWidget(self.counter)

        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel("Files created / modified"))
        self.files = QListWidget()
        ll.addWidget(self.files)
        ll.addWidget(QLabel("Project files"))
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        ll.addWidget(self.tree)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(QLabel("Terminal activity, errors and fixes"))
        self.term = QPlainTextEdit()
        self.term.setReadOnly(True)
        rl.addWidget(self.term, 2)
        rl.addWidget(QLabel("Latest change (diff)"))
        self.diff = QPlainTextEdit()
        self.diff.setReadOnly(True)
        rl.addWidget(self.diff, 1)
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([300, 600])
        lay.addWidget(split, 1)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        lay.addWidget(self.result)
        self._update_counts()

    # ------------------------------------------------------------------ actions
    def _tool_args(self, tool_name: str) -> dict:
        name = self.name.text().strip()
        if tool_name == "gace_build":
            return {"request": self.request.toPlainText().strip(), **({"name": name} if name else {})}
        return {"project": name} if name else {}

    def _start(self, tool_name: str) -> None:
        if self._busy:
            return
        args = self._tool_args(tool_name)
        if tool_name == "gace_build" and len(args["request"]) < 15:
            self.result.setText("Describe what you want built (at least a sentence).")
            return
        if tool_name != "gace_build" and not args.get("project") and not self._workspace:
            self.result.setText("Type the project name first.")
            return
        if self._workspace and not args.get("project"):
            args = {"folder": self._workspace}
        tool = self.core.registry.get(tool_name)
        if tool is None:
            self.result.setText(f"{tool_name} isn't loaded. Run Update_Genesis_3.5.bat again.")
            return
        # one clear yes before anything is written, installed or restored
        if QMessageBox.question(self, "Genesis", tool.confirmation_prompt(args)) != QMessageBox.StandardButton.Yes:
            return
        self._busy = True
        self.counts = {k: 0 for k in self.counts}
        self.files.clear()
        self.term.clear()
        self.diff.clear()
        self.bar.setValue(0)
        self.bar.setFormat("Starting…")
        self.result.setText("")
        self._update_counts()

        def job() -> None:
            try:
                self.sig.done.emit(tool.run(args, self.svc))
            except Exception as exc:
                self.sig.done.emit(exc)

        threading.Thread(target=job, name="genesis-gace", daemon=True).start()

    def _open_folder(self) -> None:
        if self._workspace and Path(self._workspace).is_dir():
            try:
                self.svc.backend.open_path(self._workspace)
            except Exception:
                pass

    # ------------------------------------------------------------------ live events
    def _on_event(self, e: dict) -> None:
        if not self._busy:
            return
        stage, kind, text = e.get("stage", ""), e.get("kind", ""), e.get("text", "")
        if stage in STAGES:
            self.bar.setValue(max(self.bar.value(), STAGES.index(stage) + 1))
            self.bar.setFormat(stage.capitalize())
        if kind == "file":
            self.counts["files"] += 1 if text.startswith("created") else 0
            self.counts["modified"] += 0 if text.startswith("created") else 1
            self.files.addItem(text)
        elif kind == "error":
            self.counts["errors"] += 1
        elif kind == "fix":
            self.counts["fixes"] += 1
            self.diff.setPlainText(e.get("diff", ""))
            self.files.addItem("fixed " + e.get("path", ""))
        elif kind == "test":
            self.counts["passed" if e.get("passed") else "failed"] += 1
        self.term.appendPlainText(f"{'$' if kind == 'command' else '!' if kind == 'error' else '✓' if kind in ('fix', 'test', 'file') else '·'} {text}")
        self._update_counts()

    def _update_counts(self) -> None:
        c = self.counts
        self.counter.setText(f"Created {c['files']} · modified {c['modified']} · errors {c['errors']} · fixes applied {c['fixes']} · "
                             f"test runs passed {c['passed']} / failed {c['failed']}")

    def _on_done(self, res) -> None:
        self._busy = False
        if isinstance(res, Exception):
            self.bar.setFormat("Failed")
            self.result.setText(f"That didn't work: {res}")
            return
        data = res.data or {}
        self._workspace = data.get("workspace") or self._workspace
        self.bar.setValue(len(STAGES) if res.success else self.bar.value())
        self.bar.setFormat({"verified": "Verified", "partial": "Partial", "not_verified": "Not verified"}.get(data.get("status"), "Done" if res.success else "Failed"))
        self.result.setText(res.message)
        self._fill_tree()

    def _fill_tree(self) -> None:
        self.tree.clear()
        if not self._workspace or not Path(self._workspace).is_dir():
            return
        from .gace.workspace import SKIP_DIRS

        root = Path(self._workspace)
        nodes: dict[str, QTreeWidgetItem] = {}
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root)
            if set(rel.parts) & SKIP_DIRS:
                continue
            parent = nodes.get(rel.parent.as_posix()) if rel.parent.parts else None
            item = QTreeWidgetItem(parent or self.tree, [p.name])
            if p.is_dir():
                nodes[rel.as_posix()] = item
