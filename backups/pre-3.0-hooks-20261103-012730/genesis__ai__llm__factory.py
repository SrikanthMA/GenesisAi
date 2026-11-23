"""Provider selection + graceful degradation."""
from __future__ import annotations

import hashlib
import os
import json
import threading
from collections import OrderedDict
import time
from typing import Callable, Optional

from ...core.config import GenesisConfig
from ...core.logging_setup import get_logger
from .base import LLMAuthError, LLMError, LLMModelError, LLMProvider, LLMUnavailable, Message
from .offline import OfflineProvider
from .openai_compat import AnthropicProvider, GeminiProvider, HuggingFaceProvider, OllamaProvider, OpenAICompatibleProvider

log = get_logger("llm")


def build_provider(config: GenesisConfig, session=None) -> LLMProvider:
    p = config.llm_provider
    kw = {"timeout": config.llm_timeout, "temperature": config.llm_temperature, "max_tokens_cap": config.llm_max_tokens}
    if session is not None:
        kw["session"] = session
    if p == "huggingface":
        return HuggingFaceProvider(config.hf_token, config.hf_model_id, config.hf_base_url, **kw)
    if p in {"openai", "openai_compatible"}:
        local = any(h in config.openai_base_url for h in ("localhost", "127.0.0.1"))
        return OpenAICompatibleProvider(config.openai_base_url, config.openai_model, config.openai_api_key,
                                        is_remote=not local, require_key=not local, **kw)
    if p == "anthropic":
        return AnthropicProvider(config.anthropic_api_key, config.llm_model or "", config.llm_base_url or "https://api.anthropic.com/v1", **kw)
    if p == "gemini":
        return GeminiProvider(config.gemini_api_key, config.llm_model or "",
                              config.llm_base_url or "https://generativelanguage.googleapis.com/v1beta/openai", **kw)
    if p == "ollama":
        return OllamaProvider(config.ollama_model, config.ollama_base_url, **kw)
    if p in {"offline", "none"}:
        return OfflineProvider("offline mode was selected in .env (GENESIS_LLM_PROVIDER=offline)")
    log.warning("unknown GENESIS_LLM_PROVIDER=%s, falling back to offline", p)
    return OfflineProvider(f"unknown provider '{p}' in .env")


def friendly_reason(kind: str, detail: str = "") -> str:
    """User-facing explanation for a failure category (never contains secrets)."""
    return {
        "not_configured": detail or "no API key is configured",
        "auth_invalid": "the API key was rejected — it may be wrong or expired",
        "model_not_found": detail or "the configured model isn't available from this provider",
        "network": "I can't reach the AI service — check your internet connection",
        "timeout": "the AI service took too long to answer",
        "rate_limited": "the AI service is rate-limiting requests right now",
        "server_error": "the AI service is having problems right now",
        "quota": ("your Hugging Face free credits for this month are used up — wait for the monthly reset, add credits "
                  "(or HF PRO) at huggingface.co/settings/billing, or switch to a free local model with Ollama"),
        "offline_selected": "offline mode is selected in .env",
    }.get(kind, detail or "the AI service returned an error")


