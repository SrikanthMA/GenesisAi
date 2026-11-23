"""Genesis 1.3 — intelligence upgrade: identity, corrections, undo, context engine, preferences & instructions, goal
planning with re-planning, the universal browser layer, screen elements, project-level code changes, safe developer
commands, tasks & notes, caching. All against fakes; code changes and commands are really executed on temp projects.
"""
from __future__ import annotations

import json
import unittest

from genesis.core.events import AssistantState
from genesis.memory.identity import ACCEPT, CONFIRM, phonetic_key, similarity
from genesis.nlp import rules
from genesis.nlp.intents import Intent
from genesis.security.validators import ValidationError
from genesis.services.screen import ScreenService
from genesis.tools.code_changes import parse_command
from genesis.tools.web import rank_elements
from genesis.voice.wakeword import matches_wake_phrase

from .helpers import GenesisTestCase, ScriptedLLM
from .test_conversation import FakeCapturer, FakeElements, FakeOcr, tools_of

NAME = "Srikanth Mandyam Manadhare"


class FakeBrowser:
    """Stands in for the Playwright controller: a page with a few named elements."""

    ELEMENTS = [{"id": 1, "role": "heading", "name": "AI Internships 2026", "href": "", "in_view": True},
                {"id": 2, "role": "button", "name": "Sign in", "href": "", "in_view": True},
                {"id": 3, "role": "link", "name": "Apply for AI Research Internship", "href": "/apply", "in_view": True},
                {"id": 4, "role": "button", "name": "Search", "href": "", "in_view": True},
                {"id": 5, "role": "field", "name": "Search jobs", "href": "", "in_view": False}]

    def __init__(self):
        self.url = None
        self.clicked: list = []
        self.keys: list = []
        self.scrolls: list = []

    @staticmethod
    def available():
        return True

    def current_url(self):
        return self.url

    def goto(self, url):
        self.url = url
        return {"url": url, "title": "Internships"}

    def read(self):
        return {"url": self.url, "title": "Internships", "text": "AI internships at many companies. " * 5}

    def elements(self, limit=80):
        return {"url": self.url, "title": "Internships", "elements": [dict(e) for e in self.ELEMENTS]}

    def click_element(self, element_id):
        self.clicked.append(element_id)
        return {"url": self.url + "#clicked", "title": "Clicked"}

    def click(self, target):
        self.clicked.append(target)
        return {"clicked": target, "url": self.url}

    def scroll(self, direction="down", amount=1):
        self.scrolls.append(direction)
        return {"url": self.url, "scroll_y": 700, "at_bottom": direction == "bottom"}

    def press(self, key):
        self.keys.append(key)
        return {"url": self.url, "title": "Internships"}

    def go_back(self):
        return {"url": self.url, "title": "Back"}

    def close(self):
        pass


# =====================================================================================
# 1. Name recognition / identity
# =====================================================================================
class TestIdentityMatching(unittest.TestCase):
    def test_phonetic_variants_match(self):
        for heard in ("Srikant", "Shri Kant", "Shreekanth", "Sri Kanth", "Sreekant"):
            self.assertGreaterEqual(similarity(heard, "Srikanth"), ACCEPT, heard)
        self.assertEqual(phonetic_key("Shri Kant"), phonetic_key("Srikanth"))
        for other in ("Rahul", "Shekhar", "Sri Ram", "Sri"):
            self.assertLess(similarity(other, "Srikanth"), CONFIRM, other)

    def test_generic_for_any_name(self):
        self.assertGreaterEqual(similarity("Priyanka", "Priyanca"), CONFIRM)
        self.assertGreaterEqual(similarity("Mohammed", "Mohammad"), CONFIRM)  # close enough to ask once
        self.assertGreaterEqual(similarity("Jonathon", "Jonathan"), CONFIRM)


