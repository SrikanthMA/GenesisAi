"""Chat building blocks: message bubbles, plan/tool cards, confirmation cards."""
from __future__ import annotations

import html
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from . import theme
from .orb import OrbWidget

STEP_ICONS = {
    "pending": ("○", theme.MUTED), "running": ("◌", theme.VIOLET), "succeeded": ("✓", theme.GREEN),
    "failed": ("✕", theme.ERROR), "skipped": ("–", theme.MUTED), "awaiting_confirmation": ("?", theme.AMBER),
    "cancelled": ("⊘", theme.MUTED), "blocked": ("⛔", theme.ERROR),
}
RISK_COLORS = {"low": theme.GREEN, "medium": theme.AMBER, "high": theme.ERROR}


def _label(text: str, *, wrap: bool = True, selectable: bool = True, name: Optional[str] = None, rich: bool = False) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(wrap)
    lab.setTextFormat(Qt.TextFormat.RichText if rich else Qt.TextFormat.PlainText)
    if selectable:
        lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.LinksAccessibleByMouse)
    if name:
        lab.setObjectName(name)
    return lab


class MessageBubble(QWidget):
    def __init__(self, role: str, text: str, parent=None) -> None:
        super().__init__(parent)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(10)
        bubble = QFrame()
        bubble.setObjectName("Bubble")
        is_user = role == "user"
        bg = "#2A1016" if is_user else theme.PANEL
        border = theme.ACCENT_DIM if is_user else theme.BORDER
        bubble.setStyleSheet(f"QFrame#Bubble {{ background: {bg}; border: 1px solid {border}; border-radius: 16px; }}")
        lay = QVBoxLayout(bubble)
        lay.setContentsMargins(14, 10, 14, 10)
        self.text_label = _label(text)
        self.text_label.setStyleSheet("font-size: 14px; line-height: 150%;")
        lay.addWidget(self.text_label)
        bubble.setMaximumWidth(620)
        bubble.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        if is_user:
            outer.addStretch(1)
            outer.addWidget(bubble)
        else:
            orb = OrbWidget(26, animated=False)
            orb.set_state("IDLE")
            outer.addWidget(orb, 0, Qt.AlignmentFlag.AlignTop)
            outer.addWidget(bubble)
            outer.addStretch(1)


