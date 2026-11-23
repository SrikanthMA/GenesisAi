"""OpenAI-compatible chat-completions provider.

Works with: Hugging Face Inference Providers router, OpenAI, Ollama, LM Studio,
vLLM, llama.cpp server, and anything else that speaks ``/v1/chat/completions``.
"""
from __future__ import annotations

from typing import Optional

import requests

from ...core.logging_setup import get_logger
from .base import LLMAuthError, LLMError, LLMProvider, LLMUnavailable, Message

log = get_logger("llm")


class OpenAICompatibleProvider(LLMProvider):
    name = "openai_compatible"

    def __init__(self, base_url: str, model: str, api_key: Optional[str] = None, *, timeout: float = 45.0,
                 temperature: float = 0.4, is_remote: bool = True, session: Optional[requests.Session] = None,
                 require_key: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self.timeout = timeout
        self.temperature = temperature
        self.is_remote = is_remote
        self.require_key = require_key
        self.session = session or requests.Session()

    def is_configured(self) -> bool:
        return bool(self.model) and (bool(self._api_key) or not self.require_key)

    def describe(self) -> dict:
        d = super().describe()
        d.update({"model": self.model, "endpoint": self.base_url})
        return d

    def chat(self, messages: list[Message], *, temperature: Optional[float] = None, max_tokens: int = 700,
             json_mode: bool = False) -> str:
        if not self.is_configured():
            raise LLMUnavailable(f"{self.name} is not configured (missing API key or model).")
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        url = f"{self.base_url}/chat/completions"
        try:
            resp = self.session.post(url, json=payload, headers=headers, timeout=self.timeout)
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise LLMUnavailable(f"Could not reach {self.name} ({type(exc).__name__}).") from exc
        if resp.status_code == 400 and json_mode:
            # some servers reject response_format — retry without it
            payload.pop("response_format", None)
            try:
                resp = self.session.post(url, json=payload, headers=headers, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                raise LLMUnavailable(f"Could not reach {self.name}.") from exc
        if resp.status_code in (401, 403):
            raise LLMAuthError(f"{self.name} rejected the credentials (HTTP {resp.status_code}). Check your token.")
        if resp.status_code == 429:
            raise LLMUnavailable(f"{self.name} rate limit reached (HTTP 429). Try again shortly.")
        if resp.status_code >= 500:
            raise LLMUnavailable(f"{self.name} server error (HTTP {resp.status_code}).")
        if resp.status_code >= 400:
            snippet = resp.text[:300]
            raise LLMError(f"{self.name} request failed (HTTP {resp.status_code}): {snippet}")
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Unexpected response format from {self.name}.") from exc
        return (content or "").strip()


class HuggingFaceProvider(OpenAICompatibleProvider):
    """Hugging Face Inference Providers (router) — default model Qwen/Qwen2.5-7B-Instruct."""

    name = "huggingface"

    def __init__(self, token: Optional[str], model: str = "Qwen/Qwen2.5-7B-Instruct",
                 base_url: str = "https://router.huggingface.co/v1", **kw) -> None:
        super().__init__(base_url=base_url, model=model, api_key=token, **kw)


class OllamaProvider(OpenAICompatibleProvider):
    """Fully local LLM through Ollama's OpenAI-compatible endpoint."""

    name = "ollama"

    def __init__(self, model: str = "qwen2.5:7b-instruct", base_url: str = "http://localhost:11434/v1", **kw) -> None:
        kw.setdefault("is_remote", False)
        super().__init__(base_url=base_url, model=model, api_key=None, require_key=False, **kw)
