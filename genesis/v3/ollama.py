"""Native Ollama client (3.0): the local model runtime.

Uses Ollama's own REST API on http://127.0.0.1:11434 (nothing leaves the PC):

* ``available()``      — is Ollama running?  (``GET /api/version``)
* ``installed()``      — downloaded models   (``GET /api/tags``)
* ``loaded()``         — models in memory now, with their VRAM use (``GET /api/ps``)
* ``chat()``           — one answer (``POST /api/chat``), with timing and token counts for the usage log
* ``stream()``         — the same, token by token (for interfaces that can show text as it arrives)
* ``pull()``           — download a model, reporting progress (only ever called after the user agreed)
* ``unload()``         — free a model's memory (``keep_alive: 0``)

Errors are turned into Genesis's LLM error types with plain explanations: Ollama not running, model not installed,
not enough memory.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional

import requests

from ..ai.llm.base import LLMError, LLMModelError, LLMUnavailable
from ..core.logging_setup import get_logger

log = get_logger("v3.ollama")
DEFAULT_URL = "http://127.0.0.1:11434"
THINKING_FAMILIES = ("qwen3", "deepseek-r1", "qwq", "gpt-oss", "magistral")


def ollama_paths() -> list[str]:
    import os
    import shutil

    local = os.getenv("LOCALAPPDATA") or ""
    cands = [os.path.join(local, "Programs", "Ollama", "ollama app.exe"), os.path.join(local, "Programs", "Ollama",
                                                                                       "ollama.exe"),
             r"C:\Program Files\Ollama\ollama app.exe", r"C:\Program Files\Ollama\ollama.exe", shutil.which("ollama") or ""]
    return [c for c in cands if c and os.path.isfile(c)]


_LAST_START = [0.0]


def start_ollama() -> bool:
    """Launch the Ollama app (or 'ollama serve') in the background, at most once a minute."""
    import os
    import subprocess

    if time.time() - _LAST_START[0] < 60:
        return True  # already starting
    paths = ollama_paths()
    if not paths:
        return False
    exe = paths[0]
    kw = {"creationflags": 0x08000000 | 0x00000008} if os.name == "nt" else {"start_new_session": True}  # no window, detached
    try:
        args = [exe] if exe.lower().endswith("ollama app.exe") else [exe, "serve"]
        subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    except OSError:
        return False
    _LAST_START[0] = time.time()
    return True


def base_url(configured: str = "") -> str:
    """Ollama's native root from the configured OpenAI-style URL (``http://localhost:11434/v1`` -> ``…:11434``)."""
    u = (configured or DEFAULT_URL).rstrip("/")
    return u[:-3] if u.endswith("/v1") else u


@dataclass
class ChatResult:
    text: str
    model: str
    eval_tokens: int = 0
    prompt_tokens: int = 0
    total_ms: int = 0
    load_ms: int = 0


