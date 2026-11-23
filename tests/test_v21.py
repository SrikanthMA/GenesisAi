"""Genesis 2.1: images (generation fallbacks, search fallback, misheard 'email'), smarter voice, Google Meet + minutes.

Nothing real is contacted: image services, Gemini, audio devices and the language model are fakes.
"""
from __future__ import annotations

import json
import math
import os
import struct
import time
import unittest
from pathlib import Path

from genesis.core.events import EventType
from genesis.nlp.intents import Intent
from genesis.nlp.pipeline import NLUPipeline
from genesis.v2 import gemini_native, images as v2images, meeting as mt, voice_ai
from tests.helpers import GenesisTestCase, ScriptedLLM

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def tools_of(resp):
    return [(s["tool"], s["status"]) for p in resp.plans for s in p["steps"]]


class FakeResponse:
    def __init__(self, status=200, js=None, content=b"", headers=None, text=""):
        self.status_code, self._js, self.content, self.headers, self.text = status, js, content, headers or {}, text

    def json(self):
        if self._js is None:
            raise ValueError("no json")
        return self._js

    def iter_content(self, n):
        yield self.content

    def close(self):
        pass


class FakeWeb:
    """Image search finds nothing; Gemini answers with ``gemini_status``; Pollinations returns a picture."""

    def __init__(self, gemini_status=429, pollinations_status=200, cloudflare_status=200, commons=False):
        self.gemini_status = gemini_status
        self.pollinations_status = pollinations_status
        self.cloudflare_status = cloudflare_status
        self.commons = commons
        self.gets, self.posts = [], []

    def get(self, url, params=None, **kw):
        self.gets.append(url)
        if "pollinations" in url:
            if self.pollinations_status != 200:
                return FakeResponse(self.pollinations_status, text="payment required")
            return FakeResponse(200, content=PNG, headers={"Content-Type": "image/jpeg"})
        if "commons" in url:
            if self.commons:
                return FakeResponse(200, {"query": {"pages": {"1": {"index": 1, "title": "File:Dragon.jpg", "imageinfo": [{
                    "mime": "image/jpeg", "thumburl": "https://upload.wikimedia.org/dragon.jpg", "descriptionurl": "https://c/d",
                    "extmetadata": {}}]}}}})
            return FakeResponse(200, {"query": {"pages": {}}})
        if "upload.wikimedia" in url:
            return FakeResponse(200, content=PNG, headers={"Content-Type": "image/jpeg"})
        if "openverse" in url:
            return FakeResponse(200, {"results": []})
        return FakeResponse(404)

    def post(self, url, json=None, headers=None, **kw):
        self.posts.append((url, json, headers))
        if "api.cloudflare.com" in url:
            import base64

            if self.cloudflare_status != 200:
                return FakeResponse(self.cloudflare_status, text='{"errors":[{"message":"daily free allocation of 10,000 neurons"}]}')
            return FakeResponse(200, {"result": {"image": base64.b64encode(PNG).decode()}, "success": True})
        if "generativelanguage" in url:
            if self.gemini_status == 200:
                import base64

                return FakeResponse(200, {"candidates": [{"content": {"parts": [
                    {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(PNG).decode()}}]}}]})
            return FakeResponse(self.gemini_status, text='{"error": {"message": "Quota exceeded, limit: 0"}}')
        return FakeResponse(402, text="payment required")


