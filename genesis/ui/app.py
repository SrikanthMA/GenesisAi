"""Desktop application bootstrap and controller."""
from __future__ import annotations

import getpass
import sys
from typing import Optional

from PySide6.QtCore import QObject, QTimer, QtMsgType, qInstallMessageHandler
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from ..core.assistant import GenesisCore
from ..core.config import GenesisConfig
from ..core.container import build_container
from ..core.logging_setup import get_logger, setup_logging
from ..voice.manager import VoiceManager
from .bridge import UiBridge
from .hotkey import GlobalHotkey
from .main_window import MainWindow
from .mini_window import MiniAssistant
from .orb import orb_icon
from .theme import stylesheet
from .tray import TrayController

log = get_logger("ui")
SERVER_NAME = f"genesis-desktop-assistant-{getpass.getuser()}"


class GenesisApp(QObject):
    def __init__(self, qapp: QApplication, config: GenesisConfig, minimized: bool = False) -> None:
        super().__init__()
        self.qapp = qapp
        self.svc = build_container(config)
        self.core = GenesisCore(self.svc)
        self.core.start()
        self.bridge = UiBridge(self.svc.events)
        self.voice: Optional[VoiceManager] = None
        try:
            self.voice = VoiceManager(config, self.svc.settings, self.svc.events, handler=self._voice_handler,
                                      session=self.core.session)
            self.voice.start()
        except Exception:
            log.exception("voice subsystem failed to start; continuing text-only")
            self.voice = None
        self.paused = False
        self._saved_wake = True

        self.mini = MiniAssistant(self.svc.settings)
        self.main = MainWindow(self.core, self.bridge, self.voice)
        self.tray = TrayController()
        self._wire()
        self.tray.show()
        self.main.chat.load_history(self.svc.repos.conversations.messages(self.core.ensure_conversation()))
        if hasattr(self.svc.llm, "status"):
            self.main.chat.set_llm(self.svc.llm.status())
        self.hotkey = GlobalHotkey(config.hotkey, lambda: self.bridge.ui_action.emit({"action": "summon"}))
        self.hotkey.start()
        if not minimized:
            QTimer.singleShot(400, self._first_show)
        self._update_listening()

    # --------------------------------------------------------------- wiring
    def _wire(self) -> None:
        b = self.bridge
        b.state_changed.connect(self._on_state)
        b.session_changed.connect(self._on_session)
        b.message.connect(self._on_message)
        b.plan_created.connect(lambda p: self.main.chat.add_plan(p.get("plan", {})))
        b.step_updated.connect(lambda p: self.main.chat.update_step(p.get("plan_id"), p.get("step", {})))
        b.confirmation_required.connect(self._on_confirmation)
        b.confirmation_resolved.connect(self._on_confirmation_resolved)
        b.notification.connect(lambda p: self.tray.notify(p.get("title", "Genesis"), p.get("message", "")))
        b.reminder_due.connect(self._on_reminder)
        b.llm_status.connect(self.main.chat.set_llm)
        b.voice_status.connect(self._on_voice_status)
        b.transcript.connect(self._on_transcript)
        b.ui_action.connect(self._on_ui_action)
        b.settings_changed.connect(self._on_settings_changed)
        b.response_ready.connect(self._on_response)
        b.media_show.connect(self._on_media)
        b.task_failed.connect(lambda msg: self.tray.notify("Genesis error", msg))
        self.tray.cancel_requested.connect(self.core.request_cancel)  # 2.0: stops after the step that is running

        self.mini.submitted.connect(self.submit)
        self.mini.mic_clicked.connect(self.push_to_talk)
        self.mini.expand_clicked.connect(self.open_main)
        self.mini.confirm_decided.connect(self.decide)
        self.main.chat.submitted.connect(self.submit)
        self.main.chat.mic_clicked.connect(self.push_to_talk)
        self.main.chat.confirm_decided.connect(self.decide)
        self.main.conversation_selected.connect(self._switch_conversation)
        self.main.new_conversation.connect(self._new_conversation)

        self.tray.open_requested.connect(self.open_main)
        self.tray.summon_requested.connect(lambda: self.mini.summon())
        self.tray.listen_toggled.connect(self._set_listening)
        self.tray.pause_toggled.connect(self._set_paused)
        self.tray.page_requested.connect(self._open_page)
        self.tray.quit_requested.connect(self.quit)

    # --------------------------------------------------------------- actions
    def _first_show(self) -> None:
        self.mini.summon(focus_input=False)
        hk = self.svc.config.hotkey.upper()
        wake = "Say “Hey Genesis”, press " if self.voice and self.voice.listening_enabled else "Press "
        self.mini.show_text(f"Hi, I'm Genesis. {wake}{hk}, or type below.")
        self.mini.schedule_auto_hide()

    def submit(self, text: str) -> None:
        self.bridge.run_async(self.core.handle, text)

    def decide(self, confirmation_id: str, approved: bool) -> None:
        self.bridge.run_async(self.core.resolve_confirmation, confirmation_id, approved)

    def push_to_talk(self) -> None:
        if self.voice is None or self.voice.mic_error:
            msg = (self.voice.mic_error if self.voice else None) or "Voice is unavailable — see Settings → Voice."
            self.mini.show_text(msg)
            return
        self.mini.summon(focus_input=False)
        self.voice.push_to_talk()

    def open_main(self) -> None:
        self.main.show()
        self.main.raise_()
        self.main.activateWindow()
        self.main.show_page("chat")

    def _open_page(self, key: str) -> None:
        self.open_main()
        self.main.show_page(key)

    def _switch_conversation(self, cid: str) -> None:
        self.core.switch_conversation(cid)
        self.main.chat.load_history(self.svc.repos.conversations.messages(cid))

    def _new_conversation(self) -> None:
        self.core.new_conversation()
        self.main.chat.clear()
        self.main.refresh_conversations()

    def _voice_handler(self, text: str) -> tuple:
        v = self.voice
        resp = self.core.handle(text, source="voice", lang_hint=getattr(v, "heard_language", None),
                                lang_confidence=getattr(v, "heard_language_probability", 0.0))
        return resp.speech, resp.status in ("awaiting_confirmation", "awaiting_input"), resp.language, resp.speech_english

    def _set_listening(self, on: bool) -> None:
        self.svc.settings.set("voice.wake_word_enabled", bool(on))
        if self.voice:
            self.voice.refresh()
        self._update_listening()

    def _set_paused(self, paused: bool) -> None:
        self.paused = paused
        if paused:
            self._saved_wake = bool(self.svc.settings.get("voice.wake_word_enabled", True))
            self._set_listening(False)
            self.tray.notify("Genesis paused", "Microphone off and hotkey ignored until you resume.")
        else:
            self._set_listening(self._saved_wake)

    def _update_listening(self) -> None:
        mic_open = bool(self.voice and self.voice.mic.is_open)
        enabled = bool(self.voice and self.voice.listening_enabled)
        conversing = self.core.session.active and mic_open
        self.tray.set_listening(mic_open, enabled, conversing)
        self.mini.set_mic(mic_open, enabled, conversing)
        self.main.chat.set_mic(mic_open, conversing)

    # ---------------------------------------------------------------- events
    def _on_state(self, p: dict) -> None:
        state = p.get("state", "IDLE")
        if state in ("IDLE", "ACTIVE_CONVERSATION", "SLEEPING") and p.get("success"):
            self.mini.set_state("SUCCESS")
            self.main.chat.set_state(state)
            QTimer.singleShot(1200, lambda: self.mini.set_state(self.core.session.phase.value)
                              if self.mini._state == "SUCCESS" else None)
            return
        self.mini.set_state(state, p.get("detail", ""))
        self.main.chat.set_state(state, p.get("detail", ""))
        self._update_listening()
        if state in ("LISTENING", "WAKING") and not self.mini.isVisible():
            self.mini.summon(focus_input=False)

    def _on_session(self, p: dict) -> None:
        if p.get("active"):
            if p.get("source") in ("wake_word", "push_to_talk", "voice") and not self.mini.isVisible():
                self.mini.summon(focus_input=False)
        else:
            reason = p.get("reason", "")
            msg = "😴 Going to sleep — say “Hey Genesis” when you need me." if reason != "user_request" else \
                "😴 Okay, sleeping. Say “Hey Genesis” when you need me."
            self.mini.show_text(msg)
            self.mini.set_state("SLEEPING")
            self.main.chat.set_state("SLEEPING")
        self._update_listening()

    def _on_message(self, p: dict) -> None:
        if p.get("conversation_id") != self.core.conversation_id:
            return
        role, text = p.get("role"), p.get("text", "")
        self.main.chat.add_message(role, text)
        if role == "assistant":
            self.mini.show_text(text)
            if not self.mini.isVisible() and not self.main.isVisible():
                self.mini.summon(focus_input=False)
        elif role == "user":
            self.mini.show_text(f"“{text}”")
        if role == "user" and self.main.isVisible():
            self.main.refresh_conversations()

    def _on_confirmation(self, conf: dict) -> None:
        self.main.chat.add_confirmation(conf)
        self.mini.ask_confirmation(conf)

    def _on_confirmation_resolved(self, p: dict) -> None:
        self.main.chat.resolve_confirmation(p.get("id", ""), bool(p.get("approved")))
        self.mini.clear_confirmation()

    def _on_reminder(self, p: dict) -> None:
        text = p.get("text", "Reminder")
        self.tray.notify("Genesis reminder", text)
        self.mini.summon(focus_input=False)
        self.mini.show_text(text)
        self.main.chat.add_message("assistant", text)
        if self.voice:
            self.voice.speak(text)

    def _on_voice_status(self, _p: dict) -> None:
        self._update_listening()

    def _on_transcript(self, p: dict) -> None:
        if p.get("note"):
            self.mini.show_text(p["note"])
        elif p.get("text"):
            self.mini.show_text(f"“{p['text']}”")

    def _on_ui_action(self, p: dict) -> None:
        action = p.get("action")
        if action == "summon":
            if not self.paused:
                self.mini.summon()
        elif action == "wake":
            self.mini.summon(focus_input=False)
            self.mini.show_text(self.svc.settings.get("voice.greeting", "I'm listening."))
        elif action == "open_chat":
            self.open_main()
        elif action == "open_settings":
            self._open_page("settings")
        elif action == "hide":
            self.mini.fade_out()
        elif action in ("new_conversation", "clear_conversation", "reload_chat"):
            self.main.chat.load_history(self.svc.repos.conversations.messages(self.core.ensure_conversation()))
            self.main.refresh_conversations()

    def _on_settings_changed(self, p: dict) -> None:
        key = p.get("key", "")
        if key == "llm.health":
            page = getattr(self.main, "settings_page", None)
            if page is not None and hasattr(page, "on_llm_health"):
                page.on_llm_health(str(p.get("value", "")))
            return
        if key.startswith("voice.") and self.voice:
            self.voice.refresh()
            QTimer.singleShot(600, self._update_listening)

    def _on_media(self, p: dict) -> None:
        if p.get("kind") == "image":
            self.main.chat.add_image(p)
            if hasattr(self.mini, "add_image"):
                self.mini.add_image(p)

    def _on_response(self, result) -> None:
        if isinstance(result, tuple) and len(result) == 3 and result[0] == "__callback__":
            _, cb, value = result
            try:
                cb(value)
            except Exception:
                log.exception("UI callback failed")

    def quit(self) -> None:
        log.info("Genesis shutting down")
        self.hotkey.stop()
        if self.voice:
            self.voice.stop()
        self.core.stop()
        self.bridge.shutdown()
        self.tray.tray.hide()
        self.qapp.quit()


