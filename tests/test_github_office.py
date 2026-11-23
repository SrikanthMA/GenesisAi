"""1.5: GitHub (open / list / create / delete repositories) and Microsoft Office (create + write into open documents),
plus the Colab one-shot "write … and run it" and extra picture phrasings.

GitHub's REST API and Office's COM interface are faked; the fakes record what Genesis actually did, so the tests check
behaviour (what was deleted, what was written, what needed confirmation) — not canned text.
"""
from __future__ import annotations

import json
from pathlib import Path

from genesis.nlp import rules
from genesis.nlp.intents import Intent
from genesis.services.colab import FakeColabDriver
from genesis.services.github import GitHubService
from genesis.services.office import FakeLiveOffice, check_file, safe_cell

from .helpers import FakeResponse, GenesisTestCase, ScriptedLLM

TOKEN = "ghp_TESTTOKEN0123456789abcdefghijklmnop"


def tools_of(resp):
    return [(s["tool"], s["status"]) for p in resp.plans for s in p["steps"]]


class FakeGitHubAPI:
    def __init__(self):
        self.repos = {"genesis": False, "demo": True, "old-notes": True}  # name -> private
        self.calls = []

    def request(self, method, url, headers=None, **kw):
        assert headers and headers.get("Authorization") == f"Bearer {TOKEN}"
        path = url.replace("https://api.github.com", "")
        self.calls.append((method, path))
        if method == "GET" and path == "/user":
            return FakeResponse(200, {"login": "srika"})
        if method == "GET" and path == "/user/repos":
            return FakeResponse(200, [{"name": n, "full_name": f"srika/{n}", "private": p, "html_url": f"https://github.com/srika/{n}",
                                       "description": "", "pushed_at": "2026-09-01T00:00:00Z"} for n, p in self.repos.items()])
        if method == "POST" and path == "/user/repos":
            body = kw["json"]
            if body["name"] in self.repos:
                return FakeResponse(422, {"message": "Validation Failed", "errors": [{"message": "name already exists"}]})
            self.repos[body["name"]] = body["private"]
            return FakeResponse(201, {"name": body["name"], "full_name": f"srika/{body['name']}", "private": body["private"],
                                      "html_url": f"https://github.com/srika/{body['name']}"})
        if path.startswith("/repos/srika/"):
            name = path.split("/")[-1]
            if name not in self.repos:
                return FakeResponse(404, {"message": "Not Found"})
            if method == "DELETE":
                del self.repos[name]
                return FakeResponse(204, None)
            return FakeResponse(200, {"name": name, "full_name": f"srika/{name}"})
        return FakeResponse(404, {"message": "Not Found"})


class GitHubCase(GenesisTestCase):
    def setUp(self):
        self.api = FakeGitHubAPI()
        self.github_service = GitHubService(session=self.api, token=TOKEN)
        super().setUp()


