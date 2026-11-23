"""Genesis 1.2 — agent capabilities: YouTube, media, windows, calendar, Gmail, web answers, coding agent, memory,
permissions, recovery, and the spec's end-to-end scenarios. Everything runs against fakes; programs written by the
coding agent are really executed (Python via the current interpreter; Node/Java tests run only if installed).
"""
from __future__ import annotations

import json
import os
import shutil
import unittest
from datetime import datetime, timedelta

from genesis.agent.plan import Plan
from genesis.agent.planner import LLM_EXCLUDED_TOOLS
from genesis.core.events import AssistantState, EventType
from genesis.nlp import rules
from genesis.nlp.intents import Intent
from genesis.nlp.pipeline import NLUPipeline
from genesis.services import web
from genesis.services.coderunner import CodeRunner, language_for, safety_scan
from genesis.services.screen import ScreenService
from genesis.services.youtube import parse_results, search_url
from genesis.tools.coding_agent import _clean_files
from genesis.security.validators import ValidationError

from .helpers import FakeYouTube, GenesisTestCase, ScriptedLLM, youtube_page
from .test_conversation import INBOX, tools_of

TOMORROW = (datetime.now() + timedelta(days=1)).date()


def code_llm(programs: dict[str, dict], fixes: list[dict] | None = None, answer: str = "It's a bug.") -> ScriptedLLM:
    """ScriptedLLM that writes programs keyed by a word in the task, and returns queued fixes."""
    fixes = list(fixes or [])

    def reply(messages, json_mode):
        last = messages[-1]["content"]
        if "small, complete, working" in last:
            task = last.split("Task:", 1)[1].split("\n", 1)[0].lower()
            for key, prog in programs.items():
                if key in task:
                    return json.dumps(prog)
            return json.dumps(next(iter(programs.values())))
        if "failed when run" in last:
            return json.dumps(fixes.pop(0)) if fixes else json.dumps({"explanation": "no idea", "files": []})
        if "Summarise this e-mail" in last:
            return "- AMD invites you to an interview next week."
        if "Answer this for the user" in last:
            return answer
        return "{}" if json_mode else "Happy to help! What would you like to do?"

    return ScriptedLLM(reply)


FIB_BROKEN = {"name": "fibonacci", "entry": "main.py", "summary": "prints Fibonacci numbers",
              "files": [{"path": "main.py", "content": "def fib(n):\n    a, b = 0, 1\n    out = []\n    for _ in range(n):\n"
                                                       "        out.append(a)\n        a, b = b, a + c\n    return out\n\n"
                                                       "print(fib(10))\n"}]}
FIB_FIX = {"explanation": "replaced the undefined c with b",
           "files": [{"path": "main.py", "content": "def fib(n):\n    a, b = 0, 1\n    out = []\n    for _ in range(n):\n"
                                                    "        out.append(a)\n        a, b = b, a + b\n    return out\n\nprint(fib(10))\n"}]}


