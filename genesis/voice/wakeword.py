"""Local wake-word detection for “Hey Genesis”.

Raw microphone audio never leaves the computer for wake-word detection.

Engines:
* ``VoskWakeWord`` (default) — the offline Kaldi-based Vosk recogniser run with
  a tiny constrained grammar (["hey genesis", "genesis", "[unk]"]). Restricting
  the search space to the key phrase turns a general ASR model into a
  keyword spotter; everything else collapses to [unk].
* ``PorcupineWakeWord`` (optional) — Picovoice Porcupine with a custom
  “Hey Genesis” keyword file (.ppn) trained in the Picovoice console.

False-activation protection:
* phrase must match the normalised wake grammar,
* per-word confidence must exceed a threshold derived from the sensitivity,
* a minimum loudness gate rejects faint background speech (TV, other rooms),
* a refractory period prevents double triggers.
"""
from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from ..core.logging_setup import get_logger
from .audio import SAMPLE_RATE
from .vad import frame_rms

log = get_logger("voice.wake")

WAKE_VARIANTS = re.compile(r"\b(?:hey|hi|hay|a|okay|ok)\s+(?:genesis|jenesis|genesys|genisis|jennessee|genes is)\b")


class WakeWordError(RuntimeError):
    pass


def _phrase_regex(phrase: str) -> re.Pattern:
    words = re.sub(r"[^a-z ]", " ", (phrase or "hey genesis").lower()).split() or ["hey", "genesis"]
    name = words[-1]
    alts = {name}
    if name == "genesis":
        alts |= {"jenesis", "genesys", "genisis", "jennessee", "genes is"}
    lead = r"(?:hey|hi|hay|a|okay|ok)" if len(words) == 1 or words[0] in ("hey", "hi", "ok", "okay") else re.escape(" ".join(words[:-1]))
    return re.compile(rf"\b{lead}\s+(?:{'|'.join(sorted(map(re.escape, alts)))})\b")


def matches_wake_phrase(text: str, allow_bare: bool = False, phrase: str = "hey genesis") -> bool:
    t = re.sub(r"[^a-z ]", " ", (text or "").lower())
    t = re.sub(r"\s+", " ", t).strip()
    rx = WAKE_VARIANTS if phrase.strip().lower() == "hey genesis" else _phrase_regex(phrase)
    if rx.search(t):
        return True
    name = (phrase.split() or ["genesis"])[-1].lower()
    return allow_bare and bool(re.fullmatch(rf"(?:{re.escape(name)}|jenesis)", t))


class WakeWordDetector(ABC):
    name = "base"

    def __init__(self, sensitivity: float = 0.6, refractory_s: float = 2.0) -> None:
        self.sensitivity = max(0.05, min(sensitivity, 0.95))
        self.refractory_s = refractory_s
        self._last = 0.0

    @abstractmethod
    def _detect(self, frame: bytes) -> bool:
        ...

    def process(self, frame: bytes) -> bool:
        if time.monotonic() - self._last < self.refractory_s:
            return False
        if self._detect(frame):
            self._last = time.monotonic()
            self.reset()
            return True
        return False

    def reset(self) -> None:
        pass

    def close(self) -> None:
        pass