class TestIdentity(GenesisTestCase):
    def test_set_name_call_me_and_stt_hint(self):
        r = self.say(f"my name is {NAME}")
        self.assertIn("Nice to meet you, Srikanth", r.text)
        self.say("call me Sri")
        ident = self.svc.identity.get()
        self.assertEqual((ident.canonical, ident.preferred), (NAME, "Sri"))
        self.assertEqual(self.svc.settings.get("user.name"), NAME)  # e-mail signatures keep working
        self.assertIn("Srikanth Mandyam Manadhare", self.svc.settings.get("voice.stt_hint"))
        self.assertIn("Sri", self.say("what's my name").text)

    def test_voice_variants_recognised_confirmed_once_and_learned(self):
        self.say(f"my name is {NAME}")
        r = self.core.handle("hi genesis, this is Shri Kant", source="voice")
        self.assertIn("Hi Srikanth", r.text)  # confident → no question
        self.assertIn("Shri Kant", self.svc.identity.get().variants)
        r = self.core.handle("it's Srikanth Mandim here", source="voice")
        self.assertIn("Hi", r.text)
        r = self.core.handle("this is Srikan here", source="voice")
        if "did you mean" in r.text:  # uncertain → asks exactly once
            r = self.core.handle("yes", source="voice")
            self.assertIn("I'll remember", r.text)
        self.assertIn("Hi", self.core.handle("this is Srikan here", source="voice").text)  # learned: no second question

    def test_rejection_is_remembered(self):
        self.say(f"my name is {NAME}")
        self.core.handle("I'm Rahul", source="voice")
        r = self.core.handle("no", source="voice")
        self.assertIn("won't change", r.text)
        self.assertEqual(self.svc.identity.get().canonical, NAME)

    def test_spelling_fix_changes_only_that_part(self):
        self.say(f"my name is {NAME}")
        r = self.say("it's spelled Shrikanth")
        self.assertEqual(self.svc.identity.get().canonical, "Shrikanth Mandyam Manadhare")
        self.assertIn("Shrikanth", r.text)

    def test_other_users_and_forgetting(self):
        self.say("my name is Priya Sharma")
        self.assertEqual(self.svc.identity.get().call_name, "Priya")
        self.say("forget my name")
        self.assertEqual(self.svc.identity.get().canonical, "")

    def test_greeting_uses_name_on_wake(self):
        self.say(f"my name is {NAME}")
        r = self.core.handle("hey genesis", source="voice")
        self.assertTrue(r.text.startswith("Yes, Srikanth?"))

    def test_im_tired_is_not_a_name(self):
        self.assertIsNone(rules.parse("i'm tired"))
        self.assertNotEqual(getattr(rules.parse("I'm going out"), "intent", None), Intent.IDENTITY)