def _already_running() -> bool:
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.flush()
        sock.waitForBytesWritten(300)
        sock.disconnectFromServer()
        return True
    return False


# harmless notices: DPI rounding, and legacy bitmap fonts (8514oem, Fixedsys) DirectWrite can't render
_QT_NOISE = ("QWindowsWindow::setGeometry: Unable to set geometry", "DirectWrite: CreateFontFaceFromHDC() failed")


def _qt_messages(kind, _context, message: str) -> None:
    if any(n in message for n in _QT_NOISE):
        return
    if kind in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
        log.error("Qt: %s", message)
    elif kind == QtMsgType.QtWarningMsg:
        log.warning("Qt: %s", message)
    else:
        log.debug("Qt: %s", message)


def run(minimized: bool = False) -> int:
    config = GenesisConfig.from_env()
    config.paths.ensure()
    setup_logging(config.paths.logs_dir, config.log_level)
    qInstallMessageHandler(_qt_messages)
    qapp = QApplication.instance() or QApplication(sys.argv)
    qapp.setApplicationName("Genesis")
    qapp.setQuitOnLastWindowClosed(False)
    qapp.setWindowIcon(orb_icon())
    qapp.setStyleSheet(stylesheet())
    if _already_running():
        log.info("Genesis is already running; asked it to show itself")
        return 0
    QLocalServer.removeServer(SERVER_NAME)
    server = QLocalServer()
    server.listen(SERVER_NAME)
    app = GenesisApp(qapp, config, minimized=minimized)

    def on_conn() -> None:
        conn = server.nextPendingConnection()
        if conn is not None:
            conn.waitForReadyRead(300)
            app.mini.summon()
            conn.close()

    server.newConnection.connect(on_conn)
    qapp.aboutToQuit.connect(server.close)
    return qapp.exec()
