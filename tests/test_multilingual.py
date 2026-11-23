"""1.6 multilingual: detect → translate to English → normal pipeline → reply (and speak) in the user's language."""
import json
import re
import unittest

from genesis.nlp import rules
from genesis.nlp.language import LanguageService, detect, multilingual_yes_no
from genesis.voice.tts import PiperTTS, sapi_language_ids

from .helpers import GenesisTestCase, ScriptedLLM

# what the "model" translates to English
TO_EN = {
    "Ouvre YouTube": ("fr", "open YouTube"),
    "यूट्यूब खोलो": ("hi", "open YouTube"),
    "youtube kholo": ("hi", "open YouTube"),
    "éteins l'ordinateur": ("fr", "shut down the computer"),
    "quelle est la capitale de la France": ("fr", "what is the capital of France"),
    "rédige un e-mail à Rahul": ("fr", "draft an email to Rahul"),
    "abre youtube por favor": ("es", "open YouTube please"),
}


def scripted(messages, json_mode):
    content = messages[-1]["content"]
    system = messages[0]["content"] if messages[0]["role"] == "system" else ""
    if json_mode and "request for a desktop assistant into English" in content:
        text = content.rsplit("Text: ", 1)[1].strip()
        lang, english = TO_EN.get(text, ("en", text))
        return json.dumps({"language": lang, "english": english})
    if json_mode and "Translate this reply" in content:
        lang = re.search(r"from English into (.+?)\.", content).group(1)
        reply = content.split("Reply:\n", 1)[1]
        return json.dumps({"text": f"[{lang}] {reply}"})
    if json_mode:
        return json.dumps({"intent": "conversation", "confidence": 0.5, "entities": {}})
    if "Write your whole answer in French" in system:
        return "Paris est la capitale de la France."
    return "Paris is the capital of France."


def english_parser(t):
    return rules.parse(rules.normalize(t)) is not None


class TestDetection(unittest.TestCase):
    CASES = [
        ("open youtube", "en"), ("set volume to 30", "en"), ("open la la land", "en"), ("type del /s /q C:\\", "en"),
        ("mark buy milk as done", "en"), ("what is the capital of France", "en"), ("tell me a joke", "en"),
        ("Ouvre YouTube s'il te plaît", "fr"), ("quelle est la capitale de la France", "fr"),
        ("abre youtube y busca música", "es"), ("öffne bitte YouTube", "de"), ("apri youtube", "it"), ("abra o youtube", "pt"),
        ("youtube kholo", "hi-Latn"), ("mujhe ek gaana sunao", "hi-Latn"),
        ("यूट्यूब खोलो", "hi"), ("ಯೂಟ್ಯೂಬ್ ತೆರೆಯಿರಿ", "kn"), ("யூடியூப் திற", "ta"), ("యూట్యూబ్ తెరువు", "te"),
        ("открой ютуб", "ru"), ("YouTubeを開いて", "ja"), ("打开油管", "zh"), ("유튜브 열어줘", "ko"), ("افتح يوتيوب", "ar"),
        ("یوٹیوب کھولیں", "ur"),
    ]

    def test_languages(self):
        for text, lang in self.CASES:
            with self.subTest(text=text):
                self.assertEqual(detect(text, english_parser=english_parser).lang, lang)

    def test_no_evidence_is_undetermined(self):
        for text in ("Rahul", "rahul@example.com", "42", "YouTube"):
            self.assertEqual(detect(text, english_parser=english_parser).lang, "und", text)

    def test_whisper_hint(self):
        self.assertEqual(detect("lance la musique", hint="fr", hint_confidence=0.9).lang, "fr")
        self.assertEqual(detect("open youtube", hint="fr", hint_confidence=0.4, english_parser=english_parser).lang, "en")

    def test_yes_no_in_many_languages(self):
        for w in ("oui", "Sí", "ja", "haan", "हाँ", "はい", "да", "Oui, s'il vous plaît"):
            self.assertIs(multilingual_yes_no(w), True, w)
        for w in ("non", "nein", "nahi", "नहीं", "いいえ", "нет", "no"):
            self.assertIs(multilingual_yes_no(w), False, w)
        self.assertIsNone(multilingual_yes_no("ouvre youtube"))


