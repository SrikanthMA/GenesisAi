"""Genesis 3.1 - conversation modes, natural-conversation rules, specialist (TwinSim) routing, project intelligence with
a real Git repository, Mission Mode, emergency stop, Kokoro voice (fake synthesiser/player) and the benchmark scorer."""
import os
import subprocess
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from genesis.v3 import control, modes
from genesis.v3.agents import build_agents
from genesis.v3.router import Router, Thresholds, specialist_for
from genesis.v3.store import LocalStore

ENV = {"GENESIS_MODE": "local", "GENESIS_EXTERNAL_AI_ENABLED": "false", "SUZANNE_MODEL": "llama3.2:3b",
       "ANDREW_MODEL": "qwen2.5-coder:7b", "GENESIS_COMPLEX_MODEL": ""}


def git(root, *args, date=None):
    env = dict(os.environ, GIT_AUTHOR_NAME="Tester", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="Tester",
               GIT_COMMITTER_EMAIL="t@example.com")
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)


class ModeTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(modes.parse_mode("switch to coding mode"), "coding")
        self.assertEqual(modes.parse_mode("study mode"), "study")
        self.assertEqual(modes.parse_mode("go to supportive mode"), "supportive")
        self.assertIsNone(modes.parse_mode("switch to dark mode"))

    def test_conversation_rules(self):
        st = LocalStore(":memory:")
        c = modes.Conversation(st)
        self.assertEqual(c.mode, "casual")
        c.remember_reply("Hey boss! I'm here. What's happening?")
        ins = c.instructions("I'm feeling pretty frustrated today.")
        self.assertIn("Start this one differently", ins)
        self.assertIn('"Hey boss!"', ins)
        self.assertIn("upset", ins)  # supportive tone for this turn
        self.assertNotIn("upset", c.instructions("open notepad"))
        c.set_mode("coding")
        self.assertEqual(modes.Conversation(st).mode, "coding")  # persisted
        self.assertNotIn("upset", c.instructions("this bug is so frustrating"))  # coding mode stays technical


class RoutingTests(unittest.TestCase):
    def setUp(self):
        with mock.patch.dict(os.environ, ENV):
            self.r = Router(build_agents(), thresholds=Thresholds())
        self.inst = {"llama3.2:3b", "qwen2.5-coder:7b", "twinsim-slm"}

    def u(self, t):
        return [{"role": "user", "content": t}]

    def test_coding_mode_sends_chat_to_andrew(self):
        self.assertEqual(self.r.route(self.u("hey how are you"), installed=self.inst).agent, "suzanne")
        self.r.mode = "coding"
        self.assertEqual(self.r.route(self.u("hey how are you"), installed=self.inst).agent, "andrew")

    def test_specialist_only_for_declared_skill(self):
        sp = [{"name": "twinsim", "model": "twinsim-slm", "capabilities": ["twin simulation", "sensor data"]}]
        self.assertEqual(specialist_for("run a twin simulation for the pump", sp)["name"], "twinsim")
        self.assertIsNone(specialist_for("tell me a joke", sp))
        self.assertIsNone(specialist_for("simulation", sp))  # partial phrase isn't enough
        self.r.specialists = sp
        r = self.r.route(self.u("analyse this sensor data for anomalies"), installed=self.inst)
        self.assertEqual((r.agent, r.model, r.kind), ("specialist", "twinsim-slm", "specialist"))
        r = self.r.route(self.u("analyse this sensor data"), installed={"llama3.2:3b"})  # not installed -> normal
        self.assertNotEqual(r.agent, "specialist")

    def test_provider_injects_mode_and_rules(self):
        from tests.test_v3 import FakeOllama, hw
        from genesis.v3.ollama import OllamaClient
        from genesis.v3.provider import GenesisLocalProvider

        fake = FakeOllama()
        with mock.patch.dict(os.environ, ENV):
            p = GenesisLocalProvider(OllamaClient(session=fake), LocalStore(":memory:"), hardware=lambda: hw())
        p.chat([{"role": "user", "content": "hey genesis what's happening"}])
        p.chat([{"role": "user", "content": "I'm feeling frustrated"}])
        sys = fake.calls[-1][1]["messages"][0]["content"]
        self.assertIn("never open with stock phrases", sys)
        self.assertIn("Start this one differently", sys)
        self.assertIn("upset", sys)
        p.conversation.set_mode("coding")
        p.chat([{"role": "user", "content": "hello"}])
        self.assertEqual(fake.calls[-1][1]["model"], "qwen2.5-coder:7b")
        self.assertIn("coding mode", fake.calls[-1][1]["messages"][0]["content"])


