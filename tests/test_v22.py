"""Genesis 2.2: active-window awareness, coding into any window (online compilers), Colab in your own Chrome, voice.

Windows, the keyboard/clipboard, Chrome, Colab and the language model are all fakes - nothing real is typed or opened.
"""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from genesis.nlp.intents import Intent, NLUResult
from genesis.services.colab import FakeColabDriver
from genesis.services.computer import Element, RecordingInputDriver, WindowInfo
from genesis.v2 import chrome_cdp, coding_router, gemini_native, voice_ai
from genesis.v2 import window_context as wc
from tests.helpers import GenesisTestCase, ScriptedLLM

BUBBLE = "def bubble_sort(a):\n    for i in range(len(a)):\n        for j in range(len(a) - 1 - i):\n            if a[j] > a[j + 1]:\n                a[j], a[j + 1] = a[j + 1], a[j]\n    return a\n\nprint(bubble_sort([5, 2, 9, 1]))"


def tools_of(resp):
    return [(s["tool"], s["status"]) for p in resp.plans for s in p["steps"]]


class EditorDriver(RecordingInputDriver):
    """A browser window whose code editor has the keyboard focus: select-all/copy/paste behave like a real editor."""

    def __init__(self, window, content="print('Hello, World!')", focus_role="field", elements=None):
        super().__init__(window, elements or [])
        self.content = content
        self.focus_role = focus_role
        self.clipboard = "user's own clipboard text"

    def keys(self, combo):
        self.actions.append(("keys", tuple(combo)))
        if combo == ["ctrl", "c"]:
            if self.content:
                self.clipboard = self.content
        elif combo == ["ctrl", "v"]:
            self.content = self.clipboard

    def focused_element(self):
        return Element(1, "Editor content" if self.focus_role == "field" else "Home", self.focus_role)


def code_llm(messages, json_mode):
    content = messages[-1]["content"]
    if json_mode and "Colab" in content:
        return json.dumps({"code": "for i in range(20):\n    print(i)", "summary": "prints the first 20 numbers"})
    if "writing code directly" in content or "editing code" in content:
        return "```python\n" + BUBBLE + "\n```"
    return "{}" if json_mode else "OK"


PROGRAMIZ = WindowInfo("Online Python Compiler (Interpreter) - Programiz - Google Chrome", "chrome.exe", "Chrome_WidgetWin_1", 501, 4321)


class TestWindowClassification(unittest.TestCase):
    def test_kinds_and_languages(self):
        cases = [("Online Python Compiler (Interpreter) - Programiz - Google Chrome", "chrome.exe", "code_web", "python"),
                 ("Online C++ Compiler - online editor - OnlineGDB - Google Chrome", "chrome.exe", "code_web", "cpp"),
                 ("Two Sum - LeetCode - Google Chrome", "chrome.exe", "code_web", None),
                 ("Online Java Compiler - Microsoft Edge", "msedge.exe", "code_web", "java"),
                 ("Untitled0.ipynb - Colab - Google Chrome", "chrome.exe", "colab", "python"),
                 ("YouTube - Google Chrome", "chrome.exe", "browser", None),
                 ("main.py - genesis - Visual Studio Code", "Code.exe", "ide", "python"),
                 ("Windows PowerShell", "powershell.exe", "terminal", None),
                 ("notes.txt - Notepad", "notepad.exe", "editor", None)]
        for title, proc, kind, lang in cases:
            self.assertEqual(wc.classify(title, proc), (kind, lang), title)

    def test_page_and_description(self):
        t = wc.Target(title=PROGRAMIZ.title, process="chrome.exe", kind="code_web", language="python")
        self.assertEqual(t.page, "Online Python Compiler (Interpreter) - Programiz")
        self.assertIn("online code editor (Python)", t.describe())


