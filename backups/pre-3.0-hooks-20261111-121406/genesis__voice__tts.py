"""Text-to-speech with interruption (barge-in) support.

* ``SapiTTS`` (Windows default) – Microsoft SAPI 5 through COM. Speech is
  asynchronous and can be purged instantly, which is what makes “interrupt
  Genesis while it's talking” work.
* ``Pyttsx3TTS`` – cross-platform fallback.
* ``NullTTS`` – text only.
All engines speak on their own worker thread so the UI never blocks.
"""
from __future__ import annotations

import queue
import re
import sys
import threading
import time
from abc import ABC, abstractmethod
from typing import Callable, Optional

from ..core.logging_setup import get_logger

log = get_logger("voice.tts")


# Preferred natural-sounding female voices, best first (whatever of these Windows has installed is used).
FEMALE_VOICE_ORDER = ["aria", "jenny", "sonia", "libby", "natasha", "neerja", "heera", "zira", "hazel", "susan", "catherine",
                      "linda", "eva", "female"]
MALE_VOICE_ORDER = ["guy", "ryan", "william", "prabhat", "ravi", "david", "mark", "george", "james", "male"]


def pick_voice(names: list[str], gender: str = "female", genders: Optional[dict[str, str]] = None) -> Optional[str]:
    """Best voice name for a gender from what's installed (name hints + the voice's own Gender attribute)."""
    if not names or gender not in ("female", "male"):
        return None
    genders = genders or {}
    order = FEMALE_VOICE_ORDER if gender == "female" else MALE_VOICE_ORDER
    by_attr = [n for n in names if genders.get(n, "").lower() == gender]
    for hint in order:
        for n in (by_attr or names):
            if hint in n.lower():
                return n
    return by_attr[0] if by_attr else None


# ISO 639-1 → Windows primary language id (low 10 bits of an LCID; SAPI voices list LCIDs in their "Language" attribute)
PRIMARY_LANGID = {"ar": 0x01, "zh": 0x04, "cs": 0x05, "da": 0x06, "de": 0x07, "el": 0x08, "en": 0x09, "es": 0x0A, "fi": 0x0B,
                  "fr": 0x0C, "he": 0x0D, "hu": 0x0E, "it": 0x10, "ja": 0x11, "ko": 0x12, "nl": 0x13, "no": 0x14, "nb": 0x14,
                  "pl": 0x15, "pt": 0x16, "ro": 0x18, "ru": 0x19, "sv": 0x1D, "th": 0x1E, "tr": 0x1F, "ur": 0x20, "id": 0x21,
                  "uk": 0x22, "fa": 0x29, "vi": 0x2A, "hi": 0x39, "ms": 0x3E, "bn": 0x45, "pa": 0x46, "gu": 0x47, "or": 0x48,
                  "ta": 0x49, "te": 0x4A, "kn": 0x4B, "ml": 0x4C, "mr": 0x4E}


def _base(lang: Optional[str]) -> str:
    return (lang or "en").split("-")[0].split("_")[0].lower()


def sapi_language_ids(attr: str) -> set[int]:
    """'409;9' → {0x09}; '40C' → {0x0C}."""
    out = set()
    for part in (attr or "").replace(",", ";").split(";"):
        part = part.strip()
        if part:
            try:
                out.add(int(part, 16) & 0x3FF)
            except ValueError:
                pass
    return out


class TTSEngine(ABC):
    name = "base"

    def __init__(self) -> None:
        self.on_start: Optional[Callable[[], None]] = None
        self.on_done: Optional[Callable[[], None]] = None

    @abstractmethod
    def speak(self, text: str, lang: Optional[str] = None) -> None:
        """Speak ``text``; ``lang`` (ISO code) selects a voice for that language when one is installed."""

    def can_speak(self, lang: Optional[str]) -> bool:
        """True when a voice for ``lang`` is available (English always is)."""
        return _base(lang) == "en"

    @abstractmethod
    def stop(self) -> None:
        ...

    @property
    @abstractmethod
    def speaking(self) -> bool:
        ...

    def set_rate(self, rate: int) -> None:
        pass

    def set_voice(self, name_fragment: str) -> None:
        pass

    def voices(self) -> list[str]:
        return []

    def close(self) -> None:
        self.stop()