# =====================================================================================
# NLU: tool selection from natural language
# =====================================================================================
class TestUnderstanding(unittest.TestCase):
    CASES = [
        ("open youtube", Intent.OPEN_APPLICATION, {"app": "youtube"}),
        ("search youtube for python tutorials", Intent.YOUTUBE, {"youtube_op": "search", "query": "python tutorials"}),
        ("look up neural network tutorials on youtube", Intent.YOUTUBE, {"youtube_op": "search"}),
        ("play arijit singh on youtube", Intent.YOUTUBE, {"youtube_op": "play", "query": "arijit singh"}),
        ("play the latest mrbeast video", Intent.YOUTUBE, {"youtube_op": "play", "query": "mrbeast", "latest": True}),
        ("play python tutorial on youtube", Intent.YOUTUBE, {"youtube_op": "play"}),
        ("pause", Intent.MEDIA, {"action": "pause"}),
        ("pause the video", Intent.MEDIA, {"action": "pause"}),
        ("resume", Intent.MEDIA, {"action": "resume"}),
        ("skip", Intent.MEDIA, {"action": "next"}),
        ("next song", Intent.MEDIA, {"action": "next"}),
        ("make it louder", Intent.SYSTEM_CONTROL, {"system_action": "volume_up"}),
        ("take a screenshot", Intent.WINDOW, {"window_op": "screenshot"}),
        ("switch to chrome", Intent.WINDOW, {"window_op": "focus", "window": "chrome"}),
        ("minimize notepad", Intent.WINDOW, {"window_op": "minimize"}),
        ("show the desktop", Intent.WINDOW, {"window_op": "show_desktop"}),
        ("what windows are open", Intent.WINDOW, {"window_op": "list"}),
        ("put a meeting with Rahul tomorrow at 4", Intent.CALENDAR, {"calendar_op": "create", "title": "Meeting with Rahul",
                                                                    "time": "16:00"}),
        ("schedule a meeting with the team next monday", Intent.CALENDAR, {"calendar_op": "create"}),
        ("what do i have tomorrow", Intent.CALENDAR, {"calendar_op": "list"}),
        ("what's on my calendar this week", Intent.CALENDAR, {"calendar_op": "list", "range_days": 7}),
        ("move my 3 pm meeting to 5 pm", Intent.CALENDAR, {"calendar_op": "move", "find_time": "15:00", "new_time": "17:00"}),
        ("cancel the meeting tomorrow", Intent.CALENDAR, {"calendar_op": "delete"}),
        ("remind me to call mom friday evening", Intent.CREATE_REMINDER, {"reminder_text": "call mom"}),
        ("create a python script that calculates fibonacci numbers", Intent.CODE_ACTION, {"code_action": "create_program",
                                                                                          "language": "python"}),
        ("create a small flask api with a /users endpoint", Intent.CODE_ACTION, {"code_action": "create_program", "language": "python"}),
        ("write a javascript program that sorts names", Intent.CODE_ACTION, {"language": "javascript"}),
        ("build a simple web page with a contact form", Intent.CODE_ACTION, {"language": "html"}),
        ("create a program that prints hello", Intent.CODE_ACTION, {"code_action": "create_program", "language": None}),
        ("run it", Intent.CODE_ACTION, {"code_action": "run"}),
        ("run main.py", Intent.CODE_ACTION, {"code_action": "run", "path": "main.py"}),
        ("find that python tutorial we were talking about yesterday", Intent.HISTORY, {"query": "python tutorial", "when": "yesterday"}),
        ("my preferred coding language is python", Intent.MEMORY, {"memory_op": "preference", "value": "python"}),
        ("search the internet and tell me why this error is happening", Intent.RESEARCH, {"operation": "answer", "about_error": True}),
        ("find the latest information about nvidia", Intent.RESEARCH, {"operation": "news"}),
        ("find the latest email from AMD", Intent.READ_EMAIL, {"from": "AMD", "open_latest": True}),
        ("find emails with attachments", Intent.READ_EMAIL, {"attachments": True}),
        ("search the web for python decorators", Intent.SEARCH_WEB, {"query": "python decorators"}),
        ("stop", Intent.CANCEL, {}),
    ]

    def test_cases(self):
        for text, intent, slots in self.CASES:
            with self.subTest(text=text):
                r = rules.parse(text)
                self.assertIsNotNone(r, text)
                self.assertEqual(r.intent, intent, text)
                for k, v in slots.items():
                    self.assertEqual(r.slots.get(k), v, f"{text}: {k}")

    def test_typos_and_slang(self):
        p = NLUPipeline(None, use_llm=lambda: False)
        for text, app in (("opne youtub", "youtube"), ("pls open yt", "youtube"), ("lanch spotfy", "spotify")):
            r = p.parse(text)
            self.assertEqual((r.intent, r.slots.get("app")), (Intent.OPEN_APPLICATION, app), text)
        self.assertEqual(rules.normalize("gimme my emails pls"), "give me my emails please")
        self.assertEqual(rules.correct_typos("send the report to my boss"), "send the report to my boss")  # free text untouched

    def test_routine_and_compound_splitting_unchanged(self):
        self.assertEqual(rules.split_requests("find the latest email from AMD, summarize it and draft a reply saying I'm interested"),
                         ["find the latest email from AMD", "summarize it", "draft a reply saying I'm interested"])