class TestCurrentTarget(GenesisTestCase):
    def computer_driver(self):
        return RecordingInputDriver(WindowInfo("Genesis", "python.exe", "Qt", 900, os.getpid()))

    def test_typed_into_genesis_uses_the_previous_window(self):
        self.backend.windows = [{"id": 900, "title": "Genesis", "active": True},
                                {"id": 501, "title": PROGRAMIZ.title, "active": False, "process": "chrome.exe"}]
        t = wc.current_target(self.svc)
        self.assertEqual((t.handle, t.kind, t.language, t.via), (501, "code_web", "python", "previous"))


class TestRouting(unittest.TestCase):
    def route(self, text, target, intent=Intent.CODE_ACTION, source="rules"):
        class Svc:
            extras = {"v2_window_target": target}
            colab = None

        nlu = NLUResult(text=text, intent=intent, confidence=0.9, source=source, slots={})
        return coding_router.route(nlu, Svc())

    def test_online_compiler(self):
        t = wc.Target(title=PROGRAMIZ.title, process="chrome.exe", kind="code_web", language="python")
        r = self.route("write a program for bubble sort and run it", t)
        self.assertEqual((r.slots["tool"], r.slots["args"]["op"], r.slots["args"]["run"]), ("code_in_window", "write", True))
        self.assertEqual(r.slots["args"]["task"], "write a program for bubble sort")
        self.assertEqual(self.route("fix the error", t, Intent.CONVERSATION, "classifier").slots["args"]["op"], "fix")
        self.assertEqual(self.route("run it", t).slots["args"]["op"], "run")
        r = self.route("change it to take the numbers from the user", t, Intent.CONVERSATION, "classifier")
        self.assertEqual(r.slots["args"]["op"], "edit")
        self.assertEqual(self.route("write a c++ program for fibonacci", t).slots["args"]["language"], "cpp")

    def test_colab_and_explicit_destinations(self):
        colab = wc.Target(title="Untitled0.ipynb - Colab - Google Chrome", process="chrome.exe", kind="colab", language="python")
        r = self.route("write a program for fibonacci and run it", colab)
        self.assertEqual((r.slots["tool"], r.slots["args"]["run"]), ("colab_code", True))
        self.assertEqual(self.route("edit the cell to print the first 20 numbers", colab, Intent.CONVERSATION).slots["tool"], "colab_edit")
        self.assertEqual(self.route("write a python program for fibonacci in colab", None).slots["tool"], "colab_code")
        self.assertIn("Ctrl+M", self.route("delete the cell", colab, Intent.FILE_OPERATION).slots["v2_reply"])

    def test_unchanged_elsewhere(self):
        other = wc.Target(title="YouTube - Google Chrome", process="chrome.exe", kind="browser")
        for text, target in [("write a program for bubble sort", other), ("write a program for bubble sort", None),
                             ("write a program for bubble sort in vs code", wc.Target(kind="code_web")),
                             ("open youtube", wc.Target(kind="code_web"))]:
            nlu = NLUResult(text=text, intent=Intent.CODE_ACTION, confidence=0.9, source="rules", slots={"x": 1})
            self.assertEqual(coding_router.route(nlu, type("S", (), {"extras": {"v2_window_target": target}})()).slots, {"x": 1},
                             text)

    def test_where_am_i_and_what_did_you_hear(self):
        self.assertEqual(self.route("which window am i in", None, Intent.CONVERSATION).slots["tool"], "active_window")
        self.assertEqual(self.route("what did you hear", None, Intent.CONVERSATION).slots["tool"], "last_heard")


