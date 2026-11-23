"""Memory page — view, delete, disable (transparency + control)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                               QPushButton, QScrollArea, QVBoxLayout, QWidget)

from ...core.events import EventType


def _section(title: str, hint: str = "") -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 8, 0, 0)
    t = QLabel(title)
    t.setObjectName("H2")
    lay.addWidget(t)
    if hint:
        h = QLabel(hint)
        h.setObjectName("Muted")
        h.setWordWrap(True)
        lay.addWidget(h)
    return w


class MemoryPage(QWidget):
    def __init__(self, core) -> None:
        super().__init__()
        self.core = core
        self.svc = core.svc
        self.setObjectName("Page")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        body.setObjectName("Page")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(28, 22, 28, 22)
        lay.setSpacing(10)
        scroll.setWidget(body)
        outer.addWidget(scroll)

        h = QLabel("Memory")
        h.setObjectName("H1")
        lay.addWidget(h)
        info = QLabel("Genesis only keeps what you explicitly ask it to remember, plus your conversation history. "
                      "Everything is stored locally in SQLite on this computer and can be deleted here.")
        info.setObjectName("Muted")
        info.setWordWrap(True)
        lay.addWidget(info)
        self.enabled = QCheckBox("Enable memory (conversation history, facts and contacts)")
        self.enabled.toggled.connect(lambda v: self.svc.settings.set("memory.enabled", bool(v)))
        lay.addWidget(self.enabled)
        self.autosave = QCheckBox("Save e-mail addresses I give Genesis for named people (shown when it happens)")
        self.autosave.toggled.connect(lambda v: self.svc.settings.set("memory.auto_save_contacts", bool(v)))
        lay.addWidget(self.autosave)

        lay.addWidget(_section("Facts", "Things you asked Genesis to remember."))
        self.facts = QListWidget()
        self.facts.setMinimumHeight(120)
        lay.addWidget(self.facts)
        row = QHBoxLayout()
        self.fact_input = QLineEdit()
        self.fact_input.setPlaceholderText("Add a fact, e.g. “I prefer formal e-mails”")
        add = QPushButton("Add")
        add.clicked.connect(self._add_fact)
        del_fact = QPushButton("Delete selected")
        del_fact.clicked.connect(self._del_fact)
        row.addWidget(self.fact_input, 1)
        row.addWidget(add)
        row.addWidget(del_fact)
        lay.addLayout(row)

        lay.addWidget(_section("Contacts", "Used to resolve “my professor”, “Rahul”, etc. to e-mail addresses."))
        self.contacts = QListWidget()
        self.contacts.setMinimumHeight(120)
        lay.addWidget(self.contacts)
        crow = QHBoxLayout()
        self.c_name = QLineEdit()
        self.c_name.setPlaceholderText("Name (e.g. Professor Rao)")
        self.c_alias = QLineEdit()
        self.c_alias.setPlaceholderText("Alias (e.g. professor)")
        self.c_email = QLineEdit()
        self.c_email.setPlaceholderText("E-mail")
        cadd = QPushButton("Save contact")
        cadd.clicked.connect(self._add_contact)
        cdel = QPushButton("Delete selected")
        cdel.clicked.connect(self._del_contact)
        for w in (self.c_name, self.c_alias, self.c_email):
            crow.addWidget(w, 1)
        crow.addWidget(cadd)
        crow.addWidget(cdel)
        lay.addLayout(crow)

        lay.addWidget(_section("Projects", "Folders Genesis opens when you say “open my <name> project”."))
        self.projects = QListWidget()
        self.projects.setMinimumHeight(80)
        lay.addWidget(self.projects)
        pdel = QPushButton("Remove selected project")
        pdel.clicked.connect(self._del_project)
        lay.addWidget(pdel, 0, Qt.AlignmentFlag.AlignLeft)

        lay.addWidget(_section("Routines", "Scheduled or named command sequences, e.g. “Every Monday at 8 AM, open VS Code "
                                           "and my Genesis project”. They run through the normal permission checks."))
        self.routines = QListWidget()
        self.routines.setMinimumHeight(80)
        lay.addWidget(self.routines)
        rrow = QHBoxLayout()
        rtoggle = QPushButton("Enable / disable selected")
        rtoggle.clicked.connect(self._toggle_routine)
        rdel = QPushButton("Delete selected routine")
        rdel.clicked.connect(self._del_routine)
        rrow.addWidget(rtoggle)
        rrow.addWidget(rdel)
        rrow.addStretch(1)
        lay.addLayout(rrow)

        lay.addWidget(_section("Usage patterns", "Counts of the tools/apps you use (names only, no content). "
                                                 "Used for suggestions; turn off in Settings → Privacy."))
        self.usage = QListWidget()
        self.usage.setMinimumHeight(80)
        lay.addWidget(self.usage)
        uclear = QPushButton("Clear usage patterns")
        uclear.clicked.connect(self._clear_usage)
        lay.addWidget(uclear, 0, Qt.AlignmentFlag.AlignLeft)

        lay.addWidget(_section("To-do list & notes", "Say “add … to my to-do list” or “take a note: …”. Stored only on this PC."))
        self.todos = QListWidget()
        self.todos.setMinimumHeight(80)
        lay.addWidget(self.todos)
        self.notes = QListWidget()
        self.notes.setMinimumHeight(80)
        lay.addWidget(self.notes)
        trow = QHBoxLayout()
        tdel = QPushButton("Delete selected task")
        tdel.clicked.connect(self._del_todo)
        ndel = QPushButton("Delete selected note")
        ndel.clicked.connect(self._del_note)
        trow.addWidget(tdel)
        trow.addWidget(ndel)
        trow.addStretch(1)
        lay.addLayout(trow)

        lay.addWidget(_section("This session", "Tasks Genesis performed since it started (not saved)."))
        self.session = QListWidget()
        self.session.setMinimumHeight(100)
        lay.addWidget(self.session)

        danger = QHBoxLayout()
        b1 = QPushButton("Clear current conversation")
        b1.clicked.connect(self._clear_conv)
        b2 = QPushButton("Delete all conversations")
        b2.setObjectName("Danger")
        b2.clicked.connect(self._delete_all_convs)
        b3 = QPushButton("Wipe long-term memory")
        b3.setObjectName("Danger")
        b3.clicked.connect(self._wipe)
        danger.addWidget(b1)
        danger.addWidget(b2)
        danger.addWidget(b3)
        danger.addStretch(1)
        lay.addLayout(danger)
        lay.addStretch(1)

    def showEvent(self, e) -> None:  # noqa: N802
        self.refresh()
        super().showEvent(e)

    def refresh(self) -> None:
        snap = self.svc.memory.snapshot()
        self.enabled.blockSignals(True)
        self.enabled.setChecked(snap["enabled"])
        self.enabled.blockSignals(False)
        self.autosave.blockSignals(True)
        self.autosave.setChecked(bool(self.svc.settings.get("memory.auto_save_contacts", True)))
        self.autosave.blockSignals(False)
        self.facts.clear()
        for f in snap["facts"]:
            it = QListWidgetItem(f"{f['content']}   ·   {f['created_at'][:10]}")
            it.setData(Qt.ItemDataRole.UserRole, f["id"])
            self.facts.addItem(it)
        self.contacts.clear()
        for c in snap["contacts"]:
            txt = c["name"] + (f"  ({c['alias']})" if c.get("alias") else "") + (f"  <{c['email']}>" if c.get("email") else "")
            it = QListWidgetItem(txt)
            it.setData(Qt.ItemDataRole.UserRole, c["id"])
            self.contacts.addItem(it)
        self.projects.clear()
        for p in snap["projects"]:
            it = QListWidgetItem(f"{p['name']}  →  {p['path']}")
            it.setData(Qt.ItemDataRole.UserRole, p["name"])
            self.projects.addItem(it)
        self.routines.clear()
        day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        for r in self.svc.repos.routines.list():
            days = ", ".join(day_names[d] for d in r.get("days") or [] if 0 <= int(d) < 7) or "on request"
            when = f"{days} {r.get('time_of_day') or ''}".strip()
            state = "" if r.get("enabled") else "  [disabled]"
            it = QListWidgetItem(f"{r['name']}  ·  {when}  →  {r['commands']}{state}")
            it.setData(Qt.ItemDataRole.UserRole, [int(r["id"]), bool(r.get("enabled"))])
            self.routines.addItem(it)
        self.usage.clear()
        for u in self.svc.repos.usage.top(15):
            self.usage.addItem(f"{u['key']}  ×{u['count']}")
        self.todos.clear()
        for t in self.svc.repos.todos.list(include_done=True)[:50]:
            it = QListWidgetItem(("✓ " if t["done"] else "☐ ") + t["text"] + (f"   · due {t['due'][:16].replace('T', ' ')}" if t.get("due") else ""))
            it.setData(Qt.ItemDataRole.UserRole, t["id"])
            self.todos.addItem(it)
        self.notes.clear()
        for n in self.svc.repos.notes.list()[:50]:
            it = QListWidgetItem(f"📝 {n['title']}   ·   {n['updated_at'][:10]}")
            it.setToolTip(n["content"][:500])
            it.setData(Qt.ItemDataRole.UserRole, n["id"])
            self.notes.addItem(it)
        self.session.clear()
        for t in reversed(snap["session_tasks"]):
            self.session.addItem(f"[{t['status']}] {t['request']} — {t['summary'][:120]}")

    def _add_fact(self) -> None:
        t = self.fact_input.text().strip()
        if t:
            if self.svc.memory.remember(t) is None:
                QMessageBox.information(self, "Memory is off", "Enable memory to store facts.")
            self.fact_input.clear()
            self.refresh()

    def _del_fact(self) -> None:
        for it in self.facts.selectedItems():
            self.svc.repos.memory.delete(int(it.data(Qt.ItemDataRole.UserRole)))
        self.refresh()

    def _add_contact(self) -> None:
        from ...security.validators import ValidationError, validate_email

        name = self.c_name.text().strip()
        email = self.c_email.text().strip() or None
        if not name:
            return
        if email:
            try:
                email = validate_email(email)
            except ValidationError as exc:
                QMessageBox.warning(self, "Invalid e-mail", str(exc))
                return
        self.svc.repos.contacts.upsert(name, email=email, alias=self.c_alias.text().strip() or None)
        for w in (self.c_name, self.c_alias, self.c_email):
            w.clear()
        self.refresh()

    def _del_contact(self) -> None:
        for it in self.contacts.selectedItems():
            self.svc.repos.contacts.delete(int(it.data(Qt.ItemDataRole.UserRole)))
        self.refresh()

    def _del_project(self) -> None:
        for it in self.projects.selectedItems():
            self.svc.repos.projects.delete(str(it.data(Qt.ItemDataRole.UserRole)))
        self.refresh()

    def _del_todo(self) -> None:
        for it in self.todos.selectedItems():
            self.svc.repos.todos.delete(int(it.data(Qt.ItemDataRole.UserRole)))
        self.refresh()

    def _del_note(self) -> None:
        for it in self.notes.selectedItems():
            self.svc.repos.notes.delete(int(it.data(Qt.ItemDataRole.UserRole)))
        self.refresh()

    def _toggle_routine(self) -> None:
        for it in self.routines.selectedItems():
            rid, enabled = it.data(Qt.ItemDataRole.UserRole)
            self.svc.repos.routines.set_enabled(int(rid), not bool(enabled))
        self.refresh()

    def _del_routine(self) -> None:
        for it in self.routines.selectedItems():
            self.svc.repos.routines.delete(int(it.data(Qt.ItemDataRole.UserRole)[0]))
        self.refresh()

    def _clear_usage(self) -> None:
        self.svc.repos.usage.clear()
        self.refresh()

    def _clear_conv(self) -> None:
        self.core.clear_conversation()
        self.svc.events.emit(EventType.UI_ACTION, action="reload_chat")

    def _delete_all_convs(self) -> None:
        if QMessageBox.question(self, "Delete conversations", "Delete ALL conversation history? This cannot be undone.") \
                == QMessageBox.StandardButton.Yes:
            self.svc.repos.conversations.delete_all()
            self.core.new_conversation()
            self.svc.events.emit(EventType.UI_ACTION, action="reload_chat")

    def _wipe(self) -> None:
        if QMessageBox.question(self, "Wipe memory", "Delete all remembered facts, contacts and preferences?") \
                == QMessageBox.StandardButton.Yes:
            self.svc.memory.wipe_long_term()
            self.refresh()
