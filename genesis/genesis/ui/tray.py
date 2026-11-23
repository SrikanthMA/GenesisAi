"""System-tray integration."""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from .orb import orb_icon


class TrayController(QObject):
    open_requested = Signal()
    summon_requested = Signal()
    listen_toggled = Signal(bool)
    pause_toggled = Signal(bool)
    page_requested = Signal(str)
    quit_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.tray = QSystemTrayIcon(orb_icon())
        self.tray.setToolTip("Genesis — say “Hey Genesis”")
        menu = QMenu()
        self.menu = menu

        def act(text, slot):
            a = QAction(text, menu)
            a.triggered.connect(slot)
            menu.addAction(a)
            return a

        act("Open Genesis", self.open_requested.emit)
        act("Quick assistant", self.summon_requested.emit)
        menu.addSeparator()
        self.start_listen = act("Start listening", lambda: self.listen_toggled.emit(True))
        self.stop_listen = act("Stop listening", lambda: self.listen_toggled.emit(False))
        self.pause = act("Pause Genesis", self._toggle_pause)
        self.pause.setCheckable(True)
        menu.addSeparator()
        act("Settings", lambda: self.page_requested.emit("settings"))
        act("Memory", lambda: self.page_requested.emit("memory"))
        act("Connected services", lambda: self.page_requested.emit("services"))
        act("Activity", lambda: self.page_requested.emit("activity"))
        menu.addSeparator()
        act("Quit Genesis", self.quit_requested.emit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._activated)

    def show(self) -> None:
        self.tray.show()

    def _toggle_pause(self) -> None:
        self.pause_toggled.emit(self.pause.isChecked())

    def _activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.summon_requested.emit()
        elif reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.open_requested.emit()

    def set_listening(self, mic_open: bool, enabled: bool) -> None:
        self.tray.setIcon(orb_icon(listening=mic_open))
        self.tray.setToolTip("Genesis — microphone ON (listening for “Hey Genesis”)" if mic_open
                             else "Genesis — microphone off")
        self.start_listen.setVisible(not enabled)
        self.stop_listen.setVisible(enabled)

    def notify(self, title: str, message: str) -> None:
        if QSystemTrayIcon.supportsMessages():
            self.tray.showMessage(title, message, orb_icon(), 6000)