class TestGitHub(GitHubCase):
    def test_open_github_and_a_repo(self):
        r = self.say("open github")
        self.assertEqual(tools_of(r), [("github_open", "succeeded")])
        self.assertIn("https://github.com/", self.backend.actions[-1][1])
        r = self.say("open the genesis repo on github")
        self.assertEqual(self.backend.actions[-1], ("open_url", "https://github.com/srika/genesis"))

    def test_list_repositories(self):
        r = self.say("show my github repos")
        self.assertIn("genesis", r.text)
        self.assertIn("demo (private)", r.text)

    def test_create_repo_asks_then_creates_and_verifies(self):
        r = self.say("create a new private github repo called demo-app")
        self.assertEqual(r.status, "awaiting_confirmation")
        self.assertNotIn("demo-app", self.api.repos)
        r = self.say("yes")
        self.assertIn("demo-app", self.api.repos)
        step = [s for p in r.plans for s in p["steps"] if s["tool"] == "github_create_repo"][0]
        self.assertTrue(step["verified"])

    def test_delete_needs_the_typed_name_and_a_yes(self):
        r = self.say("delete my github repo demo")
        self.assertEqual(r.status, "awaiting_input")
        self.assertIn("type or say the repository's name", r.text)
        self.assertIn("demo", self.api.repos)
        r = self.say("demo")
        self.assertEqual(r.status, "awaiting_confirmation")
        self.assertIn("can't be undone", r.text)
        self.assertIn("demo", self.api.repos)  # still there until "yes"
        r = self.say("yes")
        self.assertNotIn("demo", self.api.repos)
        step = [s for p in r.plans for s in p["steps"] if s["tool"] == "github_delete_repo"][0]
        self.assertTrue(step["verified"])  # GitHub now answers 404 for it
        self.assertIn(("DELETE", "/repos/srika/demo"), self.api.calls)

    def test_wrong_name_or_cancel_deletes_nothing(self):
        self.say("delete my github repo demo")
        r = self.say("genesis")  # typed a different repo's name
        self.assertIn("doesn't match", r.text)
        self.assertNotEqual(r.status, "awaiting_confirmation")
        self.assertIn("demo", self.api.repos)
        self.assertIn("genesis", self.api.repos)
        self.assertNotIn(("DELETE", "/repos/srika/genesis"), self.api.calls)
        self.say("delete the old-notes repo from github")
        self.say("cancel")
        self.assertIn("old-notes", self.api.repos)
        self.assertFalse(any(c[0] == "DELETE" for c in self.api.calls))

    def test_delete_it_after_opening_a_repo(self):
        self.say("open repo old-notes")
        r = self.say("delete it")
        self.assertEqual(r.status, "awaiting_input")
        self.assertIn("old-notes", r.text)
        self.say("old notes")  # spoken form of the name is accepted
        self.say("yes")
        self.assertNotIn("old-notes", self.api.repos)

    def test_token_never_shown(self):
        for q in ("show my github repos", "create a github repo called genesis"):
            r = self.say(q)
            if r.status == "awaiting_confirmation":
                r = self.say("yes")
            self.assertNotIn(TOKEN, r.text)
        self.assertIn("already exists", r.text)

    def test_llm_planner_can_never_delete_repos(self):
        from genesis.agent.planner import LLM_EXCLUDED_TOOLS

        self.assertIn("github_delete_repo", LLM_EXCLUDED_TOOLS)


class TestGitHubWithoutToken(GenesisTestCase):
    def test_explains_how_to_add_a_token(self):
        r = self.say("show my github repos")
        self.assertIn("GITHUB_TOKEN", r.text)
        r = self.say("open github")
        self.assertEqual(tools_of(r), [("github_open", "succeeded")])  # opening pages needs no token


# =====================================================================================
# Office
# =====================================================================================
def office_brain(messages, json_mode):
    last = messages[-1]["content"]
    if "content of a Word document" in last:
        return json.dumps({"title": "Climate Change", "sections": [
            {"heading": "Introduction", "paragraphs": ["Climate change is the long-term shift in temperatures."], "bullets": []},
            {"heading": "Causes", "paragraphs": ["Burning fossil fuels."], "bullets": ["CO2", "Methane"]}]})
    if "Design a spreadsheet" in last:
        return json.dumps({"title": "Monthly Budget", "sheets": [{"name": "Budget", "headers": ["Item", "Amount"],
                           "rows": [["Rent", 12000], ["Food", "4500"], ["Evil", "=cmd|' /C calc'!A0"], ["Double", "=B2*2"]],
                           "total_row": True}]})
    if "Write a presentation" in last:
        n = int(last.split("exactly ")[1].split(" ")[0])
        return json.dumps({"title": "Solar Energy", "subtitle": "An overview",
                           "slides": [{"title": f"Point {i + 1}", "bullets": ["a", "b"], "notes": "say this"} for i in range(n + 2)]})
    if "for a Word document" in last:
        return json.dumps({"heading": "", "paragraphs": ["India is a country in South Asia."], "bullets": []})
    if "slide(s) for" in last:
        return json.dumps({"slides": [{"title": "Results", "bullets": ["accuracy 80%"], "notes": ""}]})
    return "{}" if json_mode else "OK"


