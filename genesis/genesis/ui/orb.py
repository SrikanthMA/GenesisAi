"""The Genesis orb — an animated visual for each assistant state."""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QIcon, QPainter, QPen, QPixmap, QRadialGradient
from PySide6.QtWidgets import QWidget

from .theme import STATE_COLORS


def paint_orb(p: QPainter, rect: QRectF, state: str, t: float, level: float = 0.0) -> None:
    c1, c2 = STATE_COLORS.get(state, STATE_COLORS["IDLE"])
    cx, cy = rect.center().x(), rect.center().y()
    r = min(rect.width(), rect.height()) / 2.0
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    breathe = 0.5 + 0.5 * math.sin(t * (1.2 if state == "IDLE" else 3.0))
    # outer glow
    glow = QRadialGradient(QPointF(cx, cy), r)
    g = QColor(c1)
    g.setAlphaF(0.18 + 0.12 * breathe)
    glow.setColorAt(0.45, g)
    glow.setColorAt(1.0, QColor(0, 0, 0, 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(glow))
    p.drawEllipse(QPointF(cx, cy), r, r)

    core_r = r * (0.42 + (0.04 * breathe if state in ("IDLE", "WAITING_FOR_CONFIRMATION") else 0.0) + 0.08 * level)

    if state == "LISTENING":
        for k in range(3):  # expanding ripples
            phase = (t * 0.9 + k / 3.0) % 1.0
            col = QColor(c1)
            col.setAlphaF(max(0.0, 0.6 * (1 - phase)))
            p.setPen(QPen(col, 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            rr = core_r + phase * (r - core_r)
            p.drawEllipse(QPointF(cx, cy), rr, rr)
    elif state in ("THINKING", "WAITING_FOR_INPUT"):
        pen = QPen(c1, max(2.0, r * 0.07))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        arc_r = core_r + r * 0.2
        box = QRectF(cx - arc_r, cy - arc_r, arc_r * 2, arc_r * 2)
        start = int(-t * 360 * 16) % (360 * 16)
        p.drawArc(box, start, 110 * 16)
        pen.setColor(c2)
        p.setPen(pen)
        p.drawArc(box, start + 180 * 16, 70 * 16)
    elif state == "EXECUTING":
        p.setPen(Qt.PenStyle.NoPen)
        for k in range(6):
            ang = t * 2.4 + k * math.tau / 6
            orbit = core_r + r * 0.22
            col = QColor(c1)
            col.setAlphaF(0.35 + 0.65 * ((k + 1) / 6))
            p.setBrush(col)
            p.drawEllipse(QPointF(cx + orbit * math.cos(ang), cy + orbit * math.sin(ang)), r * 0.06, r * 0.06)
    elif state == "SPEAKING":
        bars = 24
        pen = QPen(c1, max(1.5, r * 0.045))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        for k in range(bars):
            ang = k * math.tau / bars
            amp = 0.5 + 0.5 * math.sin(t * 9 + k * 1.7) * math.sin(t * 3.1 + k)
            inner = core_r + r * 0.08
            outer = inner + r * (0.08 + 0.25 * abs(amp))
            p.drawLine(QPointF(cx + inner * math.cos(ang), cy + inner * math.sin(ang)),
                       QPointF(cx + outer * math.cos(ang), cy + outer * math.sin(ang)))
    elif state in ("WAITING_FOR_CONFIRMATION", "ERROR", "SUCCESS"):
        col = QColor(c1)
        col.setAlphaF(0.4 + 0.5 * breathe)
        p.setPen(QPen(col, max(2.0, r * 0.06)))
        p.setBrush(Qt.BrushStyle.NoBrush)
        rr = core_r + r * 0.18
        p.drawEllipse(QPointF(cx, cy), rr, rr)

    # core sphere
    grad = QRadialGradient(QPointF(cx - core_r * 0.35, cy - core_r * 0.4), core_r * 1.5)
    grad.setColorAt(0.0, QColor(255, 255, 255, 235))
    grad.setColorAt(0.18, c1.lighter(130))
    grad.setColorAt(0.65, c1)
    grad.setColorAt(1.0, c2)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(grad))
    p.drawEllipse(QPointF(cx, cy), core_r, core_r)


class OrbWidget(QWidget):
    def __init__(self, size: int = 64, parent=None, animated: bool = True) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.state = "IDLE"
        self.level = 0.0
        self.animated = animated
        self._t0 = time.monotonic()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)
        if animated:
            self._timer.start(33)  # ~30 fps; paused automatically when hidden
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def set_state(self, state: str) -> None:
        self.state = state
        self.update()

    def showEvent(self, e) -> None:  # noqa: N802
        if self.animated:
            self._timer.start(33)
        super().showEvent(e)

    def hideEvent(self, e) -> None:  # noqa: N802
        self._timer.stop()  # zero CPU while hidden
        super().hideEvent(e)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        t = time.monotonic() - self._t0 if self.animated else 0.8
        paint_orb(p, QRectF(self.rect()), self.state, t, self.level)
        p.end()


def orb_icon(state: str = "IDLE", listening: bool = False) -> QIcon:
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        paint_orb(p, QRectF(0, 0, size, size), "LISTENING" if listening and state == "IDLE" else state, 0.8)
        if listening:  # small privacy dot = microphone is open
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#2FD18B"))
            d = max(4, size // 4)
            p.drawEllipse(size - d, size - d, d - 1, d - 1)
        p.end()
        icon.addPixmap(pm)
    return icon
