"""Connected services, knowledge base and privacy overview."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

from .. import theme

PRIVACY_ROWS = [
    ("Wake-word detection (“Hey Genesis”)", "LOCAL", "Vosk/Porcupine on this PC — raw audio is never uploaded for wake detection."),
    ("Speech-to-text", "LOCAL*", "faster-whisper / Vosk run locally. *Only if you choose the Hugging Face STT engine does audio leave the PC."),
    ("Text-to-speech", "LOCAL", "Windows SAPI voices."),
    ("Intent classification & entity extraction", "LOCAL", "Rules + TF-IDF classifier; the LLM is consulted only for unclear requests."),
    ("Conversation memory, contacts, settings, logs", "LOCAL", "SQLite + log files in the Genesis data folder."),
    ("Credentials (OAuth tokens, API keys)", "LOCAL", "Windows Credential Manager / DPAPI-encrypted vault."),
    ("Desktop actions (apps, files, power)", "LOCAL", "Executed through Genesis's restricted tool layer."),
    ("Knowledge base (RAG)", "LOCAL", "Embeddings and documents stay on this PC (unless the HF embedding provider is chosen)."),
    ("Language model replies & planning", "REMOTE", "Your message text is sent to the configured LLM provider (e.g. Hugging Face). Use Ollama for fully local."),
    ("Web search / page reading", "REMOTE", "Queries go to the search engine / website you asked for."),
    ("Gmail & Google Calendar", "REMOTE", "Only when you ask for an e-mail or calendar action, via Google's official API."),
]


def card(title: str) -> tuple[QFrame, QVBoxLayout]:
    f = QFrame()
    f.setObjectName("Card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(18, 14, 18, 14)
    t = QLabel(title)
    t.setObjectName("H2")
    lay.addWidget(t)
    return f, lay


class ServicesPage(QWidget):
    def __init__(self, core, bridge) -> None:
        super().__init__()
        self.core = core
        self.svc = core.svc
        self.bridge = bridge
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
        h = QLabel("Connected services")
        h.setObjectName("H1")
        lay.addWidget(h)

        # Google
        g, gl = card("Google — Gmail & Calendar")
        self.g_status = QLabel()
        self.g_status.setWordWrap(True)
        gl.addWidget(self.g_status)
        row = QHBoxLayout()
        self.g_connect = QPushButton("Connect Google account")
        self.g_connect.setObjectName("Primary")
        self.g_connect.clicked.connect(self._connect_google)
        self.g_disconnect = QPushButton("Disconnect")
        self.g_disconnect.clicked.connect(self._disconnect_google)
        row.addWidget(self.g_connect)
        row.addWidget(self.g_disconnect)
        row.addStretch(1)
        gl.addLayout(row)
        note = QLabel("Genesis uses Google's secure OAuth sign-in in your browser. It never sees your password. "
                      "Scopes: create drafts/send (gmail.compose), read (gmail.readonly), calendar events.")
        note.setObjectName("Muted")
        note.setWordWrap(True)
        gl.addWidget(note)
        lay.addWidget(g)

        # LLM
        l_card, ll = card("Language model")
        self.llm_label = QLabel()
        self.llm_label.setWordWrap(True)
        ll.addWidget(self.llm_label)
        test = QPushButton("Test connection")
        test.clicked.connect(self._test_llm)
        ll.addWidget(test, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(l_card)

        # Knowledge
        k, kl = card("Knowledge base (local RAG)")
        self.docs = QListWidget()
        self.docs.setMinimumHeight(120)
        kl.addWidget(self.docs)
        krow = QHBoxLayout()
        b1 = QPushButton("Add file…")
        b1.clicked.connect(self._add_file)
        b2 = QPushButton("Add folder…")
        b2.clicked.connect(self._add_folder)
        b3 = QPushButton("Remove selected")
        b3.clicked.connect(self._remove_doc)
        for b in (b1, b2, b3):
            krow.addWidget(b)
        krow.addStretch(1)
        kl.addLayout(krow)
        self.kb_status = QLabel("")
        self.kb_status.setObjectName("Muted")
        kl.addWidget(self.kb_status)
        lay.addWidget(k)

        # Privacy
        p, pl = card("Privacy — what runs where")
        for what, where, detail in PRIVACY_ROWS:
            r = QHBoxLayout()
            badge = QLabel(where)
            color = theme.GREEN if where.startswith("LOCAL") else theme.AMBER
            badge.setStyleSheet(f"color: {color}; border: 1px solid {color}; border-radius: 8px; padding: 1px 8px; font-size: 11px;")
            badge.setFixedWidth(74)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            txt = QLabel(f"<b>{what}</b><br><span style='color:{theme.MUTED}'>{detail}</span>")
            txt.setWordWrap(True)
            r.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
            r.addWidget(txt, 1)
            pl.addLayout(r)
        lay.addWidget(p)
        lay.addStretch(1)
        bridge.service_changed.connect(lambda _p: self.refresh())

    def showEvent(self, e) -> None:  # noqa: N802
        self.refresh()
        super().showEvent(e)

    def refresh(self) -> None:
        g = self.svc.google
        if not g.configured:
            self.g_status.setText("⚠ Not configured — add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to your .env file "
                                  "(see docs/SETUP.md → Gmail). Until then Genesis opens pre-filled Gmail compose windows instead.")
            self.g_connect.setEnabled(False)
            self.g_disconnect.setEnabled(False)
        elif g.connected:
            self.g_status.setText(f"✓ Connected as {g.account_email() or 'your Google account'}.")
            self.g_connect.setEnabled(True)
            self.g_connect.setText("Reconnect")
            self.g_disconnect.setEnabled(True)
        else:
            self.g_status.setText("Not connected.")
            self.g_connect.setEnabled(True)
            self.g_connect.setText("Connect Google account")
            self.g_disconnect.setEnabled(False)
        st = self.svc.llm.status() if hasattr(self.svc.llm, "status") else self.svc.llm.describe()
        where = "REMOTE (cloud)" if st.get("remote") else "LOCAL"
        online = "online" if st.get("online", st.get("configured")) else "offline — local commands still work"
        self.llm_label.setText(f"Provider: <b>{st.get('name')}</b><br>Model: {st.get('model', '—')}<br>Runs: {where}<br>"
                               f"Status: {online}" + (f"<br><span style='color:{theme.AMBER}'>{st.get('last_error')}</span>"
                                                      if st.get("last_error") else ""))
        self.docs.clear()
        for d in self.svc.knowledge.documents():
            it = QListWidgetItem(f"{d['title']}  ·  {d['chunks']} passages  ·  {d['path']}")
            it.setData(Qt.ItemDataRole.UserRole, d["id"])
            self.docs.addItem(it)

    def _connect_google(self) -> None:
        self.g_status.setText("A browser window opened — finish signing in there…")
        self.bridge.run_async(self.svc.google.connect, on_done=lambda email: (
            QMessageBox.information(self, "Google connected", f"Connected as {email}."), self.refresh()))

    def _disconnect_google(self) -> None:
        self.svc.google.disconnect()
        self.refresh()

    def _test_llm(self) -> None:
        if hasattr(self.svc.llm, "reset"):
            self.svc.llm.reset()

        def ping():
            return self.svc.llm.chat([{"role": "user", "content": "Reply with exactly: Genesis online"}], max_tokens=10)

        self.bridge.run_async(ping, on_done=lambda text: (QMessageBox.information(self, "Language model", f"Reply: {text}"),
                                                          self.refresh()))

    def _ingest(self, path: str) -> None:
        self.kb_status.setText("Indexing… (large PDFs can take a minute)")

        def done(info):
            self.kb_status.setText(f"Indexed {info.get('documents', 0)} document(s), {info.get('chunks', 0)} passages.")
            self.refresh()

        from pathlib import Path

        self.bridge.run_async(self.svc.knowledge.ingest, Path(path), on_done=done)

    def _add_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Add document", "", "Documents (*.txt *.md *.pdf *.docx *.py *.csv *.json);;All files (*)")
        if path:
            self._ingest(path)

    def _add_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Add folder")
        if path:
            self._ingest(path)

    def _remove_doc(self) -> None:
        for it in self.docs.selectedItems():
            self.svc.knowledge.remove(int(it.data(Qt.ItemDataRole.UserRole)))
        self.refresh()
