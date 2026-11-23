"""Dialogue management: slot filling, coreference, clarification, cancellation."""
import unittest

from genesis.nlp.dialogue import strip_message_prefix
from tests.helpers import GenesisTestCase


class TestDialogue(GenesisTestCase):
    def test_slot_filling_and_pronoun(self):
        r = self.say("Draft an email to Rahul.")
        self.assertEqual(r.status, "awaiting_input")
        self.assertEqual(r.question, "What should I say?")
        r = self.say("Tell him I'll reach at 5.")
        # Rahul has no known address yet -> Genesis asks for it
        self.assertEqual(r.status, "awaiting_input")
        self.assertIn("Rahul", r.text)
        r = self.say("rahul@example.com")
        self.assertIn(r.status, ("done", "partial"))
        opened = self.urls_opened()[-1]
        self.assertIn("rahul%40example.com", opened)
        self.assertIn("reach%20at%205", opened)
        # contact saved transparently (optional step) and resolvable next time
        self.assertEqual(self.svc.repos.contacts.find("rahul")["email"], "rahul@example.com")

    def test_him_resolves_to_last_person(self):
        self.svc.repos.contacts.upsert("Rahul", email="rahul@example.com")
        self.say("Draft an email to Rahul saying hi")
        r = self.say("Draft another email to him saying the call moved to 6")
        self.assertIn(r.status, ("done", "partial"))
        self.assertIn("rahul%40example.com", self.urls_opened()[-1])

    def test_cancel_during_clarification(self):
        self.say("Draft an email to Rahul.")
        r = self.say("never mind")
        self.assertIn("cancel", r.text.lower())
        st = self.core.dialogue.state(self.core.conversation_id)
        self.assertIsNone(st.frame)

    def test_new_command_abandons_question(self):
        self.say("remind me to call mom")  # missing time
        r = self.say("open gmail")
        self.assertIn("Gmail is open", r.text)

    def test_reminder_time_clarification(self):
        r = self.say("remind me to call mom")
        self.assertEqual(r.question, "When should I remind you?")
        r = self.say("in 2 hours")
        self.assertIn("remind you", r.text)
        self.assertEqual(len(self.svc.repos.reminders.pending()), 1)

    def test_reminder_text_answer_drops_leading_to(self):
        self.assertEqual(self.say("remind me").question, "What should I remind you about?")
        self.say("to drink water")
        r = self.say("in 10 minutes")
        self.assertIn("to drink water", r.text)
        self.assertNotIn("to to", r.text)
        self.assertEqual(self.svc.repos.reminders.pending()[0]["text"], "drink water")

    def test_bad_answer_reasks(self):
        self.say("remind me to call mom")
        r = self.say("purple")
        self.assertEqual(r.status, "awaiting_input")
        self.assertIn("didn't catch the time", r.text)

    def test_strip_message_prefix(self):
        self.assertEqual(strip_message_prefix("Tell him I'll reach at 5."), "I'll reach at 5.")
        self.assertEqual(strip_message_prefix("say that the build passed"), "the build passed")
        self.assertEqual(strip_message_prefix("let her know that I'm sick"), "I'm sick")

    def test_history_is_kept(self):
        self.say("what time is it")
        st = self.core.dialogue.state(self.core.conversation_id)
        self.assertEqual([m["role"] for m in st.history][-2:], ["user", "assistant"])
        msgs = self.svc.repos.conversations.messages(self.core.conversation_id)
        self.assertEqual(msgs[0]["content"], "what time is it")


if __name__ == "__main__":
    unittest.main()
