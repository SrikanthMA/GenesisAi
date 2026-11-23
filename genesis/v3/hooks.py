"""How 3.0 connects to existing Genesis code without rewriting it.

``scripts/apply_v3_hooks.py`` appends one line to the end of a few existing modules, e.g.::

    from ..v3.hooks import patch_factory as _v3_hook; _v3_hook(globals())  # v3 hook

Each ``patch_*`` function then wraps what that module defines (adds the ``genesis`` provider, registers the new tools,
recognises the new commands, keeps cloud AI features off in local mode). Wrapping instead of editing means the
original code stays byte-for-byte as it was above that line, and removing the line restores 2.x behaviour exactly.

Every patch is idempotent and does nothing if what it expects isn't there (it logs that instead of failing).
"""
from __future__ import annotations

import functools
import logging
import os

log = logging.getLogger("genesis.v3.hooks")
EXTERNAL_IMAGE_AI = {"cloudflare", "huggingface", "hf", "gemini", "openai", "dalle", "pollinations", "stability",
                     "together", "replicate", "fal", "imagen"}
APPLIED: list[str] = []


def _once(g: dict, key: str) -> bool:
    flag = f"__v3_{key}__"
    if g.get(flag):
        return False
    g[flag] = True
    APPLIED.append(key)
    return True


def _external_allowed() -> bool:
    from .mode import external_ai_allowed

    return external_ai_allowed()


# ------------------------------------------------------------------ core/config.py
def patch_config(g: dict) -> None:
    if not _once(g, "config"):
        return
    aliases = g.get("PROVIDER_ALIASES")
    if isinstance(aliases, dict):
        aliases.update({"genesis": "genesis", "local": "genesis"})
    cls = g.get("GenesisConfig")
    if cls is None:
        log.warning("v3: GenesisConfig not found; provider alias only")
        return
    orig = cls.__dict__.get("llm_key_present")
    if isinstance(orig, property):
        cls.llm_key_present = property(lambda self: True if self.llm_provider == "genesis" else orig.fget(self))
    elif callable(orig):
        cls.llm_key_present = lambda self, *a, **k: True if self.llm_provider == "genesis" else orig(self, *a, **k)
    orig_name = cls.__dict__.get("llm_model_name")
    if callable(orig_name):
        def llm_model_name(self, *a, **k):
            if self.llm_provider == "genesis":
                return "local: Suzanne {} + Andrew {}".format(os.getenv("SUZANNE_MODEL") or "llama3.2:3b",
                                                             os.getenv("ANDREW_MODEL") or "qwen2.5-coder:7b")
            return orig_name(self, *a, **k)
        cls.llm_model_name = llm_model_name


# ------------------------------------------------------------------ ai/llm/factory.py
def external_provider(config, build):
    """The external model, used only when GENESIS_EXTERNAL_AI_ENABLED=true and no local model can answer."""
    import copy

    aliases = {}
    try:
        from ..core.config import PROVIDER_ALIASES as aliases  # noqa: N811
    except Exception:
        pass
    name = (os.getenv("GENESIS_EXTERNAL_PROVIDER") or "").strip().lower()
    name = aliases.get(name, name)
    if not name or name in {"genesis", "ollama", "offline", "none"}:
        return None
    ext = copy.copy(config)
    ext.llm_provider = name
    prov = build(ext)
    return None if getattr(prov, "is_generative", True) is False or prov.name == "offline" else prov


def patch_factory(g: dict) -> None:
    if not _once(g, "factory"):
        return
    orig = g.get("build_provider")
    if not callable(orig):
        log.warning("v3: build_provider not found")
        return

    @functools.wraps(orig)
    def build_provider(config, *args, **kwargs):
        if getattr(config, "llm_provider", "") == "genesis":
            from .provider import build_local_provider

            return build_local_provider(config, external=lambda: external_provider(config, orig),
                                        session=kwargs.get("session", args[0] if args else None))
        return orig(config, *args, **kwargs)

    g["build_provider"] = build_provider