class TestMisheardImageRequests(unittest.TestCase):
    """Speech recognition often hears 'image' as 'email': 2.0 then opened e-mail for 'an image of a dragon'."""

    @classmethod
    def setUpClass(cls):
        cls.nlu = NLUPipeline()

    def test_email_of_a_thing_is_an_image(self):
        for text, op, key, val in [("show me an email of a flying dragon", "search", "query", "flying dragon"),
                                   ("search for an email of a flying dragon", "search", "query", "flying dragon"),
                                   ("email of a dragon", "search", "query", "dragon"),
                                   ("an email of a dragon", "search", "query", "dragon"),
                                   ("generate an email of a dragon", "generate", "prompt", "a dragon"),
                                   ("image of a dragon", "search", "query", "dragon"),
                                   ("i want an image of a cat", "search", "query", "cat"),
                                   ("picture of a cat", "search", "query", "cat")]:
            r = self.nlu.parse(text)
            self.assertEqual(r.intent, Intent.IMAGE, text)
            self.assertEqual(r.slots["image_op"], op, text)
            self.assertEqual(r.slots[key], val, text)

    def test_real_email_requests_unchanged(self):
        for text, intent in [("show me the email of my professor", Intent.READ_EMAIL), ("read my emails", Intent.READ_EMAIL),
                             ("send an email to rahul", Intent.SEND_EMAIL), ("draft an email to my professor", Intent.DRAFT_EMAIL),
                             ("email rahul about the meeting", Intent.SEND_EMAIL)]:
            self.assertEqual(self.nlu.parse(text).intent, intent, text)


class TestImageProviders(GenesisTestCase):
    def setUp(self):
        super().setUp()
        v2images.reset()
        self._env = {k: os.environ.get(k) for k in ("GENESIS_IMAGE_PROVIDER", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN",
                                                    "POLLINATIONS_API_KEY")}
        self.web = FakeWeb()
        self.svc.images.session = self.web
        self.shown = []
        self.svc.events.subscribe(lambda e: self.shown.append(e.payload), EventType.MEDIA_SHOW)

    def tearDown(self):
        for k, v in self._env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        v2images.reset()
        super().tearDown()

    def test_free_fallback_when_gemini_has_no_image_quota(self):
        os.environ["GENESIS_IMAGE_PROVIDER"] = "auto"
        self.config.gemini_api_key = "AQ.test-key-0000000000000000"
        r = self.say("generate an image of a flying dragon")
        self.assertEqual(tools_of(r), [("image_generate", "succeeded")], r.text)
        self.assertTrue(any("generativelanguage" in u for u, _, _ in self.web.posts))  # Gemini tried first
        self.assertTrue(any("pollinations" in u for u in self.web.gets))  # then the free service
        self.assertTrue(Path(self.shown[-1]["path"]).is_file())
        self.assertNotIn("AQ.test-key", r.text)
        # Gemini said "no quota": it is skipped next time instead of being asked again
        n = len(self.web.posts)
        self.say("generate an image of a cat")
        self.assertEqual(len(self.web.posts), n)

    def test_gemini_image_when_billing_allows(self):
        os.environ["GENESIS_IMAGE_PROVIDER"] = "auto"
        self.config.gemini_api_key = "AQ.test-key-0000000000000000"
        self.web.gemini_status = 200
        r = self.say("draw a dragon")
        self.assertEqual(tools_of(r), [("image_generate", "succeeded")], r.text)
        self.assertFalse(any("pollinations" in u for u in self.web.gets))
        url, body, headers = self.web.posts[-1]
        self.assertIn(":generateContent", url)
        self.assertEqual(headers.get("x-goog-api-key"), "AQ.test-key-0000000000000000")

    def test_free_service_is_opt_in(self):
        os.environ.pop("GENESIS_IMAGE_PROVIDER", None)
        self.config.gemini_api_key = "AQ.test-key-0000000000000000"
        r = self.say("generate an image of a flying dragon")
        self.assertFalse(any("pollinations" in u for u in self.web.gets))  # prompt never sent to a third party
        self.assertIn("Setup_Images.bat", r.text)

    def _setenv(self, **kv):
        for k, v in kv.items():
            self._env.setdefault(k, os.environ.get(k))
            os.environ[k] = v

    def test_cloudflare_free_allowance(self):
        self._setenv(GENESIS_IMAGE_PROVIDER="auto", CLOUDFLARE_ACCOUNT_ID="acc123", CLOUDFLARE_API_TOKEN="cf-token-secret")
        self.config.gemini_api_key = "AQ.test-key-0000000000000000"
        r = self.say("generate an image of a flying dragon")
        self.assertEqual(tools_of(r), [("image_generate", "succeeded")], r.text)
        self.assertIn("Cloudflare", r.text)
        url, body, headers = self.web.posts[-1]
        self.assertIn("/accounts/acc123/ai/run/@cf/black-forest-labs/flux-1-schnell", url)
        self.assertEqual(headers["Authorization"], "Bearer cf-token-secret")
        self.assertNotIn("cf-token-secret", r.text)
        self.assertFalse(any("pollinations" in u for u in self.web.gets))  # Cloudflare answered first

    def test_nothing_can_create_it_shows_a_real_picture_and_says_why(self):
        self._setenv(GENESIS_IMAGE_PROVIDER="auto", CLOUDFLARE_ACCOUNT_ID="acc123", CLOUDFLARE_API_TOKEN="t")
        self.web.cloudflare_status, self.web.pollinations_status, self.web.commons = 429, 402, True
        self.config.gemini_api_key = "AQ.test-key-0000000000000000"
        r = self.say("generate an image of a flying dragon")
        self.assertEqual(tools_of(r), [("image_generate", "succeeded")], r.text)
        for part in ("Gemini: needs billing", "Cloudflare: no free image quota left", "Pollinations: not allowed",
                     "real photo of flying dragon"):
            self.assertIn(part, r.text)
        self.assertTrue(Path(self.shown[-1]["path"]).is_file())

    def test_pollinations_key_uses_the_current_endpoint(self):
        self._setenv(POLLINATIONS_API_KEY="sk_pollen_test")
        r = self.say("generate an image of a cat")
        self.assertEqual(tools_of(r), [("image_generate", "succeeded")], r.text)
        self.assertTrue(any(u.startswith("https://gen.pollinations.ai/image/") for u in self.web.gets), self.web.gets)

    def test_search_with_no_results_opens_an_image_search(self):
        r = self.say("show me a picture of a flying dragon")
        self.assertIn("couldn't find a picture", r.text)
        self.assertTrue(any("tbm=isch" in u and "flying%20dragon" in u for u in self.urls_opened()), self.urls_opened())


