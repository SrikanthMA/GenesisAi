"""The compact floating assistant (State A)."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QGuiApplication, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from . import theme
from .chat_widgets import GlyphButton
from .orb import OrbWidget


class MiniAssistant(QWidget):
    submitted = Signal(str)
    mic_clicked = Signal()
    expand_clicked = Signal()
    confirm_decided = Signal(str, bool)

    WIDTH = 460

    def __init__(self, settings) -> None:
        super().__init__(None)
        self.settings = settings
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if settings.get("ui.always_on_top", True):
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("Genesis")
        self.setFixedWidth(self.WIDTH)
        self._drag: Optional[QPoint] = None
        self._state = "IDLE"
        self._confirm_id: Optional[str] = None
        self._user_moved = False

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(12)
        self.orb = OrbWidget(58)
        top.addWidget(self.orb, 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        title_row = QHBoxLayout()
        self.title = QLabel("Genesis")
        self.title.setStyleSheet("font-size: 13px; font-weight: 600; letter-spacing: 0.5px;")
        self.mic_dot = QLabel("● mic")
        self.mic_dot.setToolTip("Microphone status (privacy indicator)")
        self.mic_dot.setStyleSheet(f"color: {theme.MUTED}; font-size: 11px;")
        title_row.addWidget(self.title)
        title_row.addStretch(1)
        title_row.addWidget(self.mic_dot)
        texts.addLayout(title_row)
        self.body = QLabel("Say “Hey Genesis” or type below.")
        self.body.setWordWrap(True)
        self.body.setStyleSheet(f"color: {theme.TEXT}; font-size: 13px;")
        self.body.setMaximumHeight(96)
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        texts.addWidget(self.body)
        top.addLayout(texts, 1)
        btns = QVBoxLayout()
        btns.setSpacing(6)
        self.close_btn = GlyphButton("minimize", "Hide", 26, on_click=self.fade_out)
        self.expand_btn = GlyphButton("expand", "Open full chat", 26, on_click=self.expand_clicked.emit)
        btns.addWidget(self.close_btn)
        btns.addWidget(self.expand_btn)
        btns.addStretch(1)
        top.addLayout(btns)
        root.addLayout(top)

        self.confirm_bar = QWidget()
        cb = QHBoxLayout(self.confirm_bar)
        cb.setContentsMargins(0, 0, 0, 0)
        self.confirm_label = QLabel("")
        self.confirm_label.setWordWrap(True)
        self.confirm_label.setStyleSheet(f"color: {theme.AMBER}; font-size: 12px;")
        self.no_btn = QPushButton("Cancel")
        self.yes_btn = QPushButton("Confirm")
        self.yes_btn.setObjectName("Primary")
        cb.addWidget(self.confirm_label, 1)
        cb.addWidget(self.no_btn)
        cb.addWidget(self.yes_btn)
        self.no_btn.clicked.connect(lambda: self._decide(False))
        self.yes_btn.clicked.connect(lambda: self._decide(True))
        self.confirm_bar.hide()
        root.addWidget(self.confirm_bar)

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Ask Genesis to do something…")
        self.input.returnPressed.connect(self._submit)
        self.input.setAccessibleName("Message Genesis")
        self.mic_btn = GlyphButton("mic", "Talk (push to talk)", 36, on_click=self.mic_clicked.emit)
        self.send_btn = GlyphButton("send", "Send", 36, on_click=self._submit)
        bottom.addWidget(self.input, 1)
        bottom.addWidget(self.mic_btn)
        bottom.addWidget(self.send_btn)
        root.addLayout(bottom)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._maybe_auto_hide)
        self._anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.adjustSize()

    # ------------------------------------------------------------ painting
    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(r, 22, 22)
        grad = QLinearGradient(r.topLeft(), r.bottomRight())
        grad.setColorAt(0, QColor(22, 22, 30, 246))
        grad.setColorAt(1, QColor(10, 10, 14, 250))
        p.fillPath(path, QBrush(grad))
        c1, _ = theme.STATE_COLORS.get(self._state, theme.STATE_COLORS["IDLE"])
        edge = QColor(c1)
        edge.setAlphaF(0.55 if self._state != "IDLE" else 0.25)
        p.setPen(QPen(edge, 1.2))
        p.drawPath(path)
        p.end()

    # ------------------------------------------------------------ behaviour
    def place_default(self) -> None:
        if self._user_moved:
            return
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        g = screen.availableGeometry()
        self.adjustSize()
        self.move(g.right() - self.width() - 24, g.bottom() - self.height() - 24)

    def summon(self, focus_input: bool = True) -> None:
        self._hide_timer.stop()
        self.place_default()
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self.activateWindow()
        self._anim.stop()
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.start()
        if focus_input:
            self.input.setFocus()

    def fade_out(self) -> None:
        self._anim.stop()
        self._anim.setStartValue(self.windowOpacity())
        self._anim.setEndValue(0.0)
        try:
            self._anim.finished.disconnect()
        except (RuntimeError, TypeError):
            pass
        self._anim.finished.connect(self._after_fade)
        self._anim.start()

    def _after_fade(self) -> None:
        try:
            self._anim.finished.disconnect()
        except (RuntimeError, TypeError):
            pass
        if self.windowOpacity() <= 0.01:
            self.hide()
            self.setWindowOpacity(1.0)

    def schedule_auto_hide(self) -> None:
        secs = int(self.settings.get("ui.auto_hide_seconds", 10) or 0)
        if secs > 0:
            self._hide_timer.start(secs * 1000)

    def _maybe_auto_hide(self) -> None:
        if self.underMouse() or self.input.hasFocus() and self.input.text() or self._confirm_id or \
                self._state not in ("IDLE", "SUCCESS", "ERROR"):
            self.schedule_auto_hide()
            return
        self.fade_out()

    def set_state(self, state: str, detail: str = "") -> None:
        self._state = state
        self.orb.set_state(state)
        label = theme.STATE_LABELS.get(state, state.title())
        self.title.setText(f"Genesis · {label}" if state != "IDLE" else "Genesis")
        if detail and state in ("EXECUTING", "THINKING"):
            self.body.setText(detail)
        if state in ("IDLE", "SUCCESS", "ERROR"):
            self.schedule_auto_hide()
        else:
            self._hide_timer.stop()
        self.update()

    def show_text(self, text: str) -> None:
        t = text.strip()
        self.body.setText(t if len(t) < 420 else t[:400] + "… (open chat for more)")
        self.adjustSize()

    def set_mic(self, open_: bool, listening: bool) -> None:
        if open_:
            self.mic_dot.setText("● mic on")
            self.mic_dot.setStyleSheet(f"color: {theme.GREEN}; font-size: 11px;")
        else:
            self.mic_dot.setText("● mic off" if not listening else "● mic")
            self.mic_dot.setStyleSheet(f"color: {theme.MUTED}; font-size: 11px;")

    def ask_confirmation(self, conf: dict) -> None:
        self._confirm_id = conf.get("id")
        self.confirm_label.setText(conf.get("prompt", "Continue?"))
        self.confirm_bar.show()
        self.adjustSize()
        if not self.isVisible():
            self.summon(focus_input=False)

    def clear_confirmation(self) -> None:
        self._confirm_id = None
        self.confirm_bar.hide()
        self.adjustSize()

    def _decide(self, ok: bool) -> None:
        if self._confirm_id:
            cid = self._confirm_id
            self.clear_confirmation()
            self.confirm_decided.emit(cid, ok)

    def _submit(self) -> None:
        text = self.input.text().strip()
        if text:
            self.input.clear()
            self.body.setText(f"“{text}”")
            self.submitted.emit(text)

    # ------------------------------------------------------------ dragging
    def mousePressEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        if self._drag is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)
            self._user_moved = True
            e.accept()

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        self._drag = None

    def keyPressEvent(self, e) -> None:  # noqa: N802
        if e.key() == Qt.Key.Key_Escape:
            self.fade_out()
        else:
            super().keyPressEvent(e)