class TestCodeInWindow(GenesisTestCase):
    llm = ScriptedLLM(code_llm)

    def computer_driver(self):
        self.drv = EditorDriver(PROGRAMIZ, elements=[Element(7, "Run", "button", (10, 10, 40, 20))])
        return self.drv

    def setUp(self):
        super().setUp()
        self.svc.extras["v2_window_target"] = wc.Target(title=PROGRAMIZ.title, process="chrome.exe", handle=501,
                                                        kind="code_web", language="python")

    def test_writes_code_into_the_online_compiler_and_runs_it(self):
        r = self.say("write a program for bubble sort and run it")
        self.assertEqual(r.status, "awaiting_confirmation")  # it replaces what's in the editor: ask first
        self.assertIn("Programiz", r.text)
        r = self.say("yes")
        self.assertEqual(tools_of(r)[-1], ("code_in_window", "succeeded"), r.text)
        self.assertEqual(self.drv.content, BUBBLE)  # the generated code is now in the editor
        self.assertIn(("invoke", "Run"), self.drv.actions)  # and Run was pressed
        self.assertEqual(self.drv.clipboard, "user's own clipboard text")  # clipboard restored
        self.assertIn("```python", r.text)

    def test_refuses_terminals_and_unfocused_pages(self):
        self.svc.extras["v2_window_target"] = wc.Target(title="Windows PowerShell", process="powershell.exe", kind="terminal")
        self.say("write a program for bubble sort here")
        r = self.say("yes")
        self.assertIn("terminal", r.text.lower())
        self.svc.extras["v2_window_target"] = wc.Target(title=PROGRAMIZ.title, process="chrome.exe", kind="code_web")
        self.drv.focus_role = "link"
        self.say("write a program for bubble sort")
        r = self.say("yes")
        self.assertIn("Click inside the code editor", r.text)
        self.assertEqual(self.drv.content, "print('Hello, World!')")  # nothing was replaced


class TestColab(GenesisTestCase):
    llm = ScriptedLLM(lambda m, j: code_llm(m, j) if "Edit this Google Colab cell" in m[-1]["content"]
                      else json.dumps({"code": "print('fib')", "summary": "fibonacci"}) if j and "Colab" in m[-1]["content"]
                      else code_llm(m, j))

    def setUp(self):
        self.colab_driver = FakeColabDriver({"print('fib')": "0 1 1 2 3"})
        super().setUp()

    def test_write_and_run_in_colab_creates_a_notebook_first(self):
        r = self.say("write a python program for fibonacci in colab and run it")
        self.assertEqual([t for t, _ in tools_of(r)], ["colab_code", "colab_run"], r.text)
        self.assertEqual(self.colab_driver.runs, ["print('fib')"])

    def test_edit_cell(self):
        self.say("write a python program for fibonacci in colab")
        self.svc.extras["v2_window_target"] = wc.Target(title="Untitled0.ipynb - Colab - Google Chrome", process="chrome.exe",
                                                        kind="colab", language="python")
        r = self.say("edit the cell to print the first 20 numbers")
        self.assertEqual(tools_of(r), [("colab_edit", "succeeded")], r.text)
        self.assertEqual(self.colab_driver.cells[-1], "for i in range(20):\n    print(i)")


class TestChrome(unittest.TestCase):
    def test_launch_uses_a_dedicated_profile_and_local_port(self):
        args = chrome_cdp.launch_args("C:/chrome.exe", Path("C:/g/chrome-profile"), "https://colab.research.google.com/", 9333)
        self.assertIn("--remote-debugging-port=9333", args)
        self.assertTrue(any(a.startswith("--user-data-dir=") and "chrome-profile" in a for a in args))

    def test_choice_and_availability(self):
        old = os.environ.get("GENESIS_COLAB_BROWSER")
        try:
            os.environ["GENESIS_COLAB_BROWSER"] = "edge"
            self.assertIsNone(chrome_cdp.colab_browser(object()))
        finally:
            os.environ.pop("GENESIS_COLAB_BROWSER", None) if old is None else os.environ.__setitem__("GENESIS_COLAB_BROWSER", old)
        self.assertFalse(chrome_cdp.ChromeCDP(Path("x"), chrome_path=None).available() and chrome_cdp.find_chrome() is None)

    def test_starts_chrome_only_when_not_running(self):
        import time as _t
        from unittest import mock

        started = []
        c = chrome_cdp.ChromeCDP(Path("genesis-test-profile"), chrome_path="chrome.exe", cdp_port=1,
                                 popen=lambda args, **kw: started.append(args))
        with mock.patch.object(chrome_cdp, "_port_open", lambda p, timeout=0.3: False), \
                mock.patch.object(_t, "sleep", lambda s: None), mock.patch.object(Path, "mkdir", lambda *a, **k: None):
            with self.assertRaises(RuntimeError):  # Chrome "never" opens its port in this test
                c.start_chrome("https://colab.research.google.com/")
        self.assertEqual(len(started), 1)
        with mock.patch.object(chrome_cdp, "_port_open", lambda p, timeout=0.3: True):
            c.start_chrome("https://colab.research.google.com/")  # already running: not started again
        self.assertEqual(len(started), 1)


