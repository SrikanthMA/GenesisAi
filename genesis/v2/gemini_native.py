"""Google Gemini's native REST API (2.1): audio transcription and image generation.

The chat model goes through Gemini's OpenAI-compatible endpoint (genesis/ai/llm). Audio input and image output are
simpler and better documented on the native ``models/{model}:generateContent`` endpoint, so 2.1 uses it for those two
jobs. The API key is read from the running config (``GEMINI_API_KEY``) and is only ever sent to Google; it is never
logged. Both AI Studio key formats work: the key goes in the ``x-goog-api-key`` header, and if Google rejects that, it is
retried once as a Bearer token.
"""
from __future__ import annotations

import base64
import io
import time
import wave
from typing import Any, Optional

import requests

from ..core.logging_setup import get_logger

log = get_logger("v2.gemini")
HOST = "https://generativelanguage.googleapis.com"
DEFAULT_TEXT_MODEL = "gemini-flash-lite-latest"


class GeminiError(RuntimeError):
    def __init__(self, message: str, kind: str = "error", status: int = 0) -> None:
        super().__init__(message)
        self.kind = kind  # not_configured | auth | quota | not_found | network | refused | error
        self.status = status


def gemini_key(config: Any) -> Optional[str]:
    key = getattr(config, "gemini_api_key", None)
    return key.strip() if isinstance(key, str) and key.strip() else None


def text_model(config: Any) -> str:
    if getattr(config, "llm_provider", "") == "gemini" and getattr(config, "llm_model", ""):
        return config.llm_model
    return DEFAULT_TEXT_MODEL


def pcm_to_wav(pcm16: bytes, sample_rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm16)
    return buf.getvalue()


def _classify(status: int, body: str) -> GeminiError:
    low = body.lower()
    short = " ".join(body.split())[:160]
    if status == 429 or "quota" in low or "resource_exhausted" in low or "billing" in low or "limit: 0" in low:
        return GeminiError(f"Gemini quota/billing limit (HTTP {status}).", "quota", status)
    if status in (401, 403) or "api key not valid" in low or "unauthenticated" in low or "permission" in low:
        return GeminiError(f"Gemini rejected the key (HTTP {status}).", "auth", status)
    if status == 404 or ("model" in low and "not found" in low):
        return GeminiError(f"Gemini model not available (HTTP {status}).", "not_found", status)
    if status == 400 and ("safety" in low or "blocked" in low or "prohibited" in low):
        return GeminiError("Gemini declined that request.", "refused", status)
    return GeminiError(f"Gemini request failed (HTTP {status}): {short}", "error", status)


def generate_content(config: Any, model: str, parts: list[dict], *, generation_config: Optional[dict] = None,
                     timeout: float = 60.0, session: Any = None) -> dict:
    """POST models/{model}:generateContent and return the JSON response (raises GeminiError)."""
    key = gemini_key(config)
    if not key:
        raise GeminiError("No Gemini API key is configured (GEMINI_API_KEY in .env).", "not_configured")
    http = session or requests
    url = f"{HOST}/v1beta/models/{model}:generateContent"
    body: dict = {"contents": [{"role": "user", "parts": parts}]}
    if generation_config:
        body["generationConfig"] = generation_config
    last: Optional[GeminiError] = None
    for headers in ({"x-goog-api-key": key}, {"Authorization": f"Bearer {key}"}):
        t0 = time.perf_counter()
        try:
            r = http.post(url, json=body, headers={"Content-Type": "application/json", **headers}, timeout=timeout)
        except requests.RequestException as exc:
            raise GeminiError(f"Couldn't reach Gemini ({type(exc).__name__}).", "network") from exc
        log.info("[gemini] %s %s in %d ms", model, getattr(r, "status_code", "?"), int((time.perf_counter() - t0) * 1000))
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError as exc:
                raise GeminiError("Gemini returned an unreadable answer.", "error") from exc
        last = _classify(r.status_code, getattr(r, "text", "") or "")
        if last.kind != "auth":
            break  # only an auth failure is worth retrying with the other header style
    raise last  # type: ignore[misc]


def _parts(resp: dict) -> list[dict]:
    try:
        return resp["candidates"][0]["content"]["parts"] or []
    except (KeyError, IndexError, TypeError):
        fb = (resp or {}).get("promptFeedback") or {}
        if fb.get("blockReason"):
            raise GeminiError("Gemini declined that request.", "refused")
        return []


VOCABULARY = ("Genesis, Google Colab, Colab, notebook, cell, VS Code, Visual Studio Code, PyCharm, OnlineGDB, Programiz, "
              "LeetCode, HackerRank, Replit, GeeksforGeeks, compiler, Python, Java, JavaScript, C++, C#, HTML, CSS, SQL, "
              "Fibonacci, factorial, palindrome, bubble sort, binary search, linked list, recursion, function, loop, array, "
              "Google Meet, Gmail, WhatsApp, YouTube, Spotify, Chrome, Notepad, Excel, Word, PowerPoint, minutes of meeting, MOM")
# transcription uses the lite model first (largest free allowance, so voice keeps working), then Flash; each model has
# its own free quota, so one being busy doesn't stop the other
STT_MODELS_DEFAULT = ("gemini-flash-lite-latest", "gemini-flash-latest")