# =====================================================================================
# YouTube + media
# =====================================================================================
class TestYouTube(GenesisTestCase):
    def test_parse_results_page(self):
        res = parse_results(youtube_page(FakeYouTube.VIDEOS))
        self.assertEqual([r["video_id"] for r in res], [v[0] for v in FakeYouTube.VIDEOS])
        self.assertEqual(res[0]["channel"], "Programming with Mosh")
        self.assertEqual(parse_results("<html>no data</html>"), [])
        self.assertIn("sp=CAI", search_url("mrbeast", latest=True))

    def test_open_search_play_nth_pause_skip_louder(self):
        self.say("open youtube")
        r = self.say("search for python tutorials")  # "search" means YouTube because that's what's open
        self.assertEqual(tools_of(r), [("youtube_search", "succeeded")])
        r = self.say("play the second one")
        self.assertEqual(tools_of(r), [("youtube_play", "succeeded")])
        self.assertIn("Python Full Course", r.text)
        self.assertEqual(self.urls_opened()[-1], "https://www.youtube.com/watch?v=bbbbbbbbbb2")
        self.say("pause")
        r = self.say("pause")
        self.assertIn("already paused", r.text)
        self.say("resume")
        self.say("skip")
        self.say("make it louder")
        keys = [a[1] for a in self.backend.actions if a[0] == "media_key"]
        self.assertEqual(keys, ["play_pause", "play_pause", "next", "volume_up"])

    def test_play_directly_and_latest(self):
        r = self.say("play the latest mrbeast video")
        self.assertIn("Playing", r.text)
        self.assertIn("sp=CAI", self.youtube.urls[-1])
        r = self.say("play arijit singh on youtube")
        self.assertTrue(self.urls_opened()[-1].startswith("https://www.youtube.com/watch?v="))

    def test_search_youtube_while_on_youtube_keeps_the_query_clean(self):
        self.say("open youtube")
        self.say("search youtube for python tutorial")
        self.assertIn("search_query=python+tutorial", self.youtube.urls[-1])
        self.say("search for jazz")  # the site-search follow-up still works
        self.assertIn("search_query=jazz", self.youtube.urls[-1])

    def test_search_failure_is_honest(self):
        self.youtube.fail = True
        r = self.say("search youtube for lofi music")
        self.assertIn("couldn't read the list", r.text)
        self.assertTrue(self.urls_opened()[-1].startswith("https://www.youtube.com/results?"))
        r = self.say("play lofi music on youtube")
        self.assertIn("click the one you want", r.text)
        self.assertNotIn("Playing", r.text)


# =====================================================================================
# Windows controls
# =====================================================================================
class _Capturer:
    name = "fake"

    def available(self):
        return True

    def capture(self):
        raise AssertionError("not used")

    def save(self, path):
        path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        return (1920, 1080)


class TestWindows(GenesisTestCase):
    def test_list_switch_minimize_desktop(self):
        r = self.say("what windows are open")
        self.assertIn("Visual Studio Code", r.text)
        r = self.say("switch to the second one")
        self.assertIn(("window", 102, "focus"), self.backend.actions)
        self.say("switch to notepad")
        r = self.say("minimize it")
        self.assertIn(("window", 103, "minimize"), self.backend.actions)
        self.say("show the desktop")
        self.assertIn(("minimize_all",), self.backend.actions)
        r = self.say("switch to photoshop")
        self.assertEqual(r.status, "failed")

    def test_screenshot_saved_locally(self):
        self.svc.screen = ScreenService(capturer=_Capturer())
        r = self.say("take a screenshot")
        self.assertEqual(tools_of(r), [("take_screenshot", "succeeded")])
        shots = list((self.home / "Pictures" / "Screenshots").glob("Genesis_*.png"))
        self.assertEqual(len(shots), 1)

    def test_power_actions_still_confirm(self):
        r = self.say("shut down the computer")
        self.assertEqual(r.status, "awaiting_confirmation")
        self.say("no")
        self.assertEqual(self.backend.names_of("shutdown"), [])