class TestFreeImages(unittest.TestCase):
    """2.2: a free image service that needs no account (AI Horde), honest reasons, no stray '?'."""

    class R:
        def __init__(self, code=200, js=None, content=b"", headers=None, text=""):
            self.status_code, self._js, self.content, self.headers, self.text = code, js, content, headers or {}, text

        def json(self):
            return self._js

    def _service(self, session):
        from genesis.services.images import ImageService

        cfg = type("C", (), {"hf_token": None, "llm_provider": "gemini", "gemini_api_key": "AQ.k", "openai_api_key": None})()
        import tempfile

        return ImageService(cfg, Path(tempfile.mkdtemp()), session=session)

    def setUp(self):
        from genesis.v2 import images

        self.images = images
        images.reset()
        self._old = os.environ.get("GENESIS_IMAGE_PROVIDER")
        os.environ["GENESIS_IMAGE_PROVIDER"] = "auto"

    def tearDown(self):
        self.images.reset()
        if self._old is None:
            os.environ.pop("GENESIS_IMAGE_PROVIDER", None)
        else:
            os.environ["GENESIS_IMAGE_PROVIDER"] = self._old

    def test_horde_creates_the_image_when_everything_else_refuses(self):
        R, calls = self.R, []
        polls = {"n": 0}

        class Sess:
            def post(self, url, json=None, headers=None, timeout=None):
                calls.append(("post", url, headers))
                if "aihorde" in url:
                    return R(202, {"id": "job-1", "kudos": 10})
                return R(429, text="quota exceeded, limit: 0")  # Gemini: no free image quota

            def get(self, url, params=None, headers=None, timeout=None):
                calls.append(("get", url, headers))
                if "pollinations" in url:
                    return R(402, text="payment required")
                if "/check/" in url:
                    polls["n"] += 1
                    return R(200, {"done": polls["n"] >= 2, "faulted": False})
                if "/status/" in url:
                    return R(200, {"generations": [{"img": "https://r2.example/cat.webp"}]})
                if url == "https://r2.example/cat.webp":
                    return R(200, content=b"RIFFxxxxWEBP" + b"0" * 40, headers={"Content-Type": "image/webp"})
                return R(404)

        svc = self._service(Sess())
        orig = self.images.time.sleep
        self.images.time.sleep = lambda s: None
        try:
            item = svc.generate("a cat?")
        finally:
            self.images.time.sleep = orig
        self.assertEqual(item["provider"], "horde")
        self.assertTrue(Path(item["path"]).is_file())
        self.assertEqual(item["title"], "a cat")  # the stray "?" is gone
        post = next(c for c in calls if c[0] == "post" and "aihorde" in c[1])
        self.assertEqual(post[2]["apikey"], "0000000000")  # anonymous - no account needed

    def test_recent_failures_are_reported_not_hidden(self):
        from genesis.services.images import ImageError

        for name in ("gemini", "pollinations", "horde"):
            self.images._remember_failure(name, "quota")
        self.images._remember_failure("pollinations", "auth")
        svc = self._service(type("S", (), {"post": lambda *a, **k: self.R(500), "get": lambda *a, **k: self.R(500)})())
        with self.assertRaises(ImageError) as cm:
            svc.generate("a dog")
        msg = str(cm.exception)
        self.assertNotIn("no image service is set up", msg)
        self.assertIn("Gemini", msg)