class VoskWakeWord(WakeWordDetector):
    name = "vosk"

    def __init__(self, model_path: Path, sensitivity: float = 0.6, phrase: str = "hey genesis", **kw) -> None:
        super().__init__(sensitivity, **kw)
        self.phrase = (phrase or "hey genesis").strip().lower()
        try:
            from vosk import KaldiRecognizer, Model, SetLogLevel  # type: ignore
        except ImportError as exc:
            raise WakeWordError("The 'vosk' package is not installed (pip install vosk).") from exc
        if not Path(model_path).exists():
            raise WakeWordError(f"Vosk model not found at {model_path}. Run: python scripts/download_models.py")
        SetLogLevel(-1)
        self._model = Model(str(model_path))
        name = self.phrase.split()[-1]
        grammar = sorted({self.phrase, f"hey {name}", f"hi {name}", f"okay {name}", name}) + ["[unk]"]
        self._rec = KaldiRecognizer(self._model, SAMPLE_RATE, json.dumps(grammar))
        self._rec.SetWords(True)
        self.min_conf = 0.35 + (1 - self.sensitivity) * 0.55  # sensitivity 0.6 -> 0.57
        self.min_level = 120 + (1 - self.sensitivity) * 300  # peak RMS gate against faint background speech
        self._peak = 0.0

    @property
    def model(self):
        return self._model

    def _detect(self, frame: bytes) -> bool:
        self._peak = max(self._peak, frame_rms(frame))
        if self._rec.AcceptWaveform(frame):
            result = json.loads(self._rec.Result())
            ok = self._judge(result)
            self._peak = 0.0
            return ok
        partial = json.loads(self._rec.PartialResult()).get("partial", "")
        if matches_wake_phrase(partial, phrase=self.phrase) and self.sensitivity >= 0.75 and self._peak >= self.min_level:
            return True  # very sensitive mode: trigger on partials for lower latency
        return False

    def _judge(self, result: dict) -> bool:
        text = result.get("text", "")
        allow_bare = self.sensitivity >= 0.8
        if not matches_wake_phrase(text, allow_bare=allow_bare, phrase=self.phrase):
            return False
        words = result.get("result") or []
        keys = set(self.phrase.split()) | {"hey", "hi", "okay"}
        conf = min((w.get("conf", 0.0) for w in words if w.get("word") in keys), default=0.0)
        if conf < self.min_conf or self._peak < self.min_level:
            log.debug("wake rejected (conf=%.2f level=%.0f)", conf, self._peak)
            return False
        return True

    def reset(self) -> None:
        self._rec.Reset()
        self._peak = 0.0


class PorcupineWakeWord(WakeWordDetector):
    name = "porcupine"

    def __init__(self, access_key: str, keyword_path: str, sensitivity: float = 0.6, **kw) -> None:
        super().__init__(sensitivity, **kw)
        try:
            import pvporcupine  # type: ignore
        except ImportError as exc:
            raise WakeWordError("The 'pvporcupine' package is not installed.") from exc
        if not access_key or not keyword_path or not Path(keyword_path).exists():
            raise WakeWordError("Porcupine needs PICOVOICE_ACCESS_KEY and PORCUPINE_KEYWORD_PATH (a 'Hey Genesis' .ppn file).")
        self._pp = pvporcupine.create(access_key=access_key, keyword_paths=[keyword_path], sensitivities=[self.sensitivity])
        self._buf = b""
        import struct

        self._struct = struct

    def _detect(self, frame: bytes) -> bool:
        self._buf += frame
        n = self._pp.frame_length * 2
        hit = False
        while len(self._buf) >= n:
            chunk, self._buf = self._buf[:n], self._buf[n:]
            pcm = self._struct.unpack_from(f"{self._pp.frame_length}h", chunk)
            if self._pp.process(pcm) >= 0:
                hit = True
        return hit

    def close(self) -> None:
        self._pp.delete()


class TextWakeWord(WakeWordDetector):
    """Testing helper: frames are UTF-8 text transcripts instead of audio."""

    name = "text"

    def _detect(self, frame: bytes) -> bool:
        return matches_wake_phrase(frame.decode("utf-8", errors="ignore"))


def create_wake_detector(config, sensitivity: float, phrase: str = "hey genesis") -> Optional[WakeWordDetector]:
    engine = config.wake_word_engine
    if engine == "none":
        return None
    if engine == "porcupine":
        return PorcupineWakeWord(config.porcupine_access_key or "", config.porcupine_keyword_path or "", sensitivity)
    return VoskWakeWord(config.resolved_vosk_model_path, sensitivity, phrase=phrase)