class PlanCard(QFrame):
    """Live view of an agent plan: each tool call with its status."""

    def __init__(self, plan: dict, parent=None) -> None:
        super().__init__(parent)
        self.plan_id = plan.get("id")
        self.setObjectName("Card")
        self.setStyleSheet(f"QFrame#Card {{ background: {theme.PANEL}; border: 1px solid {theme.BORDER}; border-radius: 14px; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(6)
        head = QHBoxLayout()
        title = _label(f"⚙  Plan · {len(plan.get('steps', []))} step(s)", selectable=False, name="H2")
        head.addWidget(title)
        head.addStretch(1)
        src = _label(plan.get("source", "rules"), selectable=False, name="Badge")
        head.addWidget(src)
        lay.addLayout(head)
        self._rows: dict[int, tuple[QLabel, QLabel, QLabel]] = {}
        self._lay = lay
        for step in plan.get("steps", []):
            self.update_step(step)
        self.setMaximumWidth(680)

    def update_step(self, step: dict) -> None:
        sid = step.get("id")
        if sid not in self._rows:
            row = QHBoxLayout()
            icon = _label("", selectable=False)
            icon.setFixedWidth(18)
            desc = _label("", selectable=False)
            risk = _label("", selectable=False, name="Badge")
            row.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(desc, 1)
            row.addWidget(risk, 0, Qt.AlignmentFlag.AlignTop)
            self._lay.addLayout(row)
            self._rows[sid] = (icon, desc, risk)
        icon, desc, risk = self._rows[sid]
        status = step.get("status", "pending")
        glyph, color = STEP_ICONS.get(status, ("•", theme.MUTED))
        icon.setText(glyph)
        icon.setStyleSheet(f"color: {color}; font-weight: 700;")
        tool = step.get("tool", "")
        text = f"<b>{html.escape(step.get('description') or tool)}</b> <span style='color:{theme.MUTED}'>· {html.escape(tool)}</span>"
        res = step.get("result") or {}
        if res.get("message") and not step.get("hidden") and status in ("failed", "blocked"):
            text += f"<br><span style='color:{theme.ERROR}'>{html.escape(res['message'][:240])}</span>"
        elif res.get("message") and status == "succeeded" and step.get("verified") is True:
            text += f" <span style='color:{theme.GREEN}'>· verified</span>"
        desc.setTextFormat(Qt.TextFormat.RichText)
        desc.setText(text)
        r = step.get("risk")
        if r:
            risk.setText(r)
            risk.setStyleSheet(f"color: {RISK_COLORS.get(r, theme.MUTED)}; border: 1px solid {theme.BORDER}; border-radius: 9px; "
                               "padding: 1px 8px; font-size: 11px;")
            risk.show()
        else:
            risk.hide()


class ConfirmationCard(QFrame):
    decided = Signal(str, bool)

    def __init__(self, conf: dict, parent=None) -> None:
        super().__init__(parent)
        self.conf_id = conf.get("id", "")
        risk = conf.get("risk", "medium")
        color = theme.ERROR if risk == "high" else theme.AMBER
        self.setObjectName("Confirm")
        self.setStyleSheet(f"QFrame#Confirm {{ background: #1A1408; border: 1px solid {color}; border-radius: 14px; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        top = _label(f"⚠  Confirmation required · {risk.upper()} risk", selectable=False)
        top.setStyleSheet(f"color: {color}; font-weight: 600;")
        lay.addWidget(top)
        prompt = _label(conf.get("prompt", ""))
        prompt.setStyleSheet("font-size: 14px;")
        lay.addWidget(prompt)
        args = conf.get("args") or {}
        details = [f"{k}: {v}" for k, v in args.items() if v not in (None, "", []) and k not in ("base_dir",)]
        if details:
            d = _label("\n".join(str(x)[:300] for x in details[:8]), name="Muted")
            d.setFont(QFont("Consolas", 9))
            lay.addWidget(d)
        row = QHBoxLayout()
        row.addStretch(1)
        self.cancel_btn = QPushButton("Cancel")
        self.ok_btn = QPushButton("Confirm")
        self.ok_btn.setObjectName("Primary")
        row.addWidget(self.cancel_btn)
        row.addWidget(self.ok_btn)
        lay.addLayout(row)
        self.cancel_btn.clicked.connect(lambda: self._decide(False))
        self.ok_btn.clicked.connect(lambda: self._decide(True))
        self.setMaximumWidth(680)

    def _decide(self, ok: bool) -> None:
        self.mark_resolved(ok)
        self.decided.emit(self.conf_id, ok)

    def mark_resolved(self, ok: bool) -> None:
        self.ok_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.ok_btn.setText("Confirmed" if ok else "Confirm")
        self.cancel_btn.setText("Cancelled" if not ok else "Cancel")


class GlyphButton(QPushButton):
    def __init__(self, glyph_name: str, tooltip: str = "", size: int = 34, parent=None,
                 on_click: Optional[Callable[[], None]] = None) -> None:
        super().__init__(parent)
        code, family = theme.glyph(glyph_name)
        self.setText(code)
        if family:
            self.setFont(QFont(family, 11))
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip or glyph_name)
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            f"QPushButton {{ background: {theme.SURFACE_2}; border: 1px solid {theme.BORDER}; border-radius: {size // 2}px; padding: 0; }}"
            f"QPushButton:hover {{ border-color: {theme.ACCENT}; }}"
            f"QPushButton:checked {{ background: {theme.ACCENT}; border-color: {theme.ACCENT}; }}"
        )
        if on_click:
            self.clicked.connect(on_click)
