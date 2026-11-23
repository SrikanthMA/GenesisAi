"""Expanded Genesis window (full chat + management pages)."""
from __future__ import annotations

import sys

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
                               QStackedWidget, QVBoxLayout, QWidget)

from .orb import OrbWidget, orb_icon
from .pages.chat import ChatPage
from .pages.memory import MemoryPage
from .pages.services import ServicesPage
from .pages.settings import SettingsPage
from .pages.tools_activity import ActivityPage, ToolsPage


def _dark_title_bar(widget: QWidget) -> None:
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        hwnd = int(widget.winId())
        value = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (Win10 20H1+ / older builds)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except Exception:
        pass


class MainWindow(QWidget):
    conversation_selected = Signal(str)
    new_conversation = Signal()

    def __init__(self, core, bridge, voice=None) -> None:
        super().__init__()
        self.core = core
        self.setObjectName("ChatRoot")
        self.setWindowTitle("Genesis")
        self.setWindowIcon(orb_icon())
        self.resize(1180, 780)
        self.setMinimumSize(860, 560)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        side = QFrame()
        side.setObjectName("Sidebar")
        side.setFixedWidth(250)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(14, 16, 14, 14)
        sl.setSpacing(6)
        brand = QHBoxLayout()
        brand.addWidget(OrbWidget(30))
        name = QLabel("GENESIS")
        name.setStyleSheet("font-size: 15px; font-weight: 700; letter-spacing: 3px;")
        brand.addWidget(name)
        brand.addStretch(1)
        sl.addLayout(brand)
        sl.addSpacing(10)
        newc = QPushButton("+  New conversation")
        newc.setObjectName("Primary")
        newc.clicked.connect(self.new_conversation.emit)
        sl.addWidget(newc)
        sl.addSpacing(8)

        self.stack = QStackedWidget()
        self.chat = ChatPage()
        self.memory = MemoryPage(core)
        self.tools = ToolsPage(core)
        self.activity = ActivityPage(core)
        self.services = ServicesPage(core, bridge)
        self.settings_page = SettingsPage(core, voice)
        self._pages = {"chat": self.chat, "memory": self.memory, "tools": self.tools, "activity": self.activity,
                       "services": self.services, "settings": self.settings_page}
        group = QButtonGroup(self)
        group.setExclusive(True)
        self._nav: dict[str, QPushButton] = {}
        for key, label in (("chat", "💬  Chat"), ("memory", "🧠  Memory"), ("tools", "🛠  Tools & permissions"),
                           ("activity", "🕘  Activity"), ("services", "🔌  Connected services"), ("settings", "⚙  Settings")):
            b = QPushButton(label)
            b.setObjectName("Nav")
            b.setCheckable(True)
            b.clicked.connect(lambda _c=False, k=key: self.show_page(k))
            group.addButton(b)
            sl.addWidget(b)
            self._nav[key] = b
            self.stack.addWidget(self._pages[key])
        sl.addSpacing(12)
        conv_label = QLabel("CONVERSATIONS")
        conv_label.setStyleSheet("color: #8C8C9C; font-size: 11px; letter-spacing: 1.5px;")
        sl.addWidget(conv_label)
        self.conv_list = QListWidget()
        self.conv_list.setStyleSheet("QListWidget { background: transparent; border: none; }")
        self.conv_list.itemClicked.connect(self._pick_conv)
        sl.addWidget(self.conv_list, 1)
        self.footer = QLabel("")
        self.footer.setObjectName("Muted")
        self.footer.setWordWrap(True)
        sl.addWidget(self.footer)

        root.addWidget(side)
        root.addWidget(self.stack, 1)
        self.show_page("chat")

    def showEvent(self, e) -> None:  # noqa: N802
        _dark_title_bar(self)
        self.refresh_conversations()
        super().showEvent(e)

    def show_page(self, key: str) -> None:
        self.stack.setCurrentWidget(self._pages[key])
        self._nav[key].setChecked(True)

    def refresh_conversations(self) -> None:
        self.conv_list.clear()
        current = self.core.conversation_id
        for c in self.core.svc.repos.conversations.list(60):
            if not c.get("message_count"):
                continue
            it = QListWidgetItem(c["title"][:40])
            it.setToolTip(f"{c['title']}\n{c['updated_at']}")
            it.setData(Qt.ItemDataRole.UserRole, c["id"])
            self.conv_list.addItem(it)
            if c["id"] == current:
                it.setSelected(True)

    def _pick_conv(self, item: QListWidgetItem) -> None:
        self.conversation_selected.emit(str(item.data(Qt.ItemDataRole.UserRole)))
        self.show_page("chat")
