"""NLP tests: normalisation, intent classification, entity extraction, dates, splitting."""
import unittest
from datetime import datetime

from genesis.nlp import rules
from genesis.nlp.classifier import IntentClassifier
from genesis.nlp.entities import extract_entities, find_apps, find_emails, find_urls, parse_datetime
from genesis.nlp.intents import Intent
from genesis.nlp.pipeline import NLUPipeline

NOW = datetime(2026, 9, 25, 10, 0)  # a Friday


class TestIntentRules(unittest.TestCase):
    CASES = [
        ("Hey Genesis, open Chrome.", Intent.OPEN_APPLICATION, {"app": "chrome"}),
        ("Open Gmail.", Intent.OPEN_APPLICATION, {"app": "gmail"}),
        ("Open VS Code.", Intent.OPEN_APPLICATION, {"app": "vscode"}),
        ("launch visual studio code", Intent.OPEN_APPLICATION, {"app": "vscode"}),
        ("close spotify", Intent.CLOSE_APPLICATION, {"app": "spotify"}),
        ("Open my Downloads folder.", Intent.OPEN_FOLDER, {"folder": "downloads"}),
        ("go to github.com/anthropics", Intent.OPEN_URL, {"url": "github.com/anthropics"}),
        ("Search the web for the latest NVIDIA announcement.", Intent.SEARCH_WEB, {"query": "the latest NVIDIA announcement"}),
        ("google best budget laptops", Intent.SEARCH_WEB, {"query": "best budget laptops"}),
        ("Draft an email to John saying the meeting has been moved to 4 PM.", Intent.DRAFT_EMAIL,
         {"recipient": "John", "message": "the meeting has been moved to 4 PM"}),
        ("Can you send an email to Rahul saying I'll be late?", Intent.SEND_EMAIL, {"recipient": "Rahul", "message": "I'll be late"}),
        ("Draft an email to my professor saying I will submit the NLP assignment tomorrow.", Intent.DRAFT_EMAIL,
         {"recipient": "my professor", "message": "I will submit the NLP assignment tomorrow"}),
        ("check my email", Intent.READ_EMAIL, {}),
        ("send it", Intent.SEND_EMAIL, {"use_last_draft": True}),
        ("Shut down the computer.", Intent.SHUTDOWN, {}),
        ("please restart my pc", Intent.RESTART, {}),
        ("lock my computer", Intent.SYSTEM_CONTROL, {"system_action": "lock"}),
        ("turn the volume up", Intent.SYSTEM_CONTROL, {"system_action": "volume_up"}),
        ("how much battery do I have", Intent.SYSTEM_CONTROL, {"system_action": "info"}),
        ("cancel the shutdown", Intent.SYSTEM_CONTROL, {"system_action": "cancel_shutdown"}),
        ("create a file called test.py", Intent.FILE_OPERATION, {"file_op": "create_file", "name": "test.py"}),
        ("create a folder called Reports on the desktop", Intent.FILE_OPERATION,
         {"file_op": "create_folder", "name": "Reports", "location": "desktop"}),
        ("find the latest assignment PDF", Intent.FILE_OPERATION,
         {"file_op": "search", "extension": ".pdf", "sort": "newest", "query": "assignment"}),
        ("rename it to final.pdf", Intent.FILE_OPERATION, {"file_op": "rename", "new_name": "final.pdf"}),
        ("move report.pdf to documents", Intent.FILE_OPERATION, {"file_op": "move", "source": "report.pdf", "destination": "documents"}),
        ("delete old.txt", Intent.FILE_OPERATION, {"file_op": "delete", "source": "old.txt"}),
        ("open my Genesis project", Intent.CODE_ACTION, {"code_action": "open_project", "project": "genesis"}),
        ("Open app/main.py.", Intent.CODE_ACTION, {"code_action": "open_file", "path": "app/main.py"}),
        ("Find the error in this file.", Intent.CODE_ACTION, {"code_action": "check_file"}),
        ("explain this error: NameError: name 'x' is not defined", Intent.CODE_ACTION, {"code_action": "explain"}),
        ("remind me in 10 minutes to stretch", Intent.CREATE_REMINDER, {"reminder_text": "stretch"}),
        ("what are my reminders", Intent.INFORMATION_REQUEST, {"topic": "reminders"}),
        ("what time is it", Intent.INFORMATION_REQUEST, {"topic": "datetime"}),
        ("Genesis, open chat.", Intent.UI_CONTROL, {"ui_action": "open_chat"}),
        ("stop listening", Intent.SESSION, {"session_op": "sleep"}),
        ("turn off the microphone", Intent.SETTINGS, {"setting": "listening", "value": False}),
        ("go to sleep", Intent.SESSION, {"session_op": "sleep"}),
        ("thanks, that's all", Intent.SESSION, {"session_op": "sleep"}),
        ("put the computer to sleep", Intent.SYSTEM_CONTROL, {"system_action": "sleep"}),
        ("mute your voice", Intent.SETTINGS, {"setting": "tts", "value": False}),
        ("start genesis with windows", Intent.SETTINGS, {"setting": "start_with_windows", "value": True}),
        ("what do you remember about me", Intent.MEMORY, {"memory_op": "list"}),
        ("forget my address", Intent.MEMORY, {"memory_op": "forget", "memory_text": "my address"}),
        ("add syllabus.pdf to your knowledge base", Intent.KNOWLEDGE, {"knowledge_op": "ingest", "path": "syllabus.pdf"}),
        ("according to my notes, when is the exam?", Intent.KNOWLEDGE, {"knowledge_op": "query"}),
        ("read this page", Intent.BROWSER_ACTION, {"browser_op": "read"}),
        ("never mind", Intent.CANCEL, {}),
        ("what is the latest news about spacex", Intent.RESEARCH, {"operation": "news"}),
    ]

    def test_cases(self):
        for text, intent, slots in self.CASES:
            with self.subTest(text=text):
                r = rules.parse(text, NOW)
                self.assertIsNotNone(r, text)
                self.assertEqual(r.intent, intent, text)
                for k, v in slots.items():
                    self.assertEqual(r.slots.get(k), v, f"{text}: slot {k}")

    def test_reminder_datetime(self):
        r = rules.parse("Remind me tomorrow at 9 AM to submit my assignment.", NOW)
        self.assertEqual(r.intent, Intent.CREATE_REMINDER)
        self.assertEqual(r.slots["datetime"], datetime(2026, 9, 26, 9, 0))
        self.assertEqual(r.slots["reminder_text"], "submit my assignment")

    def test_contact_memory(self):
        r = rules.parse("remember that my professor is Professor Rao, rao@university.edu", NOW)
        self.assertEqual(r.slots["memory_op"], "add_contact")
        self.assertEqual(r.slots["contact_name"], "Professor Rao")
        self.assertEqual(r.slots["contact_alias"], "professor")
        self.assertEqual(r.slots["email"], "rao@university.edu")

    def test_chitchat_not_matched_by_rules(self):
        for t in ("how are you", "tell me a joke", "thanks!"):
            self.assertIsNone(rules.parse(t, NOW), t)

    def test_power_words_in_reminder_are_not_power_actions(self):
        r = rules.parse("remind me to shut down the computer at 6 pm", NOW)
        self.assertEqual(r.intent, Intent.CREATE_REMINDER)