class TestSpokenReplies(unittest.TestCase):
    """2.2.2: what Genesis says aloud is natural - no code, file names, settings or bracketed asides."""

    def test_speech_is_natural(self):
        from genesis.core.assistant import speech_text

        cases = [
            ("I couldn't create a new picture right now, so here's a real photo of a cat instead - it's in the chat window. "
             "(Why: Gemini: needs billing.) (Tip: for faster, reliable free images run Setup_Images.bat once.)",
             "I couldn't create a new picture right now, so here's a real photo of a cat instead - it's in the chat window."),
            ("Wrote the Python code in “Programiz”.\n```python\nprint(1)\n```\nPressed Run - the output is on the page.",
             "Wrote the Python code in Programiz. Pressed Run - the output is on the page. The code is on your screen."),
            ("Gemini only creates images with billing. Set GENESIS_IMAGE_PROVIDER=auto in .env.",
             "Gemini only creates images with billing."),
            ("Here's a picture of a peacock — it's in the chat window. Credit: Jane Doe.",
             "Here's a picture of a peacock — it's in the chat window."),
        ]
        for screen, said in cases:
            self.assertEqual(speech_text(screen), said, screen)
        self.assertIn("Setup Images file", speech_text("Run Setup_Images.bat to connect Cloudflare."))


class TestGeminiTuning(unittest.TestCase):
    """2.2.2: Gemini Flash answers without 'thinking' (so short replies aren't empty); falls back if a model refuses."""

    def test_flash_thinking_off_and_fallback(self):
        from genesis.ai.llm.openai_compat import GeminiProvider
        from genesis.v2 import gemini_tuning

        gemini_tuning._UNSUPPORTED.clear()
        sent = []

        class R:
            def __init__(self, code, js=None, text=""):
                self.status_code, self._js, self.text, self.headers = code, js, text, {}

            def json(self):
                return self._js

        class Sess:
            def post(self, url, json=None, headers=None, timeout=None):
                sent.append(dict(json))
                if "reasoning_effort" in json and json["model"] == "gemini-x-flash-old":
                    return R(400, text='{"error":{"message":"Invalid value for reasoning_effort"}}')
                if "reasoning_effort" in json and json["model"] == "gemini-flash-generic":  # 2.2.4: Google's real reply
                    return R(400, text='[{"error": {"code": 400, "message": "Request contains an invalid argument.", '
                                       '"status": "INVALID_ARGUMENT"}}]')
                return R(200, {"choices": [{"message": {"content": "ready"}}]})

        p = GeminiProvider("AQ.k", "gemini-flash-latest", session=Sess(), retries=0)
        self.assertEqual(p.chat([{"role": "user", "content": "hi"}], max_tokens=20), "ready")
        self.assertEqual(sent[-1]["reasoning_effort"], "none")
        old = GeminiProvider("AQ.k", "gemini-x-flash-old", session=Sess(), retries=0)
        self.assertEqual(old.chat([{"role": "user", "content": "hi"}], max_tokens=20), "ready")  # retried without it
        self.assertNotIn("reasoning_effort", sent[-1])
        old.chat([{"role": "user", "content": "hi"}], max_tokens=20)
        self.assertNotIn("reasoning_effort", sent[-1])  # remembered: not sent again
        generic = GeminiProvider("AQ.k", "gemini-flash-generic", session=Sess(), retries=0)
        self.assertEqual(generic.chat([{"role": "user", "content": "hi"}], max_tokens=20), "ready")
        self.assertNotIn("reasoning_effort", sent[-1])
        gemini_tuning._UNSUPPORTED.clear()


