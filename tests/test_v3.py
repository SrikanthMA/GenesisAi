"""Genesis 3.0 - local-first intelligence (Ollama, Suzanne/Andrew, router, store, Genesis SLM).

No real Ollama or GPU is needed: HTTP calls go to a fake Ollama server object, and command runners are fakes. Real speed
and answer quality can only be measured on the user's PC (``python -m genesis.v3.selftest`` there).
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from genesis.ai.llm.base import LLMModelError, LLMUnavailable
from genesis.v3 import catalog, commands, mode
from genesis.v3.agents import build_agents
from genesis.v3.hardware import GPU, Hardware, detect, parse_nvidia_smi
from genesis.v3.ollama import OllamaClient, base_url
from genesis.v3.provider import GenesisLocalProvider
from genesis.v3.router import Router, Thresholds, arithmetic
from genesis.v3.slm import EVAL_SET, SLMManager, build_modelfile, score_answer
from genesis.v3.store import LocalStore

GB = 1024 ** 3


class Resp:
    def __init__(self, status=200, data=None, text="", lines=None):
        self.status_code, self._data, self.text, self._lines = status, data, text or json.dumps(data or {}), lines

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data

    def iter_lines(self):
        for line in self._lines or []:
            yield json.dumps(line).encode()


class FakeOllama:
    """Minimal stand-in for Ollama's REST API."""

    def __init__(self, installed=("llama3.2:3b", "qwen2.5-coder:7b"), running=True):
        self.installed = list(installed)
        self.loaded = []
        self.running = running
        self.calls = []
        self.fail = {}  # model -> Resp to return on /api/chat

    def get(self, url, timeout=None):
        if not self.running:
            raise requests.ConnectionError("refused")
        if url.endswith("/api/version"):
            return Resp(data={"version": "0.12.0"})
        if url.endswith("/api/tags"):
            return Resp(data={"models": [{"name": n, "size": 2 * GB, "details": {"parameter_size": "3B",
                                                                               "quantization_level": "Q4_K_M"}}
                                         for n in self.installed]})
        if url.endswith("/api/ps"):
            return Resp(data={"models": [{"name": n, "size": 3 * GB, "size_vram": 3 * GB} for n in self.loaded]})
        return Resp(404, text="not found")

    def post(self, url, json=None, timeout=None, stream=False):
        if not self.running:
            raise requests.ConnectionError("refused")
        self.calls.append((url.rsplit("/", 1)[-1], json))
        if url.endswith("/api/chat"):
            model = json["model"]
            if model in self.fail:
                r = self.fail[model]
                return r() if callable(r) else r
            if model not in self.installed:
                return Resp(404, text=f'{{"error":"model \\"{model}\\" not found, try pulling it first"}}')
            if model not in self.loaded:
                self.loaded.append(model)
            if stream:
                return Resp(lines=[{"message": {"content": "Hel"}}, {"message": {"content": "lo"}}, {"done": True}])
            sys = next((m["content"] for m in json["messages"] if m["role"] == "system"), "")
            return Resp(data={"message": {"content": f"[{model}] ok"}, "eval_count": 40, "prompt_eval_count": 20,
                              "total_duration": 800_000_000, "load_duration": 100_000_000, "_sys": sys})
        if url.endswith("/api/generate"):
            if json["model"] in self.loaded:
                self.loaded.remove(json["model"])
            return Resp(data={})
        if url.endswith("/api/pull"):
            self.installed.append(json["model"])
            return Resp(lines=[{"status": "pulling manifest"}, {"status": "downloading", "total": 100, "completed": 50},
                               {"status": "success"}])
        return Resp(404, text="not found")


def hw(vram_free=7.5, ram_avail=10.0, disk=200.0):
    return Hardware(cpu="Test CPU", cores_physical=8, cores_logical=16, ram_total_gb=16, ram_available_gb=ram_avail,
                    gpus=[GPU("NVIDIA GeForce RTX 5060", 8.0, vram_free, 3.0, "580.00")] if vram_free else [],
                    disk_free_gb=disk)


ENV_LOCAL = {"GENESIS_MODE": "local", "GENESIS_EXTERNAL_AI_ENABLED": "false", "SUZANNE_MODEL": "llama3.2:3b",
             "ANDREW_MODEL": "qwen2.5-coder:7b", "GENESIS_COMPLEX_MODEL": ""}


