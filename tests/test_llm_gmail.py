"""LLM provider + Google OAuth/Gmail integration tests (network is faked, code paths are real)."""
import json
import time
import unittest

import requests

from genesis.ai.llm.base import LLMAuthError, LLMError, LLMUnavailable, extract_json
from genesis.ai.llm.factory import ResilientLLM, build_provider
from genesis.ai.llm.offline import OfflineProvider
from genesis.ai.llm.openai_compat import HuggingFaceProvider, OllamaProvider
from genesis.core.config import GenesisConfig
from genesis.security.secrets import MemorySecretStore
from genesis.services.gmail import GmailService, compose_url
from genesis.services.google_auth import GoogleAuth, GoogleAuthError
from tests.helpers import FakeGoogle, FakeResponse, GenesisTestCase


class FakeSession:
    def __init__(self, response=None, exc=None):
        self.response, self.exc, self.calls = response, exc, []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        if self.exc:
            raise self.exc
        return self.response


class TestProviders(unittest.TestCase):
    def ok(self, content="Hello!"):
        return FakeResponse(200, {"choices": [{"message": {"content": content}}]})

    def test_hf_request_shape(self):
        s = FakeSession(self.ok())
        p = HuggingFaceProvider("hf_testtoken", session=s)
        self.assertEqual(p.chat([{"role": "user", "content": "hi"}]), "Hello!")
        url, kw = s.calls[0]
        self.assertEqual(url, "https://router.huggingface.co/v1/chat/completions")
        self.assertEqual(kw["json"]["model"], "Qwen/Qwen2.5-72B-Instruct")
        self.assertEqual(kw["headers"]["Authorization"], "Bearer hf_testtoken")

    def test_errors(self):
        p = HuggingFaceProvider("hf_x", session=FakeSession(FakeResponse(401, {"error": "bad"})))
        with self.assertRaises(LLMAuthError):
            p.chat([{"role": "user", "content": "hi"}])
        p = HuggingFaceProvider("hf_x", session=FakeSession(FakeResponse(503, {"error": "down"})))
        with self.assertRaises(LLMUnavailable):
            p.chat([{"role": "user", "content": "hi"}])
        p = HuggingFaceProvider("hf_x", session=FakeSession(exc=requests.ConnectionError("offline")))
        with self.assertRaises(LLMUnavailable):
            p.chat([{"role": "user", "content": "hi"}])
        p = HuggingFaceProvider("hf_x", session=FakeSession(FakeResponse(200, {"weird": True})))
        with self.assertRaises(LLMError):
            p.chat([{"role": "user", "content": "hi"}])

    def test_missing_token(self):
        with self.assertRaises(LLMUnavailable):
            HuggingFaceProvider(None).chat([{"role": "user", "content": "x"}])

    def test_ollama_needs_no_key_and_is_local(self):
        p = OllamaProvider(session=FakeSession(self.ok()))
        self.assertTrue(p.is_configured())
        self.assertFalse(p.is_remote)

    def test_factory(self):
        cfg = GenesisConfig()
        cfg.llm_provider = "huggingface"
        self.assertIsInstance(build_provider(cfg), HuggingFaceProvider)
        cfg.llm_provider = "offline"
        self.assertIsInstance(build_provider(cfg), OfflineProvider)

    def test_resilient_degrades_gracefully(self):
        p = HuggingFaceProvider("hf_x", session=FakeSession(exc=requests.ConnectionError("no internet")))
        r = ResilientLLM(p)
        reply = r.chat([{"role": "user", "content": "what's the meaning of life"}])
        self.assertIn("trouble connecting to my AI model", reply)
        self.assertIn("internet connection", reply)  # says why (network), not just "unavailable"
        self.assertFalse(r.online)
        self.assertFalse(r.is_generative)

    def test_extract_json(self):
        self.assertEqual(extract_json('Sure! ```json\n{"a": 1}\n``` done'), {"a": 1})
        self.assertEqual(extract_json('prefix {"a": {"b": "}"}} suffix'), {"a": {"b": "}"}})
        self.assertIsNone(extract_json("no json here"))

    def test_offline_eliza(self):
        o = OfflineProvider(seed=0)
        reply = o.chat([{"role": "user", "content": "I feel tired"}])
        self.assertTrue("Why do you feel tired" in reply or "How long have you felt tired" in reply, reply)
        with self.assertRaises(LLMUnavailable):
            o.chat([{"role": "user", "content": "x"}], json_mode=True)