class TestSplitting(unittest.TestCase):
    def test_compound(self):
        self.assertEqual(rules.split_requests("Hey Genesis, open VS Code and open my Genesis project"),
                         ["open VS Code", "open my Genesis project"])

    def test_message_body_is_protected(self):
        parts = rules.split_requests("Open Gmail and draft an email to my professor saying I will open the lab and close it")
        self.assertEqual(len(parts), 2)
        self.assertTrue(parts[1].endswith("open the lab and close it"))

    def test_multi_step(self):
        parts = rules.split_requests("Find the latest assignment PDF, rename it, open Gmail, attach it, and draft an email to my professor.")
        self.assertEqual(parts, ["Find the latest assignment PDF", "rename it", "open Gmail", "attach it", "draft an email to my professor"])

    def test_reminder_body_is_protected(self):
        self.assertEqual(len(rules.split_requests("remind me to buy milk and send the report")), 1)

    def test_wake_phrase_normalisation(self):
        for t in ("Hey Genesis, open Chrome", "hey genesis open chrome", "OK Genesis: open chrome", "Genesis, open chrome"):
            self.assertEqual(rules.normalize(t).lower(), "open chrome")


class TestEntities(unittest.TestCase):
    def test_email_url(self):
        text = "send it to john.doe@example.com and open https://example.org/page"
        self.assertEqual([e.value for e in find_emails(text)], ["john.doe@example.com"])
        self.assertIn("https://example.org/page", [e.value for e in find_urls(text)])

    def test_email_domain_not_url(self):
        self.assertEqual(find_urls("mail rao@university.edu"), [])

    def test_apps_longest_match(self):
        self.assertEqual([e.value for e in find_apps("open visual studio code")], ["vscode"])

    def test_entity_types(self):
        ents = extract_entities("Email Professor Rao at rao@uni.edu tomorrow at 5 pm about report.pdf", NOW)
        types = {e.type for e in ents}
        self.assertTrue({"person", "email", "datetime", "file_name"} <= types, types)