class OfficeCase(GenesisTestCase):
    open_docs: dict = {}

    def setUp(self):
        self.llm = ScriptedLLM(office_brain)
        self.live_office = FakeLiveOffice(dict(self.open_docs))
        super().setUp()

    def made(self, r) -> Path:
        step = [s for p in r.plans for s in p["steps"] if s["tool"] in ("office_create", "office_write")][0]
        self.assertTrue(step["verified"], step)
        return Path(step["result"]["data"]["path"])


class TestOfficeCreate(OfficeCase):
    def test_powerpoint_with_n_slides(self):
        r = self.say("make a powerpoint on solar energy with 3 slides")
        path = self.made(r)
        self.assertEqual(path.suffix, ".pptx")
        self.assertEqual(check_file("powerpoint", path)["slides"], 4)  # title + exactly 3 (the model gave 5)
        self.assertIn(("open_path", str(path)), self.backend.actions)
        self.assertIn("4 slides", r.text)

    def test_word_document(self):
        r = self.say("create a word document about climate change")
        path = self.made(r)
        from docx import Document

        text = "\n".join(p.text for p in Document(str(path)).paragraphs)
        self.assertIn("Causes", text)
        self.assertIn("Methane", text)
        self.assertEqual(path.name, "Climate Change.docx")
        r = self.say("create a word document about climate change")
        self.assertEqual(self.made(r).name, "Climate Change (2).docx")  # never overwrites

    def test_excel_sheet_with_safe_formulas_only(self):
        from openpyxl import load_workbook

        r = self.say("create an excel sheet for my monthly budget")
        ws = load_workbook(str(self.made(r)))["Budget"]
        values = [[c.value for c in row] for row in ws.iter_rows()]
        self.assertEqual(values[0], ["Item", "Amount"])
        self.assertEqual(values[2][1], 4500)  # numbers stay numbers
        self.assertTrue(str(values[3][1]).startswith("'="))  # the DDE payload is text, never a formula
        self.assertEqual(values[4][1], "=B2*2")
        self.assertEqual(values[-1][0], "Total")

    def test_needs_model_for_generated_content_but_not_for_exact_text(self):
        self.svc.llm.primary.reply = lambda m, j: (_ for _ in ()).throw(RuntimeError())  # model broken
        self.svc.llm.primary.is_generative = False
        r = self.say("create a word document about climate change")
        self.assertIn("AI model", r.text)


class TestOfficeWriteIntoOpenDocument(OfficeCase):
    open_docs = {"word": "Notes.docx", "excel": "Budget.xlsx", "powerpoint": "Talk.pptx"}

    def test_exact_text_into_word(self):
        r = self.say("write hello world in word")
        self.assertEqual(self.live_office.docs["word"]["items"], ["hello world"])
        step = [s for p in r.plans for s in p["steps"] if s["tool"] == "office_write"][0]
        self.assertTrue(step["verified"])
        self.assertIn("Notes.docx", r.text)
        self.assertIn("isn't saved yet", r.text)

    def test_generated_paragraph_after_opening_word(self):
        from unittest import mock

        from .helpers import FakeAppResolver

        with mock.patch.dict(FakeAppResolver.INSTALLED, {"word": "C:/Program Files/Microsoft Office/root/Office16/WINWORD.EXE"}):
            self.say("open word")
        r = self.say("write a paragraph about India")
        self.assertEqual(tools_of(r), [("office_write", "succeeded")])
        self.assertIn("India is a country in South Asia.", self.live_office.docs["word"]["items"])

    def test_rows_into_excel_and_slide_into_powerpoint(self):
        self.say("type 10, 20, 30 in excel")
        self.assertEqual(self.live_office.docs["excel"]["items"], ["10 20 30"])
        self.say("add a slide about results to my presentation")
        self.assertIn("Results: accuracy 80%", self.live_office.docs["powerpoint"]["items"])