class TestGoogle(unittest.TestCase):
    def setUp(self):
        self.g = FakeGoogle()
        self.store = MemorySecretStore()
        self.auth = GoogleAuth({"client_id": "cid", "client_secret": "sec"}, self.store, session=self.g)
        self.gmail = GmailService(self.auth)

    def connect(self, expired=False):
        self.store.set("google_oauth_token", json.dumps({"access_token": "ya29.old", "refresh_token": "1//r",
                                                         "expires_at": time.time() - 10 if expired else time.time() + 3600}))

    def test_not_connected(self):
        with self.assertRaises(GoogleAuthError):
            self.gmail.create_draft(["a@b.co"], "s", "b")

    def test_refresh_and_draft(self):
        self.connect(expired=True)
        info = self.gmail.create_draft(["rao@university.edu"], "NLP assignment", "Dear Professor Rao,\n\nI will submit tomorrow.")
        self.assertEqual(info["draft_id"], "r-1")
        self.assertIn("#drafts?compose=m-1", info["url"])
        self.assertTrue(any("oauth2.googleapis.com/token" in u for _, u in self.g.calls))
        msg = self.g.parsed("r-1")
        self.assertEqual(msg["Subject"], "NLP assignment")
        self.assertIn("I will submit tomorrow", msg.get_content())
        self.assertEqual(json.loads(self.store.get("google_oauth_token"))["access_token"], "ya29.fresh-token")

    def test_revoked_refresh_token(self):
        self.connect(expired=True)
        self.g.refresh_ok = False
        with self.assertRaises(GoogleAuthError) as ctx:
            self.gmail.create_draft(["a@b.co"], "s", "b")
        self.assertIn("reconnect", str(ctx.exception).lower())

    def test_send_and_search(self):
        self.connect()
        d = self.gmail.create_draft(["a@b.co"], "s", "b")
        self.assertEqual(self.gmail.send_draft(d["draft_id"])["labels"], ["SENT"])
        msgs = self.gmail.search("is:unread", 3)
        self.assertEqual(msgs[0]["subject"], "Assignment deadline")

    def test_disconnect_revokes(self):
        self.connect()
        self.auth.disconnect()
        self.assertIsNone(self.store.get("google_oauth_token"))
        self.assertTrue(any("revoke" in u for _, u in self.g.calls))

    def test_compose_url(self):
        u = compose_url(["a@b.co"], "Hi there", "Line 1\nLine 2")
        self.assertIn("to=a%40b.co", u)
        self.assertIn("su=Hi%20there", u)


class TestEmailDemo(GenesisTestCase):
    """Main demo: 'Draft an email to my professor saying I will submit the NLP assignment tomorrow.'"""

    connect_gmail = True

    def setUp(self):
        super().setUp()
        self.say("remember that my professor is Professor Rao, rao@university.edu")

    def test_draft_created_in_gmail(self):
        r = self.say("Hey Genesis, draft an email to my professor saying I will submit the NLP assignment tomorrow.")
        self.assertEqual(r.status, "done", r.text)
        self.assertIn("Created a Gmail draft to rao@university.edu", r.text)
        msg = self.google.parsed("r-1")
        body = msg.get_content()
        self.assertTrue(body.startswith("Dear Professor Rao,"), body)
        self.assertIn("I will submit the NLP assignment tomorrow.", body)
        self.assertTrue(any("#drafts?compose=" in u for u in self.urls_opened()))
        tools = [s["tool"] for s in r.plans[0]["steps"]]
        self.assertEqual(tools, ["compose_email", "gmail_create_draft"])
        self.assertTrue(r.plans[0]["steps"][1]["verified"])

    def test_send_requires_confirmation(self):
        self.say("draft an email to my professor saying I will submit the NLP assignment tomorrow")
        r = self.say("send it")
        self.assertEqual(r.status, "awaiting_confirmation")
        self.assertEqual(self.google.sent, [])
        r = self.say("yes")
        self.assertEqual(self.google.sent, ["r-1"])
        self.assertIn("Sent the e-mail", r.text)

    def test_send_email_intent_drafts_then_confirms(self):
        r = self.say("send an email to my professor saying I will be late")
        self.assertEqual(r.status, "awaiting_confirmation")
        self.assertEqual(len(self.google.drafts), 1)
        self.assertEqual(self.google.sent, [])
        self.say("no")
        self.assertEqual(self.google.sent, [])

    def test_expired_auth_recovers_with_compose_window(self):
        self.secrets.delete("google_oauth_token")
        self.secrets.set("google_oauth_token", json.dumps({"refresh_token": "1//r", "access_token": "x", "expires_at": 0}))
        self.google.refresh_ok = False
        r = self.say("draft an email to my professor saying hello")
        self.assertIn("couldn't create the Gmail draft", r.text)
        self.assertIn("pre-filled Gmail compose window", r.text)
        self.assertIn("view=cm", self.urls_opened()[-1])
        self.assertEqual(r.status, "partial")

    def test_read_email_is_untrusted(self):
        r = self.say("check my email")
        self.assertIn("Assignment deadline", r.text)
        plan = r.plans[0]
        self.assertTrue(plan["untrusted"])


class TestNotConnectedFallback(GenesisTestCase):
    def test_compose_window_when_gmail_not_connected(self):
        self.svc.repos.contacts.upsert("John", email="john@example.com")
        r = self.say("Draft an email to John saying the meeting has been moved to 4 PM.")
        self.assertEqual(r.status, "done")
        self.assertIn("pre-filled Gmail compose window", r.text)
        self.assertIn("to=john%40example.com", self.urls_opened()[-1])


if __name__ == "__main__":
    unittest.main()