class ResilientLLM(LLMProvider):
    """The LLM service used by the rest of Genesis (NLU, planning, chat, coding …).

    * wraps the configured provider (Hugging Face / OpenAI-compatible / Anthropic / Gemini / Ollama);
    * bounded retries with backoff happen in the provider; after they are exhausted this service answers with a
      clear diagnostic and pauses remote calls briefly (``cooldown``) so Genesis stays responsive;
    * ``health()`` checks configuration, reachability, authentication and the model;
    * ``reload()`` re-reads ``.env`` — also done automatically when ``.env`` changes — so fixing a key needs no restart.

    After a connectivity failure the primary is skipped for ``cooldown`` seconds
    so the UI stays fast in degraded mode. ``status_callback`` lets the UI show
    an "AI offline" badge.
    """

    def __init__(self, primary: LLMProvider, *, cooldown: float = 20.0,
                 status_callback: Optional[Callable[[dict], None]] = None,
                 loader: Optional[Callable[[], tuple[LLMProvider, Optional[GenesisConfig]]]] = None,
                 env_path: Optional[str] = None, config: Optional[GenesisConfig] = None) -> None:
        self.primary = primary
        self.fallback = OfflineProvider()
        self.cooldown = cooldown
        self.status_callback = status_callback
        self.loader = loader
        self.config = config
        self.env_path = env_path
        self._env_mtime = self._mtime()
        self._last_env_check = 0.0
        self._down_until = 0.0
        self._last_error: Optional[str] = None
        self._last_kind: Optional[str] = None
        self._lock = threading.Lock()
        self.name = primary.name
        self.is_remote = primary.is_remote
        # small TTL cache for deterministic calls (NLU, planning, JSON) — avoids paying twice for the same question
        self._cache: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
        self.cache_ttl = 600.0
        self.cache_size = 128
        self.cache_hits = 0
        self._log_init()

    def _log_init(self) -> None:
        d = self.primary.describe()
        log.info("[LLM] service ready", extra={"data": {"provider": d.get("name"), "model": d.get("model"),
                                                         "configured": d.get("configured")}})

    def _mtime(self) -> float:
        try:
            return os.path.getmtime(self.env_path) if self.env_path else 0.0
        except OSError:
            return 0.0

    # ---------------------------------------------------------------- status
    @property
    def online(self) -> bool:
        self._maybe_reload()
        return self.primary.is_generative and self.primary.is_configured() and time.time() >= self._down_until

    @property
    def is_generative(self) -> bool:  # type: ignore[override]
        return self.online

    def status(self) -> dict:
        d = self.primary.describe()
        return {**d, "online": self.primary.is_generative and self.primary.is_configured() and time.time() >= self._down_until,
                "last_error": self._last_error, "error_kind": self._last_kind}

    def _notify(self) -> None:
        if self.status_callback:
            try:
                self.status_callback(self.status())
            except Exception:
                pass

    def reset(self) -> None:
        self._down_until = 0.0
        self._last_error = None
        self._last_kind = None
        self._notify()

    # ---------------------------------------------------------------- configuration
    def _maybe_reload(self) -> None:
        """If .env was created/edited since we last read it, pick up the new settings automatically."""
        if not self.loader:
            return
        now = time.time()
        if now - self._last_env_check < 2.0:
            return
        self._last_env_check = now
        m = self._mtime()
        if self.env_path is None and self.config is not None and self.config.env_report and not self.config.env_report.file:
            # no .env existed at start-up → look again (the user may have just created it)
            from ...core.envfile import candidate_paths
            from ...core.paths import PROJECT_ROOT

            found = next((str(p) for p in candidate_paths(PROJECT_ROOT) if p.is_file()), None)
            if found:
                self.env_path = found
                m = -1.0
        if m and m != self._env_mtime:
            self.reload()

    def reload(self) -> dict:
        """Re-read .env and rebuild the provider (no restart needed). Returns the new status."""
        if not self.loader:
            self.reset()
            return self.status()
        try:
            provider, cfg = self.loader()
        except Exception as exc:  # a broken .env must never crash Genesis
            log.warning("[LLM] reload failed: %s", type(exc).__name__)
            return self.status()
        with self._lock:
            self.primary = provider
            self.config = cfg or self.config
            self.name = provider.name
            self.is_remote = provider.is_remote
            self._cache.clear()
            if cfg is not None and cfg.env_report and cfg.env_report.file:
                self.env_path = cfg.env_report.file
            self._env_mtime = self._mtime()
        self.reset()
        log.info("[LLM] configuration reloaded", extra={"data": {"provider": provider.name,
                                                                  "configured": provider.is_configured()}})
        return self.status()

    def not_configured_reason(self) -> Optional[str]:
        missing = getattr(self.primary, "missing_config", None)
        if callable(missing):
            return missing()
        if isinstance(self.primary, OfflineProvider):
            return self.primary.reason or "offline mode is selected"
        return None if self.primary.is_configured() else "the model isn't configured"

    def health(self, *, probe: bool = True, reload: bool = False) -> dict:
        """Can Genesis use its AI model right now? Secret-free JSON-able dict."""
        if reload:
            self.reload()
        d = self.primary.describe()
        cfg = self.config
        report = {"available": False, "provider": d.get("name"), "model": d.get("model") or "", "endpoint": d.get("endpoint", ""),
                  "configured": self.primary.is_configured(), "key_present": bool(cfg.llm_key_present) if cfg else None,
                  "reachable": None, "auth_ok": None, "model_ok": None, "latency_ms": None, "error_kind": None, "message": "",
                  "env_file": (cfg.env_report.file if cfg and cfg.env_report else None),
                  "warnings": ((cfg.env_report.warnings if cfg and cfg.env_report else []) + (cfg.llm_notes if cfg else []))}
        missing = self.not_configured_reason()
        if missing:
            report.update(error_kind="not_configured" if not isinstance(self.primary, OfflineProvider) else "offline_selected",
                          message=f"The AI model isn't configured: {missing}.")
            return report
        if not probe:
            report.update(available=self.online, message="Configured (not tested).")
            return report
        t0 = time.perf_counter()
        try:
            self.primary.chat([{"role": "user", "content": "Reply with the single word: OK"}], max_tokens=5, temperature=0.0)
        except LLMError as exc:
            kind = getattr(exc, "kind", "error")
            report.update(error_kind=kind, message=friendly_reason(kind, str(exc)),
                          reachable=kind not in ("network", "timeout"),
                          auth_ok=False if kind == "auth_invalid" else (None if kind in ("network", "timeout") else True),
                          model_ok=False if kind == "model_not_found" else None)
            with self._lock:
                self._last_error, self._last_kind = friendly_reason(kind, str(exc)), kind
            self._notify()
            return report
        report.update(available=True, reachable=True, auth_ok=True, model_ok=True,
                      latency_ms=int((time.perf_counter() - t0) * 1000), message="Connected.")
        self.reset()
        return report

    # ------------------------------------------------------------------ chat
    def _offline_reply(self, messages, reason: str, kind: str, temperature, max_tokens, json_mode) -> str:
        self.fallback.reason = reason
        self.fallback.kind = kind
        return self.fallback.chat(messages, temperature=temperature, max_tokens=max_tokens, json_mode=json_mode)

    def chat(self, messages: list[Message], *, temperature: Optional[float] = None, max_tokens: int = 700,
             json_mode: bool = False) -> str:
        if not self.online:
            missing = self.not_configured_reason()
            kind = self._last_kind or ("not_configured" if missing else "unavailable")
            reason = friendly_reason("not_configured", missing) if missing else (self._last_error or "offline")
            return self._offline_reply(messages, reason, kind, temperature, max_tokens, json_mode)
        key = None
        if json_mode or (temperature is not None and temperature <= 0.2):
            key = hashlib.sha256(json.dumps([messages, max_tokens, json_mode, temperature], sort_keys=True,
                                            default=str).encode()).hexdigest()
            with self._lock:
                hit = self._cache.get(key)
                if hit and time.time() - hit[0] < self.cache_ttl:
                    self._cache.move_to_end(key)
                    self.cache_hits += 1
                    return hit[1]
        try:
            out = self.primary.chat(messages, temperature=temperature, max_tokens=max_tokens, json_mode=json_mode)
            if self._last_error:
                self._last_error = None
                self._notify()
            if key is not None and out:
                with self._lock:
                    self._cache[key] = (time.time(), out)
                    while len(self._cache) > self.cache_size:
                        self._cache.popitem(last=False)
            return out
        except LLMAuthError:
            with self._lock:
                self._last_error, self._last_kind = friendly_reason("auth_invalid"), "auth_invalid"
                self._down_until = time.time() + 3600  # until the key is fixed — .env changes are picked up automatically
            log.error("[LLM] request failed: the API key was rejected (auth_invalid)", extra={"data": {"kind": "auth_invalid"}})
            self._notify()
            raise
        except LLMModelError as exc:
            with self._lock:
                self._last_error, self._last_kind = friendly_reason("model_not_found", str(exc)), "model_not_found"
                self._down_until = time.time() + 3600
            log.error("[LLM] request failed: model not available (model_not_found)", extra={"data": {"kind": "model_not_found"}})
            self._notify()
            if json_mode:
                raise
            return self._offline_reply(messages, self._last_error, "model_not_found", temperature, max_tokens, json_mode)
        except LLMUnavailable as exc:
            kind = getattr(exc, "kind", "unavailable")
            with self._lock:
                self._last_error, self._last_kind = friendly_reason(kind, str(exc)), kind
                # retries already happened in the provider; no credits won't fix itself in seconds
                self._down_until = time.time() + (300 if kind == "quota" else self.cooldown)
            log.warning("[LLM] request failed (%s) — pausing AI calls for %ss", kind, int(self.cooldown),
                        extra={"data": {"kind": kind, "cooldown_s": self.cooldown}})
            self._notify()
            if json_mode:
                raise
            return self._offline_reply(messages, self._last_error, kind, temperature, max_tokens, json_mode)
        except LLMError:
            raise


__all__ = ["build_provider", "ResilientLLM", "LLMError", "LLMUnavailable", "friendly_reason"]
