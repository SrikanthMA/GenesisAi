"""Settings page."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QMessageBox, QPushButton, QScrollArea, QSlider, QSpinBox, QVBoxLayout, QWidget)

from ...core.events import EventType
from ...core.startup import is_start_with_windows, set_start_with_windows
from .services import card


class SettingsPage(QWidget):
    def __init__(self, core, voice=None) -> None:
        super().__init__()
        self.core = core
        self.svc = core.svc
        self.voice = voice
        self.setObjectName("Page")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        body.setObjectName("Page")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(28, 22, 28, 22)
        lay.setSpacing(14)
        scroll.setWidget(body)
        outer.addWidget(scroll)
        h = QLabel("Settings")
        h.setObjectName("H1")
        lay.addWidget(h)
        s = self.svc.settings

        # ---- general
        g, gl = card("General")
        form = QFormLayout()
        self.name = QLineEdit(s.get("user.name", ""))
        self.name.setPlaceholderText("Used to sign e-mails")
        self.name.editingFinished.connect(lambda: s.set("user.name", self.name.text().strip()))
        form.addRow("Your name", self.name)
        self.engine = QComboBox()
        for e in ("google", "bing", "duckduckgo"):
            self.engine.addItem(e.capitalize(), e)
        self.engine.setCurrentIndex(max(0, self.engine.findData(s.get("search.engine", "google"))))
        self.engine.currentIndexChanged.connect(lambda _i: s.set("search.engine", self.engine.currentData()))
        form.addRow("Search engine", self.engine)
        gl.addLayout(form)
        self.autostart = QCheckBox("Start Genesis with Windows")
        self.autostart.setChecked(is_start_with_windows() or bool(s.get("system.start_with_windows", False)))
        self.autostart.toggled.connect(self._autostart)
        gl.addWidget(self.autostart)
        lay.addWidget(g)

        # ---- voice
        v, vl = card("Voice")
        self.voice_on = QCheckBox("Enable voice features")
        self.voice_on.setChecked(bool(s.get("voice.enabled", True)))
        self.voice_on.toggled.connect(lambda val: self._set("voice.enabled", bool(val)))
        self.wake_on = QCheckBox("Listen for “Hey Genesis” (microphone stays open while enabled)")
        self.wake_on.setChecked(bool(s.get("voice.wake_word_enabled", True)))
        self.wake_on.toggled.connect(lambda val: self._set("voice.wake_word_enabled", bool(val)))
        self.tts_on = QCheckBox("Speak replies")
        self.tts_on.setChecked(bool(s.get("voice.tts_enabled", True)))
        self.tts_on.toggled.connect(lambda val: self._set("voice.tts_enabled", bool(val)))
        for w in (self.voice_on, self.wake_on, self.tts_on):
            vl.addWidget(w)
        vform = QFormLayout()
        self.sens = QSlider(Qt.Orientation.Horizontal)
        self.sens.setRange(10, 95)
        self.sens.setValue(int(float(s.get("voice.wake_sensitivity", 0.6)) * 100))
        self.sens.valueChanged.connect(lambda val: s.set("voice.wake_sensitivity", val / 100.0))
        vform.addRow("Wake sensitivity (restart to apply)", self.sens)
        self.rate = QSlider(Qt.Orientation.Horizontal)
        self.rate.setRange(-10, 10)
        self.rate.setValue(int(s.get("voice.tts_rate", 0)))
        self.rate.valueChanged.connect(self._rate)
        vform.addRow("Speaking rate", self.rate)
        self.voice_pick = QComboBox()
        self.voice_pick.addItem("System default", "")
        if voice is not None:
            for name in voice.tts.voices():
                self.voice_pick.addItem(name, name)
        idx = self.voice_pick.findData(s.get("voice.tts_voice", ""))
        self.voice_pick.setCurrentIndex(max(0, idx))
        self.voice_pick.currentIndexChanged.connect(self._voice)
        vform.addRow("Voice", self.voice_pick)
        vl.addLayout(vform)
        if voice is not None:
            st = voice.status()
            info = QLabel(f"Wake engine: {st.get('wake_engine') or '—'} · STT: loads on first use · TTS: {st.get('tts')}"
                          + (f"\n⚠ {st['wake_error']}" if st.get("wake_error") else ""))
            info.setObjectName("Muted")
            info.setWordWrap(True)
            vl.addWidget(info)
        lay.addWidget(v)

        # ---- assistant window
        u, ul = card("Assistant window")
        uform = QFormLayout()
        self.autohide = QSpinBox()
        self.autohide.setRange(0, 300)
        self.autohide.setSuffix(" s  (0 = never)")
        self.autohide.setValue(int(s.get("ui.auto_hide_seconds", 10)))
        self.autohide.valueChanged.connect(lambda val: s.set("ui.auto_hide_seconds", int(val)))
        uform.addRow("Auto-hide the mini assistant after", self.autohide)
        ul.addLayout(uform)
        self.ontop = QCheckBox("Keep the mini assistant on top of other windows (restart to apply)")
        self.ontop.setChecked(bool(s.get("ui.always_on_top", True)))
        self.ontop.toggled.connect(lambda val: s.set("ui.always_on_top", bool(val)))
        ul.addWidget(self.ontop)
        hk = QLabel(f"Global hotkey to summon Genesis: {self.svc.config.hotkey.upper()} (change GENESIS_HOTKEY in .env)")
        hk.setObjectName("Muted")
        ul.addWidget(hk)
        lay.addWidget(u)

        # ---- AI
        a, al = card("Language understanding")
        self.llm_nlu = QCheckBox("Use the LLM to understand requests the local NLU can't classify")
        self.llm_nlu.setChecked(bool(s.get("llm.use_for_nlu", True)))
        self.llm_nlu.toggled.connect(lambda val: s.set("llm.use_for_nlu", bool(val)))
        self.llm_plan = QCheckBox("Let the LLM plan multi-step tasks (every step is still validated and permission-checked)")
        self.llm_plan.setChecked(bool(s.get("llm.use_for_planning", True)))
        self.llm_plan.toggled.connect(lambda val: s.set("llm.use_for_planning", bool(val)))
        al.addWidget(self.llm_nlu)
        al.addWidget(self.llm_plan)
        lay.addWidget(a)

        # ---- file access
        f, fl = card("File access")
        hint = QLabel("Genesis can only touch files inside your user folder, your registered projects and the folders below. "
                      "System folders (Windows, Program Files) are always write-protected.")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        fl.addWidget(hint)
        self.roots = QListWidget()
        self.roots.setMaximumHeight(110)
        for r in s.get("security.extra_allowed_roots", []) or []:
            self.roots.addItem(r)
        fl.addWidget(self.roots)
        row = QHBoxLayout()
        add = QPushButton("Allow another folder…")
        add.clicked.connect(self._add_root)
        rem = QPushButton("Remove selected")
        rem.clicked.connect(self._remove_root)
        row.addWidget(add)
        row.addWidget(rem)
        row.addStretch(1)
        fl.addLayout(row)
        if self.svc.config.system_dry_run:
            dr = QLabel("DRY-RUN mode is ON: shutdown/restart/sleep/lock are simulated (GENESIS_SYSTEM_DRY_RUN=1).")
            dr.setStyleSheet("color: #F5A524;")
            fl.addWidget(dr)
        lay.addWidget(f)
        lay.addStretch(1)

    def _set(self, key: str, value) -> None:
        self.svc.settings.set(key, value)
        self.svc.events.emit(EventType.SETTINGS_CHANGED, key=key, value=value)

    def _autostart(self, on: bool) -> None:
        ok, detail = set_start_with_windows(bool(on))
        if not ok:
            QMessageBox.warning(self, "Startup", f"Couldn't change the startup setting: {detail}")
            return
        self.svc.settings.set("system.start_with_windows", bool(on))

    def _rate(self, val: int) -> None:
        self.svc.settings.set("voice.tts_rate", int(val))
        if self.voice is not None:
            self.voice.tts.set_rate(int(val))

    def _voice(self, _i: int) -> None:
        name = self.voice_pick.currentData() or ""
        self.svc.settings.set("voice.tts_voice", name)
        if self.voice is not None and name:
            self.voice.tts.set_voice(name)
            self.voice.speak("This is my new voice.")

    def _add_root(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Allow Genesis to access folder")
        if path:
            roots = list(self.svc.settings.get("security.extra_allowed_roots", []) or [])
            if path not in roots:
                roots.append(path)
                self.svc.settings.set("security.extra_allowed_roots", roots)
                self.roots.addItem(path)

    def _remove_root(self) -> None:
        roots = list(self.svc.settings.get("security.extra_allowed_roots", []) or [])
        for it in self.roots.selectedItems():
            if it.text() in roots:
                roots.remove(it.text())
            self.roots.takeItem(self.roots.row(it))
        self.svc.settings.set("security.extra_allowed_roots", roots)