# =====================================================================================
# Calendar
# =====================================================================================
class TestCalendar(GenesisTestCase):
    connect_gmail = True

    def test_create_confirms_with_exact_time_then_list_move_cancel(self):
        r = self.say("put a meeting with Rahul tomorrow at 4")
        self.assertEqual(r.status, "awaiting_confirmation")
        self.assertIn("tomorrow at 4:00 PM", r.text)
        self.assertEqual(self.google.events, {})
        self.say("yes")
        ev = next(iter(self.google.events.values()))
        self.assertEqual(ev["summary"], "Meeting with Rahul")
        self.assertTrue(ev["start"]["dateTime"].startswith(f"{TOMORROW.isoformat()}T16:00"))
        r = self.say("what do I have tomorrow")
        self.assertIn("4:00 PM — Meeting with Rahul", r.text)
        r = self.say("move my 4 pm meeting to 5 pm")
        self.assertEqual(r.status, "awaiting_confirmation")
        self.say("yes")
        self.assertIn("T17:00", next(iter(self.google.events.values()))["start"]["dateTime"])
        self.assertIn("T17:30", next(iter(self.google.events.values()))["end"]["dateTime"])  # duration kept
        r = self.say("cancel the meeting tomorrow")
        self.assertEqual(r.status, "awaiting_confirmation")  # deleting always asks
        self.say("yes")
        self.assertEqual(self.google.events, {})

    def test_missing_time_is_asked_not_guessed(self):
        r = self.say("schedule a meeting with the team tomorrow")
        self.assertEqual(r.status, "awaiting_input")
        self.assertIn("What time", r.text)
        r = self.say("at 11 am")
        self.assertIn("11:00 AM", r.text)
        self.say("yes")
        self.assertEqual(next(iter(self.google.events.values()))["summary"], "Meeting with the team")
        r = self.say("set up a call with Priya")
        self.assertIn("When should I schedule", r.text)

    def test_ambiguous_event_asks_which(self):
        for h in (10, 15):
            self.svc.calendar.create_event("Meeting", datetime.combine(TOMORROW, datetime.min.time()).replace(hour=h))
        r = self.say("cancel the meeting tomorrow")
        self.assertEqual(r.status, "failed")
        self.assertIn("Which one", r.text)
        self.assertEqual(len(self.google.events), 2)

    def test_not_connected_degrades_gracefully(self):
        self.secrets.delete("google_oauth_token")
        r = self.say("what do I have tomorrow")
        self.assertEqual(r.status, "failed")
        self.assertIn("Connected services", r.text)


# =====================================================================================
# Gmail agent task
# =====================================================================================
class TestGmailAgent(GenesisTestCase):
    connect_gmail = True

    def setUp(self):
        self.llm = code_llm({})
        super().setUp()
        self.google.inbox = [dict(m) for m in INBOX] + [
            {"id": "m9", "thread_id": "t9", "from": "AMD Careers <jobs@amd.com>", "subject": "Interview invitation",
             "body": "We'd like to invite you to interview.", "unread": False, "attachments": ["JD.pdf"]}]

    def test_find_summarize_reply_in_one_request(self):
        r = self.say("find the latest email from AMD, summarize it and draft a reply saying I'm interested")
        self.assertEqual([t for t, _ in tools_of(r)],
                         ["gmail_search", "gmail_open_message", "llm_analyze", "compose_email", "gmail_create_draft"])
        self.assertIn("Attachments: JD.pdf", r.text)
        self.assertIn("interview", r.text.lower())
        msg = self.google.parsed(next(iter(self.google.drafts)))
        self.assertEqual((msg["To"], msg["Subject"]), ("jobs@amd.com", "Re: Interview invitation"))
        self.assertEqual(self.google.sent, [])  # drafting never sends
        r = self.say("send it")
        self.assertEqual(r.status, "awaiting_confirmation")

    def test_open_latest_needs_no_confirmation(self):
        r = self.say("find my latest email")
        self.assertEqual(r.status, "done")
        self.assertNotIn("Found", r.text)  # the list step is hidden; the opened e-mail is shown


