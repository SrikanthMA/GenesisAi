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

from .envfile import EnvReport, load_env

KEY_ALIASES = {  # accepted names for each provider's key (first non-empty wins)
    "huggingface": ("HF_TOKEN", "HUGGINGFACE_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACEHUB_API_TOKEN", "HF_API_KEY",
                    "HUGGINGFACE_API_KEY"),
    "openai_compatible": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}
PROVIDER_ALIASES = {"hf": "huggingface", "hugging_face": "huggingface", "huggingface": "huggingface", "qwen": "huggingface",
                    "openai": "openai_compatible", "openai_compatible": "openai_compatible", "lmstudio": "openai_compatible",
                    "ollama": "ollama", "anthropic": "anthropic", "claude": "anthropic", "gemini": "gemini", "google": "gemini",
                    "offline": "offline", "none": "offline"}


def clean_secret(value: Optional[str]) -> Optional[str]:
    """Trim whitespace/quotes and a pasted 'Bearer ' prefix; empty → None."""
    v = (value or "").strip().strip("\"'").strip()
    if v.lower().startswith("bearer "):
        v = v[7:].strip()
    return v or None


def provider_for_key(key: str) -> Optional[str]:
    if key.startswith("hf_"):
        return "huggingface"
    if key.startswith("sk-ant-"):
        return "anthropic"
    if key.startswith("AIza"):
        return "gemini"
    if key.startswith("sk-"):
        return "openai_compatible"
    return None


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
    hf_model_id: str = "Qwen/Qwen2.5-72B-Instruct"
    hf_base_url: str = "https://router.huggingface.co/v1"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: Optional[str] = None
    openai_model: str = "gpt-4o-mini"
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_model: str = "qwen2.5:7b-instruct"
    llm_timeout: float = 45.0
    llm_temperature: float = 0.4
    llm_max_tokens: int = 1024  # upper bound for any single reply
    llm_model: Optional[str] = None  # generic override (LLM_MODEL)
    llm_base_url: Optional[str] = None  # generic override (LLM_BASE_URL)
    anthropic_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None
    llm_notes: list = field(default_factory=list)  # how the provider/key were resolved (no secrets)
    env_report: Optional[EnvReport] = None

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
    def from_env(cls, env_file: Optional[Path] = None, *, home: Optional[Path] = None, reload: bool = False,
                 load_file: bool = True) -> "GenesisConfig":
        paths = AppPaths(home) if home else AppPaths.default()
        report = load_env(PROJECT_ROOT, env_file=env_file, home=paths.home, reload=reload) if load_file else EnvReport()
        e = os.environ.get
        cfg = cls(
            paths=paths,
            hf_model_id=_opt(e("HF_MODEL_ID")) or "Qwen/Qwen2.5-72B-Instruct",
            hf_base_url=_opt(e("HF_BASE_URL")) or "https://router.huggingface.co/v1",
            openai_base_url=_opt(e("OPENAI_BASE_URL")) or "https://api.openai.com/v1",
            openai_model=_opt(e("OPENAI_MODEL")) or "gpt-4o-mini",
            ollama_base_url=_opt(e("OLLAMA_BASE_URL")) or "http://localhost:11434/v1",
            ollama_model=_opt(e("OLLAMA_MODEL")) or "qwen2.5:7b-instruct",
            llm_timeout=_float(e("GENESIS_LLM_TIMEOUT") or e("LLM_TIMEOUT"), 45.0),
            llm_temperature=_float(e("GENESIS_LLM_TEMPERATURE") or e("LLM_TEMPERATURE"), 0.4),
            llm_max_tokens=int(_float(e("GENESIS_LLM_MAX_TOKENS") or e("LLM_MAX_TOKENS"), 1024)),
            llm_model=_opt(e("LLM_MODEL")),
            llm_base_url=_opt(e("LLM_BASE_URL")),
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
        cfg.env_report = report
        cfg._resolve_llm(e)
        return cfg

    def _resolve_llm(self, e) -> None:
        """Pick the provider and key from the environment, accepting the common variable names."""
        keys = {p: next((clean_secret(e(n)) for n in names if clean_secret(e(n))), None) for p, names in KEY_ALIASES.items()}
        generic = clean_secret(e("LLM_API_KEY"))
        raw_provider = (e("GENESIS_LLM_PROVIDER") or e("LLM_PROVIDER") or "").strip().lower()
        provider = PROVIDER_ALIASES.get(raw_provider, raw_provider) if raw_provider else ""
        if raw_provider and provider not in set(PROVIDER_ALIASES.values()):
            self.llm_notes.append(f"Unknown LLM provider “{raw_provider}” — use huggingface, openai, anthropic, gemini or ollama.")
        if generic:  # LLM_API_KEY belongs to the chosen provider (or is recognised by its prefix)
            target = provider or provider_for_key(generic) or "huggingface"
            if target in keys and not keys[target]:
                keys[target] = generic
        if not provider:  # nothing chosen explicitly → use whichever provider actually has a key
            provider = next((p for p in ("huggingface", "openai_compatible", "anthropic", "gemini") if keys[p]), "huggingface")
            if provider != "huggingface":
                self.llm_notes.append(f"No provider set, so I'm using {provider} because that's the key I found.")
        elif provider in keys and not keys[provider]:
            other = next((p for p in ("huggingface", "openai_compatible", "anthropic", "gemini") if keys[p]), None)
            if other:
                self.llm_notes.append(f"GENESIS_LLM_PROVIDER is {provider} but its key is missing; I found a {other} key, "
                                      f"so I'm using {other}.")
                provider = other
        self.llm_provider = provider or "huggingface"
        self.hf_token = keys["huggingface"]
        self.openai_api_key = keys["openai_compatible"]
        self.anthropic_api_key = keys["anthropic"]
        self.gemini_api_key = keys["gemini"]
        if self.hf_token and not self.hf_token.startswith("hf_") and self.llm_provider == "huggingface":
            self.llm_notes.append("HF_TOKEN doesn't start with “hf_” — make sure you pasted a Hugging Face access token.")
        if self.llm_model and self.llm_provider == "huggingface":
            self.hf_model_id = self.llm_model
        elif self.llm_model and self.llm_provider == "openai_compatible":
            self.openai_model = self.llm_model
        elif self.llm_model and self.llm_provider == "ollama":
            self.ollama_model = self.llm_model
        if self.llm_base_url and self.llm_provider == "huggingface":
            self.hf_base_url = self.llm_base_url
        elif self.llm_base_url and self.llm_provider == "openai_compatible":
            self.openai_base_url = self.llm_base_url
        elif self.llm_base_url and self.llm_provider == "ollama":
            self.ollama_base_url = self.llm_base_url

    @property
    def llm_key_present(self) -> bool:
        return {"huggingface": bool(self.hf_token), "openai_compatible": bool(self.openai_api_key) or any(
            h in self.openai_base_url for h in ("localhost", "127.0.0.1")), "anthropic": bool(self.anthropic_api_key),
            "gemini": bool(self.gemini_api_key), "ollama": True, "offline": False}.get(self.llm_provider, False)

    def llm_model_name(self) -> str:
        return {"huggingface": self.hf_model_id, "openai_compatible": self.openai_model, "ollama": self.ollama_model,
                "anthropic": self.llm_model or "", "gemini": self.llm_model or ""}.get(self.llm_provider, "")

    def describe(self) -> dict:
        """Non-secret summary (safe to display / log)."""
        return {
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model_name(),
            "llm_key_present": self.llm_key_present,
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
