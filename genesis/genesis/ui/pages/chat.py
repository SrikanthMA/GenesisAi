"""Full chat page (State B)."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QScrollArea, QVBoxLayout, QWidget

from .. import theme
from ..chat_widgets import ConfirmationCard, GlyphButton, MessageBubble, PlanCard
from ..orb import OrbWidget


class ChatPage(QWidget):
    submitted = Signal(str)
    mic_clicked = Signal()
    confirm_decided = Signal(str, bool)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("Page")
        self._plans: dict[str, PlanCard] = {}
        self._confirms: dict[str, ConfirmationCard] = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 18)
        root.setSpacing(12)

        header = QHBoxLayout()
        self.orb = OrbWidget(40)
        header.addWidget(self.orb)
        tl = QVBoxLayout()
        tl.setSpacing(0)
        self.title = QLabel("Genesis")
        self.title.setObjectName("H1")
        self.subtitle = QLabel("Your desktop assistant — ask me to do things, not just talk about them.")
        self.subtitle.setObjectName("Muted")
        tl.addWidget(self.title)
        tl.addWidget(self.subtitle)
        header.addLayout(tl, 1)
        self.llm_badge = QLabel("AI: …")
        self.llm_badge.setObjectName("Badge")
        self.mic_badge = QLabel("mic off")
        self.mic_badge.setObjectName("Badge")
        header.addWidget(self.llm_badge)
        header.addWidget(self.mic_badge)
        root.addLayout(header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        holder.setObjectName("Page")
        self.msgs = QVBoxLayout(holder)
        self.msgs.setContentsMargins(0, 0, 8, 0)
        self.msgs.setSpacing(8)
        self.msgs.addStretch(1)
        self.scroll.setWidget(holder)
        root.addWidget(self.scroll, 1)

        self.status = QLabel("")
        self.status.setObjectName("Muted")
        root.addWidget(self.status)

        composer = QFrame()
        composer.setObjectName("Composer")
        cl = QHBoxLayout(composer)
        cl.setContentsMargins(10, 8, 10, 8)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Message Genesis — e.g. “draft an email to my professor saying I'll submit tomorrow”")
        self.input.setStyleSheet("QLineEdit { border: none; background: transparent; font-size: 14px; padding: 8px; }")
        self.input.returnPressed.connect(self._submit)
        self.mic_btn = GlyphButton("mic", "Talk (push to talk)", 38, on_click=self.mic_clicked.emit)
        self.send_btn = GlyphButton("send", "Send", 38, on_click=self._submit)
        cl.addWidget(self.input, 1)
        cl.addWidget(self.mic_btn)
        cl.addWidget(self.send_btn)
        root.addWidget(composer)

    # --------------------------------------------------------------- content
    def _insert(self, w: QWidget) -> None:
        self.msgs.insertWidget(self.msgs.count() - 1, w)
        QTimer.singleShot(30, lambda: self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum()))

    def clear(self) -> None:
        while self.msgs.count() > 1:
            item = self.msgs.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._plans.clear()
        self._confirms.clear()

    def add_message(self, role: str, text: str) -> None:
        if text:
            self._insert(MessageBubble(role, text))

    def add_plan(self, plan: dict) -> None:
        if not plan.get("steps"):
            return
        card = PlanCard(plan)
        self._plans[plan.get("id")] = card
        self._insert(card)

    def update_step(self, plan_id: str, step: dict) -> None:
        card = self._plans.get(plan_id)
        if card is not None:
            card.update_step(step)

    def add_confirmation(self, conf: dict) -> None:
        cid = conf.get("id")
        if not cid or cid in self._confirms:
            return
        card = ConfirmationCard(conf)
        card.decided.connect(self.confirm_decided.emit)
        self._confirms[cid] = card
        self._insert(card)

    def resolve_confirmation(self, cid: str, ok: bool) -> None:
        card = self._confirms.get(cid)
        if card is not None:
            card.mark_resolved(ok)

    def load_history(self, messages: list[dict]) -> None:
        self.clear()
        for m in messages:
            if m["role"] in ("user", "assistant"):
                for plan in (m.get("metadata") or {}).get("plans") or []:
                    if m["role"] == "assistant":
                        self.add_plan(plan)
                self.add_message(m["role"], m["content"])

    def set_state(self, state: str, detail: str = "") -> None:
        self.orb.set_state(state)
        label = theme.STATE_LABELS.get(state, state)
        self.status.setText("" if state == "IDLE" else f"{label} {detail}".strip())

    def set_llm(self, status: dict) -> None:
        online = status.get("online")
        name = status.get("model") or status.get("name", "LLM")
        where = "cloud" if status.get("remote") else "local"
        self.llm_badge.setText(f"AI: {name.split('/')[-1]} · {where}" if online else "AI: offline mode")
        self.llm_badge.setToolTip(status.get("last_error") or ("Connected" if online else "Language model unavailable — local commands still work"))
        self.llm_badge.setStyleSheet(f"color: {theme.GREEN if online else theme.AMBER};")

    def set_mic(self, mic_open: bool) -> None:
        self.mic_badge.setText("● mic on" if mic_open else "mic off")
        self.mic_badge.setStyleSheet(f"color: {theme.GREEN if mic_open else theme.MUTED};")

    def _submit(self) -> None:
        text = self.input.text().strip()
        if text:
            self.input.clear()
            self.submitted.emit(text)