# =====================================================================================
# Web answers
# =====================================================================================
class TestWebAnswers(GenesisTestCase):
    def test_duckduckgo_html_fallback_parser(self):
        class R:
            status_code = 200
            text = ('<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2F">Python '
                    '<b>docs</b></a><a class="result__snippet" href="x">Official <b>docs</b></a>')

        class S:
            def get(self, *a, **k):
                return R()

        self.assertEqual(web.fetch_results("python", 5, session=S()),
                         [{"title": "Python docs", "url": "https://docs.python.org/3/", "snippet": "Official docs"}])

    def test_answer_uses_results_or_says_it_used_its_own_knowledge(self):
        self.svc.llm.primary = code_llm({}, answer="Decorators wrap functions. See https://docs.python.org")
        self.svc.llm.primary.is_generative = True
        r = self.say("search the web and tell me what python decorators are")
        self.assertIn("Decorators wrap functions", r.text)
        self.assertIn("own knowledge", r.text)  # the fake web returns no results → says so honestly

    def test_error_question_without_an_error_asks(self):
        r = self.say("search the internet and tell me why this error is happening")
        self.assertIn("Which error", r.text)


# =====================================================================================
# Coding agent
# =====================================================================================
class TestCodingAgent(GenesisTestCase):
    def setUp(self):
        self.llm = code_llm({"fibonacci": FIB_BROKEN,
                             "divide": {"name": "divider", "entry": "main.py", "files": [{"path": "main.py", "content": "print(1/0)\n"}]},
                             "numpy": {"name": "np", "entry": "main.py",
                                       "files": [{"path": "main.py", "content": "import numpyy_not_installed\n"}]},
                             "clean": {"name": "cleaner", "entry": "main.py",
                                       "files": [{"path": "main.py", "content": "import shutil\nshutil.rmtree('old')\n"}]},
                             "server": {"name": "srv", "entry": "main.py", "files": [{"path": "main.py", "content":
                                        "import http.server\nhttp.server.HTTPServer(('127.0.0.1', 0), "
                                        "http.server.SimpleHTTPRequestHandler).serve_forever()\n"}]},
                             "secret": {"name": "env", "entry": "main.py", "files": [{"path": "main.py", "content":
                                        "import os\nprint('token=' + str(os.environ.get('HF_TOKEN')))\n"}]},
                             "page": {"name": "page", "entry": "index.html", "files": [
                                 {"path": "index.html", "content": "<html><link rel=stylesheet href=style.css><h1>Hi</h1></html>"},
                                 {"path": "style.css", "content": "h1{color:red}"}]}},
                            fixes=[FIB_FIX])
        super().setUp()

    def ws(self):
        return self.home / "GenesisProjects"

    def test_create_confirm_write_run_fix_rerun(self):
        r = self.say("create a python script that calculates fibonacci numbers")
        self.assertEqual(r.status, "awaiting_confirmation")
        self.assertFalse(self.ws().exists())  # nothing written before permission
        self.assertIn("fibonacci", r.text)
        r = self.say("yes")
        self.assertEqual([s for _, s in tools_of(r)][-1], "succeeded")
        self.assertIn("[0, 1, 1, 2, 3, 5, 8, 13, 21, 34]", r.text)
        self.assertIn("after 1 fix", r.text)
        self.assertIn("a + b", (self.ws() / "fibonacci" / "main.py").read_text())
        self.assertTrue(list((self.config.paths.data_dir / "backups").rglob("main.py")))  # original kept
        self.assertTrue(any(a[0] == "launch" and "Code.exe" in a[1][0] for a in self.backend.actions))  # opened in VS Code
        r = self.say("run it")
        self.assertEqual(r.status, "awaiting_confirmation")
        r = self.say("yes")
        self.assertIn("successfully", r.text)

    def test_gives_up_after_retry_limit_and_explains(self):
        self.svc.llm.primary = code_llm({"divide": {"name": "divider", "entry": "main.py",
                                                    "files": [{"path": "main.py", "content": "print(1/0)\n"}]}},
                                        fixes=[{"explanation": "tried", "files": [{"path": "main.py", "content": "print(2/0)\n"}]}])
        self.svc.llm.primary.is_generative = True
        self.say("create a python program that divides numbers")
        r = self.say("yes")
        self.assertNotEqual(r.status, "done")
        self.assertIn("ZeroDivisionError", r.text)
        self.assertIn("try again", r.text)
        self.assertIn("I tried 2 fixes", r.text)

    def test_error_context_feeds_web_answer(self):
        self.svc.llm.primary = code_llm({"divide": {"name": "d", "entry": "main.py", "files": [{"path": "main.py",
                                                                                                "content": "print(1/0)\n"}]}},
                                        answer="You divided by zero; check the divisor.")
        self.svc.llm.primary.is_generative = True
        self.say("create a python program that divides numbers")
        self.say("yes")
        r = self.say("search the internet and tell me why this error is happening")
        self.assertIn("divided by zero", r.text)
        self.assertIn("ZeroDivisionError", self.urls_opened()[-1].replace("+", " ").replace("%3A", ":"))

    def test_missing_package_is_reported_not_installed(self):
        self.say("create a python program that uses numpy")
        r = self.say("yes")
        self.assertIn("pip install numpyy_not_installed", r.text)
        self.assertNotIn("launch", [a[0] for a in self.backend.actions if "pip" in str(a)])

    def test_dangerous_code_needs_explicit_high_risk_yes(self):
        self.say("create a python script to clean old folders")
        r = self.say("yes")
        self.assertEqual(r.status, "awaiting_confirmation")
        self.assertIn("deletes folders recursively", r.text)
        self.assertEqual(r.confirmation["risk"], "high")
        r = self.say("no")
        self.assertIn("Cancelled", r.text)

    def test_server_is_started_then_stopped(self):
        self.say("create a python script that runs a small web server")
        r = self.say("yes")
        self.assertIn("is a server", r.text)
        self.assertIn("python -u main.py", r.text)

    def test_generated_code_cannot_read_genesis_secrets(self):
        os.environ["HF_TOKEN"] = "hf_supersecret_value"
        try:
            self.say("create a python script that prints a secret")
            r = self.say("yes")
        finally:
            os.environ.pop("HF_TOKEN", None)
        self.assertIn("token=None", r.text)
        self.assertNotIn("supersecret", r.text)

    def test_html_opens_in_browser(self):
        self.say("build a simple web page")
        r = self.say("yes")
        self.assertIn("Opened index.html in your browser", r.text)
        self.assertTrue(any(a[0] == "open_path" and a[1].endswith("index.html") for a in self.backend.actions))

    def test_preferred_language_is_used(self):
        self.say("my preferred coding language is javascript")
        self.say("create a program that prints hello")
        gen = [m for m in self.svc.llm.primary.calls if "small, complete, working" in m[-1]["content"]][-1]
        self.assertIn("working JavaScript program", gen[-1]["content"])
        self.say("forget that")
        self.assertIsNone(self.svc.memory.get_preference("coding.language"))

    def test_users_own_code_is_not_auto_fixed(self):
        proj = self.home / "Projects" / "mine"
        proj.mkdir(parents=True)
        (proj / "app.py").write_text("print(undefined_name)\n")
        self.svc.repos.projects.set("mine", str(proj))
        self.say(f"run {proj / 'app.py'}")
        r = self.say("yes")
        self.assertIn("NameError", r.text)
        self.assertIn("fix it", r.text)
        self.assertEqual((proj / "app.py").read_text(), "print(undefined_name)\n")  # untouched

    def test_offline_llm_is_honest(self):
        self.svc.llm.primary.is_generative = False
        r = self.say("create a python script that calculates fibonacci numbers")
        self.assertIn("needs the language model", r.text)
        self.assertFalse(self.ws().exists())