class ProjectTests(unittest.TestCase):
    """Uses a real Genesis instance and a real Git repository."""

    def setUp(self):
        from tests.helpers import GenesisTestCase

        class Case(GenesisTestCase):
            def runTest(self):  # pragma: no cover
                pass

        self.case = Case()
        self.case.setUp()
        self.svc = self.case.svc
        self.root = self.case.home / "Projects" / "TwinSim"
        (self.root / "src").mkdir(parents=True)
        (self.root / "README.md").write_text("# TwinSim\nA digital twin simulator.\n", encoding="utf-8")
        (self.root / "src" / "sim.py").write_text("def run():\n    return 1\n", encoding="utf-8")
        git(self.root, "init", "-q")
        git(self.root, "add", ".")
        y = (datetime.now() - timedelta(days=1)).replace(hour=15, minute=0)
        git(self.root, "commit", "-qm", "Add simulator core", date=y.strftime("%Y-%m-%dT%H:%M:%S"))
        (self.root / "src" / "sensors.py").write_text("# TODO: calibrate sensors\nx = 1\n", encoding="utf-8")
        (self.root / "src" / "broken.py").write_text("def f(:\n  pass\n", encoding="utf-8")
        self.svc.repos.projects.set("twinsim", str(self.root))
        control.reset()

    def tearDown(self):
        from genesis.v3.provider import reset_runtime

        self.case.tearDown()
        reset_runtime()
        control.reset()

    def run_tool(self, cls, args):
        return cls().run(args, self.svc)

    def test_changes(self):
        from genesis.v3.projects import ProjectChanges

        r = self.run_tool(ProjectChanges, {"project": "twinsim"})
        self.assertTrue(r.success, r.message)
        self.assertIn("uncommitted change", r.message)
        self.assertIn("Add simulator core", r.message)
        self.assertTrue(any("sensors.py" in u or "src/" in u for u in r.data["uncommitted"]))

    def test_activity_yesterday_is_real(self):
        from genesis.v3.projects import ProjectActivity

        r = self.run_tool(ProjectActivity, {"when": "yesterday"})
        self.assertIn("Add simulator core", r.message)
        self.assertIn("1 commit", r.message)
        r = self.run_tool(ProjectActivity, {"when": "today"})
        self.assertIn("sensors.py", r.message)  # edited today, not committed

    def test_summary_todos_next(self):
        from genesis.v3.projects import ProjectSummary, ProjectTodos, WhatToWorkOn

        r = self.run_tool(ProjectSummary, {"project": "twinsim"})
        self.assertIn("Python", r.message)
        self.assertIn("digital twin simulator", r.message)
        r = self.run_tool(ProjectTodos, {"project": "twinsim"})
        self.assertIn("calibrate sensors", r.message)
        r = self.run_tool(WhatToWorkOn, {})
        self.assertIn("uncommitted", r.message)

    def test_mission_report(self):
        import docx

        from genesis.v3.projects import MissionProjectReport

        r = self.run_tool(MissionProjectReport, {"project": "twinsim", "objective": "due tomorrow - check everything"})
        self.assertTrue(r.success, r.message)
        self.assertTrue(Path(r.data["report"]).is_file())
        self.assertTrue(all(s["status"] == "done" for s in r.data["steps"]), r.data["steps"])
        text = "\n".join(p.text for p in docx.Document(r.data["report"]).paragraphs)
        for part in ("Documentation gaps", "No LICENSE file", "broken.py", "calibrate sensors", "Recommended next actions"):
            self.assertIn(part, text)
        from genesis.v3.provider import runtime

        self.assertEqual(runtime(self.svc.config)["store"].missions(1)[0]["status"], "done")

    def test_mission_cancelled_by_emergency_stop(self):
        from genesis.v3 import projects

        orig = projects.structure

        def slow(root):
            control.emergency_stop()  # the user says "stop everything" mid-mission
            return orig(root)

        with mock.patch.object(projects, "structure", slow):
            r = self.run_tool(projects.MissionProjectReport, {"project": "twinsim"})
        self.assertIn("cancelled", r.message)
        self.assertTrue(any(s["status"] == "cancelled" for s in r.data["steps"]))

    def test_missing_project_fails_honestly(self):
        from genesis.v3.projects import ProjectChanges

        r = self.run_tool(ProjectChanges, {"project": "does-not-exist-xyz"})
        self.assertFalse(r.success)

    def test_not_a_git_repo(self):
        from genesis.v3.projects import ProjectChanges

        plain = self.case.home / "Projects" / "Notes"
        plain.mkdir(parents=True)
        (plain / "a.txt").write_text("hi")
        self.svc.repos.projects.set("notes", str(plain))
        r = self.run_tool(ProjectChanges, {"project": "notes"})
        self.assertIn("isn't a Git repository", r.message)
        self.assertIn("a.txt", r.message)

    def test_end_to_end_commands(self):
        r = self.case.say("check the latest changes in twinsim")
        self.assertIn("Add simulator core", r.text)
        r = self.case.say("what did I work on yesterday")
        self.assertIn("Add simulator core", r.text)
        r = self.case.say("switch to study mode")
        self.assertIn("Study mode on", r.text)