class NullTTS(TTSEngine):
    name = "none"

    def speak(self, text: str, lang: Optional[str] = None) -> None:
        pass

    def can_speak(self, lang: Optional[str]) -> bool:
        return True

    def stop(self) -> None:
        pass

    @property
    def speaking(self) -> bool:
        return False


class _ThreadedTTS(TTSEngine):
    """Runs a backend on one dedicated thread (COM/pyttsx3 objects are thread-affine)."""

    def __init__(self) -> None:
        super().__init__()
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._speaking = threading.Event()
        self._interrupt = threading.Event()
        self._ready = threading.Event()
        self._error: Optional[BaseException] = None
        self._thread = threading.Thread(target=self._run, name=f"genesis-tts-{self.name}", daemon=True)
        self._thread.start()
        self._ready.wait(10)
        if self._error:
            raise self._error

    @property
    def speaking(self) -> bool:
        return self._speaking.is_set()

    def speak(self, text: str, lang: Optional[str] = None) -> None:
        if text and text.strip():
            self._interrupt.clear()
            self._q.put(("speak", (text, lang)))

    def stop(self) -> None:
        self._interrupt.set()
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        self._q.put(("stop", None))

    def set_rate(self, rate: int) -> None:
        self._q.put(("rate", rate))

    def set_voice(self, name_fragment: str) -> None:
        self._q.put(("voice", name_fragment))

    def close(self) -> None:
        self.stop()
        self._q.put(("quit", None))

    def _run(self) -> None:
        try:
            self._init_backend()
        except BaseException as exc:  # surfaced to the constructor
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        while True:
            cmd, arg = self._q.get()
            try:
                if cmd == "quit":
                    break
                if cmd == "speak":
                    text, lang = arg if isinstance(arg, tuple) else (arg, None)
                    self._use_language(lang)
                    self._speaking.set()
                    if self.on_start:
                        self.on_start()
                    self._do_speak(text)
                    self._speaking.clear()
                    if self.on_done:
                        self.on_done()
                elif cmd == "stop":
                    self._do_stop()
                    self._speaking.clear()
                elif cmd == "rate":
                    self._do_rate(arg)
                elif cmd == "voice":
                    self._do_voice(arg)
            except Exception:
                log.exception("TTS error")
                self._speaking.clear()

    def _init_backend(self) -> None: ...
    def _use_language(self, lang: Optional[str]) -> None: ...
    def _do_speak(self, text: str) -> None: ...
    def _do_stop(self) -> None: ...
    def _do_rate(self, rate: int) -> None: ...
    def _do_voice(self, frag: str) -> None: ...