class TestCodeRunnerUnits(unittest.TestCase):
    def test_file_validation(self):
        for bad in ("../evil.py", "C:/Windows/x.py", "/etc/passwd", "a/../../b.py", ".genesis-project"):
            with self.assertRaises(ValidationError):
                _clean_files([{"path": bad, "content": "x"}], "")
        files, entry = _clean_files([{"path": "src/main.py", "content": "print(1)"}], "src/main.py")
        self.assertEqual(entry, "src/main.py")

    def test_safety_scan(self):
        self.assertEqual(safety_scan({"a.py": "print('hi')"}), [])
        found = safety_scan({"a.py": "import subprocess, shutil\nshutil.rmtree(p)\nsubprocess.run(['x'])"})
        self.assertTrue(any("recursively" in f for f in found) and any("programs" in f for f in found))
        self.assertTrue(safety_scan({"a.js": "require('child_process').exec('del /s x')"}))

    def test_language_registry(self):
        for name, key in (("py", "python"), ("main.js", "javascript"), ("TypeScript", "typescript"), ("Main.java", "java"),
                          ("index.html", "html"), ("css", "html")):
            self.assertEqual(language_for(name).key, key, name)
        self.assertIsNone(language_for("cobol"))

    def test_timeout_and_no_shell(self):
        import sys
        import tempfile
        from pathlib import Path

        d = Path(tempfile.mkdtemp())
        (d / "loop.py").write_text("while True: pass\n")
        res = CodeRunner().run([sys.executable, str(d / "loop.py")], d, timeout=1.5)
        self.assertTrue(res.timed_out)
        with self.assertRaises(ValueError):
            CodeRunner().run("python loop.py", d)  # strings (shell commands) are refused

    @unittest.skipUnless(shutil.which("node"), "Node.js not installed")
    def test_javascript_runs(self):
        import tempfile
        from pathlib import Path

        d = Path(tempfile.mkdtemp())
        (d / "main.js").write_text("console.log([3,1,2].sort().join(','))\n")
        lang = language_for("main.js")
        res = CodeRunner().run(lang.command(d / "main.js"), d)
        self.assertEqual(res.stdout.strip(), "1,2,3")

    @unittest.skipUnless(shutil.which("java"), "Java not installed")
    def test_java_runs(self):
        import tempfile
        from pathlib import Path

        d = Path(tempfile.mkdtemp())
        (d / "Main.java").write_text('public class Main { public static void main(String[] a) { System.out.println("hi"); } }\n')
        res = CodeRunner().run(language_for("java").command(d / "Main.java"), d, timeout=60)
        self.assertEqual(res.stdout.strip(), "hi")


