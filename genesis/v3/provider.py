"""GenesisLocalProvider (3.0): the LLM provider Genesis uses in local mode.

Selected with ``GENESIS_LLM_PROVIDER=genesis`` (Setup_Local_AI.bat writes this). Every model call Genesis makes - chat,
understanding, planning, coding - goes through ``chat()`` here, which:

1. asks the router for a decision (deterministic answer / agent / level / model);
2. adds the agent's persona to conversations (not to Genesis's internal JSON steps);
3. calls Ollama; if the model runs out of memory it unloads other models and retries, then tries smaller models;
4. logs the call (agent, model, level, latency, tokens, success) to the local store;
5. only if Ollama can't answer at all *and* external AI is explicitly enabled, uses the external provider - and says so
   in the reply. With external AI disabled (the default in local mode) it raises, and Genesis shows its offline notice.

``describe()`` / ``status()`` expose the last route, loaded models and hardware so the UI can show which agent and which
model answered, local or external, and memory use.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from ..ai.llm.base import LLMError, LLMProvider, LLMUnavailable, Message
from ..core.logging_setup import get_logger
from . import catalog, mode
from .agents import Agent, build_agents, default_models
from .hardware import Hardware, detect
from .ollama import OllamaClient
from .router import Route, Router
from .store import LocalStore

log = get_logger("v3.provider")


class GenesisLocalProvider(LLMProvider):
    name = "genesis-local"
    is_remote = False
    is_generative = True

    def __init__(self, client: OllamaClient, store: Optional[LocalStore] = None, *,
                 hardware: Callable[[], Hardware] = detect,
                 external: Optional[Callable[[], Optional[LLMProvider]]] = None,
                 agents: Optional[dict[str, Agent]] = None, complex_model: Optional[str] = None,
                 cache_s: float = 30.0) -> None:
        self.client = client
        self.store = store
        self._hardware_fn = hardware
        self._external_factory = external
        self._external: Optional[LLMProvider] = None
        overrides = store.agent_overrides() if store else {}
        self.agents = agents or build_agents(overrides)
        cm = complex_model if complex_model is not None else (overrides.get("complex") or default_models()["complex"])
        self.router = Router(self.agents, complex_model=cm)
        self.cache_s = cache_s
        self._lock = threading.RLock()
        self._installed: tuple[float, set[str]] = (0.0, set())
        self._hw: tuple[float, Optional[Hardware]] = (0.0, None)
        self.last_route: Optional[dict] = None
        self.last_chat_route: Optional[dict] = None  # the last conversation answer (what the UI badge shows)
        self.on_route: Optional[Callable[[dict], None]] = None
        self.profile = None  # twin.Profile - the user's saved facts and writing samples (set by the container hook)
        from .modes import Conversation

        self.conversation = Conversation(store)  # 3.1: mode + natural-conversation rules
        self.router.mode = self.conversation.mode
        if store is not None:
            try:
                self.router.specialists = store.specialists()
            except Exception:
                pass

    # ------------------------------------------------------------ cached environment
    def installed(self, refresh: bool = False) -> set[str]:
        with self._lock:
            ts, names = self._installed
            if refresh or time.time() - ts > self.cache_s or not names:
                names = self.client.installed_names()
                self._installed = (time.time(), names)
            return names

    def hardware(self, refresh: bool = False) -> Optional[Hardware]:
        with self._lock:
            ts, hw = self._hw
            if refresh or hw is None or time.time() - ts > self.cache_s:
                try:
                    hw = self._hardware_fn()
                except Exception:  # pragma: no cover - detection never breaks chat
                    hw = None
                self._hw = (time.time(), hw)
            return hw

    def can_fit(self, model: str) -> bool:
        info = catalog.info(model)
        hw = self.hardware()
        if info is None or hw is None:
            return True  # unknown model or hardware: let Ollama try; out-of-memory is handled below
        loaded_gb = sum(m["size_gb"] for m in self.client.loaded() if m["name"] != model)
        free = hw.vram_free_gb + max(0.0, hw.ram_available_gb - 6.0) + loaded_gb  # loaded models can be unloaded
        return info.need_gb <= free

    # ------------------------------------------------------------ LLMProvider
    def is_configured(self) -> bool:
        return True  # Ollama availability is checked per call so starting Ollama later needs no restart

    def describe(self) -> dict:
        d = super().describe()
        lr = self.last_chat_route
        if lr:
            agent = "Python" if lr["agent"] == "python" else lr["agent"].title()
            d["model"] = f"{agent} {lr['model']}".strip() if lr.get("local", True) else f"{lr['model']} (EXTERNAL)"
            d["remote"] = not lr.get("local", True)
        else:
            d["model"] = f"Suzanne {self.agents['suzanne'].model} + Andrew {self.agents['andrew'].model}"
        d.update({"mode": mode.mode(), "external_ai_allowed": mode.external_ai_allowed(),
                  "agents": {k: a.model for k, a in self.agents.items()}, "complex_model": self.router.complex_model,
                  "last_route": self.last_route})
        return d

    def status(self) -> dict:
        """Everything the UI transparency panel shows (one call; safe when Ollama is down)."""
        hw = self.hardware()
        version = self.client.version()
        return {**self.describe(), "ollama": {"running": version is not None, "version": version},
                "loaded": self.client.loaded() if version else [],
                "installed": sorted(self.installed()) if version else [],
                "hardware": hw.to_dict() if hw else None, "hardware_summary": hw.summary() if hw else ""}

    def _with_persona(self, messages: list[Message], route: Route) -> list[Message]:
        if route.kind not in ("chat", "twin") or route.agent not in self.agents:
            return list(messages)
        from .router import last_user_text
        from .twin import TWIN_RE, Profile

        query = last_user_text(messages)
        out = list(messages)
        if route.kind == "twin":
            prof = self.profile or Profile(lambda: [])
            persona = prof.twin_prompt(query)
            for i in range(len(out) - 1, -1, -1):  # strip "answer as me:" so the model answers the actual question
                if out[i].get("role") == "user":
                    out[i] = {"role": "user", "content": TWIN_RE.sub("", str(out[i].get("content") or ""), count=1)}
                    break
        else:
            if route.agent == "specialist":
                return list(messages)  # a specialist SLM gets the request as-is (its own prompt format)
            persona = self.agents[route.agent].system_prompt() + "\n\n" + self.conversation.instructions(query)
            from . import friday

            if friday.enabled() and route.agent == "suzanne" and self.conversation.mode in ("casual", "productivity"):
                from .jarvis_style import SPEAKING_RULES

                persona += "\n\n" + SPEAKING_RULES  # 3.3: JARVIS-style spoken communication
            if route.agent == "suzanne" and self.profile is not None:
                ctx = self.profile.context(query)
                if ctx:
                    persona += "\n\n" + ctx
        if out and out[0].get("role") == "system":
            out[0] = {"role": "system", "content": persona + "\n\n" + str(out[0].get("content") or "")}
        else:
            out.insert(0, {"role": "system", "content": persona})
        return out

    def _stream_turn(self, route: Route, messages, json_mode: bool):
        """The live spoken turn to stream into, if this is a plain English chat reply during a voice conversation."""
        from . import converse

        turn = converse.current()
        if turn is None or json_mode or route.kind not in ("chat", "twin"):
            return None
        sys_text = " ".join(str(m.get("content") or "") for m in messages if m.get("role") == "system")
        if "Write your whole answer in" in sys_text:  # answering in another language: let the normal voice path decide
            return None
        return turn

    def _try_stream(self, model: str, messages, route: Route, turn, *, temperature, max_tokens) -> str:
        agent = self.agents.get(route.agent)
        temp = temperature if temperature is not None else (agent.temperature if agent else 0.5)
        cap = min(max(max_tokens, 200), agent.max_tokens) if agent else max_tokens
        t0 = time.perf_counter()
        parts: list[str] = []
        try:
            for piece in self.client.stream(model, messages, temperature=temp, max_tokens=cap,
                                            num_ctx=agent.num_ctx if agent else None):
                parts.append(piece)
                turn.feed(piece)
            turn.finish()
        except LLMError as exc:
            self._log(route, model, int((time.perf_counter() - t0) * 1000), ok=False, error=exc.kind)
            if parts:  # half an answer was already spoken: return what we have rather than starting over
                turn.finish()
                return "".join(parts).strip()
            raise
        self._log(route, model, int((time.perf_counter() - t0) * 1000), ok=True)
        return "".join(parts).strip()

    def _try(self, model: str, messages, route: Route, *, temperature, max_tokens, json_mode) -> str:
        turn = self._stream_turn(route, messages, json_mode)
        if turn is not None:
            return self._try_stream(model, messages, route, turn, temperature=temperature, max_tokens=max_tokens)
        agent = self.agents.get(route.agent)
        temp = temperature if temperature is not None else (agent.temperature if agent else 0.3)
        if route.kind == "internal":
            temp = min(temp, 0.2)
        cap = max_tokens if route.kind == "internal" or not agent else min(max(max_tokens, 200), agent.max_tokens)
        t0 = time.perf_counter()
        try:
            res = self.client.chat(model, messages, temperature=temp, max_tokens=cap, json_mode=json_mode,
                                   num_ctx=agent.num_ctx if agent else None)
        except LLMError as exc:
            self._log(route, model, int((time.perf_counter() - t0) * 1000), ok=False, error=exc.kind)
            raise
        self._log(route, model, res.total_ms, ok=True, load_ms=res.load_ms, prompt_tokens=res.prompt_tokens,
                  eval_tokens=res.eval_tokens)
        return res.text

    def _log(self, route: Route, model: str, ms: int, *, ok: bool, error: str = "", external: bool = False, **kw):
        if self.store is None:
            return
        try:
            self.store.log_usage(agent=route.agent, model=model, level=route.level, kind=route.kind, latency_ms=ms,
                                 ok=ok, error=error, external=external, **kw)
        except Exception:  # pragma: no cover - logging never breaks a reply
            log.debug("usage log failed", exc_info=True)

    def _announce(self) -> None:
        lr = self.last_route or {}
        if lr.get("kind") == "internal":
            return  # Genesis's own understanding steps: not what the user sees as "the answer"
        self.last_chat_route = dict(lr)
        if self.on_route:
            try:
                self.on_route(self.describe())
            except Exception:  # pragma: no cover - the UI never breaks a reply
                log.debug("on_route failed", exc_info=True)

    def _candidates(self, route: Route) -> list[str]:
        # the routed model first, then smaller ones (by catalogue size; unknown models keep their order)
        rest = [m for m in route.fallbacks if m and m != route.model]
        rest.sort(key=lambda m: catalog.info(m).need_gb if catalog.info(m) else 99)
        return [route.model] + rest

    def chat(self, messages: list[Message], *, temperature: Optional[float] = None, max_tokens: int = 700,
             json_mode: bool = False) -> str:
        installed = self.installed()
        loaded = {m["name"] for m in self.client.loaded()} if installed else set()
        self.router.mode = self.conversation.mode
        route = self.router.route(messages, json_mode=json_mode, installed=installed, loaded=loaded,
                                  can_fit=self.can_fit)
        self.last_route = {**route.to_dict(), "local": True, "ts": time.time()}
        if route.answer is not None:
            self._log(route, "python", 0, ok=True)
            self._announce()
            return route.answer
        msgs = self._with_persona(messages, route)
        last: Optional[LLMError] = None
        for i, model in enumerate(self._candidates(route)):
            try:
                text = self._try(model, msgs, route, temperature=temperature, max_tokens=max_tokens,
                                 json_mode=json_mode)
                if model != route.model:
                    self.last_route.update(model=model, reason=route.reason + f"; fell back to {model}")
                if route.kind in ("chat", "twin"):
                    self.conversation.remember_reply(text)
                self._announce()
                return text
            except LLMError as exc:
                last = exc
                if exc.kind == "not_running":
                    # 3.0.3: Ollama isn't running (e.g. after a PC restart) - start it and try once more
                    if not getattr(self, "_restarted", False) and self.client.ensure_running():
                        self._restarted = True
                        try:
                            text = self._try(model, msgs, route, temperature=temperature, max_tokens=max_tokens,
                                             json_mode=json_mode)
                            self._announce()
                            return text
                        except LLMError as exc2:
                            last = exc2
                        finally:
                            self._restarted = False
                    break  # no local model can answer
                if exc.kind == "out_of_memory" and i == 0:
                    for m in self.client.loaded():  # free memory held by other models, then retry the same one once
                        if m["name"] != model:
                            self.client.unload(m["name"])
                    try:
                        text = self._try(model, msgs, route, temperature=temperature, max_tokens=max_tokens,
                                         json_mode=json_mode)
                        self._announce()
                        return text
                    except LLMError as exc2:
                        last = exc2
                log.info("local model %s failed (%s); trying the next one", model, exc.kind)
        return self._external_or_raise(messages, route, last, temperature=temperature, max_tokens=max_tokens,
                                       json_mode=json_mode)

    def _external_or_raise(self, messages, route: Route, last: Optional[LLMError], **kw) -> str:
        err = last or LLMUnavailable("No local model could answer.", kind="unavailable")
        if not mode.external_fallback_enabled() or self._external_factory is None:
            raise err
        if self._external is None:
            self._external = self._external_factory()
        ext = self._external
        if ext is None or not ext.is_configured():
            raise err
        t0 = time.perf_counter()
        text = ext.chat(messages, **kw)
        self._log(route, getattr(ext, "name", "external"), int((time.perf_counter() - t0) * 1000), ok=True,
                  external=True)
        self.last_route = {**route.to_dict(), "model": getattr(ext, "name", "external"), "local": False,
                           "reason": f"local model unavailable ({err.kind}); external AI is enabled", "ts": time.time()}
        self._announce()
        if route.kind == "internal" or kw.get("json_mode"):
            return text  # structured output must stay parseable; the UI badge shows it was external
        return f"{text}\n\n_(Answered by {ext.name} - external AI, because the local model was unavailable.)_"


# ------------------------------------------------------------------ process-wide runtime (tools reach it here)
_RUNTIME: dict = {}


def runtime(config=None) -> dict:
    """{"client", "store"} shared by the provider and the v3 tools (created on first use)."""
    if not _RUNTIME:
        url = getattr(config, "ollama_base_url", "") if config is not None else ""
        data_dir = config.paths.data_dir if config is not None else None
        _RUNTIME["client"] = OllamaClient(url, auto_start=True)
        _RUNTIME["store"] = LocalStore(data_dir / "genesis_local.db") if data_dir else LocalStore(":memory:")
    return _RUNTIME


def build_local_provider(config, external: Optional[Callable[[], Optional[LLMProvider]]] = None,
                         session=None) -> GenesisLocalProvider:
    rt = runtime(config)
    client = rt["client"] if session is None else OllamaClient(getattr(config, "ollama_base_url", ""), session=session)
    timeout = getattr(config, "llm_timeout", None)
    if timeout:
        client.timeout = max(float(timeout), 120.0)  # first load of a model from disk can take a while
    prov = GenesisLocalProvider(client, rt["store"], external=external)
    rt["provider"] = prov
    return prov


def find_provider(ctx) -> Optional[GenesisLocalProvider]:
    llm = getattr(ctx, "llm", None)
    for cand in (llm, getattr(llm, "primary", None)):
        if isinstance(cand, GenesisLocalProvider):
            return cand
    return _RUNTIME.get("provider")


def reset_runtime() -> None:
    st = _RUNTIME.get("store")
    if st is not None:
        try:
            st.close()
        except Exception:
            pass
    _RUNTIME.clear()
