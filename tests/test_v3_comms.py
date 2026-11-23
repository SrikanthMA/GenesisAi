"""Genesis 3.2 - communication controller (streamed speech, echo rejection) and the autonomous debugging loop.

The debugging tests use a real temporary project with real unittest runs; only the language model is scripted."""
import json
import os
import re
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from genesis.v3 import converse, hooks

ENV = {"GENESIS_MODE": "local", "GENESIS_EXTERNAL_AI_ENABLED": "false", "SUZANNE_MODEL": "llama3.2:3b",
       "ANDREW_MODEL": "qwen2.5-coder:7b", "GENESIS_COMPLEX_MODEL": ""}


class EchoTests(unittest.TestCase):
    def test_is_echo(self):
        said = "Good evening, Boss. Genesis is online. The weather in Chennai is thirty one degrees and sunny."
        self.assertTrue(converse.is_echo("the weather in chennai is thirty one degrees", said))
        self.assertTrue(converse.is_echo("genesis is online", said) is False)  # contains "genesis" -> always heard
        self.assertFalse(converse.is_echo("open spotify and play some music", said))
        self.assertFalse(converse.is_echo("stop", said))
        self.assertFalse(converse.is_echo("wait, what about tomorrow", said))
        self.assertTrue(converse.is_echo("good evening", said))      # short phrase fully contained
        self.assertFalse(converse.is_echo("good morning", said))     # not fully contained -> keep it
        self.assertFalse(converse.is_echo("anything", ""))

    def test_spoken_log_window(self):
        log = converse.SpokenLog()
        log.add("Hello Boss.")
        self.assertIn("Hello Boss.", log.recent())
        self.assertEqual(log.recent(now=time.monotonic() + 60), "")


class StreamTests(unittest.TestCase):
    def test_sentence_cuts(self):
        done, rest = converse.split_complete("Hi Boss. Mr. Stark called at 3.5 pm. How are")
        self.assertEqual(done, ["Hi Boss.", "Mr. Stark called at 3.5 pm."])
        self.assertEqual(rest.strip(), "How are")

    def test_turn_skips_code_and_markdown(self):
        said = []
        t = converse.StreamTurn(said.append)
        for piece in ["Here's the **fix**. ", "```python\nprint(1)\n", "```\n", "Run it and tell me. ", "Done"]:
            t.feed(piece)
        t.finish()
        self.assertEqual(said[0], "Here's the fix .")
        self.assertNotIn("print(1)", " ".join(said))
        self.assertIn("Run it and tell me.", said)

    def test_provider_streams_into_the_voice(self):
        from tests.test_v3 import FakeOllama, Resp, hw
        from genesis.v3.ollama import OllamaClient
        from genesis.v3.provider import GenesisLocalProvider
        from genesis.v3.store import LocalStore

        class Streaming(FakeOllama):
            def post(self, url, json=None, timeout=None, stream=False):
                if stream and url.endswith("/api/chat"):
                    self.calls.append(("chat", json))
                    return Resp(lines=[{"message": {"content": c}} for c in ("Hey Boss! ", "I'm here. ", "What's ", "up?")]
                                + [{"done": True}])
                return super().post(url, json=json, timeout=timeout, stream=stream)

        fake = Streaming()
        with mock.patch.dict(os.environ, ENV):
            p = GenesisLocalProvider(OllamaClient(session=fake), LocalStore(":memory:"), hardware=lambda: hw())
        spoken = []
        converse.begin(spoken.append)
        try:
            out = p.chat([{"role": "user", "content": "hey genesis what's happening"}])
        finally:
            converse.end()
        self.assertEqual(out, "Hey Boss! I'm here. What's up?")
        self.assertEqual(spoken, ["Hey Boss!", "I'm here.", "What's up?"])
        self.assertTrue(fake.calls[-1][1]["stream"])
        # JSON / internal steps are never streamed into the voice
        converse.begin(spoken.append)
        try:
            p.chat([{"role": "user", "content": "open notepad"}], json_mode=True)
        finally:
            converse.end()
        self.assertEqual(len(spoken), 3)