# =====================================================================================
# 2-5. Wake word, conversation, follow-ups, corrections, context, undo
# =====================================================================================
class TestConversationIntelligence(GenesisTestCase):
    def test_custom_wake_phrase(self):
        self.assertTrue(matches_wake_phrase("okay jarvis open chrome", phrase="okay jarvis"))
        self.assertFalse(matches_wake_phrase("open chrome", phrase="okay jarvis"))

    def test_open_search_correction_in_one_session(self):
        self.say("Open Chrome.")
        sid = self.core.session.session.id
        r = self.say("Search for internships.")
        self.assertEqual(tools_of(r), [("web_search", "succeeded")])
        r = self.say("No, I meant AI internships.")
        self.assertEqual(r.plans[0]["steps"][0]["args"]["query"], "AI internships")
        self.assertEqual(self.core.session.session.id, sid)  # no wake word needed, same conversation

    def test_youtube_second_one_then_play_it(self):
        self.say("Open YouTube.")
        self.say("Search for Python tutorials.")
        r = self.say("The second one.")
        self.assertIn("Python Full Course", r.text)
        r = self.say("Play it.")
        self.assertEqual(tools_of(r), [("youtube_play", "succeeded")])
        self.assertTrue(self.urls_opened()[-1].endswith("bbbbbbbbbb2"))

    def test_correction_replaces_instead_of_duplicating(self):
        self.say("remind me tomorrow at 9 to call mom")
        self.say("actually make it 10 am")
        pending = self.svc.repos.reminders.pending()
        self.assertEqual(len(pending), 1)
        self.assertIn("T", pending[0]["due_at"])
        r = self.say("no, spotify")  # a correction that is a whole command
        self.assertIn("Spotify", r.text)

    def test_undo_chain(self):
        self.say("create a file called test.txt")
        self.say("yes")
        self.assertTrue((self.home / "Documents" / "test.txt").exists())
        r = self.say("undo that")
        self.assertEqual(r.status, "awaiting_confirmation")  # undo still goes through permissions
        self.say("yes")
        self.assertFalse((self.home / "Documents" / "test.txt").exists())
        self.say("remind me in 10 minutes to stretch")
        self.say("undo that")
        self.assertEqual(self.svc.repos.reminders.pending(), [])
        r = self.say("undo that")
        self.assertIn("Undo what", r.text)

    def test_irreversible_actions_say_so(self):
        self.connect_google()
        self.say("draft an email to rahul@example.com saying hi")
        self.say("send it")
        self.say("yes")
        r = self.say("undo that")
        self.assertIn("can't unsend", r.text)

    def test_again_and_same_thing(self):
        self.say("open notepad")
        n = len(self.launched())
        self.say("do it again")
        self.assertEqual(len(self.launched()), n + 1)

    def test_context_snapshot_is_compact(self):
        self.say("open youtube")
        self.say("search for python tutorials")
        snap = self.core.context.snapshot(self.core.dialogue.state(self.core.conversation_id)).to_prompt()
        self.assertIn("Current app: youtube", snap)
        self.assertIn("Results on screen: 1) video: Python Tutorial for Beginners", snap)
        self.assertLess(len(snap), 2000)


# =====================================================================================
# 6. Long-term memory: preferences, instructions, explainability
# =====================================================================================
class TestPreferencesAndInstructions(GenesisTestCase):
    def test_prefer_gmail_then_open_my_email(self):
        self.say("remember that I prefer Outlook")
        r = self.say("open my email")
        self.assertEqual(r.plans[0]["steps"][0]["args"]["app"], "outlook")
        self.assertIn("web version", r.text)  # Outlook isn't installed → recovered with the web version
        self.assertEqual(r.status, "done")
        r = self.say("why did you open outlook")
        self.assertIn("you told me you prefer Outlook", r.text)

    def test_browser_and_search_preferences(self):
        self.say("my default browser is chrome")
        r = self.say("open my browser")
        self.assertEqual(r.plans[0]["steps"][0]["args"]["app"], "chrome")
        self.say("use bing for search")
        self.assertEqual(self.svc.settings.get("search.engine"), "bing")

    def test_instructions_are_stored_and_reach_the_llm(self):
        self.say("from now on always keep emails under 100 words")
        facts = self.svc.repos.memory.list(kind="instruction")
        self.assertEqual(facts[0]["content"], "Always keep emails under 100 words")
        snap = self.core.context.snapshot(self.core.dialogue.state(self.core.conversation_id)).to_prompt()
        self.assertIn("Always keep emails under 100 words", snap)
        r = self.say("what do you remember")
        self.assertIn("Instructions:", r.text)

    def test_nothing_is_stored_automatically(self):
        self.say("open notepad")
        self.say("what time is it")
        self.assertEqual(self.svc.repos.memory.list(), [])
        self.assertEqual(self.svc.repos.preferences.all().get("pref.email"), None)