class TestDates(unittest.TestCase):
    def check(self, text, expected):
        got = parse_datetime(text, NOW)
        self.assertIsNotNone(got, text)
        self.assertEqual(got.value, expected, text)

    def test_forms(self):
        self.check("in 10 minutes", datetime(2026, 9, 25, 10, 10))
        self.check("in an hour", datetime(2026, 9, 25, 11, 0))
        self.check("tomorrow at 9 am", datetime(2026, 9, 26, 9, 0))
        self.check("tomorrow", datetime(2026, 9, 26, 9, 0))
        self.check("at 5", datetime(2026, 9, 25, 17, 0))
        self.check("tonight", datetime(2026, 9, 25, 20, 0))
        self.check("next monday evening", datetime(2026, 9, 28, 18, 0))
        self.check("on 5 october at 14:30", datetime(2026, 10, 5, 14, 30))
        self.check("at 9 am", datetime(2026, 9, 26, 9, 0))  # 9 am already passed today -> tomorrow

    def test_none(self):
        self.assertIsNone(parse_datetime("open chrome", NOW))


class TestClassifier(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clf = IntentClassifier()

    def test_heldout_phrases(self):
        cases = [("hiya genesis, how's it going", Intent.CONVERSATION), ("could you fire up spotify for me", Intent.OPEN_APPLICATION),
                 ("any new mail from amazon", Intent.READ_EMAIL), ("jot down a reminder for my exam", Intent.CREATE_REMINDER),
                 ("what is the capital of japan", Intent.INFORMATION_REQUEST), ("quit the calculator", Intent.CLOSE_APPLICATION)]
        correct = sum(1 for t, i in cases if self.clf.predict(t)[0][0] == i)
        self.assertGreaterEqual(correct, 5, [(t, self.clf.predict(t)[0]) for t, _ in cases])

    def test_probabilities(self):
        preds = self.clf.predict("open chrome")
        self.assertAlmostEqual(sum(p for _, p in self.clf.predict("open chrome", top_k=50)), 1.0, places=3)
        self.assertEqual(preds[0][0], Intent.OPEN_APPLICATION)


class TestPipeline(unittest.TestCase):
    def test_fallback_to_conversation_without_llm(self):
        nlu = NLUPipeline(llm=None)
        r = nlu.parse("tell me something interesting")
        self.assertIn(r.intent, (Intent.CONVERSATION, Intent.INFORMATION_REQUEST))

    def test_power_intents_never_from_classifier(self):
        nlu = NLUPipeline(llm=None)
        r = nlu.parse("please power down")  # not an exact rule match
        self.assertNotIn(r.intent, (Intent.SHUTDOWN, Intent.RESTART))

    def test_llm_nlu_used_for_unclear_requests(self):
        from tests.helpers import ScriptedLLM

        llm = ScriptedLLM(lambda m, j: '{"intent": "open_application", "confidence": 0.9, "entities": {"app": "spotify"}}')
        nlu = NLUPipeline(llm=llm)
        r = nlu.parse("i fancy some tunes, get my music player going")
        self.assertEqual(r.intent, Intent.OPEN_APPLICATION)
        self.assertEqual(r.source, "llm")

    def test_llm_cannot_produce_shutdown(self):
        from tests.helpers import ScriptedLLM

        llm = ScriptedLLM(lambda m, j: '{"intent": "shutdown", "confidence": 0.99, "entities": {}}')
        r = NLUPipeline(llm=llm).parse("make everything go dark")
        self.assertNotEqual(r.intent, Intent.SHUTDOWN)


if __name__ == "__main__":
    unittest.main()


class TestNumpyClassifierFallback(unittest.TestCase):
    """When Windows blocks scikit-learn's DLLs, Genesis trains the same model with its own NumPy code."""

    def test_same_predictions_as_scikit_learn(self):
        from unittest import mock

        from genesis.nlp.classifier import IntentClassifier

        real = IntentClassifier()
        with mock.patch.dict("sys.modules", {"sklearn.feature_extraction.text": None}):
            blocked = IntentClassifier()
        self.assertEqual(real.backend, "tfidf-logreg")
        self.assertEqual(blocked.backend, "numpy-tfidf-logreg")
        for text in ["open chrome please", "what's the weather like", "remind me to call mom", "shut the laptop down",
                     "send an email to my professor", "play some music on youtube", "opne youtub"]:
            self.assertEqual(blocked.predict(text)[0][0], real.predict(text)[0][0], text)
            self.assertAlmostEqual(blocked.predict(text)[0][1], real.predict(text)[0][1], delta=0.12)
