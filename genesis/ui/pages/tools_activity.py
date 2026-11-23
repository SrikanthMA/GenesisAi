"""Tools & permissions page, and the activity (audit log) page."""
from __future__ import annotations

from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ...security.permissions import RiskLevel
from .. import theme

POLICIES = [("Default", None), ("Always ask", "ask"), ("Allow without asking", "allow"), ("Disabled", "deny")]


def _table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.horizontalHeader().setStretchLastSection(True)
    t.setWordWrap(True)
    return t


class ToolsPage(QWidget):
    def __init__(self, core) -> None:
        super().__init__()
        self.core = core
        self.svc = core.svc
        self.setObjectName("Page")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 22, 28, 22)
        h = QLabel("Tools & permissions")
        h.setObjectName("H1")
        lay.addWidget(h)
        sub = QLabel("Every action Genesis can take is one of these tools. Low-risk tools run directly, medium-risk tools ask "
                     "first unless you allow them, and high-risk tools (shutdown, restart, bulk delete, running programs) "
                     "ALWAYS ask. There is no tool for running arbitrary shell commands.")
        sub.setObjectName("Muted")
        sub.setWordWrap(True)
        lay.addWidget(sub)
        self.table = _table(["Tool", "Category", "Risk", "Policy", "Description"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        lay.addWidget(self.table, 1)

    def showEvent(self, e) -> None:  # noqa: N802
        self.refresh()
        super().showEvent(e)

    def refresh(self) -> None:
        tools = sorted(self.core.registry.all(), key=lambda t: (t.category, t.name))
        policies = self.svc.repos.permissions.all()
        self.table.setRowCount(len(tools))
        for row, tool in enumerate(tools):
            self.table.setItem(row, 0, QTableWidgetItem(tool.name))
            self.table.setItem(row, 1, QTableWidgetItem(tool.category + (" · remote" if tool.remote else "")))
            risk = QTableWidgetItem(tool.risk.label + (" · always asks" if tool.always_confirm else ""))
            risk.setForeground(QBrush(QColor({RiskLevel.LOW: theme.GREEN, RiskLevel.MEDIUM: theme.AMBER,
                                              RiskLevel.HIGH: theme.ERROR}[tool.risk])))
            self.table.setItem(row, 2, risk)
            combo = QComboBox()
            for label, val in POLICIES:
                combo.addItem(label, val)
            if tool.risk >= RiskLevel.HIGH or tool.always_confirm:
                idx = combo.findData("allow")
                combo.model().item(idx).setEnabled(False)
            current = policies.get(tool.name)
            combo.setCurrentIndex(max(0, combo.findData(current)) if current else 0)
            combo.currentIndexChanged.connect(lambda _i, t=tool, c=combo: self._set(t, c.currentData()))
            self.table.setCellWidget(row, 3, combo)
            self.table.setItem(row, 4, QTableWidgetItem(tool.description))
        self.table.resizeRowsToContents()

    def _set(self, tool, policy) -> None:
        try:
            self.svc.permissions.set_policy(tool.name, policy, tool.risk)
        except PermissionError as exc:
            QMessageBox.warning(self, "Not allowed", str(exc))
            self.refresh()


class ActivityPage(QWidget):
    def __init__(self, core) -> None:
        super().__init__()
        self.svc = core.svc
        self.setObjectName("Page")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 22, 28, 22)
        top = QHBoxLayout()
        h = QLabel("Activity")
        h.setObjectName("H1")
        top.addWidget(h)
        top.addStretch(1)
        btn = QPushButton("Refresh")
        btn.clicked.connect(self.refresh)
        top.addWidget(btn)
        lay.addLayout(top)
        sub = QLabel("Audit log of every tool execution (arguments are redacted of secrets). Also written to logs/tool_execution.log.")
        sub.setObjectName("Muted")
        sub.setWordWrap(True)
        lay.addWidget(sub)
        self.table = _table(["Time (UTC)", "Tool", "Status", "Risk", "Confirmed", "ms", "Result"])
        lay.addWidget(self.table, 2)
        s = QLabel("Security events")
        s.setObjectName("H2")
        lay.addWidget(s)
        self.sec = _table(["Time (UTC)", "Event", "Detail"])
        lay.addWidget(self.sec, 1)

    def showEvent(self, e) -> None:  # noqa: N802
        self.refresh()
        super().showEvent(e)

    def refresh(self) -> None:
        rows = self.svc.repos.tool_executions.recent(200)
        self.table.setRowCount(len(rows))
        colors = {"succeeded": theme.GREEN, "failed": theme.ERROR, "blocked": theme.ERROR, "cancelled_by_user": theme.MUTED}
        for i, r in enumerate(rows):
            vals = [r["created_at"].replace("T", " ")[:19], r["tool"], r["status"], r["risk"], "yes" if r["confirmed"] else "",
                    str(r.get("duration_ms") or ""), (r.get("error") or r.get("message") or "")[:200]]
            for j, v in enumerate(vals):
                item = QTableWidgetItem(v)
                if j == 2 and r["status"] in colors:
                    item.setForeground(QBrush(QColor(colors[r["status"]])))
                self.table.setItem(i, j, item)
        sec = self.svc.repos.security_events.recent(100)
        self.sec.setRowCount(len(sec))
        for i, r in enumerate(sec):
            for j, v in enumerate([r["created_at"].replace("T", " ")[:19], r["event"], r["detail"][:300]]):
                self.sec.setItem(i, j, QTableWidgetItem(v))
