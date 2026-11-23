"""End-to-end scenarios from the specification, through the full pipeline
(NLU → dialogue → planner → permissions → tools → verifier → response)."""
import unittest

from genesis.core.assistant import speech_text
from tests.helpers import GenesisTestCase


class TestScenarios(GenesisTestCase):
    connect_gmail = True

    def test_1_open_chrome(self):
        r = self.say("Hey Genesis, open Chrome.")
        self.assertEqual(r.status, "done")
        self.assertEqual(r.text, "Done, Boss — Google Chrome is open.")
        self.assertTrue(self.launched()[-1][0].endswith("chrome.exe"))

    def test_2_open_gmail(self):
        r = self.say("Hey Genesis, open Gmail.")
        self.assertEqual(r.text, "Done, Boss — Gmail is open in your browser.")
        self.assertEqual(self.urls_opened(), ["https://mail.google.com/mail/u/0/#inbox"])

    def test_3_draft_an_email(self):
        self.svc.repos.contacts.upsert("Professor Rao", email="rao@university.edu", alias="professor")
        r = self.say("Hey Genesis, draft an email.")
        self.assertEqual(r.question, "Who should the e-mail go to?")
        r = self.say("my professor")
        self.assertEqual(r.question, "What should I say?")
        r = self.say("I will submit the NLP assignment tomorrow")
        self.assertEqual(r.status, "done", r.text)
        self.assertEqual(len(self.google.drafts), 1)

    def test_4_search(self):
        r = self.say("Hey Genesis, search for the latest NVIDIA announcement.")
        self.assertEqual(r.status, "done")
        self.assertIn("google.com/search", self.urls_opened()[-1])

    def test_5_reminder(self):
        r = self.say("Hey Genesis, remind me tomorrow at 9 AM to submit my assignment.")
        self.assertEqual(r.status, "done")
        self.assertIn("tomorrow at 9:00 AM to submit my assignment", r.text)
        self.assertEqual(len(self.svc.repos.reminders.pending()), 1)

    def test_6_open_project(self):
        r = self.say("Hey Genesis, open VS Code and open my Genesis project.")
        self.assertEqual(r.status, "done", r.text)
        launches = self.launched()
        self.assertEqual(len(launches), 1)  # no extra empty VS Code window
        self.assertTrue(launches[0][0].endswith("Code.exe"))
        self.assertIn("Opened the genesis project in VS Code", r.text)

    def test_7_shutdown(self):
        r = self.say("Hey Genesis, shut down the computer.")
        self.assertEqual(r.status, "awaiting_confirmation")
        self.assertEqual(self.backend.names_of("shutdown"), [])
        self.say("Yes.")
        self.assertEqual(self.backend.names_of("shutdown"), [("shutdown", 10)])

    def test_8_multi_step_open_and_draft(self):
        self.svc.repos.contacts.upsert("Professor Rao", email="rao@university.edu", alias="professor")
        r = self.say("Open Gmail and draft an email to my professor saying I'll submit the assignment tomorrow.")
        self.assertEqual(r.status, "done", r.text)
        self.assertIn("Gmail is open", r.text)
        self.assertIn("Created a Gmail draft", r.text)
        self.assertEqual(len(r.plans), 2)

    def test_open_downloads(self):
        r = self.say("Open my Downloads folder.")
        self.assertEqual(r.text, "Opened your Downloads folder.")

    def test_create_file_in_project_and_open_in_vscode(self):
        self.say("open my genesis project")
        tmp_project = self.home / "Documents" / "demo"
        tmp_project.mkdir()
        (tmp_project / ".git").mkdir()
        self.say("open my demo project")
        r = self.say("create a file called test.py")
        self.assertEqual(r.status, "awaiting_confirmation")  # writing files is medium risk
        r = self.say("yes")
        self.assertTrue((tmp_project / "test.py").exists(), r.text)
        self.assertTrue(self.launched()[-1][-1].endswith("test.py"))

    def test_chitchat_offline(self):
        r = self.say("how are you?")
        self.assertEqual(r.status, "chat")
        self.assertTrue(r.text)

    def test_time(self):
        self.assertIn("It's", self.say("what time is it").text)

    def test_ui_control(self):
        r = self.say("Genesis, open chat.")
        self.assertEqual(r.ui_action, "open_chat")

    def test_settings_voice(self):
        self.say("turn off the microphone")
        self.assertFalse(self.svc.settings.get("voice.wake_word_enabled"))

    def test_wake_word_only(self):
        r = self.say("Hey Genesis")
        self.assertEqual(r.text, "I'm listening.")

    def test_conversation_persistence_respects_memory_toggle(self):
        self.say("disable memory")
        before = len(self.svc.repos.conversations.messages(self.core.conversation_id))
        self.say("what time is it")
        self.assertEqual(len(self.svc.repos.conversations.messages(self.core.conversation_id)), before)

    def test_speech_text(self):
        self.assertEqual(speech_text("Opened **Gmail** ✉️ https://mail.google.com"), "Opened Gmail.")


class TestDegradedMode(GenesisTestCase):
    """No LLM, no Google: local features still work."""

    def test_local_tools_work_offline(self):
        self.assertEqual(self.say("open my downloads folder").status, "done")
        self.assertEqual(self.say("remind me in 5 minutes to drink water").status, "done")
        r = self.say("what is the meaning of life?")
        self.assertIn("trouble connecting to my AI model", r.text)


if __name__ == "__main__":
    unittest.main()