class TestGeminiFailover(unittest.TestCase):
    """2.2.3: when Google rate-limits one Gemini model, the question goes to another model (each has its own free quota)."""

    def setUp(self):
        from genesis.v2 import gemini_tuning

        gemini_tuning.reset()
        gemini_tuning._UNSUPPORTED.clear()

    tearDown = setUp

    def _provider(self, busy):
        from genesis.ai.llm.openai_compat import GeminiProvider

        sent = []

        class R:
            def __init__(self, code, js=None, text=""):
                self.status_code, self._js, self.text, self.headers = code, js, text, {}

            def json(self):
                return self._js

        class Sess:
            def post(self, url, json=None, headers=None, timeout=None):
                sent.append(json["model"])
                if json["model"] in busy:
                    return R(429, text="Resource has been exhausted (e.g. check quota).")
                return R(200, {"choices": [{"message": {"content": f"answer from {json['model']}"}}]})

        return GeminiProvider("AQ.k", "gemini-flash-latest", session=Sess(), retries=0, backoff=0), sent

    def test_busy_model_hands_over_and_cools_down(self):
        from genesis.v2 import gemini_tuning

        p, sent = self._provider(busy={"gemini-flash-latest"})
        self.assertEqual(p.chat([{"role": "user", "content": "hi"}]), "answer from gemini-flash-lite-latest")
        self.assertEqual(p.model, "gemini-flash-latest")  # the configured model is kept
        self.assertTrue(gemini_tuning.cooling("gemini-flash-latest"))
        sent.clear()
        p.chat([{"role": "user", "content": "again"}])
        self.assertEqual(sent, ["gemini-flash-lite-latest"])  # the busy one isn't asked again for a minute

    def test_all_busy_reports_the_limit(self):
        from genesis.ai.llm.base import LLMUnavailable
        from genesis.v2 import gemini_tuning

        p, _ = self._provider(busy=set(gemini_tuning.FALLBACK_MODELS))
        with self.assertRaises(LLMUnavailable):
            p.chat([{"role": "user", "content": "hi"}])


class TestVoice22(unittest.TestCase):
    class Cfg:
        gemini_api_key = "AQ.k"
        llm_provider = "gemini"
        llm_model = "gemini-flash-lite-latest"

    def test_auth_rejection_falls_back_to_the_openai_compatible_endpoint(self):
        calls = []

        class R:
            def __init__(self, code, js=None, text=""):
                self.status_code, self._js, self.text = code, js, text

            def json(self):
                return self._js

        class Sess:
            def post(self, url, json=None, headers=None, timeout=None):
                calls.append(url)
                if ":generateContent" in url:
                    return R(401, text="ACCESS_TOKEN_TYPE_UNSUPPORTED")
                return R(200, {"choices": [{"message": {"content": "Write a program for bubble sort."}}]})

        text = gemini_native.transcribe(self.Cfg(), b"\x10\x00" * 16000, session=Sess())
        self.assertEqual(text, "Write a program for bubble sort.")
        self.assertTrue(any(u.endswith("/v1beta/openai/chat/completions") for u in calls))

    def test_context_hint_and_last_heard(self):
        class Svc:
            extras = {"v2_window_target": wc.Target(title=PROGRAMIZ.title, process="chrome.exe", kind="code_web")}

        voice_ai.attach(Svc())
        self.assertIn("Programiz", voice_ai.context_hint())
        sent = {}

        class Local:
            name = "faster-whisper"

            def transcribe(self, pcm, sr=16000):
                return "x"

        def cloud(cfg, pcm, sr, hint="", timeout=8):
            sent["hint"] = hint
            return "write a program for bubble sort"

        stt = voice_ai.SmartSTT(Local(), self.Cfg(), {}, transcribe_fn=cloud)
        stt.settings = type("S", (), {"get": lambda self, k, d=None: d})()
        self.assertEqual(stt.transcribe(b"\x10\x00" * 16000), "write a program for bubble sort")
        self.assertIn("Programiz", sent["hint"])
        self.assertEqual(voice_ai.LAST_HEARD["text"], "write a program for bubble sort")
        voice_ai._CONTEXT_SVC.clear()


if __name__ == "__main__":
    unittest.main()