class TestPipeline(GenesisTestCase):
    llm = ScriptedLLM(scripted)

    def calls(self):
        return len(self.llm.calls)

    def test_english_costs_nothing(self):
        n = self.calls()
        r = self.say("open youtube")
        self.assertEqual(self.calls(), n)  # no detection or translation call for English
        self.assertEqual(r.language, "en")
        self.assertNotIn("🌐", r.text)

    def test_french_command_is_translated_executed_and_answered_in_french(self):
        r = self.say("Ouvre YouTube")
        self.assertEqual(self.urls_opened()[-1], "https://www.youtube.com/")
        self.assertEqual(r.language, "fr")
        self.assertTrue(r.text.startswith("[French] "), r.text)
        self.assertIn("🌐 French → “open YouTube”", r.text)
        self.assertNotIn("🌐", r.speech)  # the note is shown, not spoken
        self.assertIn("YouTube is open", r.speech_english)  # spoken if no French voice is installed

    def test_hindi_script_and_hinglish(self):
        self.say("यूट्यूब खोलो")
        self.assertEqual(self.urls_opened()[-1], "https://www.youtube.com/")
        r = self.say("youtube kholo")
        self.assertIn("Hindi written in Latin letters", self.llm.calls[-1][-1]["content"])  # replies in Hinglish, not Devanagari
        self.assertEqual(r.language, "hi-Latn")

    def test_question_is_answered_directly_in_french(self):
        n = self.calls()
        r = self.say("quelle est la capitale de la France")
        self.assertEqual(r.status, "chat")
        self.assertIn("Paris est la capitale", r.text)
        self.assertEqual(self.calls() - n, 2)  # translate in + answer — no extra call to translate the answer back

    def test_confirmation_in_french_no_is_understood_offline(self):
        r = self.say("éteins l'ordinateur")
        self.assertEqual(r.status, "awaiting_confirmation")
        self.assertTrue(r.text.startswith("[French] "))
        n = self.calls()
        r = self.say("non")
        translations_in = [c for c in self.llm.calls[n:] if "request for a desktop assistant into English" in c[-1]["content"]]
        self.assertEqual(translations_in, [])  # "non" needed no model
        self.assertIn("Cancelled", r.speech_english)
        self.assertFalse(any(a[0] == "shutdown" for a in self.backend.actions))

    def test_message_body_is_kept_in_the_users_words(self):
        self.say("rédige un e-mail à Rahul")
        self.say("je serai en retard")  # the body: never translated
        r = self.say("rahul@example.com")
        self.assertIn(r.status, ("done", "partial"))
        self.assertRegex(self.urls_opened()[-1], r"je(%20|\+)serai(%20|\+)en(%20|\+)retard")

    def test_reply_language_preferences(self):
        r = self.say("always reply in English")
        self.assertIn("always reply in English", r.text)
        n = self.calls()
        r = self.say("Ouvre YouTube")
        self.assertFalse(any("Translate this reply" in c[-1]["content"] for c in self.llm.calls[n:]))
        self.assertTrue(r.text.startswith("Done"), r.text)
        self.say("reply in French")
        r = self.say("open youtube")  # English in, French out
        self.assertTrue(r.text.startswith("[French] "))
        self.say("reply in my language")
        self.assertEqual(self.svc.settings.get("language.reply"), "same")

    def test_english_only_mode(self):
        self.say("stop translating")
        n = self.calls()
        self.say("Ouvre YouTube")
        self.assertFalse(any("request for a desktop assistant into English" in c[-1]["content"] for c in self.llm.calls[n:]))
        self.say("understand all languages")
        self.assertEqual(self.svc.settings.get("language.mode"), "auto")

    def test_what_languages(self):
        r = self.say("what languages do you speak")
        self.assertIn("Kannada", r.text)


class TestWithoutModel(GenesisTestCase):
    def test_says_why_instead_of_guessing(self):
        r = self.say("Ouvre YouTube")
        self.assertEqual(r.status, "failed")
        self.assertIn("speaking French", r.text)
        self.assertEqual(self.urls_opened(), [])
        self.assertEqual(self.say("open youtube").status, "done")  # English still works offline


class TestLanguageService(unittest.TestCase):
    def test_cache_avoids_repeat_calls(self):
        llm = ScriptedLLM(scripted)
        from genesis.ai.llm.factory import ResilientLLM

        svc = LanguageService(ResilientLLM(llm), {"language.mode": "auto", "language.reply": "same"}, english_parser)
        a = svc.understand("abre youtube por favor")
        b = svc.understand("abre youtube por favor")
        self.assertEqual((a.english, a.lang), ("open YouTube please", "es"))
        self.assertEqual(b.english, a.english)
        self.assertEqual(len(llm.calls), 1)


class TestVoices(unittest.TestCase):
    def test_sapi_language_attribute(self):
        self.assertEqual(sapi_language_ids("409;9"), {0x09})
        self.assertEqual(sapi_language_ids("40C"), {0x0C})
        self.assertEqual(sapi_language_ids("439"), {0x39})

    def test_piper_voice_language(self):
        p = PiperTTS.__new__(PiperTTS)
        p.model_path = "data/models/piper/en_US-hfc_female-medium.onnx"
        self.assertTrue(p.can_speak("en"))
        self.assertFalse(p.can_speak("fr"))

    def test_whisper_multilingual(self):
        from genesis.voice.stt import FasterWhisperSTT

        class Seg:
            def __init__(self, t):
                self.text = t

        class Info:
            language, language_probability = "fr", 0.97

        class Model:
            def __init__(self, lang, prob):
                self.lang, self.prob, self.kw = lang, prob, None

            def detect_language(self, audio):
                return self.lang, self.prob, []

            def transcribe(self, audio, **kw):
                self.kw = kw
                return [Seg("ouvre youtube")], Info()

        stt = FasterWhisperSTT.__new__(FasterWhisperSTT)
        stt.hint, stt.multilingual, stt.model_size = None, (lambda: True), "base"
        stt.model = Model("fr", 0.95)
        self.assertEqual(stt.transcribe(b"\x00\x00" * 1600), "ouvre youtube")
        self.assertEqual((stt.last_language, stt.model.kw["language"]), ("fr", "fr"))
        self.assertNotIn("VS Code", stt.model.kw["initial_prompt"] or "")  # no English bias for French
        stt.model = Model("de", 0.4)  # unsure → English wins
        stt.transcribe(b"\x00\x00" * 1600)
        self.assertEqual(stt.model.kw["language"], "en")


if __name__ == "__main__":
    unittest.main()