class TestSmartTranscription(unittest.TestCase):
    class Local:
        name = "faster-whisper"
        last_language = "en"
        last_language_probability = 0.9

        def __init__(self):
            self.calls = 0

        def transcribe(self, pcm, sr=16000):
            self.calls += 1
            return "show me an email of a dragon"

    class Cfg:
        gemini_api_key = "AQ.k"
        llm_provider = "gemini"
        llm_model = "gemini-flash-lite-latest"
        stt_engine = "auto"

    class Settings(dict):
        def get(self, k, d=None):
            return super().get(k, d)

    PCM = b"\x10\x00" * 16000  # 1 s

    def test_cloud_first_then_local_fallback_with_backoff(self):
        local = self.Local()
        calls = []

        def cloud(cfg, pcm, sr, hint="", timeout=8):
            calls.append(len(pcm))
            return "show me an image of a dragon"

        stt = voice_ai.SmartSTT(local, self.Cfg(), self.Settings(), transcribe_fn=cloud)
        self.assertEqual(stt.transcribe(self.PCM), "show me an image of a dragon")
        self.assertEqual(local.calls, 0)

        def over_limit(*a, **k):
            raise gemini_native.GeminiError("quota", "quota")

        stt2 = voice_ai.SmartSTT(local, self.Cfg(), self.Settings(), transcribe_fn=over_limit)
        self.assertEqual(stt2.transcribe(self.PCM), "show me an email of a dragon")  # offline engine used
        self.assertFalse(stt2.cloud_enabled())  # and the cloud is paused for a while

    def test_off_switch_and_explicit_local_engine(self):
        local = self.Local()
        stt = voice_ai.SmartSTT(local, self.Cfg(), self.Settings({"voice.cloud_transcription": "off"}),
                                transcribe_fn=lambda *a, **k: self.fail("cloud used"))
        stt.transcribe(self.PCM)
        self.assertEqual(local.calls, 1)
        cfg = self.Cfg()
        cfg.stt_engine = "faster_whisper"
        self.assertIs(voice_ai.wrap_stt(local, cfg, self.Settings()), local)
        nokey = self.Cfg()
        nokey.gemini_api_key = None
        self.assertFalse(voice_ai.SmartSTT(local, nokey, self.Settings()).cloud_enabled())

    def test_gemini_transcription_request(self):
        sent = []

        class Sess:
            def post(self, url, json=None, headers=None, timeout=None):
                sent.append((url, json, headers))
                return FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": "Open YouTube."}]}}]})

        text = gemini_native.transcribe(self.Cfg(), self.PCM, session=Sess())
        self.assertEqual(text, "Open YouTube.")
        url, body, headers = sent[0]
        self.assertIn("gemini-flash-lite-latest:generateContent", url)  # 2.2.3: lite first - the largest free allowance
        self.assertEqual(body["generationConfig"]["thinkingConfig"], {"thinkingBudget": 0})
        audio = body["contents"][0]["parts"][1]["inline_data"]
        self.assertEqual(audio["mime_type"], "audio/wav")
        self.assertNotIn("AQ.k", url)  # the key travels in a header, never in the URL