class TestMode(unittest.TestCase):
    def test_local_blocks_external_by_default(self):
        with mock.patch.dict(os.environ, {"GENESIS_MODE": "local"}, clear=False):
            os.environ.pop("GENESIS_EXTERNAL_AI_ENABLED", None)
            self.assertTrue(mode.is_local())
            self.assertFalse(mode.external_ai_allowed())
        with mock.patch.dict(os.environ, {"GENESIS_MODE": "local", "GENESIS_EXTERNAL_AI_ENABLED": "true"}):
            self.assertTrue(mode.external_ai_allowed())

    def test_hybrid_default(self):
        with mock.patch.dict(os.environ, {"GENESIS_MODE": ""}):
            self.assertEqual(mode.mode(), "hybrid")
            self.assertTrue(mode.external_ai_allowed())


class TestHardware(unittest.TestCase):
    def test_parse_nvidia_smi(self):
        g = parse_nvidia_smi("NVIDIA GeForce RTX 5060 Laptop GPU, 8151, 7420, 4, 581.29\n")
        self.assertEqual(len(g), 1)
        self.assertAlmostEqual(g[0].vram_total_gb, 7.96, places=1)
        self.assertEqual(g[0].utilisation_pct, 4.0)
        self.assertEqual(parse_nvidia_smi("garbage"), [])

    def test_detect_degrades(self):
        def boom(cmd):
            raise FileNotFoundError

        h = detect(run=boom)
        self.assertEqual(h.gpus, [])
        self.assertGreater(h.ram_total_gb, 0)
        self.assertIn("no NVIDIA GPU", h.summary())

    def test_detect_with_gpu(self):
        h = detect(run=lambda cmd: "NVIDIA GeForce RTX 5060, 8151, 7000, 10, 581.29")
        self.assertEqual(h.best_gpu.name, "NVIDIA GeForce RTX 5060")
        self.assertIn("video memory free", h.summary())


class TestCatalog(unittest.TestCase):
    def test_rtx5060_16gb(self):
        rec = catalog.recommend(hw())
        self.assertEqual(rec["suzanne"], "llama3.2:3b")
        self.assertEqual(rec["andrew"], "qwen2.5-coder:7b")
        self.assertEqual(rec["where"], "gpu")
        self.assertTrue(rec["disk_ok"])
        self.assertEqual(rec["complex"], "qwen2.5:14b")  # GPU + system RAM split
        self.assertTrue(any("share system RAM" in n for n in rec["notes"]))

    def test_cpu_only_small_ram(self):
        rec = catalog.recommend(hw(vram_free=0, ram_avail=6.0))
        self.assertEqual(rec["where"], "cpu")
        self.assertIsNone(rec["complex"])
        self.assertLessEqual(catalog.BY_NAME[rec["andrew"]].need_gb, 6.0)

    def test_low_disk(self):
        rec = catalog.recommend(hw(disk=5))
        self.assertFalse(rec["disk_ok"])


