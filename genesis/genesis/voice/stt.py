"""Speech-to-text engines.

* ``FasterWhisperSTT`` – OpenAI Whisper (CTranslate2 port) running locally on CPU (int8).
* ``VoskSTT``          – offline Kaldi recogniser (reuses the wake-word model; lower accuracy, very light).
* ``HuggingFaceSTT``   – Whisper via Hugging Face Inference (remote; audio leaves the machine — opt-in only).
"""
from __future__ import annotations

import io
import json
import wave
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

import numpy as np
import requests

from ..core.logging_setup import get_logger
from .audio import SAMPLE_RATE

log = get_logger("voice.stt")


class STTError(RuntimeError):
    pass


class STTEngine(ABC):
    name = "base"
    is_remote = False

    @abstractmethod
    def transcribe(self, pcm16: bytes, sample_rate: int = SAMPLE_RATE) -> str:
        ...


def pcm_to_wav(pcm16: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm16)
    return buf.getvalue()


class FasterWhisperSTT(STTEngine):
    name = "faster-whisper"

    def __init__(self, model_size: str = "base.en", download_root: Optional[Path] = None) -> None:
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError as exc:
            raise STTError("faster-whisper is not installed (pip install faster-whisper).") from exc
        self.model = WhisperModel(model_size, device="cpu", compute_type="int8",
                                  download_root=str(download_root) if download_root else None)

    def transcribe(self, pcm16: bytes, sample_rate: int = SAMPLE_RATE) -> str:
        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _info = self.model.transcribe(audio, language="en", beam_size=1, vad_filter=True,
                                                initial_prompt="Hey Genesis, open VS Code. Draft an email to my professor.")
        return " ".join(s.text.strip() for s in segments).strip()


class VoskSTT(STTEngine):
    name = "vosk"

    def __init__(self, model=None, model_path: Optional[Path] = None) -> None:
        try:
            from vosk import KaldiRecognizer, Model, SetLogLevel  # type: ignore
        except ImportError as exc:
            raise STTError("vosk is not installed.") from exc
        SetLogLevel(-1)
        if model is None:
            if not model_path or not Path(model_path).exists():
                raise STTError("Vosk model not found. Run: python scripts/download_models.py")
            model = Model(str(model_path))
        self._model = model
        self._Rec = KaldiRecognizer

    def transcribe(self, pcm16: bytes, sample_rate: int = SAMPLE_RATE) -> str:
        rec = self._Rec(self._model, sample_rate)
        step = 8000
        for i in range(0, len(pcm16), step):
            rec.AcceptWaveform(pcm16[i:i + step])
        return json.loads(rec.FinalResult()).get("text", "").strip()


class HuggingFaceSTT(STTEngine):
    name = "huggingface-whisper"
    is_remote = True

    def __init__(self, token: str, model_id: str = "openai/whisper-large-v3-turbo", timeout: float = 60.0) -> None:
        if not token:
            raise STTError("HF_TOKEN is required for Hugging Face speech recognition.")
        self.url = f"https://router.huggingface.co/hf-inference/models/{model_id}"
        self.token = token
        self.timeout = timeout

    def transcribe(self, pcm16: bytes, sample_rate: int = SAMPLE_RATE) -> str:
        try:
            r = requests.post(self.url, data=pcm_to_wav(pcm16, sample_rate), timeout=self.timeout,
                              headers={"Authorization": f"Bearer {self.token}", "Content-Type": "audio/wav"})
        except requests.RequestException as exc:
            raise STTError(f"Speech service unreachable ({type(exc).__name__}).") from exc
        if r.status_code >= 400:
            raise STTError(f"Speech service error (HTTP {r.status_code}).")
        return (r.json().get("text") or "").strip()


def create_stt(config, vosk_model=None) -> STTEngine:
    engine = config.stt_engine
    errors = []
    order = {"auto": ["faster_whisper", "vosk"], "faster_whisper": ["faster_whisper", "vosk"], "vosk": ["vosk"],
             "huggingface": ["huggingface", "faster_whisper", "vosk"]}.get(engine, ["faster_whisper", "vosk"])
    for name in order:
        try:
            if name == "faster_whisper":
                return FasterWhisperSTT(config.whisper_model_size, config.paths.models_dir / "whisper")
            if name == "vosk":
                return VoskSTT(vosk_model, config.resolved_vosk_model_path)
            if name == "huggingface":
                return HuggingFaceSTT(config.hf_token or "", config.hf_asr_model)
        except STTError as exc:
            errors.append(f"{name}: {exc}")
    raise STTError("No speech-to-text engine available — " + "; ".join(errors))