class VoiceHookTests(unittest.TestCase):
    def setUp(self):
        hooks._VOICE.clear()

        class TTS:
            def __init__(self):
                self.said, self.speaking = [], False

            def speak(self, text, lang=None):
                self.said.append(text)

        class VM:
            def __init__(self, handler):
                self.handler = handler
                self.settings = {"voice.tts_enabled": True}
                self.tts = TTS()
                self.next_text = ""
                self.spoke_calls = []

            def speak(self, text):
                self.tts.speak(text)

            def _record_and_transcribe(self, carry=None):
                return self.next_text

            def _speak_interruptible(self, speech, lang=None):
                self.tts.speak(speech)
                self.spoke_calls.append(speech)
                return []

        self.g = {"__name__": "fakevm", "VoiceManager": VM}
        hooks.patch_voice_manager(self.g)

    def tearDown(self):
        hooks._VOICE.clear()

    def test_echo_dropped_and_override_kept(self):
        vm = self.g["VoiceManager"](lambda t: ("ok", False, "en", "ok"))
        vm.speak("The meeting with the design team is at four thirty tomorrow afternoon.")
        vm.next_text = "the meeting with the design team is at four thirty"
        self.assertIsNone(vm._record_and_transcribe())
        vm.next_text = "stop, cancel the meeting"
        self.assertEqual(vm._record_and_transcribe(), "stop, cancel the meeting")
        vm.next_text = "open notepad"
        self.assertEqual(vm._record_and_transcribe(), "open notepad")

    def test_streamed_answer_not_spoken_twice(self):
        def handler(text):
            turn = converse.current()
            self.assertIsNotNone(turn)  # the voice turn is open for streaming
            turn.feed("Hey Boss! What's on your mind? ")
            turn.finish()
            return ("Hey Boss! What's on your mind?", False, "en", "")

        vm = self.g["VoiceManager"](handler)
        out = vm.handler("hey genesis what's happening")
        self.assertEqual(vm.tts.said, ["Hey Boss!", "What's on your mind?"])
        vm._speak_interruptible(out[0])
        self.assertEqual(vm.tts.said, ["Hey Boss!", "What's on your mind?"])  # no repeat
        self.assertIsNone(converse.current())
        # a normal (non-streamed) reply is still spoken by the manager
        vm2 = self.g["VoiceManager"](lambda t: ("Done, Boss.", False, "en", ""))
        out = vm2.handler("open notepad")
        vm2._speak_interruptible(out[0])
        self.assertIn("Done, Boss.", vm2.tts.said)

    def test_streaming_can_be_switched_off(self):
        with mock.patch.dict(os.environ, {"GENESIS_STREAM_SPEECH": "false"}):
            vm = self.g["VoiceManager"](lambda t: (str(converse.current()), False, "en", ""))
            self.assertEqual(vm.handler("hello")[0], "None")


class FridayVoiceTests(unittest.TestCase):
    def test_friday_blend_and_acks(self):
        import numpy as np

        from genesis.v3 import friday, kokoro

        class K:
            def get_voice_style(self, name):
                return np.full(4, {"bf_isabella": 1.0, "bf_emma": 2.0, "af_heart": 4.0}[name])

        v = kokoro.blend_style(K(), kokoro.FRIDAY_BLEND)
        self.assertAlmostEqual(float(v[0]), 0.55 + 0.5 + 0.8)
        self.assertIsNone(kokoro.blend_style(object(), kokoro.FRIDAY_BLEND))
        picks = [friday.pick(friday.ACKS) for _ in range(30)]
        self.assertTrue(all(a != b for a, b in zip(picks, picks[1:])))  # never the same line twice running