# =====================================================================================
# Goals and re-planning (tasks that aren't predefined commands)
# =====================================================================================
class TestGoalsAndReplanning(GenesisTestCase):
    def setUp(self):
        self.llm_calls = []

        def reply(messages, json_mode):
            last = messages[-1]["content"]
            self.llm_calls.append(last)
            if "natural-language-understanding" in last:
                return json.dumps({"intent": "unknown", "confidence": 0.3, "entities": {}})
            if "planning component" in last and "previous plan for the user's request failed" in last:
                return json.dumps({"steps": [{"tool": "open_folder", "args": {"folder": "downloads"}, "description": "Open Downloads"}],
                                   "reason": "the Downloads folder is where new files land"})
            if "planning component" in last and "User request: prepare my computer for coding" in last:
                return json.dumps({"steps": [{"tool": "open_application", "args": {"app": "vscode"}, "description": "Open VS Code"},
                                             {"tool": "open_terminal", "args": {}, "description": "Open a terminal"}],
                                   "reason": "coding needs the editor and a terminal"})
            if "planning component" in last:
                return json.dumps({"steps": [{"tool": "open_application", "args": {"app": "flurbometer"}, "description": "Open it"}]})
            return "{}" if json_mode else "ok"

        self.llm = ScriptedLLM(reply)
        super().setUp()

    def test_goal_planned_by_llm(self):
        r = self.say("prepare my computer for coding")
        self.assertEqual([t for t, _ in tools_of(r)], ["open_application", "open_terminal"])
        self.assertEqual(r.status, "done")

    def test_unseen_request_replans_once_after_failure(self):
        r = self.say("go ahead and get my newest stuff sorted out")
        tools = [t for t, _ in tools_of(r)]
        self.assertIn("open_application", tools)
        self.assertIn("open_folder", tools)
        self.assertIn("tried another way", r.text)
        replans = [c for c in self.llm_calls if "previous plan for the user's request failed" in c]
        self.assertEqual(len(replans), 1)  # never loops

    def test_goal_uses_matching_routine_first(self):
        self.svc.repos.routines.add("coding setup", "open notepad", [], None)
        r = self.say("prepare my computer for coding")
        self.assertIn("coding setup", r.text)

    def test_offline_goal_fallback(self):
        self.svc.llm.primary.is_generative = False
        r = self.say("get my computer ready for coding")
        self.assertIn(("open_terminal", "succeeded"), tools_of(r))
        r = self.say("prepare my computer for gardening")
        self.assertIn("language model", r.text)

    def test_research_list_goal(self):
        r = self.say("Find five AI internships that I can apply for")
        steps = r.plans[0]["steps"]
        self.assertEqual(steps[0]["tool"], "web_search")
        self.assertEqual(steps[0]["args"]["read_pages"], 3)
        self.assertEqual(steps[1]["tool"], "llm_analyze")


# =====================================================================================
# 7-8. Browser layer and screen elements
# =====================================================================================
class TestBrowserAgent(GenesisTestCase):
    def setUp(self):
        super().setUp()
        self.fb = FakeBrowser()
        self.svc.browser = self.fb
        self.fb.goto("https://jobs.example.com/internships")

    def test_rank_elements(self):
        best = rank_elements(FakeBrowser.ELEMENTS, "the internship application button")[0][1]
        self.assertEqual(best["id"], 3)

    def test_see_find_click_scroll_press(self):
        r = self.say("what's on this page")
        self.assertIn("Apply for AI Research Internship", r.text)
        r = self.say("find the internship application button")
        self.assertIn("Apply for AI Research Internship", r.text)
        r = self.say("click it")
        self.assertEqual(r.status, "awaiting_confirmation")  # clicking can submit things → asks
        self.say("yes")
        self.assertEqual(self.fb.clicked, [3])
        self.say("click the second button")
        self.say("yes")
        self.assertEqual(self.fb.clicked[-1], 4)  # buttons: Sign in, Search
        self.say("scroll down")
        self.assertEqual(self.fb.scrolls, ["down"])
        r = self.say("press enter")
        self.assertEqual(r.status, "awaiting_confirmation")
        self.say("yes")
        self.assertEqual(self.fb.keys, ["Enter"])

    def test_list_then_ordinal_click(self):
        self.say("what can i click")
        self.say("click the second one")
        self.say("yes")
        self.assertEqual(self.fb.clicked, [3])  # items listed (no headings): Sign in, Apply…, Search, Search jobs

    def test_summarize_this_page(self):
        r = self.say("summarize this page")
        self.assertEqual(r.plans[0]["steps"][0]["tool"], "browser_read_page")