# =====================================================================================
# Memory: preferences + task history
# =====================================================================================
class TestMemoryAndHistory(GenesisTestCase):
    def test_find_what_we_talked_about(self):
        self.say("play python tutorial on youtube")
        self.say("open notepad")
        r = self.say("find that python tutorial we were talking about today")
        self.assertEqual(tools_of(r), [("search_history", "succeeded")])
        self.assertIn("Python Tutorial for Beginners", r.text)
        r = self.say("open the first one")
        self.assertEqual(tools_of(r), [("youtube_play", "succeeded")])
        r = self.say("find that rust book we talked about yesterday")
        self.assertIn("couldn't find anything", r.text)

    def test_preference_persists(self):
        self.say("my preferred coding language is java")
        self.assertEqual(self.svc.memory.get_preference("coding.language"), "java")
        r = self.say("what do you remember")
        self.assertIn("preferred coding language: Java", r.text)


# =====================================================================================
# Permissions, planning safety, task states
# =====================================================================================
class TestAgentSafety(GenesisTestCase):
    def test_llm_cannot_plan_destructive_or_code_execution_tools(self):
        for t in ("delete_calendar_event", "run_program", "write_code_files", "gmail_send_draft", "shutdown_computer"):
            self.assertIn(t, LLM_EXCLUDED_TOOLS)
        catalog = self.core.registry.catalog_for_llm(exclude=LLM_EXCLUDED_TOOLS)
        self.assertIn("youtube_play", catalog)
        self.assertNotIn("run_program", catalog)

    def test_trusted_input_exemption_is_narrow(self):
        (self.home / "Documents" / "a.txt").write_text("hello")
        plan = Plan("x")
        plan.add("read_file", {"path": "a.txt"}, "Read")
        plan.add("open_url", {"url": "https://example.com"}, "Open", trusted_input=True)
        plan.source = "llm"  # LLM-made plans never get the exemption
        out = self.core.executor.run(plan)
        self.assertEqual(out.status, "awaiting_confirmation")

    def test_new_tools_registered_with_schemas(self):
        names = self.core.registry.names()
        for t in ("youtube_search", "youtube_play", "media_control", "list_windows", "manage_window", "show_desktop",
                  "take_screenshot", "create_calendar_event", "list_calendar_events", "find_calendar_event",
                  "update_calendar_event", "delete_calendar_event", "generate_code", "write_code_files", "run_program",
                  "search_history", "remember_preference"):
            self.assertIn(t, names)
            self.assertEqual(self.core.registry.get(t).parameters["type"], "object")
        self.assertEqual(self.core.registry.get("delete_calendar_event").always_confirm, True)

    def test_planning_and_retry_states_emitted(self):
        states = []
        self.svc.events.subscribe(lambda e: states.append(e.payload.get("state")), EventType.STATE_CHANGED)
        self.say("open notepad")
        self.assertIn(AssistantState.PLANNING.value, states)