class TestVoiceGuardAndChat(GenesisTestCase):
    def tearDown(self):
        voice_ai.CHAT.stop()
        super().tearDown()

    def test_low_confidence_voice_action_becomes_conversation(self):
        from genesis.nlp.intents import NLUResult

        guess = NLUResult(text="mail of a dragon", intent=Intent.DRAFT_EMAIL, confidence=0.6, source="classifier", slots={})
        self.assertEqual(voice_ai.route(guess, "voice").intent, Intent.CONVERSATION)
        precise = NLUResult(text="email rahul", intent=Intent.SEND_EMAIL, confidence=0.95, source="rules", slots={"recipient": "rahul"})
        self.assertEqual(voice_ai.route(precise, "voice").intent, Intent.SEND_EMAIL)
        typed = NLUResult(text="x", intent=Intent.DRAFT_EMAIL, confidence=0.6, source="classifier", slots={})
        self.assertEqual(voice_ai.route(typed, "text").intent, Intent.DRAFT_EMAIL)  # typed requests unchanged

    def test_voice_chat_mode(self):
        base = self.core.session.timeout_seconds()
        r = self.say("let's chat")
        self.assertEqual(tools_of(r), [("voice_chat", "succeeded")])
        self.assertTrue(voice_ai.CHAT.active)
        self.assertGreaterEqual(self.core.session.timeout_seconds(), voice_ai.CHAT_TIMEOUT_S)
        self.assertIn("VOICE CHAT", voice_ai.chat_prompt_addition())
        r = self.say("stop voice chat")
        self.assertFalse(voice_ai.CHAT.active)
        self.assertEqual(self.core.session.timeout_seconds(), base)


class LoudSource:
    """Fake audio track: speech-level tone for ``seconds``, then silence."""

    def __init__(self, track, seconds=2.2):
        self.track, self.left = track, int(seconds * 10)
        tone = [int(3000 * math.sin(i / 5)) for i in range(1600)]
        self.frame = struct.pack("<1600h", *tone)

    def open(self):
        pass

    def read(self):
        time.sleep(0.002)
        if self.left <= 0:
            time.sleep(0.01)
            return None
        self.left -= 1
        return self.frame

    def close(self):
        pass


MOM_JSON = {"title": "Project sync", "summary": "The team reviewed the demo plan.", "attendees": ["Asha"],
            "discussion": ["Demo on Friday"], "decisions": ["Ship v2.1"],
            "action_items": [{"task": "Prepare slides", "owner": "Asha", "due": "Thursday"}], "open_questions": [],
            "next_meeting": ""}


def mom_llm(messages, json_mode):
    if json_mode and "minutes of a meeting" in messages[-1]["content"]:
        return json.dumps(MOM_JSON)
    return json.dumps({"intent": "conversation", "confidence": 0.5, "entities": {}}) if json_mode else "ok"