def _stt_instruction(hint: str, long_form: bool) -> str:
    if long_form:
        return ("Transcribe this meeting audio verbatim in its original language. Write one line per speaker turn. "
                "Do not summarise, translate or add anything. If nothing intelligible is said, reply with nothing.")
    text = ("Transcribe exactly what the speaker says to their desktop voice assistant called Genesis. Return only the "
            "words spoken, in the language spoken, with normal punctuation - no quotes, no comments. "
            "Typical requests: open apps or websites, write/run/fix code (in Google Colab, an online compiler or an editor), "
            "search the web, show or generate an image, play music, write an email, set reminders, take meeting notes, ask "
            "questions. Sound-alike words - choose by meaning: 'image' (pictures) vs 'email'; 'code' vs 'coat'; 'cell' vs "
            "'sell'; 'Colab' vs 'collab'; 'run it' vs 'ran it'; 'C++' vs 'C plus plus' (write C++). "
            f"Vocabulary that may occur: {VOCABULARY}. If there is no speech, return an empty answer.")
    if hint:
        text += f" Context (may help with names): {hint[:500]}."
    return text


def _transcribe_openai_compat(config: Any, wav: bytes, instruction: str, model: str, *, session: Any = None,
                              timeout: float = 15.0) -> str:
    """Same request through Gemini's OpenAI-compatible endpoint (the one the chat model already uses successfully)."""
    key = gemini_key(config)
    http = session or requests
    body = {"model": model, "temperature": 0, "max_tokens": 300,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": instruction},
                {"type": "input_audio", "input_audio": {"data": base64.b64encode(wav).decode(), "format": "wav"}}]}]}
    try:
        r = http.post(f"{HOST}/v1beta/openai/chat/completions", json=body, timeout=timeout,
                      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    except requests.RequestException as exc:
        raise GeminiError(f"Couldn't reach Gemini ({type(exc).__name__}).", "network") from exc
    if r.status_code != 200:
        raise _classify(r.status_code, getattr(r, "text", "") or "")
    try:
        return (r.json()["choices"][0]["message"]["content"] or "").strip()
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise GeminiError("Gemini returned an unreadable answer.", "error") from exc


def transcribe(config: Any, pcm16: bytes, sample_rate: int = 16000, *, hint: str = "", session: Any = None,
               timeout: float = 15.0, model: Optional[str] = None, long_form: bool = False) -> str:
    """Speech -> text with Gemini (understands accents and context far better than a small local model).

    Tries the better Flash model (thinking off) then the configured chat model, on the native endpoint; if Google rejects
    the key there, the same request goes through the OpenAI-compatible endpoint the chat already uses."""
    import os

    if not pcm16:
        return ""
    instruction = _stt_instruction(hint, long_form)
    wav = pcm_to_wav(pcm16, sample_rate)
    if model:
        models = [model]
    else:
        env = (os.getenv("GENESIS_STT_MODEL") or "").strip()
        models = list(dict.fromkeys(([env] if env else list(STT_MODELS_DEFAULT)) + [text_model(config)]))
    parts = [{"text": instruction}, {"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(wav).decode()}}]
    last: Optional[GeminiError] = None
    for m in models:
        gen: dict = {"temperature": 0.0, "maxOutputTokens": 4096 if long_form else 300}
        if "flash" in m:
            gen["thinkingConfig"] = {"thinkingBudget": 0}
        try:
            try:
                resp = generate_content(config, m, parts, generation_config=gen, timeout=timeout, session=session)
            except GeminiError as exc:
                if exc.kind == "error" and exc.status == 400 and "thinkingConfig" in gen:
                    gen.pop("thinkingConfig")  # this model can't switch thinking off - ask again without it
                    resp = generate_content(config, m, parts, generation_config=gen, timeout=timeout, session=session)
                else:
                    raise
            text = " ".join(p.get("text", "") for p in _parts(resp) if isinstance(p, dict)).strip()
            return text.strip().strip('"').strip()
        except GeminiError as exc:
            last = exc
            if exc.kind == "auth":
                try:
                    return _transcribe_openai_compat(config, wav, instruction, m, session=session, timeout=timeout).strip('"')
                except GeminiError as exc2:
                    last = exc2
            if exc.kind in ("network", "refused"):
                break
            continue  # quota / not available for this model -> the next model
    raise last or GeminiError("Transcription failed.", "error")


IMAGE_MODELS = ("gemini-2.5-flash-image", "gemini-3.1-flash-lite-image", "gemini-3.1-flash-image")


def generate_image(config: Any, prompt: str, *, model: Optional[str] = None, session: Any = None,
                   timeout: float = 90.0) -> tuple[bytes, str, str]:
    """Text → image with a Gemini image model. Returns (bytes, mime type, model). Needs billing on most accounts."""
    candidates = [model] if model else list(IMAGE_MODELS)
    last: Optional[GeminiError] = None
    for m in candidates:
        try:
            resp = generate_content(config, m, [{"text": prompt}], session=session, timeout=timeout,
                                    generation_config={"responseModalities": ["TEXT", "IMAGE"]})
        except GeminiError as exc:
            last = exc
            if exc.kind == "not_found":
                continue  # try the next model name
            raise
        for p in _parts(resp):
            data = (p.get("inlineData") or p.get("inline_data") or {}) if isinstance(p, dict) else {}
            if data.get("data"):
                return base64.b64decode(data["data"]), data.get("mimeType") or data.get("mime_type") or "image/png", m
        last = GeminiError("Gemini didn't return an image.", "error")
    raise last or GeminiError("No Gemini image model is available.", "not_found")
