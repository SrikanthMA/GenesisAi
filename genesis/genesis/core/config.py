"""Environment-driven configuration.

Secrets and deployment choices come from environment variables / ``.env``.
User-facing preferences that can change at runtime live in the SQLite
``settings`` table (see :mod:`genesis.core.settings`).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .paths import PROJECT_ROOT, AppPaths

try:  # optional at import time so tests can run without it
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None


def _bool(value: Optional[str], default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _float(value: Optional[str], default: float) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except ValueError:
        return default


def _opt(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip()
    return value or None


@dataclass
class GenesisConfig:
    paths: AppPaths = field(default_factory=AppPaths.default)

    # --- LLM ---------------------------------------------------------------
    llm_provider: str = "huggingface"  # huggingface | openai_compatible | ollama | offline
    hf_token: Optional[str] = None
    hf_model_id: str = "Qwen/Qwen2.5-7B-Instruct"
    hf_base_url: str = "https://router.huggingface.co/v1"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: Optional[str] = None
    openai_model: str = "gpt-4o-mini"
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_model: str = "qwen2.5:7b-instruct"
    llm_timeout: float = 45.0
    llm_temperature: float = 0.4

    # --- Embeddings / knowledge -------------------------------------------
    embedding_provider: str = "auto"  # auto | sentence_transformers | huggingface | hashing
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"

    # --- Google / Gmail -----------------------------------------------------
    google_client_id: Optional[str] = None
    google_client_secret: Optional[str] = None
    google_client_secrets_file: Optional[str] = None

    # --- Voice -------------------------------------------------------------
    wake_word_engine: str = "vosk"  # vosk | porcupine | none
    vosk_model_path: Optional[str] = None
    porcupine_access_key: Optional[str] = None
    porcupine_keyword_path: Optional[str] = None
    stt_engine: str = "auto"  # auto | faster_whisper | vosk | huggingface
    whisper_model_size: str = "base.en"
    hf_asr_model: str = "openai/whisper-large-v3-turbo"
    tts_engine: str = "auto"  # auto | sapi | pyttsx3 | none
    audio_input_device: Optional[str] = None

    # --- Safety / runtime ---------------------------------------------------
    system_dry_run: bool = False  # simulate shutdown/restart/sleep/lock (for demos)
    log_level: str = "INFO"
    hotkey: str = "ctrl+alt+g"

    @property
    def default_vosk_model_dir(self) -> Path:
        return self.paths.models_dir / "vosk-model-small-en-us-0.15"

    @property
    def resolved_vosk_model_path(self) -> Path:
        if self.vosk_model_path:
            return Path(self.vosk_model_path).expanduser()
        return self.default_vosk_model_dir

    @classmethod
    def from_env(cls, env_file: Optional[Path] = None, *, home: Optional[Path] = None) -> "GenesisConfig":
        if load_dotenv is not None:
            import sys

            candidates = [env_file] if env_file else [PROJECT_ROOT / ".env", Path(sys.executable).parent / ".env",
                                                      AppPaths.default().home / ".env"]
            for env_path in candidates:
                if env_path and env_path.exists():
                    load_dotenv(env_path, override=False)
                    break
        e = os.environ.get
        paths = AppPaths(home) if home else AppPaths.default()
        return cls(
            paths=paths,
            llm_provider=(e("GENESIS_LLM_PROVIDER") or "huggingface").strip().lower(),
            hf_token=_opt(e("HF_TOKEN")),
            hf_model_id=_opt(e("HF_MODEL_ID")) or "Qwen/Qwen2.5-7B-Instruct",
            hf_base_url=_opt(e("HF_BASE_URL")) or "https://router.huggingface.co/v1",
            openai_base_url=_opt(e("OPENAI_BASE_URL")) or "https://api.openai.com/v1",
            openai_api_key=_opt(e("OPENAI_API_KEY")),
            openai_model=_opt(e("OPENAI_MODEL")) or "gpt-4o-mini",
            ollama_base_url=_opt(e("OLLAMA_BASE_URL")) or "http://localhost:11434/v1",
            ollama_model=_opt(e("OLLAMA_MODEL")) or "qwen2.5:7b-instruct",
            llm_timeout=_float(e("GENESIS_LLM_TIMEOUT"), 45.0),
            llm_temperature=_float(e("GENESIS_LLM_TEMPERATURE"), 0.4),
            embedding_provider=(e("GENESIS_EMBEDDING_PROVIDER") or "auto").strip().lower(),
            embedding_model=_opt(e("GENESIS_EMBEDDING_MODEL")) or "sentence-transformers/all-MiniLM-L6-v2",
            google_client_id=_opt(e("GOOGLE_CLIENT_ID")),
            google_client_secret=_opt(e("GOOGLE_CLIENT_SECRET")),
            google_client_secrets_file=_opt(e("GOOGLE_CLIENT_SECRETS_FILE")),
            wake_word_engine=(e("GENESIS_WAKE_WORD_ENGINE") or "vosk").strip().lower(),
            vosk_model_path=_opt(e("VOSK_MODEL_PATH")),
            porcupine_access_key=_opt(e("PICOVOICE_ACCESS_KEY")),
            porcupine_keyword_path=_opt(e("PORCUPINE_KEYWORD_PATH")),
            stt_engine=(e("GENESIS_STT_ENGINE") or "auto").strip().lower(),
            whisper_model_size=_opt(e("GENESIS_WHISPER_MODEL")) or "base.en",
            hf_asr_model=_opt(e("HF_ASR_MODEL")) or "openai/whisper-large-v3-turbo",
            tts_engine=(e("GENESIS_TTS_ENGINE") or "auto").strip().lower(),
            audio_input_device=_opt(e("GENESIS_AUDIO_DEVICE")),
            system_dry_run=_bool(e("GENESIS_SYSTEM_DRY_RUN"), False),
            log_level=(e("GENESIS_LOG_LEVEL") or "INFO").upper(),
            hotkey=(e("GENESIS_HOTKEY") or "ctrl+alt+g").lower(),
        )

    def describe(self) -> dict:
        """Non-secret summary (safe to display / log)."""
        return {
            "llm_provider": self.llm_provider,
            "hf_model_id": self.hf_model_id,
            "hf_token_configured": bool(self.hf_token),
            "openai_compatible_configured": bool(self.openai_api_key),
            "google_oauth_configured": bool(
                (self.google_client_id and self.google_client_secret) or self.google_client_secrets_file
            ),
            "wake_word_engine": self.wake_word_engine,
            "stt_engine": self.stt_engine,
            "tts_engine": self.tts_engine,
            "embedding_provider": self.embedding_provider,
            "system_dry_run": self.system_dry_run,
            "home": str(self.paths.home),
        }