# ------------------------------------------------------------------ tools/registry.py
def patch_registry(g: dict) -> None:
    if not _once(g, "registry"):
        return
    orig = g.get("build_default_registry")
    if not callable(orig):
        log.warning("v3: build_default_registry not found")
        return

    @functools.wraps(orig)
    def build_default_registry(*args, **kwargs):
        reg = orig(*args, **kwargs)
        from .tools import TOOLS

        for tool in TOOLS:
            try:
                if reg.get(tool.name) is None:
                    reg.register(tool)
            except Exception:  # pragma: no cover
                log.exception("v3: could not register %s", tool.name)
        return reg

    g["build_default_registry"] = build_default_registry


# ------------------------------------------------------------------ nlp/rules.py
def patch_rules(g: dict) -> None:
    if not _once(g, "rules"):
        return
    orig = g.get("parse")
    if not callable(orig):
        log.warning("v3: rules.parse not found")
        return

    @functools.wraps(orig)
    def parse(text, *args, **kwargs):
        from .commands import nlu

        norm = text
        try:
            norm = g["normalize"](text) if callable(g.get("normalize")) else text
            hit = nlu(norm, "hard")
            if hit is not None:
                return hit
        except Exception:  # pragma: no cover - never break understanding
            log.exception("v3: command match failed")
        res = orig(text, *args, **kwargs)
        if res is None:  # Genesis's own rules found nothing: try the 3.0.1 fallback commands
            try:
                return nlu(norm, "soft")
            except Exception:  # pragma: no cover
                log.exception("v3: command match failed")
        return res

    g["parse"] = parse


# ------------------------------------------------------------------ memory/embeddings.py
def patch_embeddings(g: dict) -> None:
    if not _once(g, "embeddings"):
        return
    orig = g.get("create_embedder")
    if not callable(orig):
        return

    @functools.wraps(orig)
    def create_embedder(kind="auto", *args, **kwargs):
        if kind == "huggingface" and not _external_allowed():
            kind = "auto"  # local sentence-transformers or hashing: no cloud embeddings in local mode
        return orig(kind, *args, **kwargs)

    g["create_embedder"] = create_embedder


# ------------------------------------------------------------------ core/container.py
def patch_container(g: dict) -> None:
    if not _once(g, "container"):
        return
    orig = g.get("build_container")
    if not callable(orig):
        return

    @functools.wraps(orig)
    def build_container(*args, **kwargs):
        svc = orig(*args, **kwargs)
        _connect_badge(svc)
        if not _external_allowed():
            screen = getattr(svc, "screen", None)
            if screen is not None and getattr(screen, "vision", None) is not None:
                screen.vision = None  # no cloud vision model in local mode (OCR still works)
                log.info("v3: local mode - cloud screen vision disabled")
        return svc

    g["build_container"] = build_container


def _connect_badge(svc) -> None:
    """After each answer, refresh the chat window's AI badge: "AI: Andrew qwen2.5-coder:7b · local"."""
    from .provider import find_provider

    prov = find_provider(svc)
    events = getattr(svc, "events", None)
    if prov is None or events is None:
        return
    try:
        from ..core.events import EventType

        kind = EventType.LLM_STATUS
    except Exception:  # pragma: no cover
        return
    llm = svc.llm

    def on_route(_desc: dict) -> None:
        status = llm.status() if hasattr(llm, "status") else prov.describe()
        events.emit(kind, **status)

    prov.on_route = on_route
    try:  # 3.0.3: start Ollama in the background when Genesis starts, so the first question doesn't fail
        import threading

        threading.Thread(target=prov.client.ensure_running, daemon=True, name="genesis-start-ollama").start()
    except Exception:  # pragma: no cover
        pass
    try:
        from .twin import from_services

        prov.profile = from_services(svc)  # digital twin: the user's saved facts + writing samples
    except Exception:  # pragma: no cover
        log.exception("v3: profile setup failed")