class TestOllamaClient(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama()
        self.c = OllamaClient("http://localhost:11434/v1", session=self.fake)

    def test_base_url(self):
        self.assertEqual(base_url("http://localhost:11434/v1/"), "http://localhost:11434")
        self.assertEqual(self.c.url, "http://localhost:11434")

    def test_discovery(self):
        self.assertTrue(self.c.available())
        self.assertIn("llama3.2:3b", self.c.installed_names())
        self.assertEqual(self.c.loaded(), [])

    def test_chat_and_payload(self):
        r = self.c.chat("llama3.2:3b", [{"role": "user", "content": "hi"}], max_tokens=50, json_mode=True)
        self.assertEqual(r.text, "[llama3.2:3b] ok")
        self.assertEqual((r.eval_tokens, r.total_ms, r.load_ms), (40, 800, 100))
        body = self.fake.calls[-1][1]
        self.assertEqual(body["options"]["num_predict"], 50)
        self.assertEqual(body["format"], "json")
        self.assertNotIn("think", body)
        self.assertIs(OllamaClient._payload("qwen3:8b", [], temperature=0, max_tokens=1, json_mode=False,
                                            num_ctx=None, keep_alive="1m", stream=False)["think"], False)

    def test_errors(self):
        with self.assertRaises(LLMModelError):
            self.c.chat("missing:1b", [{"role": "user", "content": "hi"}])
        self.fake.fail["llama3.2:3b"] = Resp(500, text='{"error":"model requires more system memory (9 GiB)"}')
        with self.assertRaises(LLMUnavailable) as cm:
            self.c.chat("llama3.2:3b", [{"role": "user", "content": "hi"}])
        self.assertEqual(cm.exception.kind, "out_of_memory")
        self.fake.running = False
        self.assertFalse(self.c.available())
        with self.assertRaises(LLMUnavailable) as cm:
            self.c.chat("llama3.2:3b", [{"role": "user", "content": "hi"}])
        self.assertEqual(cm.exception.kind, "not_running")

    def test_stream_pull_unload(self):
        self.assertEqual("".join(self.c.stream("llama3.2:3b", [{"role": "user", "content": "hi"}])), "Hello")
        seen = []
        self.assertTrue(self.c.pull("phi4-mini", progress=lambda s, f: seen.append(f)))
        self.assertIn(0.5, seen)
        self.fake.loaded = ["llama3.2:3b"]
        self.assertTrue(self.c.unload("llama3.2:3b"))
        self.assertEqual(self.fake.loaded, [])


class TestRouter(unittest.TestCase):
    def setUp(self):
        with mock.patch.dict(os.environ, ENV_LOCAL):
            self.agents = build_agents()
        self.r = Router(self.agents, complex_model="qwen2.5:14b", thresholds=Thresholds(280, 1500))
        self.inst = {"llama3.2:3b", "qwen2.5-coder:7b", "qwen2.5:14b"}

    def u(self, text):
        return [{"role": "user", "content": text}]

    def test_arithmetic(self):
        self.assertEqual(arithmetic("what is 12*(3+4)?"), "12*(3+4) = 84")
        self.assertEqual(arithmetic("17.5% of 240"), "17.5% of 240 is 42.")
        self.assertEqual(arithmetic("2^10"), "2^10 = 1,024")
        self.assertIn("divides by zero", arithmetic("5/0"))
        self.assertIsNone(arithmetic("what is love"))
        self.assertIsNone(arithmetic("2024"))
        self.assertIsNone(arithmetic("__import__('os')"))
        self.assertIsNone(arithmetic("9**9**9"))
        route = self.r.route(self.u("calculate 1500 / 12"), installed=self.inst)
        self.assertEqual((route.agent, route.model, route.answer), ("python", "", "1500 / 12 = 125"))

    def test_conversation_goes_to_suzanne(self):
        route = self.r.route(self.u("hey, how was your day?"), installed=self.inst)
        self.assertEqual((route.agent, route.level, route.model), ("suzanne", 1, "llama3.2:3b"))

    def test_code_goes_to_andrew(self):
        route = self.r.route(self.u("write a python function that reverses a list"), installed=self.inst)
        self.assertEqual((route.agent, route.level, route.model), ("andrew", 2, "qwen2.5-coder:7b"))

    def test_internal_json(self):
        route = self.r.route(self.u("open notepad"), json_mode=True, installed=self.inst)
        self.assertEqual((route.agent, route.kind), ("andrew", "internal"))
        # a quick internal step reuses Suzanne's already-loaded model rather than swapping
        route = self.r.route(self.u("open notepad"), json_mode=True, installed=self.inst, loaded={"llama3.2:3b"})
        self.assertEqual(route.model, "llama3.2:3b")

    def test_level3(self):
        text = "Design the architecture for a full application in depth: " + "requirements. " * 40
        route = self.r.route(self.u(text), installed=self.inst, can_fit=lambda m: True)
        self.assertEqual((route.level, route.model), (3, "qwen2.5:14b"))
        route = self.r.route(self.u(text), installed=self.inst, can_fit=lambda m: False)
        self.assertEqual(route.model, "qwen2.5-coder:7b")
        self.assertIn("doesn't fit", route.reason)

    def test_prefers_loaded_for_simple_chat(self):
        route = self.r.route(self.u("thanks!"), installed=self.inst, loaded={"qwen2.5-coder:7b"})
        self.assertEqual(route.model, "qwen2.5-coder:7b")
        self.assertIn("already loaded", route.reason)

    def test_missing_model_falls_back(self):
        route = self.r.route(self.u("write a python script"), installed={"llama3.2:3b"})
        self.assertEqual(route.model, "llama3.2:3b")


class TestStore(unittest.TestCase):
    def test_roundtrip(self):
        st = LocalStore(":memory:")
        st.log_usage(agent="suzanne", model="m", level=1, kind="chat", latency_ms=500, load_ms=100, eval_tokens=40)
        st.log_usage(agent="suzanne", model="m", level=1, kind="chat", latency_ms=50, ok=False, error="timeout")
        stats = st.usage_stats()
        self.assertEqual((stats[0]["calls"], stats[0]["failures"]), (2, 1))
        self.assertEqual(stats[0]["tok_per_s"], 100.0)
        st.set_agent_model("andrew", "qwen2.5:7b")
        self.assertEqual(st.agent_overrides(), {"andrew": "qwen2.5:7b"})
        st.add_example("q", "a")
        with self.assertRaises(ValueError):
            st.add_example("q", " ")
        v1 = st.add_version(model_name="genesis-slm:v1", base_model="llama3.2:3b", method="modelfile", examples=1)
        v2 = st.add_version(model_name="genesis-slm:v2", base_model="llama3.2:3b", method="modelfile", examples=1)
        st.activate(v2)
        st.activate(v1)
        self.assertEqual(st.active_version()["version"], 1)
        self.assertEqual(set(st.export()), {"model_usage", "agent_config", "session_summary", "slm_examples",
                                            "slm_versions", "missions", "specialists"})
        st.wipe()
        self.assertEqual((st.examples(), st.recent_usage()), ([], []))
        self.assertEqual(len(st.versions()), 2)


class TestProvider(unittest.TestCase):
    def make(self, fake=None, external=None, env=None):
        self.fake = fake or FakeOllama()
        self.store = LocalStore(":memory:")
        with mock.patch.dict(os.environ, {**ENV_LOCAL, **(env or {})}):
            return GenesisLocalProvider(OllamaClient(session=self.fake), self.store, hardware=lambda: hw(),
                                        external=external)

    def test_chat_persona_and_logging(self):
        p = self.make()
        out = p.chat([{"role": "system", "content": "Genesis chat"}, {"role": "user", "content": "hi there"}])
        self.assertEqual(out, "[llama3.2:3b] ok")
        sent = self.fake.calls[-1][1]["messages"][0]["content"]
        self.assertIn("Suzanne", sent)
        self.assertIn("Genesis chat", sent)
        self.assertEqual(p.last_route["agent"], "suzanne")
        self.assertTrue(p.last_route["local"])
        self.assertEqual(self.store.recent_usage()[0]["model"], "llama3.2:3b")
        self.assertFalse(p.describe()["remote"])

    def test_internal_has_no_persona(self):
        p = self.make()
        p.chat([{"role": "system", "content": "Return only JSON."}, {"role": "user", "content": "open notepad"}],
               json_mode=True)
        sent = self.fake.calls[-1][1]["messages"][0]["content"]
        self.assertNotIn("Suzanne", sent)
        self.assertLessEqual(self.fake.calls[-1][1]["options"]["temperature"], 0.2)

    def test_math_needs_no_model(self):
        p = self.make()
        self.assertEqual(p.chat([{"role": "user", "content": "what is 6*7"}]), "6*7 = 42")
        self.assertEqual([c for c in self.fake.calls if c[0] == "chat"], [])

    def test_out_of_memory_unloads_then_falls_back(self):
        fake = FakeOllama()
        fake.loaded = ["llama3.2:3b"]
        oom = Resp(500, text='{"error":"CUDA error: out of memory"}')
        fake.fail["qwen2.5-coder:7b"] = oom
        p = self.make(fake)
        out = p.chat([{"role": "user", "content": "debug this python error please"}])
        self.assertEqual(out, "[llama3.2:3b] ok")  # retried after unloading, then a smaller model answered
        self.assertIn(("generate", {"model": "llama3.2:3b", "keep_alive": 0}), fake.calls)
        self.assertIn("fell back", p.last_route["reason"])

    def test_not_running_no_silent_external(self):
        ext = mock.Mock()
        p = self.make(FakeOllama(running=False), external=lambda: ext)
        for env in ({"GENESIS_MODE": "local", "GENESIS_EXTERNAL_AI_ENABLED": "false"},
                    {"GENESIS_MODE": "hybrid", "GENESIS_EXTERNAL_AI_ENABLED": ""}):
            with mock.patch.dict(os.environ, env), self.assertRaises(LLMUnavailable):
                p.chat([{"role": "user", "content": "hello"}])
        ext.chat.assert_not_called()

    def test_external_only_when_enabled_and_reported(self):
        ext = mock.Mock()
        ext.name = "gemini"
        ext.is_configured.return_value = True
        ext.chat.return_value = "external answer"
        p = self.make(FakeOllama(running=False), external=lambda: ext)
        with mock.patch.dict(os.environ, {"GENESIS_MODE": "local", "GENESIS_EXTERNAL_AI_ENABLED": "true"}):
            out = p.chat([{"role": "user", "content": "hello"}])
        self.assertIn("external answer", out)
        self.assertIn("external AI", out)
        self.assertFalse(p.last_route["local"])
        self.assertEqual(self.store.recent_usage()[0]["external"], 1)

    def test_status_when_down(self):
        p = self.make(FakeOllama(running=False))
        st = p.status()
        self.assertFalse(st["ollama"]["running"])
        self.assertIn("RTX 5060", st["hardware_summary"])


class TestSLM(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LocalStore(":memory:")
        self.fake = FakeOllama(installed=("llama3.2:3b",))
        self.cmds = []

        def runner(cmd):
            self.cmds.append(cmd)
            if cmd[1] == "create":
                self.fake.installed.append(cmd[2])
            return 0, "success"

        self.mgr = SLMManager(self.store, OllamaClient(session=self.fake), self.tmp.name, runner=runner)

    def tearDown(self):
        self.tmp.cleanup()

    def test_modelfile(self):
        mf = build_modelfile("llama3.2:3b", examples=[{"prompt": 'say """hi', "answer": "hello Boss"}])
        self.assertTrue(mf.startswith("FROM llama3.2:3b\n"))
        self.assertIn("SYSTEM \"\"\"You are Genesis", mf)
        self.assertIn('MESSAGE assistant """hello Boss"""', mf)
        self.assertEqual(mf.count('"""'), 6)  # system + 2 messages, inner quotes neutralised
        self.assertIn("ADAPTER /a", build_modelfile("b", adapter="/a"))

    def test_build_evaluate_activate_rollback(self):
        self.mgr.approve("Who are you?", "I'm Genesis, Boss.")
        r1 = self.mgr.build("llama3.2:3b")
        self.assertTrue(r1["ok"])
        self.assertEqual(r1["model"], "genesis-slm:v1")
        self.assertIn("MESSAGE user", (Path(self.tmp.name) / "v1" / "Modelfile").read_text())
        self.assertIsNone(self.store.active_version())  # never activated automatically
        rep = self.mgr.evaluate_version(1)
        self.assertEqual(len(rep["results"]), len(EVAL_SET))
        self.assertIsNotNone(self.store.versions()[0]["score"])
        r2 = self.mgr.build("llama3.2:3b")
        self.assertEqual(r2["version"], 2)
        self.assertTrue(self.mgr.activate(2)["ok"])
        self.assertTrue(self.mgr.rollback(1)["ok"])
        self.assertEqual(self.store.active_version()["version"], 1)
        self.assertEqual(self.cmds[-1], ["ollama", "cp", "genesis-slm:v1", "genesis-slm"])
        self.assertFalse(self.mgr.activate(9)["ok"])

    def test_dataset_export(self):
        self.mgr.approve("q1", "a1")
        self.mgr.correct("q2", "better")
        path, n = self.mgr.export_dataset()
        recs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(n, 2)
        self.assertEqual([m["role"] for m in recs[0]["messages"]], ["system", "user", "assistant"])

    def test_scoring(self):
        who, memory = EVAL_SET[0], EVAL_SET[1]
        self.assertTrue(score_answer(who, "I'm Genesis, your local assistant.")[0])
        self.assertFalse(score_answer(who, "I am ChatGPT")[0])
        self.assertTrue(score_answer(memory, "I don't know - you haven't told me.")[0])
        self.assertFalse(score_answer(memory, "You had pancakes!")[0])

    def test_failed_create(self):
        mgr = SLMManager(self.store, OllamaClient(session=self.fake), self.tmp.name,
                         runner=lambda cmd: (1, "Error: invalid model name"))
        r = mgr.build("llama3.2:3b")
        self.assertFalse(r["ok"])
        self.assertEqual(self.store.versions(), [])


class TestCommands(unittest.TestCase):
    def test_phrases(self):
        cases = {
            "check my local AI setup": ("local_ai_status", {}),
            "which model answered?": ("local_ai_status", {}),
            "recommend models for my pc": ("local_ai_recommend", {}),
            "download the model phi4-mini": ("local_ai_download", {"model": "phi4-mini"}),
            "use qwen2.5:7b for andrew": ("local_ai_set_model", {"model": "qwen2.5:7b", "agent": "andrew"}),
            "set Suzanne's model to gemma3:4b": ("local_ai_set_model", {"agent": "suzanne", "model": "gemma3:4b"}),
            "Genesis, save that as a good answer": ("slm_approve_answer", {}),
            "no, the right answer is: Paris": ("slm_approve_answer", {"answer": "Paris"}),
            "build the genesis slm": ("slm_build", {}),
            "build a new genesis slm from qwen2.5:7b": ("slm_build", {"base": "qwen2.5:7b"}),
            "roll back to genesis slm version 2": ("slm_activate", {"version": 2}),
            "free gpu memory": ("local_ai_unload", {}),
            "delete my local ai data": ("local_ai_wipe", {}),
        }
        for text, want in cases.items():
            self.assertEqual(commands.match(text), want, text)
        self.assertIsNone(commands.match("what's the weather like"))


class TestTools(unittest.TestCase):
    def test_tool_specs(self):
        from genesis.security.permissions import RiskLevel
        from genesis.v3.tools import TOOLS

        names = [t.name for t in TOOLS]
        self.assertEqual(len(names), len(set(names)))
        by = {t.name: t for t in TOOLS}
        for risky in ("local_ai_download", "slm_build", "slm_activate", "local_ai_wipe"):
            self.assertTrue(by[risky].always_confirm, risky)
        self.assertEqual(by["local_ai_wipe"].risk, RiskLevel.HIGH)
        for t in TOOLS:
            t.spec()

    def test_command_args_validate(self):
        from genesis.v3.tools import TOOLS

        by = {t.name: t for t in TOOLS}
        for text in ("download the model phi4-mini", "roll back to genesis slm version 2", "use qwen2.5:7b for andrew"):
            tool, args = commands.match(text)
            by[tool].validate(args)


class TestHooks(unittest.TestCase):
    def test_v2_gates(self):
        from genesis.v3 import hooks

        class SmartSTT:
            @property
            def cloud_enabled(self):
                return True

        def provider_order():
            return ["cloudflare", "pollinations", "wikimedia"]

        g = {"__name__": "fake_v2", "SmartSTT": SmartSTT}
        hooks.patch_v2_voice(g)
        g2 = {"__name__": "fake_img", "provider_order": provider_order}
        hooks.patch_v2_images(g2)
        with mock.patch.dict(os.environ, {"GENESIS_MODE": "local", "GENESIS_EXTERNAL_AI_ENABLED": "false",
                                          "GENESIS_CLOUD_VOICE": "false"}):
            self.assertFalse(SmartSTT().cloud_enabled)
            self.assertEqual(g2["provider_order"](), ["wikimedia"])
        with mock.patch.dict(os.environ, {"GENESIS_MODE": "hybrid"}):
            self.assertTrue(SmartSTT().cloud_enabled)
            self.assertEqual(len(g2["provider_order"]()), 3)
        hooks.patch_v2_voice(g)  # idempotent

    def test_patcher_apply_check_remove(self):
        import shutil
        import subprocess
        import sys

        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as d:
            for rel in ("genesis/v3/hooks.py", "genesis/core/config.py", "genesis/nlp/rules.py"):
                (Path(d) / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(root / rel, Path(d) / rel)
            cfg = Path(d) / "genesis/core/config.py"
            cfg.write_text("".join(ln for ln in cfg.read_text().splitlines(keepends=True) if "v3 hook" not in ln))
            original = cfg.read_text()
            script = str(root / "scripts" / "apply_v3_hooks.py")
            r = subprocess.run([sys.executable, script, "--root", d], capture_output=True, text=True)
            self.assertEqual(r.returncode, 1)  # factory/registry missing in this partial copy -> warning
            self.assertTrue(cfg.read_text().startswith(original))
            self.assertIn("patch_config", cfg.read_text())
            subprocess.run([sys.executable, script, "--root", d], capture_output=True, text=True)
            self.assertEqual(cfg.read_text().count("v3 hook"), 1)  # idempotent
            subprocess.run([sys.executable, script, "--root", d, "--remove"], capture_output=True, text=True)
            self.assertEqual(cfg.read_text().rstrip(), original.rstrip())


class TestEndToEnd(unittest.TestCase):
    """A full Genesis instance (planner, executor, tools) answering through the local provider and a fake Ollama."""

    def setUp(self):
        from tests.helpers import GenesisTestCase

        self.env = mock.patch.dict(os.environ, ENV_LOCAL)
        self.env.start()
        self.fake = FakeOllama()
        provider = GenesisLocalProvider(OllamaClient(session=self.fake), LocalStore(":memory:"), hardware=lambda: hw())

        class Case(GenesisTestCase):
            llm = provider

            def runTest(self):  # pragma: no cover
                pass

        self.case = Case()
        self.case.setUp()
        self.provider = provider

    def tearDown(self):
        from genesis.v3.provider import reset_runtime

        self.case.tearDown()
        self.env.stop()
        reset_runtime()

    def test_chat_status_and_approve(self):
        r = self.case.say("hey, how are you doing today?")
        self.assertIn("ok", r.text)
        self.assertTrue(any(c[0] == "chat" for c in self.fake.calls))
        self.assertIsNotNone(self.provider.last_route)
        self.assertIsNotNone(self.provider.on_route)  # the container hook connected the UI badge
        st = self.case.svc.llm.status()
        self.assertFalse(st["remote"])
        self.assertTrue(st["model"].startswith(("Suzanne", "Andrew")))
        r = self.case.say("check my local AI setup")
        self.assertIn("Mode: local", r.text)
        self.assertIn("Suzanne uses llama3.2:3b", r.text)
        r = self.case.say("save that as a good answer")
        self.assertIn("Saved as a training example", r.text)


if __name__ == "__main__":
    unittest.main()


class TestIntegration(unittest.TestCase):
    def tearDown(self):
        from genesis.v3.provider import reset_runtime

        reset_runtime()

    def test_factory_builds_local_provider(self):
        from genesis.ai.llm.factory import ResilientLLM, build_provider
        from genesis.core.config import GenesisConfig

        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {**ENV_LOCAL, "GENESIS_LLM_PROVIDER":
                                                                             "genesis"}):
            cfg = GenesisConfig.from_env(env_file=Path(d) / "none.env", home=Path(d))
            self.assertEqual(cfg.llm_provider, "genesis")
            self.assertTrue(cfg.llm_key_present)
            prov = build_provider(cfg, session=FakeOllama())
            self.assertIsInstance(prov, GenesisLocalProvider)
            self.assertFalse(prov.is_remote)
            self.assertEqual(ResilientLLM(prov).chat([{"role": "user", "content": "hello"}]), "[llama3.2:3b] ok")
            self.assertTrue((Path(d) / "data" / "genesis_local.db").exists())

    def test_external_provider_hook(self):
        from genesis.ai.llm.factory import build_provider
        from genesis.core.config import GenesisConfig
        from genesis.v3.hooks import external_provider

        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GENESIS_EXTERNAL_PROVIDER": ""}):
            cfg = GenesisConfig.from_env(env_file=Path(d) / "none.env", home=Path(d))
            self.assertIsNone(external_provider(cfg, build_provider))
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GENESIS_EXTERNAL_PROVIDER": "gemini",
                                                                             "GEMINI_API_KEY": "test-not-real"}):
            cfg = GenesisConfig.from_env(env_file=Path(d) / "none.env", home=Path(d))
            ext = external_provider(cfg, build_provider)
            self.assertTrue(ext is None or ext.is_remote)

    def test_rules_and_registry(self):
        from genesis.nlp import rules
        from genesis.nlp.intents import Intent
        from genesis.tools.registry import build_default_registry

        r = rules.parse("check my local AI setup")
        self.assertEqual((r.intent, r.slots["tool"]), (Intent.FOLLOW_UP, "local_ai_status"))
        reg = build_default_registry()
        self.assertIsNotNone(reg.get("slm_build"))

    def test_local_mode_blocks_cloud_embeddings(self):
        from genesis.memory.embeddings import HuggingFaceEmbedder, create_embedder

        with mock.patch.dict(os.environ, {"GENESIS_MODE": "local", "GENESIS_EXTERNAL_AI_ENABLED": "false"}):
            self.assertNotIsInstance(create_embedder("huggingface", "x", "hf_token"), HuggingFaceEmbedder)
