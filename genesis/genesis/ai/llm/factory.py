"""Provider selection + graceful degradation."""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from ...core.config import GenesisConfig
from ...core.logging_setup import get_logger
from .base import LLMAuthError, LLMError, LLMProvider, LLMUnavailable, Message
from .offline import OfflineProvider
from .openai_compat import HuggingFaceProvider, OllamaProvider, OpenAICompatibleProvider

log = get_logger("llm")


def build_provider(config: GenesisConfig) -> LLMProvider:
    p = config.llm_provider
    kw = {"timeout": config.llm_timeout, "temperature": config.llm_temperature}
    if p == "huggingface":
        return HuggingFaceProvider(config.hf_token, config.hf_model_id, config.hf_base_url, **kw)
    if p in {"openai", "openai_compatible"}:
        local = any(h in config.openai_base_url for h in ("localhost", "127.0.0.1"))
        return OpenAICompatibleProvider(config.openai_base_url, config.openai_model, config.openai_api_key,
                                        is_remote=not local, require_key=not local, **kw)
    if p == "ollama":
        return OllamaProvider(config.ollama_model, config.ollama_base_url, **kw)
    if p in {"offline", "none"}:
        return OfflineProvider("offline mode selected")
    log.warning("unknown GENESIS_LLM_PROVIDER=%s, falling back to offline", p)
    return OfflineProvider(f"unknown provider '{p}'")


class ResilientLLM(LLMProvider):
    """Wraps the primary provider; falls back to the offline responder on failure.

    After a connectivity failure the primary is skipped for ``cooldown`` seconds
    so the UI stays fast in degraded mode. ``status_callback`` lets the UI show
    an "AI offline" badge.
    """

    def __init__(self, primary: LLMProvider, *, cooldown: float = 60.0,
                 status_callback: Optional[Callable[[dict], None]] = None) -> None:
        self.primary = primary
        self.fallback = OfflineProvider()
        self.cooldown = cooldown
        self.status_callback = status_callback
        self._down_until = 0.0
        self._last_error: Optional[str] = None
        self._lock = threading.Lock()
        self.name = primary.name
        self.is_remote = primary.is_remote

    # ---------------------------------------------------------------- status
    @property
    def online(self) -> bool:
        return self.primary.is_generative and self.primary.is_configured() and time.time() >= self._down_until

    @property
    def is_generative(self) -> bool:  # type: ignore[override]
        return self.online

    def status(self) -> dict:
        return {
            **self.primary.describe(),
            "online": self.online,
            "last_error": self._last_error,
        }

    def _notify(self) -> None:
        if self.status_callback:
            try:
                self.status_callback(self.status())
            except Exception:
                pass

    def reset(self) -> None:
        self._down_until = 0.0
        self._last_error = None
        self._notify()

    # ------------------------------------------------------------------ chat
    def chat(self, messages: list[Message], *, temperature: Optional[float] = None, max_tokens: int = 700,
             json_mode: bool = False) -> str:
        if not self.online:
            reason = self._last_error or ("no API key configured" if not self.primary.is_configured() else "offline")
            self.fallback.reason = reason
            return self.fallback.chat(messages, temperature=temperature, max_tokens=max_tokens, json_mode=json_mode)
        try:
            out = self.primary.chat(messages, temperature=temperature, max_tokens=max_tokens, json_mode=json_mode)
            if self._last_error:
                self._last_error = None
                self._notify()
            return out
        except LLMAuthError as exc:
            with self._lock:
                self._last_error = str(exc)
                self._down_until = time.time() + 3600
            log.error("LLM auth error: %s", exc)
            self._notify()
            raise
        except LLMUnavailable as exc:
            with self._lock:
                self._last_error = str(exc)
                self._down_until = time.time() + self.cooldown
            log.warning("LLM unavailable, degrading to offline mode: %s", exc)
            self._notify()
            if json_mode:
                raise
            self.fallback.reason = str(exc)
            return self.fallback.chat(messages)
        except LLMError:
            raise


__all__ = ["build_provider", "ResilientLLM", "LLMError", "LLMUnavailable"]