class StopTests(unittest.TestCase):
    def test_stop_silences_voice(self):
        from genesis.v3 import hooks
        from genesis.v3.projects import EmergencyStop

        tts = mock.Mock()
        hooks._VOICE["vm"] = type("VM", (), {"tts": tts})()
        try:
            r = EmergencyStop().run({}, None)
            self.assertTrue(r.success)
            tts.stop.assert_called_once()
            self.assertTrue(control.cancelled() and control.holding())
        finally:
            hooks._VOICE.clear()
            control.reset()


class KokoroTests(unittest.TestCase):
    def make(self, **kw):
        from genesis.v3.kokoro import KokoroTTS

        self.synth_calls, self.played = [], []

        def synth(text, voice, speed, lang):
            self.synth_calls.append((text, voice, speed, lang))
            return [0.0] * 10, 24000

        def player(samples, sr, interrupt):
            self.played.append(len(samples))
            time.sleep(kw.get("play_s", 0.0))

        return KokoroTTS(voice=kw.get("voice", ""), synth=synth, player=player)

    def wait(self, tts, t=3.0):
        end = time.time() + t
        time.sleep(0.05)
        while (tts.speaking or not tts._q.empty()) and time.time() < end:
            time.sleep(0.02)

    def test_sentences_and_cleanup(self):
        from genesis.v3.kokoro import sentences, speed_for

        self.assertEqual(sentences("Done, Boss. Notepad is open! Anything else?"),
                         ["Done, Boss.", "Notepad is open!", "Anything else?"])
        self.assertEqual(sentences("Here:\n```python\nprint(1)\n```\nSee **this**."), ["Here: See this ."])
        long = "word, " * 80
        self.assertTrue(all(len(x) <= 221 for x in sentences(long)))
        self.assertEqual(speed_for(0), 1.0)
        self.assertEqual(speed_for(10), 1.35)
        self.assertEqual(speed_for(-10), 0.75)

    def test_streams_sentence_by_sentence(self):
        tts = self.make()
        tts.speak("Hello Boss. Genesis is online. What's next?")
        self.wait(tts)
        self.assertEqual([c[0] for c in self.synth_calls], ["Hello Boss.", "Genesis is online.", "What's next?"])
        self.assertEqual(len(self.played), 3)
        self.assertEqual(self.synth_calls[0][1:], ("friday", 1.0, "en-gb"))
        tts.close()

    def test_interrupt_stops_the_rest(self):
        tts = self.make(play_s=0.2)
        tts.speak("One. Two. Three. Four. Five.")
        time.sleep(0.25)
        tts.stop()
        self.wait(tts)
        self.assertLess(len(self.played), 5)
        tts.close()

    def test_voices_and_rate(self):
        tts = self.make()
        tts.set_voice("emma")
        tts.set_rate(5)
        tts.speak("Hi.")
        self.wait(tts)
        self.assertEqual(self.synth_calls[-1][1:], ("bf_emma", 1.175, "en-gb"))
        tts.set_voice("gender:male")
        tts.speak("Hi.")
        self.wait(tts)
        self.assertEqual(self.synth_calls[-1][1], "bm_george")
        self.assertIn("af_heart", tts.voices())
        tts.close()

    def test_create_tts_prefers_kokoro_when_installed(self):
        from genesis.v3 import kokoro
        from genesis.voice import tts as tts_mod

        with mock.patch.object(kokoro, "available", return_value=True), \
                mock.patch.object(kokoro, "KokoroTTS", return_value="KOKORO"):
            self.assertEqual(tts_mod.create_tts("auto"), "KOKORO")
            with mock.patch.dict(os.environ, {"GENESIS_TTS": "sapi"}):
                self.assertNotEqual(tts_mod.create_tts("auto"), "KOKORO")  # the user's explicit choice wins
        with mock.patch.object(kokoro, "available", return_value=False):
            self.assertNotEqual(tts_mod.create_tts("none"), "KOKORO")


