"""Genesis 3.0.1 - Wikipedia -> Word, documents/IEEE, Excel, code in VS Code/Colab, digital twin.

The AI model is a scripted fake and Wikipedia is a fake HTTP session; files are really written and re-opened to check."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from genesis.v3 import twin, work
from genesis.v3.commands import match
from genesis.v3.router import Router, Thresholds
from genesis.v3.agents import build_agents


class FakeLLM:
    is_generative = True
    name = "fake"

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def chat(self, messages, **kw):
        self.calls.append(messages)
        return self.reply(messages) if callable(self.reply) else self.reply


class Backend:
    def __init__(self):
        self.actions = []

    def open_path(self, p):
        self.actions.append(("open_path", p))

    def open_url(self, u):
        self.actions.append(("open_url", u))
        return True

    def launch(self, argv, cwd=None):
        self.actions.append(("launch", argv))
        return 1


class Apps:
    def vscode_executable(self):
        return r"C:\VSCode\Code.exe"


class Resp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


class WikiSession:
    def get(self, url, params=None, headers=None, timeout=None):
        assert "User-Agent" in headers
        if params.get("list") == "search":
            return Resp({"query": {"search": [{"title": "Black hole"}]}})
        return Resp({"query": {"pages": {"1": {"title": "Black hole", "fullurl": "https://en.wikipedia.org/wiki/Black_hole",
                     "extract": "A black hole is a region of spacetime.\nIts gravity is strong.\n\n== History ==\n"
                                "Early ideas date to 1784.\n=== Modern era ===\nGeneral relativity.\n"
                                "== See also ==\nNeutron star\n== References ==\n[1] x"}}}})


class Ctx:
    def __init__(self, llm=None, tmp=None):
        self.llm = llm
        self.backend = Backend()
        self.apps = Apps()
        self.extras = {"web_session": WikiSession()}
        self.office = type("O", (), {"out_dir": tmp})()
        self.colab = None
        self.memory = None
        self.repos = None


class WorkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = mock.patch.dict(os.environ, {"OneDrive": self.tmp.name, "HOME": self.tmp.name,
                                                 "USERPROFILE": self.tmp.name})
        self.home.start()
        (Path(self.tmp.name) / "Documents").mkdir()

    def tearDown(self):
        self.home.stop()
        self.tmp.cleanup()

    def test_wikipedia_to_word(self):
        import docx

        ctx = Ctx(tmp=self.tmp.name)
        r = work.WikipediaToWord().run({"topic": "black holes"}, ctx)
        self.assertTrue(r.success, r.message)
        self.assertTrue(r.untrusted_content)
        self.assertIn(("open_url", "https://en.wikipedia.org/wiki/Black_hole"), ctx.backend.actions)
        text = "\n".join(p.text for p in docx.Document(r.data["path"]).paragraphs)
        self.assertIn("A black hole is a region", text)
        self.assertIn("Modern era", text)
        self.assertIn("Source: Wikipedia", text)
        self.assertNotIn("Neutron star", text)  # "See also" / references skipped
        r2 = work.WikipediaToWord().run({"topic": "black holes"}, ctx)
        self.assertNotEqual(r.data["path"], r2.data["path"])  # never overwrites

    def test_ieee_paper(self):
        import docx

        def reply(msgs):
            u = msgs[-1]["content"]
            if "index terms" in u.lower():
                return "federated learning, privacy, healthcare"
            if "abstract" in u.lower():
                return "This paper studies federated learning."
            return "Sure, here is the section:\n**Federated** learning keeps data local.\n\nIt helps hospitals."

        ctx = Ctx(FakeLLM(reply), tmp=self.tmp.name)
        r = work.DraftDocument().run({"topic": "Federated learning in healthcare", "kind": "ieee"}, ctx)
        self.assertTrue(r.success, r.message)
        d = docx.Document(r.data["path"])
        text = "\n".join(p.text for p in d.paragraphs)
        for part in ("Abstract—", "Index Terms—", "I. INTRODUCTION", "V. CONCLUSION", "REFERENCES"):
            self.assertIn(part, text)
        self.assertNotIn("**", text)
        self.assertNotIn("Sure, here is", text)
        self.assertIn('w:num="2"', d.sections[-1]._sectPr.xml)  # two columns
        self.assertIn("placeholders", r.message)

    def test_report_and_letter(self):
        import docx

        def reply(msgs):
            if "section headings" in msgs[-1]["content"]:
                return "1. Introduction\n2. Causes\n3. Effects\n4. Conclusion"
            return "Some text.\n- a point"

        ctx = Ctx(FakeLLM(reply), tmp=self.tmp.name)
        r = work.DraftDocument().run({"topic": "climate change", "kind": "report"}, ctx)
        self.assertEqual(r.data["sections"], ["Introduction", "Causes", "Effects", "Conclusion"])
        heads = [p.text for p in docx.Document(r.data["path"]).paragraphs if p.style.name.startswith("Heading")]
        self.assertIn("Causes", heads)
        r = work.DraftDocument().run({"topic": "leave request", "kind": "letter"}, ctx)
        self.assertTrue(r.success)

    def test_excel(self):
        from openpyxl import load_workbook

        csv_text = "Here you go:\n```csv\nDate,Item,Amount\n2026-11-01,Groceries,\"1,200\"\n2026-11-02,Bus,40\n```"
        ctx = Ctx(FakeLLM(csv_text), tmp=self.tmp.name)
        r = work.ExcelCreate().run({"request": "monthly expenses"}, ctx)
        self.assertTrue(r.success, r.message)
        ws = load_workbook(r.data["path"]).active
        self.assertEqual([c.value for c in ws[1]], ["Date", "Item", "Amount"])
        self.assertEqual(ws["C2"].value, 1200)
        self.assertEqual(ws["A4"].value, "Total")
        self.assertEqual(ws["C4"].value, "=SUM(C2:C3)")
        self.assertEqual(ws.freeze_panes, "A2")
        bad = work.ExcelCreate().run({"request": "x"}, Ctx(FakeLLM("no table here"), tmp=self.tmp.name))
        self.assertFalse(bad.success)

    def test_code_in_vscode(self):
        ctx = Ctx(FakeLLM("```python\nprint('snake')\n```\nRun it with python."), tmp=self.tmp.name)
        r = work.CodeInVSCode().run({"task": "a snake game in python"}, ctx)
        self.assertTrue(r.success, r.message)
        self.assertEqual(Path(r.data["path"]).read_text(), "print('snake')\n")
        self.assertTrue(r.data["path"].endswith(".py"))
        self.assertEqual(ctx.backend.actions[-1][1][0], r"C:\VSCode\Code.exe")
        self.assertEqual(work.language_of("a calculator in javascript"), ("javascript", "js"))

    def test_code_in_colab_fallback(self):
        import json

        ctx = Ctx(FakeLLM("```python\nimport pandas as pd\n```"), tmp=self.tmp.name)
        r = work.CodeInColab().run({"task": "load iris"}, ctx)
        self.assertTrue(r.success)
        nb = json.loads(Path(r.data["path"]).read_text())
        self.assertEqual(nb["cells"][1]["source"], ["import pandas as pd\n"])
        self.assertTrue(any(a[0] == "open_url" and "colab" in a[1] for a in ctx.backend.actions))


class TwinTests(unittest.TestCase):
    def test_preferences(self):
        self.assertEqual(twin.preference_from("My favourite pizza is a Farmhouse from Domino's"),
                         "User's favourite pizza: a Farmhouse from Domino's")
        self.assertEqual(twin.preference_from("I always order cold coffee"), "User always orders cold coffee")
        self.assertIsNone(twin.preference_from("I like it, thanks"))  # needs "remember" for likes
        self.assertEqual(twin.preference_from("remember that I love dark chocolate"), "User loves dark chocolate")

    def test_relevance(self):
        facts = ["User's favourite coffee: latte", "User lives in Chennai", "User's favourite pizza: Farmhouse"]
        self.assertEqual(twin.relevant(facts, "which pizza should I order", 1), ["User's favourite pizza: Farmhouse"])
        self.assertEqual(twin.facts_about("pizzas", facts), ["User's favourite pizza: Farmhouse"])

    def _ctx(self, facts):
        ctx = Ctx()
        ctx.repos = type("R", (), {})()
        ctx.repos.memory = type("M", (), {"list": lambda self: [{"content": f} for f in facts],
                                          "add": lambda self, c, kind="fact": 1})()
        ctx.repos.preferences = None
        ctx.repos.conversations = None
        return ctx

    def test_order_and_recall(self):
        ctx = self._ctx(["User's favourite pizza: Farmhouse from Domino's"])
        r = work.TwinOrder().run({"item": "pizza"}, ctx)
        self.assertIn("Your usual pizza is Farmhouse from Domino's", r.message)
        self.assertIn("never place or pay", r.message)
        self.assertIn("dominos", ctx.backend.actions[-1][1])
        r = work.TwinRecall().run({"item": "pizza"}, ctx)
        self.assertIn("Farmhouse", r.message)
        ctx2 = self._ctx([])
        r = work.TwinOrder().run({"item": "biryani"}, ctx2)
        self.assertIn("swiggy.com/search?query=biryani", ctx2.backend.actions[-1][1])
        self.assertIn("don't know your usual", r.message)

    def test_remember_uses_memory(self):
        ctx = self._ctx([])
        saved = []
        ctx.memory = type("MM", (), {"enabled": True, "remember": lambda self, t, kind="fact": saved.append((t, kind)) or 7})()
        r = work.TwinRemember().run({"statement": "my favourite pizza is Farmhouse"}, ctx)
        self.assertTrue(r.success)
        self.assertEqual(saved, [("User's favourite pizza: Farmhouse", "preference")])

    def test_twin_route_and_prompt(self):
        with mock.patch.dict(os.environ, {"SUZANNE_MODEL": "genesis-slm", "ANDREW_MODEL": "phi4-mini"}):
            r = Router(build_agents(), thresholds=Thresholds()).route(
                [{"role": "user", "content": "answer as me: are you coming tonight?"}], installed={"genesis-slm",
                                                                                                    "phi4-mini"})
        self.assertEqual((r.kind, r.agent, r.model), ("twin", "suzanne", "genesis-slm"))
        prof = twin.Profile(lambda: ["User lives in Chennai"], lambda: ["bro im on my way da"])
        p = prof.twin_prompt("where do you live")
        self.assertIn("first person", p)
        self.assertIn("User lives in Chennai", p)
        self.assertIn("bro im on my way da", p)

    def test_provider_injects_profile_and_twin(self):
        from tests.test_v3 import ENV_LOCAL, FakeOllama, hw
        from genesis.v3.ollama import OllamaClient
        from genesis.v3.provider import GenesisLocalProvider
        from genesis.v3.store import LocalStore

        fake = FakeOllama()
        with mock.patch.dict(os.environ, ENV_LOCAL):
            p = GenesisLocalProvider(OllamaClient(session=fake), LocalStore(":memory:"), hardware=lambda: hw())
        p.profile = twin.Profile(lambda: ["User's favourite pizza: Farmhouse"], lambda: ["sup"])
        p.chat([{"role": "user", "content": "which pizza should i get tonight?"}])
        sys = fake.calls[-1][1]["messages"][0]["content"]
        self.assertIn("Suzanne", sys)
        self.assertIn("Farmhouse", sys)
        p.chat([{"role": "user", "content": "reply as me: wanna hang out?"}])
        msgs = fake.calls[-1][1]["messages"]
        self.assertIn("DIGITAL TWIN", msgs[0]["content"])
        self.assertNotIn("Suzanne", msgs[0]["content"])
        self.assertEqual(msgs[-1]["content"], "wanna hang out?")

    def test_voice_switch(self):
        from genesis.v3 import hooks

        class STT:
            cloud_enabled = property(lambda self: True)

        hooks.patch_v2_voice({"__name__": "v", "STT": STT})
        with mock.patch.dict(os.environ, {"GENESIS_MODE": "local", "GENESIS_EXTERNAL_AI_ENABLED": "false",
                                          "GENESIS_CLOUD_VOICE": ""}):
            self.assertTrue(STT().cloud_enabled)  # voice keeps working by default
        with mock.patch.dict(os.environ, {"GENESIS_MODE": "local", "GENESIS_EXTERNAL_AI_ENABLED": "false",
                                          "GENESIS_CLOUD_VOICE": "false"}):
            self.assertFalse(STT().cloud_enabled)


class CommandTests(unittest.TestCase):
    def test_phrases(self):
        cases = {
            "open wikipedia on black holes and copy it into word": "wikipedia_to_word",
            "write an IEEE paper on federated learning": "draft_document",
            "draft a report on climate change": "draft_document",
            "create an excel sheet for my monthly expenses": "excel_create",
            "write a snake game in python in vs code": "code_in_vscode",
            "write a linear regression on iris in colab": "code_in_colab",
            "i want to order pizza": "twin_order",
            "which pizza do i like": "twin_recall",
            "my favourite pizza is a farmhouse": "twin_remember",
            "answer as me: are you free?": "twin_answer",
        }
        for text, tool in cases.items():
            self.assertEqual(match(text)[0], tool, text)
        for text in ("open notepad", "order a cab", "write a python function to reverse a list"):
            self.assertIsNone(match(text), text)

    def test_tools_registered_and_valid(self):
        from genesis.v3.tools import TOOLS

        by = {t.name: t for t in TOOLS}
        for text in ("draft a report on climate change; with charts", "i want to order pizza from swiggy",
                     "search wikipedia for Alan Turing", "create an excel sheet for marks"):
            tool, args = match(text)
            by[tool].validate(args)


if __name__ == "__main__":
    unittest.main()


class EndToEnd(unittest.TestCase):
    """Real Genesis instance: remember a preference, then order 'my usual' and ask as the twin."""

    def setUp(self):
        from tests.helpers import GenesisTestCase
        from tests.test_v3 import ENV_LOCAL, FakeOllama, hw
        from genesis.v3.ollama import OllamaClient
        from genesis.v3.provider import GenesisLocalProvider
        from genesis.v3.store import LocalStore

        self.env = mock.patch.dict(os.environ, ENV_LOCAL)
        self.env.start()
        self.fake = FakeOllama()
        prov = GenesisLocalProvider(OllamaClient(session=self.fake), LocalStore(":memory:"), hardware=lambda: hw())

        class Case(GenesisTestCase):
            llm = prov

            def runTest(self):  # pragma: no cover
                pass

        self.case = Case()
        self.case.setUp()
        self.prov = prov

    def tearDown(self):
        from genesis.v3.provider import reset_runtime

        self.case.tearDown()
        self.env.stop()
        reset_runtime()

    def test_twin_flow(self):
        r = self.case.say("my favourite pizza is a Farmhouse from Domino's")
        self.assertIn("remember", r.text.lower())
        self.assertIsNotNone(self.prov.profile)
        self.assertTrue(any("Farmhouse" in f for f in self.prov.profile.facts()))
        r = self.case.say("i want to order pizza")
        self.assertIn("Farmhouse", r.text)
        self.assertTrue(any("dominos" in u for u in self.case.urls_opened()))
        self.case.say("answer as me: what pizza do you like?")
        sys = self.fake.calls[-1][1]["messages"][0]["content"]
        self.assertIn("DIGITAL TWIN", sys)
        self.assertIn("Farmhouse", sys)