class SapiTTS(_ThreadedTTS):
    name = "sapi"
    SVSF_ASYNC = 1
    SVSF_PURGE = 2

    def _init_backend(self) -> None:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore

        pythoncom.CoInitialize()
        self._voice = win32com.client.Dispatch("SAPI.SpVoice")
        self._tokens: dict[str, object] = {}
        self._genders: dict[str, str] = {}
        try:  # cache names here: the COM object must only be used from this thread
            for v in self._voice.GetVoices():
                self._tokens[v.GetDescription()] = v
        except Exception:
            pass
        try:  # the newer, more natural "OneCore" voices (e.g. Aria/Jenny/Heera) aren't listed by default — add them
            cat = win32com.client.Dispatch("SAPI.SpObjectTokenCategory")
            cat.SetId(r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech_OneCore\Voices", False)
            for v in cat.EnumerateTokens():
                self._tokens.setdefault(v.GetDescription(), v)
        except Exception:
            pass
        self._langs: dict[str, set[int]] = {}
        for name, tok in self._tokens.items():
            try:
                self._genders[name] = tok.GetAttribute("Gender") or ""
            except Exception:
                self._genders[name] = ""
            try:
                self._langs[name] = sapi_language_ids(tok.GetAttribute("Language") or "")
            except Exception:
                self._langs[name] = set()
        self._voice_names = list(self._tokens)
        self._chosen = None  # the user's voice (restored after speaking another language)
        self._active_lang = "en"

    def _voices_for(self, lang: Optional[str]) -> list[str]:
        want = PRIMARY_LANGID.get(_base(lang))
        return [n for n, ids in getattr(self, "_langs", {}).items() if want is not None and want in ids]

    def can_speak(self, lang: Optional[str]) -> bool:
        return _base(lang) == "en" or bool(self._voices_for(lang))

    def _use_language(self, lang: Optional[str]) -> None:
        code = _base(lang)
        if code == self._active_lang:
            return
        if code == "en":
            if self._chosen is not None:
                self._voice.Voice = self._chosen
            self._active_lang = "en"
            return
        names = self._voices_for(code)
        if not names:
            return
        if self._active_lang == "en":
            self._chosen = self._voice.Voice
        pick = pick_voice(names, "female", self._genders) or names[0]
        try:
            self._voice.Voice = self._tokens[pick]
            self._active_lang = code
        except Exception:
            log.info("voice %s can't be used by SAPI", pick)

    def _do_speak(self, text: str) -> None:
        self._voice.Speak(text, self.SVSF_ASYNC)
        while not self._interrupt.is_set():
            if self._voice.WaitUntilDone(50):  # returns True when finished
                return
        self._voice.Speak("", self.SVSF_ASYNC | self.SVSF_PURGE)

    def _do_stop(self) -> None:
        self._voice.Speak("", self.SVSF_ASYNC | self.SVSF_PURGE)

    def _do_rate(self, rate: int) -> None:
        self._voice.Rate = max(-10, min(10, int(rate)))

    def _do_voice(self, frag: str) -> None:
        if not frag:
            return
        if frag.startswith("gender:"):
            frag = pick_voice(self._voice_names, frag.split(":", 1)[1], self._genders) or ""
            if not frag:
                return
        for name, tok in self._tokens.items():
            if frag.lower() in name.lower():
                try:
                    self._voice.Voice = tok
                except Exception:
                    log.info("voice %s can't be used by SAPI", name)
                    continue
                self.current_voice = name
                return

    def voices(self) -> list[str]:
        return list(getattr(self, "_voice_names", []))

    def voice_genders(self) -> dict[str, str]:
        return dict(getattr(self, "_genders", {}))


class Pyttsx3TTS(_ThreadedTTS):
    name = "pyttsx3"

    def _init_backend(self) -> None:
        import pyttsx3  # type: ignore

        self._engine = pyttsx3.init()
        self._langs: dict[str, str] = {}  # voice id → language code(s) as reported by the driver
        for v in self._engine.getProperty("voices"):
            langs = " ".join((x.decode(errors="ignore") if isinstance(x, bytes) else str(x)) for x in (getattr(v, "languages", None) or []))
            self._langs[v.id] = (langs + " " + (v.name or "")).lower()
        self._chosen_id = self._engine.getProperty("voice")
        self._active_lang = "en"

    def _ids_for(self, lang: Optional[str]) -> list[str]:
        from ..nlp.language import language_name

        code, name = _base(lang), language_name(_base(lang)).lower()
        return [vid for vid, desc in getattr(self, "_langs", {}).items()
                if re.search(rf"(^|[^a-z]){re.escape(code)}([-_ ]|$)", desc) or (name and name in desc)]

    def can_speak(self, lang: Optional[str]) -> bool:
        return _base(lang) == "en" or bool(self._ids_for(lang))

    def _use_language(self, lang: Optional[str]) -> None:
        code = _base(lang)
        if code == self._active_lang:
            return
        if code == "en":
            self._engine.setProperty("voice", self._chosen_id)
            self._active_lang = "en"
            return
        ids = self._ids_for(code)
        if ids:
            if self._active_lang == "en":
                self._chosen_id = self._engine.getProperty("voice")
            self._engine.setProperty("voice", ids[0])
            self._active_lang = code

    def _do_speak(self, text: str) -> None:
        # speak sentence by sentence so interruption takes effect quickly
        import re

        for sentence in re.split(r"(?<=[.!?])\s+", text):
            if self._interrupt.is_set():
                break
            self._engine.say(sentence)
            self._engine.runAndWait()

    def _do_stop(self) -> None:
        try:
            self._engine.stop()
        except Exception:
            pass

    def _do_rate(self, rate: int) -> None:
        self._engine.setProperty("rate", 185 + int(rate) * 12)

    def _do_voice(self, frag: str) -> None:
        voices = self._engine.getProperty("voices")
        if frag.startswith("gender:"):
            names = [v.name or "" for v in voices]
            genders = {v.name or "": str(getattr(v, "gender", "") or "") for v in voices}
            frag = pick_voice(names, frag.split(":", 1)[1], genders) or ""
        for v in voices:
            if frag and frag.lower() in (v.name or "").lower():
                self._engine.setProperty("voice", v.id)
                return


class PiperTTS(_ThreadedTTS):
    """Local neural voice (Piper, https://github.com/rhasspy/piper) — natural-sounding and fully offline.

    Needs ``pip install piper-tts`` and a voice model (``python scripts/download_models.py --voice``). Audio is played with
    sounddevice in small blocks so barge-in still stops it within ~100 ms. Nothing is recorded or uploaded.
    """

    name = "piper"

    def __init__(self, model_path: str) -> None:
        self.model_path = model_path
        self._rate = 0
        super().__init__()

    def _init_backend(self) -> None:
        from pathlib import Path

        import sounddevice  # noqa: F401  (fail early if audio output is unavailable)
        from piper import PiperVoice  # type: ignore

        if not Path(self.model_path).is_file():
            raise FileNotFoundError(f"Piper voice model not found: {self.model_path}")
        self._voice = PiperVoice.load(self.model_path)
        self._sr = int(getattr(getattr(self._voice, "config", None), "sample_rate", 22050))

    def _pcm(self, text: str):
        """Yield int16 PCM chunks (supports piper-tts 1.2 and 1.3 APIs)."""
        v = self._voice
        if hasattr(v, "synthesize_stream_raw"):  # piper-tts <= 1.2
            scale = {"length_scale": max(0.6, min(1.6, 1.0 - self._rate * 0.04))}
            try:
                yield from v.synthesize_stream_raw(text, **scale)
            except TypeError:
                yield from v.synthesize_stream_raw(text)
            return
        for chunk in v.synthesize(text):  # piper-tts >= 1.3: AudioChunk objects
            self._sr = int(getattr(chunk, "sample_rate", self._sr))
            yield chunk.audio_int16_bytes

    def _do_speak(self, text: str) -> None:
        import re

        import numpy as np
        import sounddevice as sd

        for sentence in re.split(r"(?<=[.!?])\s+", text):
            if self._interrupt.is_set() or not sentence.strip():
                continue
            with sd.OutputStream(samplerate=self._sr, channels=1, dtype="int16") as out:
                for raw in self._pcm(sentence):
                    data = np.frombuffer(raw, dtype=np.int16)
                    for i in range(0, len(data), 2048):
                        if self._interrupt.is_set():
                            return
                        out.write(data[i:i + 2048])

    def _do_stop(self) -> None:
        pass  # _interrupt stops playback between blocks

    def _do_rate(self, rate: int) -> None:
        self._rate = int(rate)

    def voices(self) -> list[str]:
        from pathlib import Path

        return [Path(self.model_path).stem]

    def can_speak(self, lang: Optional[str]) -> bool:
        """Piper voices are one language each, named like 'en_US-hfc_female-medium'."""
        from pathlib import Path

        return _base(lang) == _base(Path(self.model_path).stem.split("-")[0])


def piper_model_path() -> Optional[str]:
    """The configured/downloaded Piper voice (GENESIS_PIPER_VOICE = a .onnx path or a voice name in data/models/piper)."""
    import os
    from pathlib import Path

    from ..core.paths import AppPaths

    want = os.getenv("GENESIS_PIPER_VOICE", "").strip()
    if want and want.endswith(".onnx"):
        return want if Path(want).is_file() else None
    folder = AppPaths.default().models_dir / "piper"
    if not folder.is_dir():
        return None
    models = sorted(folder.glob("*.onnx"))
    if want:
        models = [m for m in models if want in m.stem] or models
    return str(models[0]) if models else None


def create_tts(engine: str = "auto") -> TTSEngine:
    if engine == "none":
        return NullTTS()
    candidates = ["piper", "sapi", "pyttsx3"] if sys.platform.startswith("win") else ["piper", "pyttsx3"]
    if engine in ("sapi", "pyttsx3", "piper"):
        candidates = [engine] + [c for c in candidates if c != engine]
    for c in candidates:
        try:
            if c == "piper":
                path = piper_model_path()
                if not path:
                    continue
                return PiperTTS(path)
            return SapiTTS() if c == "sapi" else Pyttsx3TTS()
        except BaseException as exc:
            log.warning("TTS engine %s unavailable: %s", c, exc)
    log.warning("no TTS engine available; replies will be text only")
    return NullTTS()


def wait_until_quiet(tts: TTSEngine, timeout: float = 30.0) -> None:
    end = time.time() + timeout
    while tts.speaking and time.time() < end:
        time.sleep(0.05)
