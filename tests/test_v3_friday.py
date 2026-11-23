"""Genesis 3.0.2 - Friday mode: greeting, "On it, Boss", "Done, Boss", typed replies spoken, never-silent fallback."""
import os
import random
import unittest
from datetime import datetime
from unittest import mock

from genesis.v3 import friday, hooks, jarvis_style

LINES = set(jarvis_style.ACKNOWLEDGE + jarvis_style.WORKING + [l for _, ls in jarvis_style.BY_TASK for l in ls])


class Settings(dict):
    def get(self, k, d=None):
        return super().get(k, d)

    def set(self, k, v):
        self[k] = v


class NullTTS:
    name = "none"


class FakeVM:
    def __init__(self, handler, tts=None):
        self.handler = handler
        self.settings = Settings({"voice.greeting": "I'm listening."})
        self.tts = tts or NullTTS()
        self.said = []

    def speak(self, text):
        self.said.append(text)

    def start(self):
        return "started"


class FridayTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"GENESIS_FRIDAY": "true", "GENESIS_SPEAK_TYPED": "true"})
        self.env.start()
        hooks._VOICE.clear()

    def tearDown(self):
        self.env.stop()
        hooks._VOICE.clear()

    def test_acks(self):
        r = random.Random(1)
        self.assertIn(friday.ack_for("open notepad", r), LINES)
        self.assertIn(friday.ack_for("hey genesis, write an IEEE paper on solar energy", r),
                      ["Drafting it now.", "Under way, Boss. Give me a minute."])
        self.assertIn(friday.ack_for("can you search for flights to goa", r), ["Searching.", "Consulting the record."])
        self.assertIsNone(friday.ack_for("what's the capital of France?", r))
        self.assertIsNone(friday.ack_for("how are you", r))
        with mock.patch.dict(os.environ, {"GENESIS_FRIDAY": "false"}):
            self.assertIsNone(friday.ack_for("open notepad"))

    def test_reports(self):
        self.assertIn(friday.report("Notepad is open.", acted=True), ("Notepad is open.", "Notepad is open, Boss."))
        self.assertEqual(friday.report("I couldn't find that file.", acted=True), "I'm afraid I couldn't find that file.")
        self.assertEqual(friday.report("Paris is the capital.", acted=False), "Paris is the capital.")
        self.assertIn(friday.report("Done, Boss — Notepad is open.", acted=True), ("Notepad is open.", "Notepad is open, Boss."))
        self.assertTrue(friday.report("Shall I send it?", acted=True, status="awaiting_confirmation").startswith("Boss, "))
        long = "First sentence here. " * 40
        self.assertTrue(friday.shorten(long).endswith("The rest is on screen, Boss."))
        self.assertIn("code on screen", friday.shorten("Here:\n```python\nprint(1)\n```"))
        self.assertNotIn("C:\\", friday.shorten(r"Saved to C:\Users\x\a.docx"))

    def test_greeting(self):
        self.assertEqual(friday.greeting(datetime(2026, 1, 1, 20)), "Good evening, Boss. Genesis is online.")
        self.assertTrue(friday.greeting(datetime(2026, 1, 1, 8)).startswith("Good morning"))

    def test_voice_manager_patch(self):
        g = {"__name__": "fake_voice", "VoiceManager": type("VoiceManager", (FakeVM,), {})}
        hooks.patch_voice_manager(g)
        vm = g["VoiceManager"](lambda text: ("Opened Notepad.", False, "en", "Opened Notepad."))
        self.assertIs(hooks._VOICE["vm"], vm)
        self.assertEqual(vm.settings["voice.greeting"], "Yes, Boss?")
        self.assertEqual(vm.settings["voice.gender"], "female")
        out = vm.handler("open notepad")
        self.assertIn(vm.said[0], LINES)  # an acknowledgement is said first
        self.assertIn(out[0], ("Opened Notepad.", "Opened Notepad, Boss."))
        vm.start()
        self.assertEqual(vm.said[-1], friday.greeting())
        q = g["VoiceManager"](lambda text: ("Paris.", False, "en", "Paris."))
        self.assertEqual(q.handler("what's the capital of france")[0], "Paris.")
        self.assertEqual(q.said, [])  # questions: no "on it"

    def test_fallback_voice_used_when_engine_missing(self):
        g = {"__name__": "fake_voice2", "VoiceManager": type("VoiceManager", (FakeVM,), {})}
        hooks.patch_voice_manager(g)
        with mock.patch.object(hooks.os, "name", "nt"), mock.patch.object(friday, "fallback_speaker",
                                                                         return_value="FALLBACK"):
            vm = g["VoiceManager"](lambda t: ("x",))
        self.assertEqual(vm.tts, "FALLBACK")

    def test_fallback_speaker_queue(self):
        spoken = []
        sp = friday.FallbackSpeaker(runner=spoken.append)
        sp.speak("On it, Boss.")
        sp.speak("  ")
        import time
        for _ in range(100):
            if spoken:
                break
            time.sleep(0.01)
        self.assertEqual(spoken, ["On it, Boss."])
        sp.close()

    def test_typed_replies_spoken_through_genesis(self):
        from tests.helpers import GenesisTestCase

        class Case(GenesisTestCase):
            def runTest(self):  # pragma: no cover
                pass

        c = Case()
        c.setUp()
        try:
            c.say("open notepad")  # no voice manager registered (e.g. tests): nothing is spoken, nothing breaks
            vm = FakeVM(lambda t: None)
            hooks._VOICE["vm"] = vm
            r = c.say("open notepad")
            self.assertIn(vm.said[0], LINES)
            self.assertTrue(vm.said[1] and not vm.said[1].startswith(("Done", "Sorry")), vm.said)
            self.assertTrue(r.text)
            vm.said.clear()
            with mock.patch.dict(os.environ, {"GENESIS_SPEAK_TYPED": "false"}):
                c.say("open notepad")
            self.assertEqual(vm.said, [])
        finally:
            c.tearDown()


class OllamaAutoStartTests(unittest.TestCase):
    def test_starts_ollama_and_answers(self):
        from tests.test_v3 import ENV_LOCAL, FakeOllama, hw
        from genesis.v3.ollama import OllamaClient
        from genesis.v3.provider import GenesisLocalProvider
        from genesis.v3.store import LocalStore

        fake = FakeOllama(running=False)
        client = OllamaClient(session=fake, auto_start=True)
        launched = []

        def launcher():
            launched.append(1)
            fake.running = True
            return True

        client.ensure_running = lambda: OllamaClient.ensure_running(client, wait_s=2, launcher=launcher)
        with mock.patch.dict(os.environ, ENV_LOCAL):
            p = GenesisLocalProvider(client, LocalStore(":memory:"), hardware=lambda: hw())
            self.assertEqual(p.chat([{"role": "user", "content": "hello"}]), "[llama3.2:3b] ok")
        self.assertEqual(launched, [1])

    def test_no_auto_start_by_default(self):
        from tests.test_v3 import FakeOllama
        from genesis.v3.ollama import OllamaClient

        self.assertFalse(OllamaClient(session=FakeOllama(running=False)).ensure_running(wait_s=0.1))


if __name__ == "__main__":
    unittest.main()