class TestScreenElements(GenesisTestCase):
    def setUp(self):
        super().setUp()
        self.els = FakeElements()
        self.svc.screen = ScreenService(capturer=FakeCapturer(), ocr=FakeOcr(), elements=self.els)

    def test_find_and_click_nth_button_by_name(self):
        r = self.say("find the save button")
        self.assertIn("Save", r.text)
        self.say("click it")
        self.say("yes")
        self.assertEqual(self.els.clicked, ["Save"])
        r = self.say("click the second button")
        self.assertEqual(r.status, "awaiting_confirmation")
        self.say("yes")
        self.assertEqual(self.els.clicked[-1], "Cancel")  # "Run" is disabled, so it isn't counted

    def test_what_does_this_error_mean(self):
        r = self.say("what does this error mean")
        self.assertIn("Cannot find module", r.text)


# =====================================================================================
# 12. Coding agent: project-level change + safe commands
# =====================================================================================
class TestProjectChanges(GenesisTestCase):
    def setUp(self):
        def reply(messages, json_mode):
            last = messages[-1]["content"]
            if "You are editing an existing project" in last:
                if "break it" in last:
                    return json.dumps({"summary": "x", "edits": [{"path": "style.css", "find": "nothing-like-this", "replace": "y"}]})
                return json.dumps({"summary": "Added a dark theme and a toggle button.",
                                   "edits": [{"path": "style.css", "find": "body { color: black; background: white; }",
                                              "replace": "body { color: black; background: white; }\nbody.dark { color: #eee; background: #111; }"},
                                             {"path": "index.html", "find": "<h1>Site</h1>",
                                              "replace": "<h1>Site</h1><button id=\"theme\">Dark mode</button><script src=\"theme.js\"></script>"}],
                                   "new_files": [{"path": "theme.js", "content": "document.getElementById('theme').onclick = () => document.body.classList.toggle('dark');\n"}]})
            return "{}" if json_mode else "ok"

        self.llm = ScriptedLLM(reply)
        super().setUp()
        self.proj = self.home / "Projects" / "site"
        self.proj.mkdir(parents=True)
        (self.proj / "index.html").write_text("<html><link rel=stylesheet href=style.css><h1>Site</h1></html>\n")
        (self.proj / "style.css").write_text("body { color: black; background: white; }\n")
        self.svc.repos.projects.set("site", str(self.proj))

    def test_plan_show_diff_confirm_apply_undo(self):
        r = self.say("add dark mode to my site project")
        self.assertEqual(r.status, "awaiting_confirmation")
        self.assertIn("style.css", r.text)
        self.assertIn("theme.js (new)", r.text)
        self.assertIn("+body.dark", r.text)  # the user sees what will change
        self.assertNotIn("dark", (self.proj / "style.css").read_text())  # nothing changed yet
        r = self.say("yes")
        self.assertIn("changed 3 files", r.text)
        self.assertIn("body.dark", (self.proj / "style.css").read_text())
        self.assertTrue((self.proj / "theme.js").exists())
        r = self.say("undo that")
        self.say("yes")
        self.assertNotIn("dark", (self.proj / "style.css").read_text())
        self.assertFalse((self.proj / "theme.js").exists())

    def test_invalid_edit_changes_nothing(self):
        r = self.say("add a way to break it to my site project")
        self.assertIn("didn't match", r.text)
        self.assertEqual((self.proj / "style.css").read_text(), "body { color: black; background: white; }\n")

    def test_allowlisted_command_runs_real_tests(self):
        (self.proj / "tests").mkdir()
        (self.proj / "tests" / "__init__.py").write_text("")
        (self.proj / "tests" / "test_ok.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_x(self):\n"
                                                         "        self.assertTrue(True)\n")
        r = self.say("run python -m unittest in my site project")
        self.assertEqual(r.status, "awaiting_confirmation")
        r = self.say("yes")
        self.assertIn("finished successfully", r.text)


