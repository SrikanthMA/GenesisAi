"""The AI brain: configuration loading (the root causes of "no API key configured"), the LLM service (retries,
diagnostics, health check, reload without restart) and routing — natural language goes to the LLM, local commands keep
working, and the whole thing degrades with a useful message instead of crashing.
"""
from __future__ import annotations

import io
import json
import logging
import os
import tempfile
import time
import unittest
from pathlib import Path

import requests

from genesis.ai.llm.base import LLMAuthError, LLMError, LLMModelError, LLMUnavailable
from genesis.ai.llm.factory import ResilientLLM, build_provider
from genesis.ai.llm.openai_compat import HuggingFaceProvider
from genesis.core import envfile
from genesis.core.config import GenesisConfig

from .helpers import FakeResponse, GenesisTestCase, ScriptedLLM
from .test_conversation import tools_of

KEYS = ("HF_TOKEN", "LLM_API_KEY", "LLM_PROVIDER", "LLM_MODEL", "GENESIS_LLM_PROVIDER", "OPENAI_API_KEY", "HUGGINGFACE_TOKEN",
        "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "HF_MODEL_ID", "LLM_BASE_URL")
TOKEN = "hf_TESTSECRET1234567890"


class EnvIsolation(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in KEYS}
        envfile._applied.clear()
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        for k in KEYS:
            os.environ.pop(k, None)
            if self._saved[k] is not None:
                os.environ[k] = self._saved[k]
        envfile._applied.clear()

    def cfg(self, name=".env", data: bytes = b"", reload=False) -> GenesisConfig:
        if data:
            (self.dir / name).write_bytes(data)
        return GenesisConfig.from_env(env_file=self.dir / name if data or (self.dir / name).exists() else self.dir / "missing.env",
                                      home=self.dir, reload=reload)


# =====================================================================================
# Root causes: .env loading on Windows
# =====================================================================================
class TestEnvLoading(EnvIsolation):
    def test_bom_no_longer_hides_the_first_key(self):
        c = self.cfg(data=b"\xef\xbb\xbfHF_TOKEN=" + TOKEN.encode() + b"\n")
        self.assertEqual(c.hf_token, TOKEN)  # was lost as "﻿HF_TOKEN" before the fix
        self.assertTrue(c.llm_key_present)

    def test_utf16_notepad_unicode(self):
        c = self.cfg(data=("HF_TOKEN=" + TOKEN + "\r\n").encode("utf-16"))
        self.assertEqual(c.hf_token, TOKEN)
        self.assertTrue(any("UTF-16" in w for w in c.env_report.warnings))

    def test_env_txt_is_found_and_flagged(self):
        (self.dir / ".env.txt").write_text(f"HF_TOKEN={TOKEN}\n")
        report = envfile.load_env(self.dir)
        self.assertTrue(report.file.endswith(".env.txt"))
        self.assertTrue(any("rename it" in w for w in report.warnings))
        self.assertEqual(os.environ.get("HF_TOKEN"), TOKEN)

    def test_empty_shell_variable_no_longer_blocks_env(self):
        os.environ["HF_TOKEN"] = ""  # e.g. inherited empty from the terminal / `setx HF_TOKEN ""`
        c = self.cfg(data=f"HF_TOKEN={TOKEN}\n".encode())
        self.assertEqual(c.hf_token, TOKEN)

    def test_real_env_var_still_wins(self):
        os.environ["HF_TOKEN"] = "hf_FROM_SYSTEM"
        c = self.cfg(data=f"HF_TOKEN={TOKEN}\n".encode())
        self.assertEqual(c.hf_token, "hf_FROM_SYSTEM")

    def test_whitespace_quotes_and_bearer_are_cleaned(self):
        c = self.cfg(data=f'HF_TOKEN="  Bearer {TOKEN}  "\n'.encode())
        self.assertEqual(c.hf_token, TOKEN)

    def test_generic_names_and_provider_detection(self):
        c = self.cfg(data=f"LLM_API_KEY={TOKEN}\n".encode())
        self.assertEqual((c.llm_provider, c.hf_token), ("huggingface", TOKEN))
        envfile._applied.clear()
        for k in KEYS:
            os.environ.pop(k, None)
        c = self.cfg(name="b.env", data=b"LLM_PROVIDER=openai\nLLM_API_KEY=sk-test123\nLLM_MODEL=gpt-test\n")
        self.assertEqual((c.llm_provider, c.openai_api_key, c.openai_model), ("openai_compatible", "sk-test123", "gpt-test"))

    def test_key_for_other_provider_is_used_with_a_note(self):
        c = self.cfg(data=b"GENESIS_LLM_PROVIDER=huggingface\nOPENAI_API_KEY=sk-abc\n")
        self.assertEqual(c.llm_provider, "openai_compatible")
        self.assertTrue(any("using openai_compatible" in n for n in c.llm_notes))

    def test_alias_huggingface_token_names(self):
        c = self.cfg(data=f"HUGGINGFACE_TOKEN={TOKEN}\n".encode())
        self.assertEqual(c.hf_token, TOKEN)

    def test_missing_env_is_reported(self):
        c = GenesisConfig.from_env(env_file=self.dir / "nope.env", home=self.dir)
        self.assertFalse(c.llm_key_present)
        self.assertIn("No .env file found", " ".join(c.env_report.warnings))

    def test_report_and_describe_never_contain_secrets(self):
        c = self.cfg(data=f"HF_TOKEN={TOKEN}\n".encode())
        blob = json.dumps(c.describe()) + json.dumps(c.env_report.__dict__) + " ".join(c.llm_notes)
        self.assertNotIn(TOKEN, blob)
        self.assertIn("HF_TOKEN", c.env_report.keys)  # names only

    def test_reload_picks_up_edits_and_removals(self):
        self.cfg(data=f"HF_TOKEN={TOKEN}\n".encode())
        (self.dir / ".env").write_text("HF_TOKEN=hf_NEWTOKEN\n")
        c = GenesisConfig.from_env(env_file=self.dir / ".env", home=self.dir, reload=True)
        self.assertEqual(c.hf_token, "hf_NEWTOKEN")
        (self.dir / ".env").write_text("# key removed\n")
        c = GenesisConfig.from_env(env_file=self.dir / ".env", home=self.dir, reload=True)
        self.assertIsNone(c.hf_token)


# =====================================================================================
# Provider: retries, timeouts, classified errors, no secrets in logs
# =====================================================================================
class SeqSession:
    def __init__(self, *items):
        self.items, self.calls = list(items), []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        item = self.items.pop(0) if len(self.items) > 1 else self.items[0]
        if isinstance(item, Exception):
            raise item
        return item


def ok(text="Hello!"):
    return FakeResponse(200, {"choices": [{"message": {"content": text}}]})


class TestProvider(unittest.TestCase):
    def p(self, *items, **kw):
        return HuggingFaceProvider(TOKEN, session=SeqSession(*items), backoff=0.01, **kw)

    def test_transient_failure_is_retried_with_backoff(self):
        p = self.p(FakeResponse(503, {"e": 1}), ok("fine"))
        self.assertEqual(p.chat([{"role": "user", "content": "hi"}]), "fine")
        self.assertEqual(len(p.session.calls), 2)

    def test_retries_are_bounded(self):
        p = self.p(FakeResponse(503, {"e": 1}))
        with self.assertRaises(LLMUnavailable) as cm:
            p.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(len(p.session.calls), 3)  # 1 + 2 retries, never infinite
        self.assertEqual(cm.exception.kind, "server_error")

    def test_error_kinds(self):
        cases = [(FakeResponse(401, {"error": "bad"}), LLMAuthError, "auth_invalid"),
                 (FakeResponse(404, {"error": "no"}), LLMModelError, "model_not_found"),
                 (FakeResponse(400, {"error": "The requested model 'x' is not supported by any provider"}), LLMModelError,
                  "model_not_found"),
                 (FakeResponse(402, {"error": "You have exceeded your monthly included credits for Inference Providers."}),
                  LLMUnavailable, "quota"),
                 (FakeResponse(400, {"error": "bad things"}), LLMError, "bad_request"),
                 (requests.Timeout("slow"), LLMUnavailable, "timeout"),
                 (requests.ConnectionError("down"), LLMUnavailable, "network")]
        for item, exc_type, kind in cases:
            with self.subTest(kind=kind), self.assertRaises(exc_type) as cm:
                self.p(item).chat([{"role": "user", "content": "hi"}])
            self.assertEqual(cm.exception.kind, kind)

    def test_failure_log_says_why(self):
        records = []

        class H(logging.Handler):
            def emit(self, r):
                records.append(r.getMessage())

        h = H()
        logging.getLogger("genesis").addHandler(h)
        try:
            with self.assertRaises(LLMUnavailable):
                self.p(FakeResponse(402, {"error": "You have exceeded your monthly included credits"})).chat(
                    [{"role": "user", "content": "hi"}])
        finally:
            logging.getLogger("genesis").removeHandler(h)
        line = next(m for m in records if "request failed" in m)
        self.assertIn("HTTP 402", line)
        self.assertIn("exceeded your monthly included credits", line)
        self.assertNotIn(TOKEN, "\n".join(records))

    def test_timeout_and_max_tokens_are_configurable(self):
        p = self.p(ok(), max_tokens_cap=256, timeout=12)
        p.chat([{"role": "user", "content": "hi"}], max_tokens=9999)
        _, kw = p.session.calls[0]
        self.assertEqual(kw["json"]["max_tokens"], 256)
        self.assertEqual(kw["timeout"], (10.0, 12))

    def test_logs_never_contain_the_key(self):
        records = []

        class H(logging.Handler):
            def emit(self, r):
                records.append(r.getMessage() + json.dumps(getattr(r, "data", {}), default=str))

        h = H()
        logging.getLogger("genesis").addHandler(h)
        logging.getLogger("genesis").setLevel(logging.INFO)
        try:
            self.p(FakeResponse(401, {})).chat([{"role": "user", "content": "hi"}]) if False else None
            p = self.p(ok())
            p.chat([{"role": "user", "content": "hi"}])
        finally:
            logging.getLogger("genesis").removeHandler(h)
        text = "\n".join(records)
        self.assertIn("[LLM] request started", text)
        self.assertIn("[LLM] response received", text)
        self.assertNotIn(TOKEN, text)


# =====================================================================================
# Service: health check, degraded replies, reload without restart
# =====================================================================================
class TestLLMService(EnvIsolation):
    def service(self, *items, token=TOKEN):
        cfg = GenesisConfig()
        cfg.hf_token = token
        prov = HuggingFaceProvider(token, session=SeqSession(*items), backoff=0.01)
        return ResilientLLM(prov, config=cfg)

    def test_health_ok(self):
        h = self.service(ok("OK")).health()
        self.assertTrue(h["available"])
        self.assertEqual((h["provider"], h["model"]), ("huggingface", "Qwen/Qwen2.5-72B-Instruct"))
        self.assertNotIn(TOKEN, json.dumps(h))

    def test_health_diagnoses(self):
        self.assertEqual(self.service(FakeResponse(401, {})).health()["error_kind"], "auth_invalid")
        self.assertEqual(self.service(FakeResponse(404, {})).health()["error_kind"], "model_not_found")
        self.assertEqual(self.service(requests.ConnectionError("x")).health()["error_kind"], "network")
        h = self.service(FakeResponse(402, {"error": "exceeded your monthly included credits"})).health()
        self.assertEqual(h["error_kind"], "quota")
        self.assertIn("credits", h["message"])
        h = self.service(ok(), token=None).health()
        self.assertEqual(h["error_kind"], "not_configured")
        self.assertFalse(h["key_present"])

    def test_missing_key_gives_useful_message_not_a_crash(self):
        reply = self.service(ok(), token=None).chat([{"role": "user", "content": "what is ai"}])
        self.assertIn("trouble connecting to my AI model", reply)
        self.assertIn("no API key is configured", reply)
        self.assertIn(".env", reply)

    def test_one_failure_does_not_disable_the_brain_for_long(self):
        s = self.service(requests.ConnectionError("blip"))
        s.chat([{"role": "user", "content": "hi"}])
        self.assertLessEqual(s._down_until - time.time(), 20.5)  # short cooldown (was 60 s)

    def test_reload_after_adding_key_without_restart(self):
        env = self.dir / ".env"
        env.write_text("# no key yet\n")
        sessions = []

        def loader():
            cfg = GenesisConfig.from_env(env_file=env, home=self.dir, reload=True)
            sess = SeqSession(ok("I'm back"))
            sessions.append(sess)
            return build_provider(cfg, session=sess), cfg

        prov, cfg = loader()
        svc = ResilientLLM(prov, loader=loader, config=cfg, env_path=str(env))
        self.assertFalse(svc.online)
        self.assertIn("no API key", svc.chat([{"role": "user", "content": "what is recursion?"}]))
        env.write_text(f"HF_TOKEN={TOKEN}\n")
        os.utime(env, (time.time() + 5, time.time() + 5))
        svc._last_env_check = 0
        self.assertTrue(svc.online)  # picked up automatically
        self.assertEqual(svc.chat([{"role": "user", "content": "hi"}]), "I'm back")
        env.write_text("# removed again\n")
        os.utime(env, (time.time() + 10, time.time() + 10))
        svc._last_env_check = 0
        self.assertFalse(svc.online)


# =====================================================================================
# Routing: natural language → LLM; commands → tools; context-aware answers
# =====================================================================================
def brain(messages, json_mode):
    """A scripted 'model': answers questions, classifies unknown requests, never invents tool results."""
    last = messages[-1]["content"]
    if "natural-language-understanding" in last:
        return json.dumps({"intent": "information_request", "confidence": 0.9, "entities": {}})
    if "planning component" in last:
        return json.dumps({"steps": [], "reply": "Here's a plan: 9am deep work, 1pm lunch, 3pm emails."})
    if json_mode:
        return "{}"
    system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
    history = " | ".join(m["content"] for m in messages[1:-1])
    if "what did i just ask" in last.lower():
        return "ANSWER(history): " + history
    if "what did we just do" in last.lower():
        return "ANSWER(context): " + system.split("What's going on right now:")[-1]
    if "what can you do" in last.lower():
        return "ANSWER(capabilities): " + ("YouTube" if "YouTube" in system else "?")
    return f"ANSWER: {last}"


class TestBrainRouting(GenesisTestCase):
    def setUp(self):
        self.llm = ScriptedLLM(brain)
        super().setUp()

    def chats(self):
        return [m for m in self.svc.llm.primary.calls if m and m[0]["role"] == "system"]

    def test_1_hello(self):
        r = self.say("Hello Genesis")
        self.assertNotIn("trouble connecting", r.text)

    def test_2_3_questions_go_to_the_llm(self):
        for q in ("What is artificial intelligence?", "Explain Python lists.", "What's the capital of Japan?",
                  "Tell me a joke.", "What's the difference between Python and Java?"):
            r = self.say(q)
            self.assertTrue(r.text.startswith("ANSWER:"), (q, r.text))
            self.assertEqual(r.plans, [])  # answered, not turned into a web search or tool call

    def test_write_a_function_is_answered_not_turned_into_a_project(self):
        r = self.say("Write a Python function that reverses a string.")
        self.assertTrue(r.text.startswith("ANSWER:"), r.text)

    def test_voice_questions_reach_the_llm(self):
        r = self.core.handle("what is machine learning", source="voice")
        self.assertTrue(r.text.startswith("ANSWER:"), r.text)

    def test_4_local_command_needs_no_llm(self):
        n = len(self.svc.llm.primary.calls)
        r = self.say("Open VS Code.")
        self.assertEqual(tools_of(r), [("open_application", "succeeded")])
        self.assertEqual(len(self.svc.llm.primary.calls), n)  # fast path: no model call

    def test_5_open_youtube_and_search(self):
        r = self.say("Open YouTube and search for Python tutorials.")
        self.assertEqual([t for t, _ in tools_of(r)], ["open_application", "youtube_search"])

    def test_6_7_context_and_follow_ups(self):
        self.say("Explain Python lists.")
        r = self.say("What did I just ask you?")
        self.assertIn("Explain Python lists", r.text)
        r = self.say("And how is that different in Java?")
        self.assertTrue(r.text.startswith("ANSWER:"))
        last_call = self.chats()[-1]
        self.assertIn("Explain Python lists.", " ".join(m["content"] for m in last_call))  # the follow-up sees the context

    def test_what_did_we_just_do(self):
        self.say("Open Chrome.")
        r = self.say("What did we just do?")
        self.assertIn("open chrome", r.text.lower())

    def test_what_can_you_do_knows_the_tools(self):
        r = self.say("What can you do?")
        self.assertIn("YouTube", r.text)

    def test_help_me_plan_my_day_uses_the_llm(self):
        r = self.say("Help me plan my day.")
        self.assertIn("plan", r.text.lower())
        self.assertNotIn("trouble connecting", r.text)

    def test_check_ai_connection_command(self):
        r = self.say("check your AI connection")
        self.assertIn("connected", r.text.lower())

    def test_8_9_remove_and_restore_key(self):
        real = self.svc.llm.primary
        self.svc.llm.primary = HuggingFaceProvider(None)  # key removed
        r = self.say("What is artificial intelligence?")
        self.assertIn("trouble connecting to my AI model", r.text)
        self.assertIn("no API key is configured", r.text)
        self.assertEqual(self.say("open my downloads folder").status, "done")  # 10: local commands keep working
        self.svc.llm.primary = real  # key restored
        self.svc.llm.reset()
        r = self.say("What is artificial intelligence?")
        self.assertTrue(r.text.startswith("ANSWER:"), r.text)

    def test_10_local_commands_still_work(self):
        for text, tool in (("open my downloads folder", "open_folder"), ("open chrome", "open_application"),
                           ("remind me in 5 minutes to stretch", "create_reminder")):
            r = self.say(text)
            self.assertEqual(r.plans[0]["steps"][0]["tool"], tool, text)
            self.assertEqual(r.status, "done", text)


class TestCheckLLMCli(EnvIsolation):
    def test_cli_prints_json_without_secret(self):
        from contextlib import redirect_stdout

        from genesis.__main__ import main

        (self.dir / ".env").write_text(f"HF_TOKEN={TOKEN}\nHF_BASE_URL=http://127.0.0.1:9/v1\nGENESIS_LLM_TIMEOUT=1\n")
        buf = io.StringIO()
        from genesis.core import config as config_mod

        orig = config_mod.GenesisConfig.from_env.__func__

        def fake_from_env(cls, env_file=None, **kw):
            return orig(cls, env_file=self.dir / ".env", home=self.dir)

        config_mod.GenesisConfig.from_env = classmethod(fake_from_env)
        try:
            with redirect_stdout(buf):
                code = main(["--check-llm"])
        finally:
            config_mod.GenesisConfig.from_env = classmethod(orig)
        out = json.loads(buf.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(out["error_kind"], "network")  # nothing listens on port 9
        self.assertTrue(out["key_present"])
        self.assertNotIn(TOKEN, buf.getvalue())


if __name__ == "__main__":
    unittest.main()


# =====================================================================================
# Speed: as few model calls as possible per request (1.4.1)
# =====================================================================================
class TestFastRoutes(GenesisTestCase):
    def setUp(self):
        self.kinds = []

        def reply(messages, json_mode):
            last = messages[-1]["content"]
            kind = "nlu" if "natural-language-understanding" in last else "plan" if "planning component" in last else "chat"
            self.kinds.append(kind)
            if kind == "plan":
                return json.dumps({"steps": [], "reply": "Here's how."})
            return "{}" if json_mode else "ANSWER"

        self.llm = ScriptedLLM(reply)
        super().setUp()

    def calls_for(self, text, source="text"):
        self.kinds.clear()
        self.core.handle(text, source=source)
        return list(self.kinds)

    def test_questions_take_one_chat_call(self):
        for q in ("what's the capital of japan", "explain recursion", "how do i make pasta", "write a poem about rain",
                  "what did we just do", "tell me a joke"):
            self.assertEqual(self.calls_for(q), ["chat"], q)

    def test_unfamiliar_instructions_take_one_planner_call(self):
        for q in ("i want to learn machine learning", "can you help me with my homework", "summarize the french revolution"):
            self.assertEqual(self.calls_for(q), ["plan"], q)

    def test_commands_need_no_model_call(self):
        for q in ("open vs code", "set volume to 50", "add eggs to my to do list", "weather in bangalore", "what time is it",
                  "remind me in 10 minutes to drink water"):
            self.assertEqual(self.calls_for(q), [], q)
        self.assertIn(("media_key", "volume_up", 25), self.backend.actions)

    def test_live_questions_are_not_answered_from_memory(self):
        self.kinds.clear()
        r = self.core.handle("what's the latest news about nvidia")
        self.assertNotIn("nlu", self.kinds)
        self.assertTrue(any(s["tool"] in ("web_search", "web_answer", "research") for p in r.plans for s in p["steps"]),
                        r.plans)  # searched the web instead of answering from the model's memory

    def test_spoken_answers_are_short(self):
        self.core.handle("what is machine learning", source="voice")
        self.assertEqual(self.svc.llm.primary.max_tokens[-1], 300)
        self.assertIn("spoken aloud", self.svc.llm.primary.calls[-1][0]["content"])
        self.core.handle("what is machine learning", source="text")
        self.assertEqual(self.svc.llm.primary.max_tokens[-1], 600)

    def test_fast_routes_can_be_turned_off(self):
        self.svc.settings.set("llm.fast_routes", False)
        self.assertEqual(self.calls_for("explain recursion"), ["nlu", "chat"])


class TestBenchmarkCli(unittest.TestCase):
    def test_recommends_the_fastest_working_model_without_printing_the_key(self):
        import contextlib
        import io
        from unittest import mock

        from genesis import benchmark
        from genesis.ai.llm.base import LLMModelError
        from genesis.ai.llm.openai_compat import HuggingFaceProvider

        speed = {"Qwen/Qwen2.5-72B-Instruct": 0.03, "meta-llama/Llama-3.3-70B-Instruct": 0.005}

        def fake_chat(self, messages, **kw):
            if self.model not in speed:
                raise LLMModelError("gone")
            time.sleep(speed[self.model])
            return "OK"

        from genesis.core.paths import AppPaths

        cfg = GenesisConfig(paths=AppPaths(Path(tempfile.mkdtemp())))
        cfg.llm_provider, cfg.hf_token, cfg.hf_model_id = "huggingface", TOKEN, "Qwen/Qwen2.5-72B-Instruct"
        out = io.StringIO()
        with mock.patch.object(GenesisConfig, "from_env", return_value=cfg), \
                mock.patch.object(HuggingFaceProvider, "chat", fake_chat), contextlib.redirect_stdout(out):
            code = benchmark.run_benchmark()
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("HF_MODEL_ID=meta-llama/Llama-3.3-70B-Instruct", text)
        self.assertIn("not usable (model_not_found)", text)
        self.assertNotIn(TOKEN, text)


class TestMalformedModelInterpretation(GenesisTestCase):
    """05:17 crash report: the AI model classified a request as 'window' but gave no window operation."""

    def setUp(self):
        self.nlu_reply = {"intent": "window_control", "confidence": 0.8, "entities": {}}

        def reply(messages, json_mode):
            last = messages[-1]["content"]
            if "natural-language-understanding" in last:
                return json.dumps(self.nlu_reply)
            if "planning component" in last:
                return json.dumps({"steps": [], "reply": "Planned answer."})
            return "{}" if json_mode else "Chat answer."

        self.llm = ScriptedLLM(reply)
        super().setUp()

    def test_window_intent_without_operation_never_crashes(self):
        self.svc.settings.set("llm.fast_routes", False)  # force the model-NLU path that crashed
        r = self.say("the thing on my screen is annoying me")
        self.assertNotIn("went wrong", r.text)
        self.assertIn(r.text, ("Chat answer.", "Planned answer."))

    def test_operation_is_inferred_from_the_words(self):
        from genesis.agent.plan import Plan
        from genesis.nlp.intents import Intent, NLUResult

        st = self.core.dialogue.state(self.core.ensure_conversation())
        d = self.core.planner.build(NLUResult(text="please minimise chrome", intent=Intent.WINDOW, confidence=0.8,
                                              slots={"window": "chrome"}, source="llm"), st)
        self.assertIsInstance(d, Plan)
        self.assertEqual((d.steps[0].tool, d.steps[0].args["action"]), ("manage_window", "minimize"))

    def test_any_missing_slot_falls_back_to_the_model(self):
        self.svc.settings.set("llm.fast_routes", False)
        self.nlu_reply = {"intent": "calendar", "confidence": 0.8, "entities": {"calendar_op": "move"}}
        r = self.say("shift that thing to later please")
        self.assertNotIn("went wrong", r.text)
