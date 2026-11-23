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
        ident = self.svc.identity.get()
        self.name = QLineEdit(ident.canonical)
        self.name.setPlaceholderText("Full name — used to sign e-mails")
        self.name.editingFinished.connect(self._save_name)
        form.addRow("Your name", self.name)
        self.call_me = QLineEdit(ident.preferred)
        self.call_me.setPlaceholderText("What Genesis calls you, e.g. Sri")
        self.call_me.editingFinished.connect(self._save_call_me)
        form.addRow("Call me", self.call_me)
        self.variants = QLineEdit(", ".join(ident.nicknames + ident.variants))
        self.variants.setPlaceholderText("Other spellings/nicknames the speech recogniser uses, comma-separated")
        self.variants.editingFinished.connect(self._save_variants)
        form.addRow("Also recognise", self.variants)
        self.engine = QComboBox()
        for e in ("google", "bing", "duckduckgo"):
            self.engine.addItem(e.capitalize(), e)
        self.engine.setCurrentIndex(max(0, self.engine.findData(s.get("search.engine", "google"))))
        self.engine.currentIndexChanged.connect(lambda _i: s.set("search.engine", self.engine.currentData()))
        form.addRow("Search engine", self.engine)
        self.address = QLineEdit(str(s.get("persona.address", "Boss") or ""))
        self.address.setPlaceholderText("How Genesis addresses you in replies (“Done, Boss.”) — leave empty for none")
        self.address.editingFinished.connect(lambda: s.set("persona.address", self.address.text().strip()[:40]))
        form.addRow("Address me as", self.address)
        self.wa_mode = QComboBox()
        for label, val in (("Automatic", "auto"), ("WhatsApp Web (in Genesis's browser)", "web"), ("WhatsApp desktop app", "desktop")):
            self.wa_mode.addItem(label, val)
        self.wa_mode.setCurrentIndex(max(0, self.wa_mode.findData(s.get("whatsapp.mode", "auto"))))
        self.wa_mode.currentIndexChanged.connect(lambda _i: s.set("whatsapp.mode", self.wa_mode.currentData()))
        form.addRow("WhatsApp", self.wa_mode)
        # languages (1.6)
        self.lang_mode = QComboBox()
        for label, val in (("Understand any language (translate to English)", "auto"), ("English only", "english")):
            self.lang_mode.addItem(label, val)
        self.lang_mode.setCurrentIndex(max(0, self.lang_mode.findData(s.get("language.mode", "auto"))))
        self.lang_mode.currentIndexChanged.connect(lambda _i: s.set("language.mode", self.lang_mode.currentData()))
        form.addRow("Languages", self.lang_mode)
        self.lang_reply = QComboBox()
        self.lang_reply.addItem("The language I speak", "same")
        self.lang_reply.addItem("Always English", "english")
        from ...nlp.language import LANGUAGES

        for code, (name, native) in sorted(LANGUAGES.items(), key=lambda kv: kv[1][0]):
            if code not in ("en", "hi-Latn"):
                self.lang_reply.addItem(f"Always {name} ({native})", code)
        self.lang_reply.setCurrentIndex(max(0, self.lang_reply.findData(s.get("language.reply", "same"))))
        self.lang_reply.currentIndexChanged.connect(lambda _i: s.set("language.reply", self.lang_reply.currentData()))
        form.addRow("Reply in", self.lang_reply)
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
        self.gender = QComboBox()
        for label, val in (("Female", "female"), ("Male", "male"), ("Any", "any")):
            self.gender.addItem(label, val)
        self.gender.setCurrentIndex(max(0, self.gender.findData(s.get("voice.gender", "female"))))
        self.gender.currentIndexChanged.connect(self._gender)
        vform.addRow("Voice style", self.gender)
        self.voice_pick = QComboBox()
        self.voice_pick.addItem("Automatic (most natural voice of that style)", "")
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

        # ---- conversation mode
        c, cl = card("Conversation mode")
        chint = QLabel("After you say the wake phrase once, Genesis keeps listening for follow-ups without the wake word, "
                       "then goes back to sleep after the silence timeout below. The timeout only starts after Genesis "
                       "has finished replying.")
        chint.setObjectName("Muted")
        chint.setWordWrap(True)
        cl.addWidget(chint)
        self.conv_on = QCheckBox("Continuous conversation (no wake word needed for follow-ups)")
        self.conv_on.setChecked(bool(s.get("conversation.enabled", True)))
        self.conv_on.toggled.connect(lambda val: self._set("conversation.enabled", bool(val)))
        self.auto_sleep = QCheckBox("Go back to sleep automatically when I stop talking")
        self.auto_sleep.setChecked(bool(s.get("conversation.auto_sleep", True)))
        self.auto_sleep.toggled.connect(lambda val: self._set("conversation.auto_sleep", bool(val)))
        self.barge = QCheckBox("Let me interrupt Genesis while it is speaking (a headset works best)")
        self.barge.setChecked(bool(s.get("voice.barge_in", True)))
        self.barge.toggled.connect(lambda val: self._set("voice.barge_in", bool(val)))
        for w in (self.conv_on, self.auto_sleep, self.barge):
            cl.addWidget(w)
        cform = QFormLayout()
        self.conv_timeout = self._spin(5, 300, " s", "conversation.timeout_seconds", 20)
        cform.addRow("Sleep after silence of", self.conv_timeout)
        self.q_timeout = self._spin(10, 600, " s", "conversation.question_timeout_seconds", 45)
        cform.addRow("…or, when Genesis asked you a question", self.q_timeout)
        self.max_session = self._spin(1, 240, " min", "conversation.max_session_minutes", 30)
        cform.addRow("Longest continuous session", self.max_session)
        self.eot = self._spin(300, 3000, " ms", "voice.end_of_turn_ms", 900)
        self.eot.setSingleStep(100)
        cform.addRow("Pause that ends a sentence", self.eot)
        self.wake_phrase = QLineEdit(s.get("voice.wake_phrase", "hey genesis"))
        self.wake_phrase.setPlaceholderText("hey genesis")
        self.wake_phrase.editingFinished.connect(self._wake_phrase)
        cform.addRow("Wake phrase (restart to apply)", self.wake_phrase)
        cl.addLayout(cform)
        lay.addWidget(c)

        # ---- privacy & safety
        p, pl = card("Privacy & safety")
        self.track = QCheckBox("Learn which apps and actions I use most (names only — never message content)")
        self.track.setChecked(bool(s.get("memory.track_usage", True)))
        self.track.toggled.connect(lambda val: self._set("memory.track_usage", bool(val)))
        self.suggest = QCheckBox("Suggest routines when I repeat the same steps")
        self.suggest.setChecked(bool(s.get("memory.suggest_routines", True)))
        self.suggest.toggled.connect(lambda val: self._set("memory.suggest_routines", bool(val)))
        self.screen_on = QCheckBox("Allow screen analysis when I ask for it (never captured automatically)")
        self.screen_on.setChecked(bool(s.get("screen.enabled", True)))
        self.screen_on.toggled.connect(lambda val: self._set("screen.enabled", bool(val)))
        self.confirm_med = QCheckBox("Ask before medium-risk actions too (closing apps, moving files, typing text)")
        self.confirm_med.setChecked(bool(s.get("security.confirm_medium", True)))
        self.confirm_med.toggled.connect(lambda val: self._set("security.confirm_medium", bool(val)))
        self.log_content = QCheckBox("Debug: include what I say in log files (off by default)")
        self.log_content.setChecked(bool(s.get("debug.log_content", False)))
        self.log_content.toggled.connect(lambda val: self._set("debug.log_content", bool(val)))
        for w in (self.track, self.suggest, self.screen_on, self.confirm_med, self.log_content):
            pl.addWidget(w)
        always = QLabel("Sending e-mails, deleting files, shutdown/restart, code changes and screen clicks always ask "
                        "for confirmation — that cannot be turned off.")
        always.setObjectName("Muted")
        always.setWordWrap(True)
        pl.addWidget(always)
        lay.addWidget(p)

        # ---- coding agent
        k, kl = card("Coding agent")
        khint = QLabel("When you say “create a Python script that …”, Genesis writes the code, creates a new project folder, "
                       "runs it with a time limit and fixes errors (up to 3 tries). It always asks before creating or running "
                       "code, and warns you if the code deletes files or runs other programs. It never installs packages.")
        khint.setObjectName("Muted")
        khint.setWordWrap(True)
        kl.addWidget(khint)
        kform = QFormLayout()
        self.workspace = QLineEdit(s.get("coding.workspace", ""))
        self.workspace.setPlaceholderText("~/GenesisProjects")
        self.workspace.editingFinished.connect(lambda: s.set("coding.workspace", self.workspace.text().strip()))
        kform.addRow("New projects folder", self.workspace)
        kl.addLayout(kform)
        self.open_vs = QCheckBox("Open new projects in VS Code")
        self.open_vs.setChecked(bool(s.get("coding.open_vscode", True)))
        self.open_vs.toggled.connect(lambda val: s.set("coding.open_vscode", bool(val)))
        kl.addWidget(self.open_vs)
        pref = self.svc.memory.get_preference("coding.language")
        plabel = QLabel(f"Preferred language: {pref or 'not set'} (say “my preferred coding language is Python” to change it)")
        plabel.setObjectName("Muted")
        kl.addWidget(plabel)
        lay.addWidget(k)

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
        st = self.svc.llm.status() if hasattr(self.svc.llm, "status") else {}
        self.ai_status = QLabel(self._ai_text(st))
        self.ai_status.setObjectName("Muted")
        self.ai_status.setWordWrap(True)
        al.addWidget(self.ai_status)
        test_row = QHBoxLayout()
        self.ai_test = QPushButton("Test AI connection")
        self.ai_test.clicked.connect(self._test_ai)
        test_row.addWidget(self.ai_test)
        test_row.addStretch(1)
        al.addLayout(test_row)
        ai_help = QLabel("The model and key come from the .env file in the Genesis folder (HF_TOKEN=hf_… for Hugging Face, or "
                         "LLM_PROVIDER / LLM_API_KEY / LLM_MODEL). Editing .env takes effect without a restart.")
        ai_help.setObjectName("Muted")
        ai_help.setWordWrap(True)
        al.addWidget(ai_help)
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

    @staticmethod
    def _ai_text(st: dict) -> str:
        if not st:
            return ""
        name = f"{st.get('name', '')} · {st.get('model', '')}".strip(" ·")
        if st.get("online"):
            return f"AI model: {name} — ready."
        return f"AI model: {name} — not available ({st.get('last_error') or 'not configured'})."

    def _test_ai(self) -> None:
        """Runs in a background thread (a slow network must never freeze the window)."""
        import threading

        self.ai_test.setEnabled(False)
        self.ai_status.setText("Testing the AI connection…")

        def work():
            try:
                h = self.svc.llm.health(probe=True, reload=True)
                msg = (f"Connected to {h['provider']} · {h['model']} ({h['latency_ms']} ms)." if h["available"]
                       else f"{h['message']}" + (" " + " ".join(h["warnings"]) if h.get("warnings") else ""))
            except Exception as exc:  # pragma: no cover - defensive
                msg = f"The test failed: {type(exc).__name__}"
            self.svc.events.emit(EventType.NOTIFICATION, title="AI connection", message=msg)
            self.svc.events.emit(EventType.LLM_STATUS, **self.svc.llm.status())
            self.svc.events.emit(EventType.SETTINGS_CHANGED, key="llm.health", value=msg)

        threading.Thread(target=work, name="genesis-llm-health", daemon=True).start()

    def on_llm_health(self, msg: str) -> None:
        self.ai_status.setText(msg)
        self.ai_test.setEnabled(True)

    def _save_name(self) -> None:
        name = self.name.text().strip()
        if name and name != self.svc.identity.get().canonical:
            ident = self.svc.identity.set_name(name)
            self.call_me.setText(ident.preferred)

    def _save_call_me(self) -> None:
        if self.call_me.text().strip():
            self.svc.identity.set_preferred(self.call_me.text().strip())

    def _save_variants(self) -> None:
        ident = self.svc.identity.get()
        ident.variants = [v.strip() for v in self.variants.text().split(",") if v.strip()]
        ident.nicknames = []
        self.svc.identity.save(ident)

    def _spin(self, lo: int, hi: int, suffix: str, key: str, default: int) -> QSpinBox:
        sp = QSpinBox()
        sp.setRange(lo, hi)
        sp.setSuffix(suffix)
        try:
            sp.setValue(int(self.svc.settings.get(key, default)))
        except (TypeError, ValueError):
            sp.setValue(default)
        sp.valueChanged.connect(lambda val: self._set(key, int(val)))
        return sp

    def _wake_phrase(self) -> None:
        text = " ".join(self.wake_phrase.text().lower().split())
        if len(text.split()) < 2:
            QMessageBox.information(self, "Wake phrase", "Use at least two words (e.g. “hey genesis”) to avoid false wake-ups.")
            self.wake_phrase.setText(self.svc.settings.get("voice.wake_phrase", "hey genesis"))
            return
        self._set("voice.wake_phrase", text)

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
        if self.voice is not None:
            self.voice.tts.set_voice(name or f"gender:{self.svc.settings.get('voice.gender', 'female')}")
            self.voice.speak("This is my new voice.")

    def _gender(self, _i: int) -> None:
        self.svc.settings.set("voice.gender", self.gender.currentData())
        if self.voice is not None and not (self.svc.settings.get("voice.tts_voice", "") or ""):
            self.voice.tts.set_voice(f"gender:{self.gender.currentData()}")
            self.voice.speak("Hello. This is how I'll sound.")

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