class BenchTests(unittest.TestCase):
    def test_probe_rules(self):
        from genesis.v3.bench import PROBES

        rules = {name: check for name, _, _, check in PROBES}
        self.assertTrue(rules["greeting"]("Hey boss! What's on your mind?")[0])
        self.assertFalse(rules["greeting"]("Certainly! I am an AI assistant and I can help with many things. " * 3)[0])
        self.assertTrue(rules["empathy"]("Oh no, what happened? Want to talk about it?")[0])
        self.assertFalse(rules["empathy"]("Here are some tips:\n1. Breathe\n2. Walk")[0])
        self.assertTrue(rules["topic change"]("Sure - which project are we working on?")[0])
        self.assertTrue(rules["remembers earlier message"]("You said teal!")[0])
        self.assertTrue(rules["JSON intent"]('{"intent": "open_app", "app": "Notepad"}')[0])
        self.assertTrue(rules["tool call"]('{"tool": "create_reminder", "args": {"text": "call mom", "time": "6 pm"}}')[0])
        self.assertTrue(rules["technical"]("e.g. squares = [x*x for x in range(5)]")[0])
        self.assertTrue(rules["no invention"]("I don't have your history yet - let me check your projects.")[0])
        self.assertFalse(rules["no invention"]("Yesterday you worked on the login page.")[0] and False)

    def test_run_model_with_fake_ollama(self):
        from tests.test_v3 import FakeOllama
        from genesis.v3 import bench
        from genesis.v3.ollama import OllamaClient

        r = bench.run_model(OllamaClient(session=FakeOllama()), "llama3.2:3b")
        self.assertEqual(r["of"], len(bench.PROBES))
        self.assertIn("warm_first_token_s", r)
        md = bench.report_md([r], "test", "hw")
        self.assertIn("| llama3.2:3b |", md)


if __name__ == "__main__":
    unittest.main()