class TestCommandValidation(unittest.TestCase):
    def test_allowlist(self):
        self.assertEqual(parse_command("git status"), (["git", "status"], "low"))
        self.assertEqual(parse_command("npm test")[1], "medium")
        self.assertEqual(parse_command("npm install lodash")[1], "high")
        self.assertEqual(parse_command("python -m pip install -r requirements.txt")[1], "high")
        self.assertEqual(parse_command("git push")[1], "high")
        for bad in ("git status && rm -rf /", "npm test | tee x", "python -c 'import os'", "curl http://x", "rm -rf build",
                    "powershell Remove-Item x", "format c:", "echo $HOME", "notepad"):
            with self.assertRaises(ValidationError, msg=bad):
                parse_command(bad)


# =====================================================================================
# 13-15. Recovery, verification, permissions
# =====================================================================================
class TestRecoveryVerificationPermissions(GenesisTestCase):
    def test_missing_app_uses_web_version(self):
        r = self.say("open whatsapp")
        self.assertEqual(r.status, "done")
        self.assertEqual(self.urls_opened()[-1], "https://web.whatsapp.com/")
        r = self.say("open flurbometer")
        self.assertEqual(r.status, "failed")
        self.assertIn("search the web for how to install", r.text)

    def test_new_tool_risk_levels(self):
        reg = self.core.registry
        self.assertTrue(reg.get("apply_code_change").always_confirm)
        self.assertEqual(reg.get("run_project_command").risk_for({"command": "git status", "path": "."}).label, "low")
        self.assertEqual(reg.get("run_project_command").risk_for({"command": "npm install x", "path": "."}).label, "high")
        self.assertEqual(reg.get("browser_press").risk.label, "medium")
        from genesis.agent.planner import LLM_EXCLUDED_TOOLS

        for t in ("apply_code_change", "run_project_command", "browser_press", "delete_task", "set_identity"):
            self.assertIn(t, LLM_EXCLUDED_TOOLS)

    def test_planning_state_and_llm_cache(self):
        states = []
        self.svc.events.subscribe(lambda e: states.append(e.payload.get("state")))
        self.say("open notepad")
        self.assertIn(AssistantState.PLANNING.value, states)
        llm = self.svc.llm
        llm.primary = ScriptedLLM("{\"a\": 1}")
        llm.primary.is_generative = True
        llm.chat_json([{"role": "user", "content": "same question"}])
        llm.chat_json([{"role": "user", "content": "same question"}])
        self.assertEqual(len(llm.primary.calls), 1)
        self.assertEqual(llm.cache_hits, 1)

    def test_youtube_results_cached_between_search_and_play(self):
        self.say("search youtube for jazz")
        n = len(self.youtube.urls)
        self.say("play jazz on youtube")
        self.assertEqual(len(self.youtube.urls), n)


# =====================================================================================
# Tasks & notes
# =====================================================================================
class TestTasksAndNotes(GenesisTestCase):
    def test_todo_flow(self):
        self.say("add buy milk to my to-do list")
        self.say("add a task to submit the application by friday")
        r = self.say("what's on my to-do list")
        self.assertIn("buy milk", r.text)
        self.assertIn("due", r.text)
        self.say("mark buy milk as done")
        self.assertEqual([t["text"] for t in self.svc.repos.todos.list()], ["submit the application"])
        self.say("add call the bank to my to-do list")
        self.say("undo that")
        self.say("yes")
        self.assertNotIn("call the bank", [t["text"] for t in self.svc.repos.todos.list()])

    def test_notes_flow(self):
        self.say("take a note: the wifi password is on the router")
        self.say("note that Rahul prefers morning meetings")
        r = self.say("what did I note about rahul")
        self.assertIn("morning meetings", r.text)
        r = self.say("show my notes")
        self.assertIn("wifi", r.text)


if __name__ == "__main__":
    unittest.main()