class OllamaClient:
    def __init__(self, url: str = DEFAULT_URL, session: Any = None, timeout: float = 180.0,
                 auto_start: bool = False) -> None:
        self.auto_start = auto_start  # may Genesis launch Ollama itself when it isn't running?
        self.url = base_url(url)
        self.http = session or requests.Session()
        self.timeout = timeout

    # ---------------------------------------------------------------- discovery
    def version(self) -> Optional[str]:
        try:
            r = self.http.get(self.url + "/api/version", timeout=2.5)
            return (r.json() or {}).get("version") if r.status_code == 200 else None
        except Exception:
            return None

    def available(self) -> bool:
        return self.version() is not None

    def ensure_running(self, wait_s: float = 25.0, launcher=None) -> bool:
        """Start Ollama if it isn't running (3.0.3: e.g. after a restart of the PC). True when it answers."""
        if self.available():
            return True
        if not self.auto_start and launcher is None:
            return False
        if not self.url.startswith(("http://127.0.0.1", "http://localhost")):
            return False  # a remote Ollama server can't be started from here
        started = (launcher or start_ollama)()
        if not started:
            return False
        end = time.time() + wait_s
        while time.time() < end:
            time.sleep(0.5)
            if self.available():
                log.info("started Ollama")
                return True
        return False

    def installed(self) -> list[dict]:
        """[{name, size_gb, parameters, quantization, family}] for every downloaded model."""
        try:
            r = self.http.get(self.url + "/api/tags", timeout=5)
            items = (r.json() or {}).get("models") or [] if r.status_code == 200 else []
        except Exception:
            return []
        out = []
        for m in items:
            d = m.get("details") or {}
            out.append({"name": m.get("name") or m.get("model", ""), "size_gb": round((m.get("size") or 0) / 1024 ** 3, 2),
                        "parameters": d.get("parameter_size", ""), "quantization": d.get("quantization_level", ""),
                        "family": d.get("family", "")})
        return out

    def installed_names(self) -> set[str]:
        names = set()
        for m in self.installed():
            n = m["name"]
            names.add(n)
            if n.endswith(":latest"):
                names.add(n[: -len(":latest")])
        return names

    def loaded(self) -> list[dict]:
        """Models currently in memory: [{name, vram_gb, size_gb}]."""
        try:
            r = self.http.get(self.url + "/api/ps", timeout=5)
            items = (r.json() or {}).get("models") or [] if r.status_code == 200 else []
        except Exception:
            return []
        return [{"name": m.get("name") or m.get("model", ""), "vram_gb": round((m.get("size_vram") or 0) / 1024 ** 3, 2),
                 "size_gb": round((m.get("size") or 0) / 1024 ** 3, 2)} for m in items]

    # ---------------------------------------------------------------- inference
    @staticmethod
    def _payload(model: str, messages: list[dict], *, temperature: float, max_tokens: int, json_mode: bool,
                 num_ctx: Optional[int], keep_alive: str, stream: bool) -> dict:
        opts: dict = {"temperature": temperature, "num_predict": int(max_tokens)}
        if num_ctx:
            opts["num_ctx"] = int(num_ctx)
        body: dict = {"model": model, "messages": messages, "stream": stream, "options": opts, "keep_alive": keep_alive}
        if json_mode:
            body["format"] = "json"
        if any(f in model.lower() for f in THINKING_FAMILIES):
            body["think"] = False  # an assistant reply, not a long chain of thought (faster, and fits short budgets)
        return body

    def _error(self, r: Any, model: str) -> LLMError:
        body = (getattr(r, "text", "") or "")[:300]
        low = body.lower()
        if r.status_code == 404 or "not found" in low or "pull" in low and "model" in low:
            return LLMModelError(f"The local model “{model}” isn't installed. Run Setup_Local_AI.bat (or "
                                 f"`ollama pull {model}`).")
        if "memory" in low or "cuda" in low and "alloc" in low:
            return LLMUnavailable(f"The local model “{model}” needs more memory than is free right now.",
                                  kind="out_of_memory")
        return LLMError(f"Ollama returned an error (HTTP {r.status_code}): {' '.join(body.split())[:160]}",
                        kind="server_error")

    def chat(self, model: str, messages: list[dict], *, temperature: float = 0.4, max_tokens: int = 700,
             json_mode: bool = False, num_ctx: Optional[int] = None, keep_alive: str = "10m") -> ChatResult:
        body = self._payload(model, messages, temperature=temperature, max_tokens=max_tokens, json_mode=json_mode,
                             num_ctx=num_ctx, keep_alive=keep_alive, stream=False)
        t0 = time.perf_counter()
        try:
            r = self.http.post(self.url + "/api/chat", json=body, timeout=self.timeout)
        except requests.ConnectionError as exc:
            raise LLMUnavailable("Ollama isn't running. Start the Ollama app (it lives in the system tray) or run "
                                 "Setup_Local_AI.bat.", kind="not_running") from exc
        except requests.Timeout as exc:
            raise LLMUnavailable(f"The local model took longer than {int(self.timeout)} s.", kind="timeout") from exc
        if r.status_code != 200:
            raise self._error(r, model)
        try:
            data = r.json()
        except ValueError as exc:
            raise LLMError("Ollama returned an unreadable answer.", kind="bad_response") from exc
        if data.get("error"):
            raise self._error(type("R", (), {"status_code": 500, "text": str(data["error"])})(), model)
        text = ((data.get("message") or {}).get("content") or "").strip()
        return ChatResult(text=text, model=model, eval_tokens=int(data.get("eval_count") or 0),
                          prompt_tokens=int(data.get("prompt_eval_count") or 0),
                          total_ms=int((data.get("total_duration") or 0) / 1e6) or int((time.perf_counter() - t0) * 1000),
                          load_ms=int((data.get("load_duration") or 0) / 1e6))

    def stream(self, model: str, messages: list[dict], *, temperature: float = 0.4, max_tokens: int = 700,
               num_ctx: Optional[int] = None, keep_alive: str = "10m") -> Iterator[str]:
        """Yield the answer piece by piece as the model writes it."""
        body = self._payload(model, messages, temperature=temperature, max_tokens=max_tokens, json_mode=False,
                             num_ctx=num_ctx, keep_alive=keep_alive, stream=True)
        try:
            r = self.http.post(self.url + "/api/chat", json=body, timeout=self.timeout, stream=True)
        except requests.ConnectionError as exc:
            raise LLMUnavailable("Ollama isn't running.", kind="not_running") from exc
        if r.status_code != 200:
            raise self._error(r, model)
        for line in r.iter_lines():
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except ValueError:
                continue
            if chunk.get("error"):
                raise LLMError(str(chunk["error"])[:200], kind="server_error")
            piece = (chunk.get("message") or {}).get("content") or ""
            if piece:
                yield piece
            if chunk.get("done"):
                break

    # ---------------------------------------------------------------- lifecycle
    def unload(self, model: str) -> bool:
        try:
            r = self.http.post(self.url + "/api/generate", json={"model": model, "keep_alive": 0}, timeout=20)
            return r.status_code == 200
        except Exception:
            return False

    def pull(self, model: str, progress: Optional[Callable[[str, float], None]] = None) -> bool:
        """Download ``model``; ``progress(status, fraction)`` is called as it goes. Returns True when complete."""
        try:
            r = self.http.post(self.url + "/api/pull", json={"model": model, "stream": True}, timeout=None, stream=True)
        except requests.ConnectionError as exc:
            raise LLMUnavailable("Ollama isn't running.", kind="not_running") from exc
        if r.status_code != 200:
            raise self._error(r, model)
        ok = False
        for line in r.iter_lines():
            if not line:
                continue
            try:
                st = json.loads(line)
            except ValueError:
                continue
            if st.get("error"):
                raise LLMError(f"Downloading {model} failed: {st['error']}"[:200], kind="server_error")
            total, done = st.get("total") or 0, st.get("completed") or 0
            if progress:
                progress(st.get("status", ""), (done / total) if total else 0.0)
            ok = ok or st.get("status") == "success"
        return ok