class TestLLMUnderstanding(GenesisTestCase):
    def setUp(self):
        def reply(messages, json_mode):
            last = messages[-1]["content"]
            if "natural-language-understanding" in last:
                return json.dumps({"intent": "youtube", "confidence": 0.9, "entities": {"youtube_op": "play", "query": "lofi beats"}})
            if "planning component" in last:  # 1.4.1: unfamiliar instructions go straight to the planner (one model call)
                return json.dumps({"steps": [{"tool": "youtube_play", "args": {"query": "lofi beats"},
                                              "description": "Play lofi beats"}]})
            return "{}" if json_mode else "ok"

        self.llm = ScriptedLLM(reply)
        super().setUp()

    def test_loose_phrasing_goes_through_llm_to_the_youtube_tool(self):
        r = self.say("could you maybe put something chill like lofi beats on for me")
        self.assertEqual(tools_of(r), [("youtube_play", "succeeded")])
        self.assertIn("lofi+beats", self.youtube.urls[-1])

    def test_malformed_llm_output_does_not_crash(self):
        self.svc.llm.primary.reply = lambda m, j: "not json at all {"
        r = self.say("could you maybe put something chill on for me")
        self.assertIn(r.status, ("chat", "done", "failed", "awaiting_input"))


# =====================================================================================
# The spec's end-to-end scenarios (in one continuous conversation)
# =====================================================================================
class TestSpecScenarios(GenesisTestCase):
    connect_gmail = True

    def setUp(self):
        self.llm = code_llm({"python": {"name": "hello", "entry": "main.py", "files": [{"path": "main.py",
                                                                                        "content": "print('hello from genesis')\n"}]}})
        super().setUp()
        self.google.inbox = [dict(m) for m in INBOX]

    def test_twelve_scenarios(self):
        c = self.core.session
        self.assertTrue(c.sleeping)
        # 1-3 YouTube
        self.assertIn("YouTube is open", self.say("Open YouTube.").text)
        self.assertTrue(c.active)  # entered conversation mode
        self.assertIn("Python Tutorial for Beginners", self.say("Search YouTube for Python tutorials.").text)
        self.assertIn("Playing", self.say("Play the first result.").text)
        # 4 web
        r = self.say("Search the web for Python decorators.")
        self.assertIn("python+decorators", self.urls_opened()[-1].lower())
        # 5-7 Gmail
        self.assertIn("Gmail is open", self.say("Open Gmail.").text)
        self.assertIn("Assignment deadline", self.say("Find my latest email.").text)
        r = self.say("Draft a reply.")
        self.assertEqual(r.status, "awaiting_input")  # asks what to say instead of inventing it
        r = self.say("I'll send it tonight")
        self.assertEqual(len(self.google.drafts), 1)
        # 8 calendar
        self.say("Create a calendar event called project review tomorrow at 3 pm")
        self.say("yes")
        self.assertEqual(len(self.google.events), 1)
        # 9 create and run a python script
        self.say("Create and run a Python script that prints hello")
        r = self.say("yes")
        self.assertIn("hello from genesis", r.text)
        # 10 VS Code + file
        r = self.say("Open VS Code and create a file called notes.txt")
        if r.status == "awaiting_confirmation":
            r = self.say("yes")
        self.assertIn("notes.txt", r.text)
        # 11 normal conversation
        r = self.say("How are you today?")
        self.assertEqual(r.status, "chat")
        # 12 leaves conversation mode
        c._idle_since -= 3600
        self.assertTrue(c.check_timeout())
        self.assertTrue(c.sleeping)


if __name__ == "__main__":
    unittest.main()
