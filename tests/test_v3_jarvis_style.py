"""Genesis 3.3 - JARVIS-style communication (ported from adewaskar/jarvis, MIT): speaking rules, task fillers,
reporting, wake-word replies, and utterance assembly (a mid-sentence pause doesn't end the turn)."""
import os
import random
import unittest
from dataclasses import dataclass
from unittest import mock

from genesis.v3 import hooks, jarvis_style as js


class StyleTests(unittest.TestCase):
    def test_hold_for(self):
        self.assertEqual(js.hold_for("what time is it?"), 0)
        self.assertEqual(js.hold_for("open notepad"), 0)
        self.assertEqual(js.hold_for("stop"), 0)
        self.assertEqual(js.hold_for("what's the weather in"), js.CONTINUE_MS)
        self.assertEqual(js.hold_for("What's the weather in."), js.CONTINUE_MS)  # Whisper's stray full stop
        self.assertEqual(js.hold_for("send an email to"), js.CONTINUE_MS)
        self.assertEqual(js.hold_for("remind me about the meeting,"), js.CONTINUE_MS)
        self.assertEqual(js.hold_for("hmm"), js.CONTINUE_MS)
        self.assertEqual(js.hold_for("turn the volume down a bit please"), 0)

    def test_report(self):
        r = random.Random(1)
        out = js.report("Done, Boss — the report is ready!", r)
        self.assertIn(out, ("The report is ready.", "The report is ready, Boss."))
        self.assertEqual(js.report("Sorry, I couldn't find that file.", r), "I'm afraid I couldn't find that file.")
        self.assertEqual(js.report("Unable to reach Gmail.", r), "I'm afraid I'm unable to reach Gmail.")
        self.assertIn(js.report("Here's what I found: Paris is the capital.", random.Random(0)),
                      ("Paris is the capital.", "Paris is the capital, Boss."))  # unframed
        self.assertTrue(all(js.report("Notepad is open.", random.Random(i)).count("Boss") <= 1 for i in range(20)))

    def test_fillers(self):
        self.assertIn(js.for_task("check my email"), ["Checking your mail.", "Reading the inbox."])
        self.assertIn(js.for_task("what's on my calendar today"), ["Checking your calendar.", "Consulting the diary."])
        self.assertIn(js.for_task("play some lo-fi on youtube"), ["Queuing it up.", "Putting it on."])
        seq = [js.attention() for _ in range(20)]
        self.assertTrue(all(a != b for a, b in zip(seq, seq[1:])))
        self.assertIn("Two sentences is the ceiling", js.SPEAKING_RULES)
        self.assertIn("Boss", js.SPEAKING_RULES)

    def test_rules_reach_the_model_in_casual_mode_only(self):
        from tests.test_v3 import ENV_LOCAL, FakeOllama, hw
        from genesis.v3.ollama import OllamaClient
        from genesis.v3.provider import GenesisLocalProvider
        from genesis.v3.store import LocalStore

        fake = FakeOllama()
        with mock.patch.dict(os.environ, {**ENV_LOCAL, "GENESIS_FRIDAY": "true"}):
            p = GenesisLocalProvider(OllamaClient(session=fake), LocalStore(":memory:"), hardware=lambda: hw())
            p.chat([{"role": "user", "content": "hey what's up"}])
            self.assertIn("Two sentences is the ceiling", fake.calls[-1][1]["messages"][0]["content"])
            p.conversation.set_mode("coding")
            p.chat([{"role": "user", "content": "hey what's up"}])
            self.assertNotIn("Two sentences is the ceiling", fake.calls[-1][1]["messages"][0]["content"])
        with mock.patch.dict(os.environ, {**ENV_LOCAL, "GENESIS_FRIDAY": ""}):
            p.conversation.set_mode("casual")
            p.chat([{"role": "user", "content": "hey"}])
            self.assertNotIn("Two sentences is the ceiling", fake.calls[-1][1]["messages"][0]["content"])


@dataclass
class Cfg:
    no_speech_ms: int = 8000


class EP:
    def __init__(self, cfg):
        self.cfg = cfg


class AssemblyTests(unittest.TestCase):
    def make(self, segments):
        calls = []

        class VM:
            settings = None

            def __init__(self, handler=None):
                self.handler = handler or (lambda t: ("", False, "en", ""))
                self.tts = None
                self.session = type("S", (), {"remaining": lambda s: 30})()

            def _endpoint_config(self, remaining):
                return Cfg()

            def _record_and_transcribe_inner(self, ep, carry):
                calls.append(ep.cfg.no_speech_ms)
                return segments.pop(0) if segments else None

        g = {"__name__": "asm", "VoiceManager": VM}
        hooks.patch_voice_manager(g)
        return g["VoiceManager"](), calls

    def test_pause_mid_sentence_is_joined(self):
        vm, calls = self.make(["What's the weather in.", "London tomorrow?"])
        self.assertEqual(vm._record_and_transcribe_inner(EP(Cfg()), None), "What's the weather in London tomorrow?")
        self.assertEqual(calls[1], js.CONTINUE_MS)  # waited only briefly for the rest

    def test_complete_sentence_answers_immediately(self):
        vm, calls = self.make(["What time is it?", "unused"])
        self.assertEqual(vm._record_and_transcribe_inner(EP(Cfg()), None), "What time is it?")
        self.assertEqual(len(calls), 1)

    def test_no_continuation_keeps_what_was_said(self):
        vm, calls = self.make(["Send an email to"])
        self.assertEqual(vm._record_and_transcribe_inner(EP(Cfg()), None), "Send an email to")
        self.assertEqual(len(calls), 2)

    def test_switch_off(self):
        vm, calls = self.make(["What's the weather in.", "London?"])
        with mock.patch.dict(os.environ, {"GENESIS_ASSEMBLE_SPEECH": "false"}):
            self.assertEqual(vm._record_and_transcribe_inner(EP(Cfg()), None), "What's the weather in.")

    def test_wake_reply_rotates(self):
        class Settings(dict):
            def get(self, k, d=None):
                return dict.get(self, k, d)

            def set(self, k, v):
                self[k] = v

        class VM:
            def __init__(self):
                self.settings = Settings({"voice.greeting": "Yes, Boss?"})
                self.tts = None
                self.handler = None

        g = {"__name__": "rot", "VoiceManager": VM}
        hooks.patch_voice_manager(g)
        with mock.patch.dict(os.environ, {"GENESIS_FRIDAY": "true"}):
            vm = g["VoiceManager"]()
            seen = {vm.settings.get("voice.greeting") for _ in range(30)}
        self.assertTrue(seen <= set(js.ATTENTION) and len(seen) > 2)


if __name__ == "__main__":
    unittest.main()