class TestMeet(GenesisTestCase):
    llm = ScriptedLLM(mom_llm)

    def setUp(self):
        super().setUp()
        self.svc.extras["v2_meeting_sources"] = lambda cfg: ([LoudSource("Others"), LoudSource("You")], [])
        self.svc.extras["v2_meeting_transcriber"] = lambda pcm: "We agreed to ship version 2.1 and Asha prepares slides."
        self.svc.extras["v2_meeting_chunk_seconds"] = 1
        self.svc.extras["v2_meeting_sync"] = True

    def test_meet_patterns_dont_steal_other_commands(self):
        nlu = NLUPipeline()
        for text in ("open calculator", "open calendar", "open notepad", "start the music", "schedule a meeting tomorrow at 3"):
            self.assertNotEqual(nlu.parse(text).source, "v2-21", text)
        self.assertEqual(nlu.parse("open meeting abcdefghij").slots["args"], {"op": "join", "code": "abcdefghij"})

    def test_open_new_and_join(self):
        self.say("open google meet")
        self.say("start a new meeting")
        self.say("join meeting abc-defg-hij")
        self.assertEqual(self.urls_opened()[-3:], ["https://meet.google.com/", "https://meet.new",
                                                   "https://meet.google.com/abc-defg-hij"])

    def test_notes_need_consent_and_produce_minutes(self):
        r = self.say("take meeting notes")
        self.assertEqual(r.status, "awaiting_confirmation")  # everyone must agree to being recorded
        self.assertIn("agrees", r.text)
        r = self.say("no")
        self.assertIsNone(self.svc.extras.get("v2_meeting"))
        self.say("take meeting notes")
        r = self.say("yes")
        self.assertEqual(tools_of(r)[-1], ("meeting_notes_start", "succeeded"), r.text)
        time.sleep(0.6)
        r = self.say("stop meeting notes")
        self.assertEqual(tools_of(r), [("meeting_notes_stop", "succeeded")], r.text)
        res = self.svc.extras["v2_meeting_last"]
        self.assertTrue(res["llm_ok"])
        from docx import Document

        text = "\n".join(p.text for p in Document(res["docx"]).paragraphs)
        for expected in ("Minutes - Project sync", "Ship v2.1", "Prepare slides (owner: Asha, due: Thursday)", "Summary"):
            self.assertIn(expected, text)
        self.assertIn("Others: We agreed", Path(res["transcript"]).read_text(encoding="utf-8"))
        self.assertTrue(Path(res["docx"]).parent.name == "Meeting Minutes")
        self.assertEqual(list(Path(self.svc.extras["v2_meeting"].folder).glob("chunk-*.wav")), [])  # audio deleted
        self.assertIn(("open_path", res["docx"]), self.backend.actions)

    def test_write_the_mom_before_recording_starts_recording_with_consent(self):
        r = self.say("write the mom")
        self.assertEqual(r.status, "awaiting_confirmation")
        self.assertIn("agrees", r.text)

    def test_meet_and_notes_in_one_sentence(self):
        r = self.say("open google meet and take notes")
        self.assertEqual(self.urls_opened()[-1], "https://meet.google.com/")
        self.assertEqual(r.status, "awaiting_confirmation")


class TestMinutesWithoutModel(unittest.TestCase):
    def test_transcript_only_when_the_model_fails(self):
        import tempfile
        from datetime import datetime

        class Broken:
            def chat(self, *a, **k):
                raise RuntimeError("offline")

        out = Path(tempfile.mkdtemp())
        res = mt.write_minutes(llm=Broken(), lines=[mt.Line(0, "Others", "Hello everyone", 0)], out_dir=out,
                               started=datetime(2026, 10, 14, 15, 0), minutes=3)
        self.assertFalse(res["llm_ok"])
        self.assertTrue(Path(res["docx"]).is_file())
        self.assertEqual(mt.meet_url("join", "ABC DEFG HIJ"), "https://meet.google.com/abc-defg-hij")


if __name__ == "__main__":
    unittest.main()