# ------------------------------------------------------------------ v2 cloud features (speech, images)
def _gate_attr(cls, attr: str, blocked_value, allowed=None) -> bool:
    allowed = allowed or _external_allowed
    orig = cls.__dict__.get(attr)
    if isinstance(orig, property):
        setattr(cls, attr, property(lambda self: orig.fget(self) if allowed() else blocked_value))
        return True
    if callable(orig):
        @functools.wraps(orig)
        def wrapper(self, *a, **k):
            return orig(self, *a, **k) if allowed() else blocked_value
        setattr(cls, attr, wrapper)
        return True
    return False


def cloud_voice_allowed() -> bool:
    """Voice may use its cloud engine unless the user set GENESIS_CLOUD_VOICE=false (3.0.1: voice keeps working by
    default in local mode; chat, images, embeddings and vision stay local)."""
    v = (os.getenv("GENESIS_CLOUD_VOICE") or "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return _external_allowed()
    return True


def patch_v2_voice(g: dict) -> None:
    """Cloud speech only when allowed: on by default (GENESIS_CLOUD_VOICE=true); set it to false for fully-offline voice."""
    if not _once(g, "v2_voice"):
        return
    done = False
    for obj in list(g.values()):
        if isinstance(obj, type) and "cloud_enabled" in obj.__dict__:
            done = _gate_attr(obj, "cloud_enabled", False, cloud_voice_allowed) or done
    if not done:
        log.warning("v3: no cloud_enabled switch found in %s", g.get("__name__"))


def patch_v2_images(g: dict) -> None:
    """No external AI image generation in local mode (image *search* such as Wikimedia isn't AI and stays)."""
    if not _once(g, "v2_images"):
        return

    def filt(order):
        if _external_allowed() or not isinstance(order, (list, tuple)):
            return order
        return type(order)(p for p in order if str(p).lower() not in EXTERNAL_IMAGE_AI)

    done = False
    fn = g.get("provider_order")
    if callable(fn) and not isinstance(fn, type):
        g["provider_order"] = functools.wraps(fn)(lambda *a, **k: filt(fn(*a, **k)))
        done = True
    for obj in list(g.values()):
        if isinstance(obj, type):
            orig = obj.__dict__.get("provider_order")
            if isinstance(orig, property):
                obj.provider_order = property(lambda self, o=orig: filt(o.fget(self)))
                done = True
            elif callable(orig):
                def wrapper(self, *a, _o=orig, **k):
                    return filt(_o(self, *a, **k))
                obj.provider_order = wrapper
                done = True
    if not done:
        log.warning("v3: no provider_order found in %s", g.get("__name__"))


# ------------------------------------------------------------------ voice/manager.py + core/assistant.py: Friday mode
_VOICE: dict = {}


def _wrap_handler(vm, handler):
    from . import friday

    @functools.wraps(handler)
    def wrapped(text, *a, **k):
        from . import converse

        ack = friday.ack_for(text) if friday.enabled() else None
        if ack:
            try:
                vm.speak(ack)  # said straight away, while the task runs
            except Exception:
                pass
        streaming = stream_speech_enabled() and _tts_on(vm)
        if streaming:
            converse.begin(lambda sentence: vm.tts.speak(sentence))  # 3.2: chat answers are spoken as they're written
        try:
            out = handler(text, *a, **k)
        finally:
            turn = converse.end() if streaming else None
        if turn is not None and turn.spoken:
            vm._v3_streamed = True  # already spoken - the manager only listens for interruptions now
        try:
            if friday.enabled() and isinstance(out, tuple) and out and isinstance(out[0], str):
                lang = out[2] if len(out) > 2 else None
                if not lang or str(lang).startswith("en"):
                    status = "awaiting_input" if (len(out) > 1 and out[1]) else ""
                    return (friday.report(out[0], acted=bool(ack), status=status),) + tuple(out[1:])
        except Exception:  # pragma: no cover
            log.exception("v3: friday report failed")
        return out

    return wrapped


def assemble_enabled() -> bool:
    return (os.getenv("GENESIS_ASSEMBLE_SPEECH") or "true").strip().lower() not in ("0", "false", "no", "off")


def _rotate_greeting(vm) -> None:
    """Answer the wake word with a different short line each time ("Yes, Boss?", "Standing by." …)."""
    st = getattr(vm, "settings", None)
    if st is None or getattr(st, "_v3_greeting", False):
        return
    orig_get = st.get

    def get(key, default=None, *a, **k):
        val = orig_get(key, default, *a, **k)
        if key == "voice.greeting" and val in ("Yes, Boss?", "I'm listening."):
            from .jarvis_style import attention

            return attention()
        return val

    try:
        st.get = get
        st._v3_greeting = True
    except Exception:  # pragma: no cover
        pass


def stream_speech_enabled() -> bool:
    return (os.getenv("GENESIS_STREAM_SPEECH") or "true").strip().lower() not in ("0", "false", "no", "off")


def _tts_on(vm) -> bool:
    try:
        return bool(vm.settings.get("voice.tts_enabled", True))
    except Exception:
        return True


def _log_speech(vm) -> None:
    """Remember what Genesis says (for echo rejection) by wrapping its voice's speak()."""
    from .converse import SpokenLog

    vm.spoken_log = SpokenLog()
    tts = getattr(vm, "tts", None)
    orig = getattr(tts, "speak", None)
    if tts is None or not callable(orig) or getattr(tts, "_v3_logged", False):
        return

    def speak(text, *a, **k):
        try:
            vm.spoken_log.add(str(text))
        except Exception:
            pass
        return orig(text, *a, **k)

    try:
        tts.speak = speak
        tts._v3_logged = True
    except Exception:  # pragma: no cover - a read-only engine object
        pass


def _patch_turns(cls) -> None:
    """Echo rejection on transcripts; don't re-speak an answer that was already streamed."""
    rec = cls.__dict__.get("_record_and_transcribe")
    if callable(rec):
        @functools.wraps(rec)
        def _record_and_transcribe(self, *a, **k):
            from .converse import is_echo

            text = rec(self, *a, **k)
            log_ = getattr(self, "spoken_log", None)
            if text and log_ is not None and is_echo(text, log_.recent()):
                log.info("v3: ignored my own voice coming back through the microphone: %r", text[:60])
                return None
            return text

        cls._record_and_transcribe = _record_and_transcribe
    inner = cls.__dict__.get("_record_and_transcribe_inner")
    if callable(inner):
        @functools.wraps(inner)
        def _record_and_transcribe_inner(self, ep, carry, *a, **k):
            """A pause in the middle of a sentence is not the end of the turn (JARVIS-style assembly)."""
            import dataclasses
            import time as _t

            from .jarvis_style import MAX_HOLD_MS, hold_for

            text = inner(self, ep, carry, *a, **k)
            if not text or not assemble_enabled():
                return text
            t0 = _t.monotonic()
            while True:
                wait = hold_for(text)
                spent = (_t.monotonic() - t0) * 1000
                if wait <= 0 or spent >= MAX_HOLD_MS:
                    return text
                try:
                    cfg = self._endpoint_config(self.session.remaining())
                    cfg = dataclasses.replace(cfg, no_speech_ms=int(min(wait, MAX_HOLD_MS - spent)))
                    ep2 = type(ep)(cfg)
                except Exception:
                    return text
                more = inner(self, ep2, None, *a, **k)
                if not more:
                    return text
                text = f"{text.rstrip(' .')} {more}".strip()

        cls._record_and_transcribe_inner = _record_and_transcribe_inner
    spk = cls.__dict__.get("_speak_interruptible")
    if callable(spk):
        @functools.wraps(spk)
        def _speak_interruptible(self, speech, *a, **k):
            if not getattr(self, "_v3_streamed", False):
                return spk(self, speech, *a, **k)
            self._v3_streamed = False
            tts = self.tts
            if not getattr(tts, "speaking", False) and getattr(getattr(tts, "_q", None), "empty", lambda: True)():
                return []  # the streamed answer already finished playing
            real = tts.speak
            tts.speak = lambda *x, **y: None  # it's already queued: just watch for interruptions while it plays
            try:
                return spk(self, speech, *a, **k)
            finally:
                tts.speak = real

        cls._speak_interruptible = _speak_interruptible


def patch_voice_manager(g: dict) -> None:
    """Female voice, "Yes, Boss?" on the wake word, "On it, Boss" / "Done, Boss", greeting, never-silent fallback."""
    if not _once(g, "voice_manager"):
        return
    cls = g.get("VoiceManager")
    if not isinstance(cls, type):
        log.warning("v3: VoiceManager not found")
        return
    from . import friday

    orig_init, orig_start = cls.__init__, getattr(cls, "start", None)

    @functools.wraps(orig_init)
    def __init__(self, *a, **k):
        orig_init(self, *a, **k)
        _VOICE["vm"] = self
        try:
            tts = getattr(self, "tts", None)
            if tts is None or type(tts).__name__ == "NullTTS" or getattr(tts, "name", "") in ("none", "null"):
                if os.name == "nt":
                    self.tts = friday.fallback_speaker()  # Genesis's own speech engine is missing: use Windows's voice
                    log.info("v3: using the Windows fallback voice")
            st = getattr(self, "settings", None)
            if st is not None and friday.enabled():
                if (st.get("voice.greeting", "I'm listening.") or "I'm listening.") == "I'm listening.":
                    st.set("voice.greeting", "Yes, Boss?")
                if not st.get("voice.gender") and not st.get("voice.tts_voice"):
                    st.set("voice.gender", "female")
            if callable(getattr(self, "handler", None)):
                self.handler = _wrap_handler(self, self.handler)
            _log_speech(self)
            if friday.enabled():
                _rotate_greeting(self)
        except Exception:  # pragma: no cover
            log.exception("v3: friday voice setup failed")

    cls.__init__ = __init__
    _patch_turns(cls)
    if callable(orig_start):
        @functools.wraps(orig_start)
        def start(self, *a, **k):
            res = orig_start(self, *a, **k)
            if friday.enabled():
                try:
                    self.speak(friday.greeting())
                except Exception:
                    pass
            return res

        cls.start = start
    from . import privacy

    privacy.patch_voice_manager(cls)  # real mic / speaker mute (3.5)


def patch_assistant(g: dict) -> None:
    """Speak replies to typed requests too ("On it, Boss" … "Done, Boss"), through the same voice."""
    if not _once(g, "assistant"):
        return
    cls = g.get("GenesisCore")
    if not isinstance(cls, type) or not callable(getattr(cls, "handle", None)):
        return
    from . import friday

    orig = cls.handle

    @functools.wraps(orig)
    def handle(self, text, source="text", *a, **k):
        vm = _VOICE.get("vm")
        if source != "text" or vm is None or not friday.speak_typed():
            return orig(self, text, source, *a, **k)
        ack = friday.ack_for(text)
        if ack:
            try:
                vm.speak(ack)
            except Exception:
                pass
        resp = orig(self, text, source, *a, **k)
        try:
            lang = getattr(resp, "language", "en") or "en"
            if str(lang).startswith("en"):
                speech = getattr(resp, "speech", None) or getattr(resp, "text", "")
                vm.speak(friday.report(speech, acted=bool(ack), status=getattr(resp, "status", "")))
        except Exception:  # pragma: no cover
            log.exception("v3: speaking the typed reply failed")
        return resp

    cls.handle = handle


# ------------------------------------------------------------------ voice/tts.py: natural voice (Kokoro) - 3.1
def patch_tts(g: dict) -> None:
    """Prefer Kokoro (most natural local voice) when it is installed; otherwise Genesis's own choice (Piper, SAPI)."""
    if not _once(g, "tts"):
        return
    orig = g.get("create_tts")
    if not callable(orig):
        return

    @functools.wraps(orig)
    def create_tts(engine: str = "auto", *a, **k):
        want = (os.getenv("GENESIS_TTS") or "auto").strip().lower()
        if engine == "kokoro" or (engine == "auto" and want in ("auto", "kokoro")):
            try:
                from . import kokoro

                if kokoro.available():
                    return kokoro.KokoroTTS()
                if engine == "kokoro" or want == "kokoro":
                    log.warning("v3: Kokoro voice not installed - run Setup_Natural_Voice.bat; using the next engine")
            except Exception:
                log.exception("v3: Kokoro voice failed to start; using the next engine")
        return orig("auto" if engine == "kokoro" else engine, *a, **k)

    g["create_tts"] = create_tts


# ------------------------------------------------------------------ ui/main_window.py (3.4 Business Automation page)
def patch_main_window(g: dict) -> None:
    if not _once(g, "main_window"):
        return
    cls = g.get("MainWindow")
    if cls is None:
        return
    orig_init = cls.__init__

    @functools.wraps(orig_init)
    def __init__(self, core, *args, **kwargs):
        orig_init(self, core, *args, **kwargs)
        try:  # the page is an add-on: if anything goes wrong the normal window still opens
            from .business_page import BusinessPage

            page = BusinessPage(core)
            anchor = self._nav.get("activity") or self._nav.get("chat")
            side_layout = anchor.parentWidget().layout()
            btn = type(anchor)("📊  Business automation")
            btn.setObjectName("Nav")
            btn.setCheckable(True)
            btn.clicked.connect(lambda _c=False: self.show_page("business"))
            group = anchor.group()
            if group is not None:
                group.addButton(btn)
            side_layout.insertWidget(side_layout.indexOf(anchor) + 1, btn)
            self._pages["business"] = page
            self._nav["business"] = btn
            self.stack.addWidget(page)
            self.business = page
        except Exception:
            log.exception("v3: could not add the Business Automation page")
        try:  # 3.5: Coding mode page + microphone / speaker mute bar + Ctrl+Shift+M
            from . import privacy
            from .coding_page import CodingPage, MuteBar

            page = CodingPage(core)
            anchor = self._nav.get("business") or self._nav.get("activity") or self._nav.get("chat")
            side_layout = anchor.parentWidget().layout()
            btn = type(anchor)("🛠  Coding mode")
            btn.setObjectName("Nav")
            btn.setCheckable(True)
            btn.clicked.connect(lambda _c=False: self.show_page("coding"))
            group = anchor.group()
            if group is not None:
                group.addButton(btn)
            side_layout.insertWidget(side_layout.indexOf(anchor) + 1, btn)
            self._pages["coding"] = page
            self._nav["coding"] = btn
            self.stack.addWidget(page)
            self.coding = page
            self.mute_bar = MuteBar()
            side_layout.addWidget(self.mute_bar)
            self._mute_hotkey = privacy.start_hotkey()
        except Exception:
            log.exception("v3: could not add the Coding page / mute bar")

    cls.__init__ = __init__


def patch_mini_window(g: dict) -> None:
    """3.5: microphone and speaker mute buttons in the pop-out's bottom row (next to talk and send)."""
    if not _once(g, "mini_window"):
        return
    cls = g.get("MiniAssistant")
    if cls is None:
        return
    orig_init = cls.__init__

    @functools.wraps(orig_init)
    def __init__(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        try:
            from .coding_page import MuteBar

            row = self.layout().itemAt(self.layout().count() - 1).layout()
            self.mute_bar = MuteBar(compact=True)
            row.insertWidget(row.indexOf(self.mic_btn), self.mute_bar)
        except Exception:
            log.exception("v3: could not add mute buttons to the pop-out")

    cls.__init__ = __init__