class TestOfficeWriteWithNothingOpen(OfficeCase):
    def test_creates_a_new_document_instead(self):
        r = self.say("write an essay on climate change in word")
        self.assertIn("No Word document was open", r.text)
        self.assertEqual(self.made(r).suffix, ".docx")


class TestSafeCell(GenesisTestCase):
    def test_formula_filter(self):
        self.assertEqual(safe_cell("=SUM(A1:A5)"), "=SUM(A1:A5)")
        self.assertEqual(safe_cell("=B2*C2+1"), "=B2*C2+1")
        for bad in ("=cmd|' /C calc'!A0", "=HYPERLINK(\"http://x\")", "=WEBSERVICE(\"http://x\")", "@SUM(1)", "+cmd|x"):
            self.assertTrue(str(safe_cell(bad)).startswith("'"), bad)
        self.assertEqual(safe_cell("42"), 42)
        self.assertEqual(safe_cell("-3.5"), -3.5)


# =====================================================================================
# Colab one-shot + routing
# =====================================================================================
class TestColabWriteAndRun(GenesisTestCase):
    def setUp(self):
        def reply(messages, json_mode):
            if "Colab" in messages[-1]["content"] or "notebook" in messages[-1]["content"].lower():
                return json.dumps({"code": "print('hello')", "summary": "prints hello"})
            return "{}" if json_mode else "OK"

        self.llm = ScriptedLLM(reply)
        self.colab_driver = FakeColabDriver({"print('hello')": "hello"})
        super().setUp()

    def test_write_and_run_in_one_command(self):
        self.say("open google colab")
        self.say("create a notebook")
        r = self.say("write code in colab to print hello and run it")
        self.assertEqual([t for t, s in tools_of(r)], ["colab_write_code", "colab_run"])
        self.assertTrue(all(s == "succeeded" for _, s in tools_of(r)), tools_of(r))


class TestRouting(GenesisTestCase):
    CASES = [
        ("open github", Intent.GITHUB), ("delete my github repo test", Intent.GITHUB), ("list my repositories", Intent.GITHUB),
        ("make a powerpoint on solar energy with 5 slides", Intent.OFFICE), ("create an excel sheet for my budget", Intent.OFFICE),
        ("write hello world in word", Intent.OFFICE), ("make a budget spreadsheet", Intent.OFFICE),
        ("show me an image of an elephant", Intent.IMAGE), ("show an elephant image", Intent.IMAGE),
        ("i want to see a picture of a tiger", Intent.IMAGE), ("search the internet for images of the eiffel tower", Intent.IMAGE),
        ("show me the latest photos", Intent.FILE_OPERATION), ("draft an email to rahul", Intent.DRAFT_EMAIL),
        ("delete report.txt", Intent.FILE_OPERATION), ("open word", Intent.OPEN_APPLICATION),
    ]

    def test_cases(self):
        for text, intent in self.CASES:
            hit = rules.parse(rules.normalize(text), None)
            self.assertEqual(hit.intent if hit else None, intent, text)


class TestModelCannotSkipTheTypedName(GitHubCase):
    def test_llm_supplied_confirm_name_is_ignored(self):
        from genesis.nlp.intents import NLUResult

        st = self.core.dialogue.state(self.core.ensure_conversation())
        nlu = NLUResult(text="get rid of demo", intent=Intent.GITHUB, confidence=0.9, source="llm",
                        slots={"github_op": "delete", "repo": "demo", "confirm_name": "demo"})
        r = self.core._run_requests([nlu], st)
        self.assertEqual(r.status, "awaiting_input")  # still asks the user to type the name
        self.assertIn("demo", self.api.repos)