class DebugLoopTests(unittest.TestCase):
    BUG = "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n"
    TEST = ("import unittest\nfrom calc import add, mul\n\n\nclass T(unittest.TestCase):\n"
            "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n"
            "    def test_mul(self):\n        self.assertEqual(mul(2, 3), 6)\n")

    def make(self, fix_find="return a - b", fix_replace="return a + b"):
        from tests.helpers import GenesisTestCase, ScriptedLLM

        calls = []

        def reply(messages, json_mode):
            calls.append(messages[-1]["content"])
            return json.dumps({"summary": "add() subtracted instead of adding",
                               "edits": [{"find": fix_find, "replace": fix_replace}]})

        class Case(GenesisTestCase):
            llm = ScriptedLLM(reply)

            def runTest(self):  # pragma: no cover
                pass

        self.case = Case()
        self.case.setUp()
        self.calls = calls
        self.root = self.case.home / "Projects" / "calcproj"
        (self.root / "tests").mkdir(parents=True)
        (self.root / "calc.py").write_text(self.BUG, encoding="utf-8")
        (self.root / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (self.root / "tests" / "test_calc.py").write_text(self.TEST, encoding="utf-8")
        self.case.svc.repos.projects.set("calcproj", str(self.root))
        return self.case.svc

    def tearDown(self):
        self.case.tearDown()

    def test_fixes_bug_verifies_and_can_undo(self):
        from genesis.v3.debugger import DebugProject, UndoLastFixes

        svc = self.make()
        with mock.patch("genesis.tools.code.detect_test_command",
                        return_value=[sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."]):
            r = DebugProject().run({"project": "calcproj"}, svc)
        self.assertTrue(r.success, r.message)
        self.assertTrue(r.data["passed"])
        self.assertEqual((r.data["failed_before"], r.data["failed_after"]), (1, 0))
        self.assertIn("return a + b", (self.root / "calc.py").read_text())
        self.assertIn("calc.py", self.calls[0])  # it read the code under test, not the test file
        self.assertIn("now pass", r.message)
        log = (Path(svc.config.paths.data_dir) / "code_changes.jsonl").read_text()
        self.assertIn('"kept": true', log)
        u = UndoLastFixes().run({}, svc)
        self.assertTrue(u.success)
        self.assertEqual((self.root / "calc.py").read_text(), self.BUG)

    def test_unhelpful_fix_is_undone_and_reported_honestly(self):
        from genesis.v3.debugger import DebugProject

        svc = self.make(fix_find="return a - b", fix_replace="return a - b + 0")
        with mock.patch("genesis.tools.code.detect_test_command",
                        return_value=[sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."]):
            r = DebugProject().run({"project": "calcproj", "max_attempts": 2}, svc)
        self.assertFalse(r.success)
        self.assertFalse(r.data["passed"])
        self.assertEqual((self.root / "calc.py").read_text(), self.BUG)  # left unchanged
        self.assertTrue(all("undone" in a["result"] for a in r.data["attempts"] if a.get("file")))
        self.assertIn("couldn't fix", r.message)

    def test_unsafe_fix_rejected(self):
        from genesis.v3.debugger import DebugProject

        svc = self.make(fix_replace="import os; os.system('del /q *'); return a + b")
        with mock.patch("genesis.tools.code.detect_test_command",
                        return_value=[sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."]):
            r = DebugProject().run({"project": "calcproj"}, svc)
        self.assertFalse(r.data["passed"])
        self.assertTrue(any("unsafe" in a["result"] or "syntax" in a["result"] for a in r.data["attempts"]), r.data)
        self.assertEqual((self.root / "calc.py").read_text(), self.BUG)

    def test_already_passing_and_needs_confirmation(self):
        from genesis.v3.debugger import DebugProject

        svc = self.make()
        (self.root / "calc.py").write_text(self.BUG.replace("a - b", "a + b"), encoding="utf-8")
        with mock.patch("genesis.tools.code.detect_test_command",
                        return_value=[sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."]):
            r = DebugProject().run({"project": "calcproj"}, svc)
        self.assertIn("already pass", r.message)
        self.assertTrue(DebugProject.always_confirm)

    def test_voice_command_asks_first(self):
        self.make()
        r = self.case.say("fix the failing tests in calcproj")
        self.assertEqual(r.status, "awaiting_confirmation")
        self.assertTrue(re.search(r"tests and try up to 3", r.text), r.text)


if __name__ == "__main__":
    unittest.main()
