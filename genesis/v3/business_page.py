"""The Business Automation page of the Genesis window (3.4).

Drop files, pick an action (or type a request in plain words), watch the stages, open the result. Every button calls a
real tool from ``v3.business`` on a background thread - there are no placeholder buttons."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
                               QListWidget, QPlainTextEdit, QProgressBar, QPushButton, QTableWidget, QTableWidgetItem,
                               QTabWidget, QVBoxLayout, QWidget)

from . import business
from .biz import common
from .biz.workflows import WORKFLOWS

STAGES = list(common.STAGES)


class _Signals(QObject):
    step = Signal(object)
    done = Signal(object)


class DropZone(QFrame):
    dropped = Signal(list)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("Card")
        self.setAcceptDrops(True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        self.label = QLabel("Drop Excel, CSV, Word, PDF, text or image files here")
        self.label.setObjectName("Muted")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.label)

    def dragEnterEvent(self, e) -> None:  # noqa: N802
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:  # noqa: N802
        paths = []
        for u in e.mimeData().urls():
            p = Path(u.toLocalFile())
            if p.is_dir():
                paths += [str(x) for x in sorted(p.iterdir()) if x.is_file() and x.suffix.lower() in common.KINDS["any"]]
            elif p.is_file():
                paths.append(str(p))
        if paths:
            self.dropped.emit(paths)
        e.acceptProposedAction()


def _table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.horizontalHeader().setStretchLastSection(True)
    t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
    return t


class BusinessPage(QWidget):
    def __init__(self, core) -> None:
        super().__init__()
        self.core = core
        self.svc = core.svc
        self.setObjectName("Page")
        self._busy = False
        self._last_outputs: list[str] = []
        self.sig = _Signals()
        self.sig.step.connect(self._on_step)
        self.sig.done.connect(self._on_done)
        business.STEP_LISTENERS.append(lambda step: self.sig.step.emit(step))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 22, 28, 18)
        lay.setSpacing(10)
        h = QLabel("Business Automation")
        h.setObjectName("H1")
        lay.addWidget(h)
        sub = QLabel("Genesis does the work on your files and saves the result as a new file in Documents\\Genesis Business. "
                     "Your original files are never changed, and nothing is e-mailed without your approval.")
        sub.setObjectName("Muted")
        sub.setWordWrap(True)
        lay.addWidget(sub)

        # files
        row = QHBoxLayout()
        self.drop = DropZone()
        self.drop.dropped.connect(self._add_files)
        row.addWidget(self.drop, 2)
        col = QVBoxLayout()
        add = QPushButton("Add files…")
        add.clicked.connect(self._browse)
        clear = QPushButton("Clear")
        clear.clicked.connect(self._clear_files)
        col.addWidget(add)
        col.addWidget(clear)
        row.addLayout(col)
        self.files = QListWidget()
        self.files.setMaximumHeight(74)
        row.addWidget(self.files, 3)
        lay.addLayout(row)

        # tabs
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)
        self._tab("Excel", "Works on the Excel / CSV file(s) above (or the newest one in Documents, Desktop or Downloads).", [
            ("Analyse and find problems", "business_excel_analyse", {}, None),
            ("Clean and remove duplicates", "business_excel_clean", {}, None),
            ("Combine files into one workbook", "business_excel_merge", {"files": "*"}, None),
            ("Convert CSV to Excel", "business_excel_convert", {"to": "xlsx"}, None),
            ("Export to CSV", "business_excel_convert", {"to": "csv"}, None),
            ("Export to PDF", "business_excel_convert", {"to": "pdf"}, None),
            ("Sales report and dashboard", "business_report", {"kind": "sales"}, None),
            ("Financial summary", "business_report", {"kind": "financial"}, None),
        ], ("Sort, filter, add formulas, pivot, chart - e.g. “add a column Total = Qty * Price then sort by Total descending”",
            "business_excel_edit", "request"))
        self._tab("Documents", "Create Word documents, or work on the Word / PDF files above.", [
            ("Summarise the files", "business_summarise", {"files": "*"}, None),
            ("Compare two Word documents", "business_compare_documents", {"files": "*"}, None),
            ("Convert to PDF", "business_to_pdf", {}, None),
            ("Rewrite a section…", "business_rewrite_section", {}, ("section", "Which section (its heading)?")),
            ("Create an invoice…", "business_create_document", {"kind": "invoice"},
             ("details", "Who is it for, and the items? e.g. “for Beta Ltd: 3 widgets at 1200, 1 install at 500, 18% GST”")),
            ("Create a quotation…", "business_create_document", {"kind": "quotation"}, ("details", "Who is it for, and the items?")),
            ("Write a proposal…", "business_create_document", {"kind": "proposal"}, ("topic", "What is the proposal for?")),
            ("Write a project report…", "business_create_document", {"kind": "project report"}, ("topic", "Which project?")),
            ("Write a letter…", "business_create_document", {"kind": "letter"}, ("topic", "What is the letter about, and to whom?")),
        ], None)
        self._tab("E-mail", "Uses your connected Gmail account. Genesis only reads and writes drafts - it never sends or deletes.", [
            ("Important e-mails from today", "business_email_triage", {}, None),
            ("This week's inbox by priority (save to Excel)", "business_email_triage", {"query": "newer_than:7d", "save": True}, None),
            ("Unanswered e-mails", "business_email_triage", {"unanswered": True}, None),
            ("Write reply drafts for today's unread mail", "business_email_drafts", {}, None),
            ("Write follow-up drafts for unanswered mail", "business_email_drafts", {"unanswered": True}, None),
        ], None)
        self._tab("Data extraction", "Pulls tables, dates, amounts, e-mail addresses and phone numbers out of the files above "
                                     "into Excel, with the source of every item. Scanned files need Tesseract OCR installed.", [
            ("Extract tables and details", "business_extract", {"files": "*"}, None),
            ("Read invoices into Excel", "business_process_invoices", {"files": "*"}, None),
        ], ("Build a table with your own columns (comma-separated), e.g. “Name, Company, Phone”", "business_extract", "columns"))
        self._tab("Research", "Searches the web, reads the sources and writes a cited report. Figures that are not in a source are "
                              "flagged.", [], ("Research question, e.g. “CRM software market in India 2026”", "business_research", "question"))
        self._tab("Workflows", "Ready-made multi-step workflows.", [
            (WORKFLOWS["invoices"][0], "business_process_invoices", {"files": "*"}, None),
            (WORKFLOWS["employees"][0], "business_report", {"kind": "employee"}, None),
            (WORKFLOWS["sales"][0], "business_report", {"kind": "sales"}, None),
            (WORKFLOWS["meeting"][0], "business_meeting_minutes", {}, None),
            (WORKFLOWS["inventory"][0], "business_report", {"kind": "inventory"}, None),
            (WORKFLOWS["research"][0] + "…", "business_research", {}, ("question", "What should I research?")),
        ], None, tips=[f"{v[0]}: {v[1]}" for v in WORKFLOWS.values()])
        self.history = _table(["When", "Task", "Status", "Seconds", "Result"])
        self.history.itemDoubleClicked.connect(lambda it: self._open_row(self.history, it.row()))
        self.tabs.addTab(self.history, "Task history")
        self.generated = _table(["File", "From task", "When", "Folder"])
        self.generated.itemDoubleClicked.connect(lambda it: self._open_row(self.generated, it.row()))
        self.tabs.addTab(self.generated, "Generated files")
        self.tabs.currentChanged.connect(lambda _i: self.refresh())

        # progress + result
        self.bar = QProgressBar()
        self.bar.setRange(0, len(STAGES))
        self.bar.setValue(0)
        self.bar.setFormat("Ready")
        lay.addWidget(self.bar)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(150)
        self.log.setPlaceholderText("What Genesis is doing appears here, stage by stage: understand, plan, execute, verify, deliver.")
        lay.addWidget(self.log)
        bottom = QHBoxLayout()
        self.result = QLabel("")
        self.result.setWordWrap(True)
        self.result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bottom.addWidget(self.result, 1)
        self.open_btn = QPushButton("Open result")
        self.open_btn.setObjectName("Primary")
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(lambda: self._open(self._last_outputs[0]) if self._last_outputs else None)
        folder = QPushButton("Open folder")
        folder.clicked.connect(lambda: self._open(str(common.out_dir(self.svc))))
        bottom.addWidget(self.open_btn)
        bottom.addWidget(folder)
        lay.addLayout(bottom)
        self._buttons: list[QPushButton] = self.findChildren(QPushButton)

    # ------------------------------------------------------------ building
    def _tab(self, title: str, blurb: str, actions: list, free, tips=None) -> None:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(14, 12, 14, 12)
        b = QLabel(blurb)
        b.setObjectName("Muted")
        b.setWordWrap(True)
        v.addWidget(b)
        row = None
        for i, (label, tool, args, prompt) in enumerate(actions):
            if i % 3 == 0:
                row = QHBoxLayout()
                v.addLayout(row)
            btn = QPushButton(label)
            btn.clicked.connect(lambda _c=False, t=tool, a=args, p=prompt, lb=label: self._action(t, dict(a), p, lb))
            row.addWidget(btn)
        if row is not None:
            row.addStretch(1)
        if free:
            hint, tool, key = free
            line = QHBoxLayout()
            edit = QLineEdit()
            edit.setPlaceholderText(hint)
            go = QPushButton("Run")
            go.setObjectName("Primary")

            def fire(_c=False, e=edit, t=tool, k=key):
                text = e.text().strip()
                if not text:
                    return
                val = [c.strip() for c in text.split(",") if c.strip()] if k == "columns" else text
                args = {k: val}
                if t == "business_extract":
                    args["files"] = "*"
                self._action(t, args, None, text[:60])

            go.clicked.connect(fire)
            edit.returnPressed.connect(fire)
            line.addWidget(edit, 1)
            line.addWidget(go)
            v.addLayout(line)
        for tip in tips or []:
            t = QLabel("• " + tip)
            t.setObjectName("Muted")
            t.setWordWrap(True)
            v.addWidget(t)
        v.addStretch(1)
        self.tabs.addTab(w, title)

    # ------------------------------------------------------------ files
    def _add_files(self, paths: list) -> None:
        common.attach(paths)
        self._show_files()

    def _browse(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Choose files", str(common.documents_dir()),
                                                "Supported files (*.xlsx *.xlsm *.csv *.tsv *.docx *.pdf *.txt *.md *.png *.jpg *.jpeg)")
        if paths:
            self._add_files(paths)

    def _clear_files(self) -> None:
        common.clear_attached()
        self._show_files()

    def _show_files(self) -> None:
        self.files.clear()
        for p in common.attached():
            self.files.addItem(Path(p).name)
        n = self.files.count()
        self.drop.label.setText(f"{n} file(s) ready - drop more here" if n else "Drop Excel, CSV, Word, PDF, text or image files here")

    # ------------------------------------------------------------ running
    def _action(self, tool_name: str, args: dict, prompt, label: str) -> None:
        if self._busy:
            return
        if prompt:
            key, question = prompt
            text, ok = QInputDialog.getText(self, label.rstrip("…"), question)
            if not ok or not text.strip():
                return
            args[key] = text.strip()
            if tool_name == "business_create_document" and key == "topic":
                args["details"] = text.strip()
        if args.get("files") == "*":
            args["files"] = []  # = the dropped files
        tool = self.core.registry.get(tool_name)
        if tool is None:
            self.result.setText(f"The tool {tool_name} isn't loaded. Run Update_Genesis_3.4.bat again.")
            return
        self._busy = True
        for b in self._buttons:
            b.setEnabled(False)
        self.log.clear()
        self.result.setText("")
        self.bar.setValue(0)
        self.bar.setFormat(f"{label}: starting…")
        self._last_outputs = []

        def job() -> None:
            try:
                res = tool.run(args, self.svc)
                self.sig.done.emit(res)
            except Exception as exc:  # shown to the user, never swallowed
                self.sig.done.emit(exc)

        threading.Thread(target=job, name="genesis-business", daemon=True).start()

    def _on_step(self, step: dict) -> None:
        if not self._busy:
            return
        stage = step.get("stage", "")
        if stage in STAGES:
            self.bar.setValue(max(self.bar.value(), STAGES.index(stage) + 1))
            self.bar.setFormat(stage.capitalize())
        self.log.appendPlainText(f"{'✓' if step.get('ok') else '✗'} {stage:<10} {step.get('message', '')}")

    def _on_done(self, res) -> None:
        self._busy = False
        for b in self._buttons:
            b.setEnabled(True)
        if isinstance(res, Exception):
            self.bar.setFormat("Failed")
            self.result.setText(f"That didn't work: {res}")
        else:
            self._last_outputs = [p for p in (res.data or {}).get("outputs", []) if Path(p).is_file()]
            self.bar.setValue(len(STAGES) if res.success else self.bar.value())
            self.bar.setFormat("Done" if res.success else "Failed")
            self.result.setText(res.message)
        self.open_btn.setEnabled(bool(self._last_outputs))
        self.refresh()

    # ------------------------------------------------------------ history / files
    def showEvent(self, e) -> None:  # noqa: N802
        self._show_files()
        self.refresh()
        super().showEvent(e)

    def refresh(self) -> None:
        try:
            hist = common.history(self.svc)
            runs, files = hist.recent(100), hist.files(200)
        except Exception:
            return
        self.history.setRowCount(len(runs))
        for i, r in enumerate(runs):
            vals = [time.strftime("%d %b %H:%M", time.localtime(r["ts"])), r["task"], r["status"], f"{r['seconds']:.1f}",
                    ", ".join(Path(p).name for p in r["outputs"]) or "; ".join(s["message"] for s in r["steps"] if not s["ok"])[:200]]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                it.setData(Qt.ItemDataRole.UserRole, r["outputs"][0] if r["outputs"] else "")
                it.setToolTip("\n".join(f"{s['stage']}: {s['message']}" for s in r["steps"]))
                self.history.setItem(i, j, it)
        self.generated.setRowCount(len(files))
        for i, f in enumerate(files):
            vals = [f["name"] + ("" if f["exists"] else "  (moved or deleted)"), f["task"],
                    time.strftime("%d %b %H:%M", time.localtime(f["ts"])), str(Path(f["path"]).parent)]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                it.setData(Qt.ItemDataRole.UserRole, f["path"] if f["exists"] else "")
                self.generated.setItem(i, j, it)

    def _open_row(self, table: QTableWidget, row: int) -> None:
        it = table.item(row, 0)
        path = it.data(Qt.ItemDataRole.UserRole) if it is not None else ""
        if path:
            self._open(path)

    def _open(self, path: str) -> None:
        try:
            self.svc.backend.open_path(path)
        except Exception as exc:
            self.result.setText(f"I couldn't open {path}: {exc}")
