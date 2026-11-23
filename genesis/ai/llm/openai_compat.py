"""OpenAI-compatible chat-completions provider.

Works with: Hugging Face Inference Providers router, OpenAI, Ollama, LM Studio,
vLLM, llama.cpp server, and anything else that speaks ``/v1/chat/completions``.
"""
from __future__ import annotations

import time
from typing import Optional

import requests

from ...core.logging_setup import get_logger
from ...security.redaction import redact_text
from .base import LLMAuthError, LLMError, LLMModelError, LLMProvider, LLMUnavailable, Message

log = get_logger("llm")

RETRIES = 2  # extra attempts for transient failures (so at most 3 requests), never infinite
BACKOFF = 0.6  # seconds, doubled each retry


class OpenAICompatibleProvider(LLMProvider):
    name = "openai_compatible"

    def __init__(self, base_url: str, model: str, api_key: Optional[str] = None, *, timeout: float = 45.0,
                 temperature: float = 0.4, is_remote: bool = True, session: Optional[requests.Session] = None,
                 require_key: bool = True, max_tokens_cap: int = 4096, retries: int = RETRIES, backoff: float = BACKOFF,
                 extra_headers: Optional[dict] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self.timeout = timeout
        self.temperature = temperature
        self.is_remote = is_remote
        self.require_key = require_key
        self.session = session or requests.Session()
        self.max_tokens_cap = max(64, int(max_tokens_cap))
        self.retries = max(0, int(retries))
        self.backoff = backoff
        self.extra_headers = extra_headers or {}
        log.info("[LLM] provider initialised", extra={"data": {"provider": self.name, "model": self.model, "endpoint": self.base_url,
                                                               "key_present": bool(api_key)}})

    def warm(self) -> None:
        """Open the (keep-alive) HTTPS connection ahead of the first request, so it doesn't pay for DNS + TLS."""
        if not self.is_remote or not self.is_configured():
            return
        headers = dict(self.extra_headers)
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            self.session.get(f"{self.base_url}/models", headers=headers, timeout=(5, 8))
        except Exception:
            pass

    def is_configured(self) -> bool:
        return bool(self.model) and (bool(self._api_key) or not self.require_key)

    def missing_config(self) -> Optional[str]:
        """Why this provider can't be used (no secrets), or None."""
        if not self.model:
            return f"no model is set for {self.name} (set LLM_MODEL in .env)"
        if self.require_key and not self._api_key:
            return f"no API key is configured for {self.name}"
        return None

    def describe(self) -> dict:
        d = super().describe()
        d.update({"model": self.model, "endpoint": self.base_url})
        return d

    def _post(self, url: str, payload: dict, headers: dict) -> requests.Response:
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self.session.post(url, json=payload, headers=headers, timeout=(min(10.0, self.timeout), self.timeout))
            except requests.Timeout as exc:
                err: LLMError = LLMUnavailable(f"{self.name} didn't answer within {int(self.timeout)} s.", kind="timeout")
                cause: Exception = exc
            except requests.ConnectionError as exc:
                err, cause = LLMUnavailable(f"Could not reach {self.name} ({type(exc).__name__}) — check your internet "
                                            "connection.", kind="network"), exc
            else:
                status = getattr(resp, "status_code", 200)
                if status == 429 or status >= 500:
                    err = LLMUnavailable(f"{self.name} is busy (HTTP {status}).", kind="rate_limited" if status == 429 else "server_error")
                    cause = None
                    if attempt <= self.retries:
                        wait = self.backoff * (2 ** (attempt - 1))
                        try:
                            wait = max(wait, min(5.0, float(resp.headers.get("Retry-After", 0))))
                        except (TypeError, ValueError, AttributeError):
                            pass
                        log.info("[LLM] retrying", extra={"data": {"attempt": attempt, "status": status, "wait_s": round(wait, 2)}})
                        time.sleep(wait)
                        continue
                    raise err
                return resp
            if attempt <= self.retries:
                wait = self.backoff * (2 ** (attempt - 1))
                log.info("[LLM] retrying", extra={"data": {"attempt": attempt, "error": err.kind, "wait_s": round(wait, 2)}})
                time.sleep(wait)
                continue
            raise err from cause

    def chat(self, messages: list[Message], *, temperature: Optional[float] = None, max_tokens: int = 700,
             json_mode: bool = False) -> str:
        missing = self.missing_config()
        if missing:
            raise LLMUnavailable(missing, kind="not_configured")
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": min(int(max_tokens), self.max_tokens_cap),
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        from ...v2 import gemini_tuning  # v2 hook (2.2.2): Gemini Flash answers without "thinking" (fast, full replies)

        gemini_tuning.tune(self, payload)
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        url = f"{self.base_url}/chat/completions"
        t0 = time.perf_counter()
        log.info("[LLM] request started", extra={"data": {"provider": self.name, "model": self.model, "json": json_mode,
                                                          "messages": len(messages)}})
        resp = self._post(url, payload, headers)
        if gemini_tuning.rejected(self, payload, resp):  # v2 hook: this model doesn't accept the setting - ask without it
            resp = self._post(url, payload, headers)
        if resp.status_code == 400 and json_mode and "model" not in resp.text[:400].lower():
            payload.pop("response_format", None)  # some servers reject response_format — retry without it
            resp = self._post(url, payload, headers)
        self._raise_for_status(resp)
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Unexpected response format from {self.name}.", kind="bad_response") from exc
        log.info("[LLM] response received in %d ms", int((time.perf_counter() - t0) * 1000),
                 extra={"data": {"provider": self.name, "ms": int((time.perf_counter() - t0) * 1000),
                                                            "chars": len(content or "")}})
        return (content or "").strip()

    def _raise_for_status(self, resp) -> None:
        status = resp.status_code
        if status < 400:
            return
        body = (resp.text or "")[:400]
        low = body.lower()
        reason = redact_text(" ".join(body.split()))[:160]  # the server's explanation — never contains our key
        log.warning("[LLM] request failed: HTTP %s from %s (model %s) — %s", status, self.name, self.model, reason or "no details",
                    extra={"data": {"provider": self.name, "status": status}})
        if status == 402 or (status in (400, 403, 429) and any(w in low for w in (
                "exceeded your monthly", "included credits", "pre-paid credits", "insufficient credit", "payment required",
                "quota", "billing"))):
            raise LLMUnavailable(f"{self.name}: the account has no credits left for this model (HTTP {status}).", kind="quota")
        if status in (401, 403):
            raise LLMAuthError(f"{self.name} rejected the API key (HTTP {status}). Check the key in your .env file.")
        if status == 404 or (status in (400, 422) and "model" in low and any(w in low for w in (
                "not found", "does not exist", "not supported", "unknown model", "invalid model", "no provider"))):
            raise LLMModelError(f"The model “{self.model}” isn't available from {self.name} (HTTP {status}). "
                                "Check the model name in your .env file.")
        raise LLMError(f"{self.name} request failed (HTTP {status}): {body[:200]}", kind="bad_request")


class HuggingFaceProvider(OpenAICompatibleProvider):
    """Hugging Face Inference Providers (router) — default model Qwen/Qwen2.5-72B-Instruct."""

    name = "huggingface"

    def __init__(self, token: Optional[str], model: str = "Qwen/Qwen2.5-72B-Instruct",
                 base_url: str = "https://router.huggingface.co/v1", **kw) -> None:
        super().__init__(base_url=base_url, model=model, api_key=token, **kw)


class OllamaProvider(OpenAICompatibleProvider):
    """Fully local LLM through Ollama's OpenAI-compatible endpoint."""

    name = "ollama"

    def __init__(self, model: str = "qwen2.5:7b-instruct", base_url: str = "http://localhost:11434/v1", **kw) -> None:
        kw.setdefault("is_remote", False)
        super().__init__(base_url=base_url, model=model, api_key=None, require_key=False, **kw)


class AnthropicProvider(OpenAICompatibleProvider):
    """Anthropic models through Anthropic's OpenAI-compatible endpoint (set LLM_MODEL to a Claude model name)."""

    name = "anthropic"

    def __init__(self, api_key: Optional[str], model: str = "", base_url: str = "https://api.anthropic.com/v1", **kw) -> None:
        super().__init__(base_url=base_url, model=model, api_key=api_key, **kw)


class GeminiProvider(OpenAICompatibleProvider):
    """Google Gemini through Google's OpenAI-compatible endpoint (set LLM_MODEL to a Gemini model name)."""

    name = "gemini"

    def __init__(self, api_key: Optional[str], model: str = "",
                 base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai", **kw) -> None:
        super().__init__(base_url=base_url, model=model, api_key=api_key, **kw)

    def chat(self, messages, **kw) -> str:  # v2 hook (2.2.3): a rate-limited model hands over to another Gemini model
        from ...v2 import gemini_tuning

        return gemini_tuning.chat_with_failover(super().chat, self, messages, **kw)

