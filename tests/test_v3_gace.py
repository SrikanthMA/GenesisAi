"""Genesis 3.5 - GACE (autonomous coding engine) and microphone / speaker mute.

Real files are written to temp folders and real unit tests are run by the engine; only the AI model is a scripted fake
(no network, no Ollama). The mute tests use the real VoiceManager with a fake microphone."""
import json
import os
import queue
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from genesis.v3 import coding, privacy
from genesis.v3.gace import errors, review, runner
from genesis.v3.gace.engine import Engine, Limits, code_from, validate_spec
from genesis.v3.gace.memory import ProjectMemory
from genesis.v3.gace.workspace import Workspace, WorkspaceError

GOOD_CALC = "def add(a, b):\n    return a + b\n\ndef div(a, b):\n    return a / b\n"
BAD_CALC = "def add(a, b):\n    return a - b\n\ndef div(a, b):\n    return a / b\n"
TEST_CALC = ("import os, sys, unittest\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
             "from calc import add, div\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
             "    def test_div(self):\n        self.assertEqual(div(6, 3), 2)\n")
PLAN = {"name": "calc", "summary": "A tiny calculator library", "stack": ["python"], "requirements": ["add numbers", "divide"],
        "files": [{"path": "calc.py", "purpose": "functions"}, {"path": "tests/test_calc.py", "purpose": "tests"}], "pip": [],
        "test_command": ["python", "-m", "unittest", "discover", "-s", "tests"], "run_command": [], "questions": []}


def fenced(code):
    return "```python\n" + code + "```"


class Model:
    """A scripted model: answers by purpose, records every call."""

    def __init__(self, plan=None, calc=BAD_CALC, tests=TEST_CALC, fix=GOOD_CALC):
        self.plan, self.calc, self.tests, self.fix, self.calls = plan or PLAN, calc, tests, fix, []

    def __call__(self, prompt, system="", max_tokens=0, purpose=""):
        self.calls.append(purpose)
        if purpose == "gace-plan":
            return json.dumps(self.plan)
        if purpose == "gace-code":
            return fenced(self.tests if "`tests/test_calc.py`" in prompt else self.calc)
        if purpose == "gace-fix":
            return fenced(self.fix) if isinstance(self.fix, str) else self.fix(prompt)
        raise AssertionError(purpose)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ws = Workspace(self.tmp / "proj", [self.tmp])

    def engine(self, model, confirm=lambda m: True, **lim):
        self.events = []
        return Engine(self.ws, model, confirm, on_event=self.events.append, limits=Limits(**lim))


# ------------------------------------------------------------------ workspace
class TestWorkspace(Base):
    def test_outside_approved_root_refused(self):
        with self.assertRaises(WorkspaceError):
            Workspace(self.tmp.parent / "elsewhere-x", [self.tmp])

    def test_paths_are_confined(self):
        for bad in ("../x.py", "/etc/passwd", "C:/Windows/x.py", "a/../../x.py", ".env", "keys/id_rsa", ".genesis/project.json", ""):
            with self.assertRaises(WorkspaceError, msg=bad):
                self.ws.resolve(bad)
        self.ws.resolve("src/app.py")
        self.ws.resolve(".env.example")
        spec, _ = validate_spec(dict(PLAN, files=[{"path": ".gitignore"}, {"path": "./src/a.py"}]), self.ws, Limits())
        self.assertEqual([f["path"] for f in spec["files"]], [".gitignore", "src/a.py"])

    def test_symlink_escape_refused(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        try:
            os.symlink(outside, self.ws.root / "link")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not available")
        with self.assertRaises(WorkspaceError):
            self.ws.write("link/x.py", "x=1")

    def test_write_returns_diff_and_journal(self):
        self.ws.write("a.py", "x = 1\n")
        info = self.ws.write("a.py", "x = 2\n")
        self.assertIn("-x = 1", info["diff"])
        self.assertEqual([e["op"] for e in self.ws.journal()], ["create", "modify"])

    def test_delete_needs_confirmation(self):
        self.ws.write("a.py", "x=1")
        with self.assertRaises(WorkspaceError):
            self.ws.delete("a.py")
        self.ws.delete("a.py", confirm=True)
        self.assertFalse((self.ws.root / "a.py").exists())

    def test_checkpoint_and_rollback(self):
        self.ws.write("keep.py", "v1\n")
        self.ws.write("gone.py", "important\n")
        cid = self.ws.checkpoint("start")
        self.ws.write("keep.py", "v2\n")
        self.ws.write("new_by_genesis.py", "n\n")
        self.ws.delete("gone.py", confirm=True)
        (self.ws.root / "users_own.txt").write_text("mine")  # not in the journal: must survive
        with self.assertRaises(WorkspaceError):
            self.ws.rollback(cid)  # needs confirmation
        out = self.ws.rollback(cid, confirm=True)
        self.assertEqual((self.ws.root / "keep.py").read_text(), "v1\n")
        self.assertEqual((self.ws.root / "gone.py").read_text(), "important\n")
        self.assertFalse((self.ws.root / "new_by_genesis.py").exists())
        self.assertTrue((self.ws.root / "users_own.txt").exists())
        self.assertIn("new_by_genesis.py", out["removed"])

    def test_checkpoint_skips_heavy_and_secret_files(self):
        (self.ws.root / "node_modules").mkdir()
        (self.ws.root / "node_modules" / "big.js").write_text("x")
        (self.ws.root / ".env").write_text("KEY=1")
        cid = self.ws.checkpoint("x")
        import zipfile

        names = zipfile.ZipFile(self.ws.state / "checkpoints" / f"{cid}.zip").namelist()
        self.assertNotIn("node_modules/big.js", names)
        self.assertNotIn(".env", names)


# ------------------------------------------------------------------ error analysis
class TestErrors(unittest.TestCase):
    def test_categories(self):
        cases = {
            'File "/w/app.py", line 3, in <module>\nModuleNotFoundError: No module named \'yaml\'': ("dependency", "pyyaml"),
            'File "/w/app.py", line 3\n    def f(:\n          ^\nSyntaxError: invalid syntax': ("syntax", None),
            "AttributeError: 'NoneType' object has no attribute 'x'": ("attribute", None),
            "OSError: [Errno 98] Address already in use": ("port", None),
            "sqlite3.OperationalError: no such table: users": ("database", None),
            "Module build failed: SyntaxError: Unexpected token (3:4)": ("frontend", None),
            "NameError: name 'foo' is not defined": ("name", None),
            "ImportError: cannot import name 'x' from 'y'": ("import", None),
            "TypeError: f() takes 1 positional argument but 2 were given": ("type", None),
        }
        for out, (cat, pkg) in cases.items():
            d = errors.diagnose(out, "/w")
            self.assertEqual(d.category, cat, out)
            self.assertEqual(d.missing_package, pkg, out)

    def test_frames_prefer_project_code_over_tests_and_libraries(self):
        out = ('Traceback (most recent call last):\n  File "/w/tests/test_a.py", line 9, in test_x\n    f()\n'
               '  File "/w/src/core.py", line 4, in f\n    g()\n  File "/usr/lib/python3/site-packages/lib.py", line 1, in g\n'
               'ValueError: bad')
        d = errors.diagnose(out, "/w")
        self.assertEqual(d.files[0], {"path": "src/core.py", "line": 4})
        self.assertNotIn("lib.py", " ".join(f["path"] for f in d.files))

    def test_same_error_same_signature(self):
        a = errors.diagnose('File "/w/a.py", line 3, in f\nValueError: bad 12', "/w")
        b = errors.diagnose('File "/w/a.py", line 9, in f\nValueError: bad 99', "/w")
        self.assertEqual(a.signature, b.signature)

    def test_zero_tests_collected(self):
        self.assertEqual(errors.tests_collected("Ran 0 tests in 0.0s\n\nOK"), 0)
        self.assertEqual(errors.tests_collected("3 passed in 0.1s"), 3)
        self.assertEqual(errors.tests_collected("Ran 4 tests in 0.0s\n\nOK"), 4)


# ------------------------------------------------------------------ terminal
class TestRunner(unittest.TestCase):
    root = Path(tempfile.gettempdir())

    def test_allowed(self):
        v = lambda *a: runner.validate(list(a), self.root)  # noqa: E731
        self.assertEqual(v("python", "-m", "pytest", "-q"), "safe")
        self.assertEqual(v("python", "-m", "unittest", "discover"), "safe")
        self.assertEqual(v("python", "-m", "pip", "install", "-r", "requirements.txt"), "install")
        self.assertEqual(v("python", "-m", "pip", "install", "fastapi==0.110.0", "uvicorn[standard]"), "install")
        self.assertEqual(v("npm", "install"), "install")
        self.assertEqual(v("npm", "run", "build"), "safe")
        self.assertEqual(v("python", "-m", "venv", ".venv"), "safe")

    def test_refused(self):
        bad = [["rm", "-rf", "x"], ["curl", "http://x"], ["powershell", "-c", "x"], ["python", "-c", "import os; os.system('x')"],
               ["python", "-m", "pip", "install", "git+https://evil/x.git"], ["python", "-m", "pip", "install", "--index-url", "http://x", "a"],
               ["python", "-m", "pip", "install", "-r", "../req.txt"], ["python", "-m", "pip", "uninstall", "x"],
               ["python", "-m", "http.server"], ["npm", "install", "../evil"], ["npm", "install", "http://x/y.tgz"],
               ["npm", "publish"], ["node", "../x.js"], ["python", "a.py; rm x"], ["git", "push"], ["python", "../outside.py"],
               ["python", "-m", "pip", "install", "a&b"]]
        for argv in bad:
            with self.assertRaises(runner.CommandRefused, msg=str(argv)):
                runner.validate(argv, self.root)

    def test_environment_secrets_removed(self):
        os.environ["MY_API_KEY"] = "x"
        os.environ["GEMINI_TOKEN"] = "y"
        try:
            env = runner.clean_env()
            self.assertNotIn("MY_API_KEY", env)
            self.assertNotIn("GEMINI_TOKEN", env)
        finally:
            del os.environ["MY_API_KEY"], os.environ["GEMINI_TOKEN"]

    def test_runs_and_captures(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "ok.py").write_text("print('hi')")
        (d / "bad.py").write_text("raise ValueError('boom')")
        r = runner.run(["python", "ok.py"], d, 30)
        self.assertTrue(r.ok)
        self.assertIn("hi", r.output)
        r = runner.run(["python", "bad.py"], d, 30)
        self.assertFalse(r.ok)
        self.assertIn("ValueError: boom", r.output)

    def test_timeout(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "slow.py").write_text("import time\ntime.sleep(20)")
        r = runner.run(["python", "slow.py"], d, 1)
        self.assertTrue(r.timed_out)
        self.assertFalse(r.ok)


# ------------------------------------------------------------------ review
class TestReview(unittest.TestCase):
    def test_findings(self):
        files = {"a.py": 'import subprocess\nAPI_KEY = "abcdef1234567890abcdef"\nsubprocess.run(x, shell=True)\neval(y)\n'
                         'cur.execute(f"select * from t where id={uid}")\napp.run(debug=True)\n',
                 "b.py": "def run_all():\n    raise NotImplementedError\n",
                 "tests/test_a.py": "import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertTrue(True)\n"}
        issues = " | ".join(i["issue"] for i in review.review(files))
        for needle in ("hard-coded secret", "shell=True", "eval/exec", "SQL built", "debug mode", "empty stub"):
            self.assertIn(needle, issues)
        self.assertNotIn("test_a() is an empty stub", issues)  # a test body made of calls is not a stub

    def test_duplicates_reported(self):
        files = {"a.py": "def compute_total(x):\n    return x\n", "b.py": "def compute_total(x):\n    return x + 1\n"}
        self.assertTrue(any("defined in 2 files" in i["issue"] for i in review.review(files)))


# ------------------------------------------------------------------ plan validation
class TestPlan(Base):
    def test_unsafe_plan_parts_rejected(self):
        plan = dict(PLAN, files=[{"path": "../evil.py"}, {"path": ".env"}, {"path": "ok.py"}, {"path": "x.exe"}],
                    pip=["fastapi", "git+https://x/y", "--index-url=http://z"], test_command=["curl", "http://x"])
        spec, probs = validate_spec(plan, self.ws, Limits())
        self.assertEqual([f["path"] for f in spec["files"]], ["ok.py"])  # "../evil.py" must not be "repaired" into a safe path
        self.assertEqual(spec["pip"], ["fastapi"])
        self.assertEqual(spec["test_command"], [])
        self.assertGreaterEqual(len(probs), 5)

    def test_too_many_files(self):
        plan = dict(PLAN, files=[{"path": f"f{i}.py"} for i in range(60)])
        _, probs = validate_spec(plan, self.ws, Limits())
        self.assertTrue(any("limit" in p for p in probs))

    def test_code_from(self):
        self.assertEqual(code_from("Sure!\n```python\nx = 1\n```\nbye"), "x = 1\n")
        self.assertEqual(code_from("Sure, here is the code"), "")


# ------------------------------------------------------------------ the engine, end to end
class TestEngine(Base):
    def test_build_finds_bug_repairs_and_verifies(self):
        m = Model()
        r = self.engine(m).build("make a calculator library")
        self.assertEqual(r.status, "verified", r.summary)
        self.assertGreaterEqual(r.tests_collected, 2)
        self.assertEqual((self.ws.root / "calc.py").read_text(), GOOD_CALC)
        self.assertEqual([x["result"] for x in r.repairs], ["kept"])
        self.assertEqual(m.calls.count("gace-fix"), 1)
        self.assertTrue((self.ws.root / "GENESIS_BUILD_REPORT.md").exists())
        self.assertIn("VERIFIED", r.report)
        self.assertEqual(r.metrics["unnecessary_retries"], 0)
        self.assertTrue(r.metrics["verified"])
        self.assertIn("before-build", " ".join(self.ws.checkpoints()))

    def test_no_repair_needed(self):
        r = self.engine(Model(calc=GOOD_CALC)).build("calculator")
        self.assertEqual(r.status, "verified")
        self.assertEqual(r.repairs, [])

    def test_loop_is_bounded_and_failed_fixes_are_undone(self):
        m = Model(fix=BAD_CALC.replace("a - b", "a * b"))  # a fix that never helps
        r = self.engine(m, max_repairs=5).build("calculator")
        self.assertEqual(r.status, "not_verified")
        self.assertLessEqual(m.calls.count("gace-fix"), 2)  # same error: stops after per-error limit
        self.assertEqual((self.ws.root / "calc.py").read_text(), BAD_CALC)  # undone, not left in a worse state
        self.assertGreaterEqual(r.metrics["unnecessary_retries"], 1)
        self.assertIn("NOT VERIFIED", r.report)
        self.assertNotIn("**Status: VERIFIED", r.report)

    def test_fix_that_removes_tests_is_rejected(self):
        def fix(prompt):
            return fenced("import unittest\nclass T(unittest.TestCase):\n    pass\n") if "tests/test_calc.py" in prompt.split("Current")[1][:40] else fenced(BAD_CALC)
        r = self.engine(Model(fix=fix), max_repairs=4).build("calculator")
        self.assertNotEqual(r.status, "verified")
        self.assertIn("def test_add", (self.ws.root / "tests/test_calc.py").read_text())

    def test_zero_tests_is_not_verified(self):
        plan = dict(PLAN, files=[{"path": "calc.py", "purpose": "x"}, {"path": "tests/test_empty.py", "purpose": "t"}])

        class M(Model):
            def __call__(s, prompt, system="", max_tokens=0, purpose=""):
                if purpose == "gace-code" and "test_empty" in prompt:
                    s.calls.append(purpose)
                    return fenced("import unittest\n")
                return super().__call__(prompt, system, max_tokens, purpose)

        r = self.engine(M(plan=plan, calc=GOOD_CALC)).build("calculator")
        self.assertNotEqual(r.status, "verified")
        self.assertIn("NO TESTS RAN", r.summary)  # the reason is in the answer, not just "failed"
        self.assertEqual(r.tests_collected, 0)

    def test_needs_info_asks_instead_of_guessing(self):
        plan = dict(PLAN, questions=[{"question": "Which database?", "blocking": True}, {"question": "Colour?", "blocking": False}])
        r = self.engine(Model(plan=plan)).build("app")
        self.assertEqual(r.status, "needs_info")
        self.assertEqual(r.questions, ["Which database?"])
        self.assertEqual(self.ws.files(), [])  # nothing was written

    def test_model_failure_is_reported(self):
        def broken(*a, **k):
            raise RuntimeError("ollama down")
        r = self.engine(broken).build("x")
        self.assertEqual(r.status, "failed")
        self.assertIn("No AI model", r.summary)

    def test_install_needs_confirmation_and_is_skipped_when_declined(self):
        plan = dict(PLAN, pip=["requests"])
        asked = []
        r = self.engine(Model(plan=plan, calc=GOOD_CALC), confirm=lambda m: asked.append(m) or False).build("x")
        self.assertTrue(asked and "internet" in asked[0])
        self.assertFalse((self.ws.root / ".venv").exists())
        self.assertTrue(any(c.get("skipped") for c in r.commands))
        self.assertIn("requests", (self.ws.root / "requirements.txt").read_text())

    def test_unplanned_package_not_installed_without_yes(self):
        e = self.engine(Model(), confirm=lambda m: coding.UNPLANNED not in m)
        e.res.spec = {"pip": ["flask"]}
        self.assertFalse(e._add_package("totally-new-pkg"))

    def test_cancel(self):
        e = Engine(self.ws, Model(), lambda m: True, limits=Limits(), cancelled=lambda: True)
        r = e.build("x")
        self.assertIn(r.status, ("cancelled", "failed", "partial", "not_verified"))
        self.assertNotEqual(r.status, "verified")

    def test_memory_resume_and_trust_in_files(self):
        r = self.engine(Model(calc=GOOD_CALC)).build("calculator")
        self.assertEqual(r.status, "verified")
        (self.ws.root / "tests/test_calc.py").unlink()            # a file vanishes
        (self.ws.root / "calc.py").write_text(GOOD_CALC + "\n# edited by the user\n")  # a file is changed outside Genesis
        audit = ProjectMemory(self.ws).audit()
        self.assertEqual(audit["missing"], ["tests/test_calc.py"])
        self.assertEqual(audit["changed"], ["calc.py"])
        m = Model(calc=GOOD_CALC)
        r2 = self.engine(m).resume()
        self.assertEqual(m.calls, ["gace-code"])                   # only the missing file was generated
        self.assertIn("# edited by the user", (self.ws.root / "calc.py").read_text())  # user's change kept
        self.assertEqual(r2.status, "verified")

    def test_rollback_after_build(self):
        self.engine(Model()).build("calculator")
        cps = self.ws.checkpoints()
        first = [c for c in cps if "before-build" in c][0]
        self.ws.rollback(first, confirm=True)
        self.assertFalse((self.ws.root / "calc.py").exists())      # created by Genesis after the checkpoint -> removed

    def test_events_cover_the_stages(self):
        self.engine(Model()).build("calculator")
        stages = {e["stage"] for e in self.events}
        for s in ("UNDERSTAND", "PLAN", "BUILD", "TEST", "DIAGNOSE", "REPAIR"):
            self.assertIn(s, stages)
        self.assertTrue(any(e["kind"] == "fix" and "diff" in e for e in self.events))

    def test_never_writes_secrets_or_outside(self):
        plan = dict(PLAN, files=PLAN["files"] + [{"path": ".env"}, {"path": "../x.py"}])
        self.engine(Model(plan=plan)).build("calculator")
        self.assertFalse((self.ws.root / ".env").exists())
        self.assertFalse((self.tmp / "x.py").exists())


# ------------------------------------------------------------------ tools and commands
class Backend:
    def __init__(self):
        self.opened = []

    def open_path(self, p):
        self.opened.append(p)


class FakeSettings:
    def __init__(self, ws):
        self.d = {"coding.workspace": str(ws)}

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v


class FakeLLM:
    is_generative = True
    name = "fake"

    def __init__(self, model):
        self.model = model

    def chat(self, messages, **kw):
        system, prompt = messages[0]["content"], messages[-1]["content"]
        purpose = "gace-plan" if "software architect" in system else "gace-fix" if "debugging engineer" in system else "gace-code"
        return self.model(prompt, system, 0, purpose)


class Ctx:
    def __init__(self, tmp, model):
        self.settings, self.backend, self.llm = FakeSettings(tmp / "work"), Backend(), FakeLLM(model)
        self.tmp = tmp

        class P:
            data_dir = tmp / "data"

        class C:
            paths = P()

        self.config = C()

    def allowed_roots(self):
        return [self.tmp]


class TestTools(Base):
    def test_gace_build_tool_end_to_end(self):
        ctx = Ctx(self.tmp, Model())
        t = coding.GaceBuild()
        self.assertTrue(t.always_confirm)
        self.assertIn("internet", t.confirmation_prompt({"request": "calc"}))
        res = t.run({"request": "build a calculator library", "name": "calc"}, ctx)
        self.assertTrue(res.success, res.message)
        self.assertEqual(res.data["status"], "verified")
        self.assertTrue((self.tmp / "work" / "calc" / "calc.py").exists())
        self.assertTrue(t.verify({}, res, ctx))
        self.assertTrue(ctx.backend.opened)

    def test_folder_outside_workspace_refused(self):
        ctx = Ctx(self.tmp, Model())
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, True)
        res = coding.GaceBuild().run({"request": "x", "folder": str(outside / "p")}, ctx)
        self.assertFalse(res.success)

    def test_failed_build_is_reported_as_failure(self):
        ctx = Ctx(self.tmp, Model(fix=BAD_CALC))
        res = coding.GaceBuild().run({"request": "build a calculator", "name": "c2"}, ctx)
        self.assertFalse(res.success)
        self.assertIn("couldn't verify", res.message)

    def test_resume_and_rollback_tools(self):
        ctx = Ctx(self.tmp, Model(calc=GOOD_CALC))
        coding.GaceBuild().run({"request": "build a calculator", "name": "c3"}, ctx)
        st = coding.GaceStatus().run({"project": "c3"}, ctx)
        self.assertTrue(st.success, st.message)
        self.assertIn("verified", st.message)
        rb = coding.GaceRollback()
        self.assertTrue(rb.always_confirm)
        res = rb.run({"project": "c3", "checkpoint": [c for c in Workspace(self.tmp / "work" / "c3", [self.tmp]).checkpoints() if "before-build" in c][0]}, ctx)
        self.assertTrue(res.success, res.message)
        self.assertFalse((self.tmp / "work" / "c3" / "calc.py").exists())
        self.assertFalse(coding.GaceResume().run({"project": "nope"}, ctx).success)

    def test_commands(self):
        m = coding.match
        self.assertEqual(m("build me a complete personal finance app with a FastAPI backend and React frontend")[0], "gace_build")
        self.assertEqual(m("create a full inventory management system with login and a dashboard")[0], "gace_build")
        self.assertEqual(m("mute my microphone"), ("privacy_mic", {"action": "mute", "_hard": True}))
        for existing in ("turn off the microphone", "mute your voice", "unmute yourself", "mute"):  # keep their old meaning
            self.assertIsNone(m(existing), existing)
        self.assertEqual(m("unmute the microphone")[1]["action"], "unmute")
        self.assertEqual(m("mute my mic")[0], "privacy_mic")
        self.assertEqual(m("mute the speaker")[0], "privacy_speaker")
        self.assertEqual(m("roll back the calc project")[0], "gace_rollback")
        self.assertEqual(m("continue the finance tracker project")[1]["project"], "finance tracker")
        for text in ("create an excel report of sales", "make an invoice document for Acme", "write a resume", "build", "make me a sandwich",
                     "what is the weather", "create a presentation about my app project"):
            self.assertIsNone(m(text), text)

    def test_commands_reach_tools_through_real_parser(self):
        from genesis.v3 import commands

        n = commands.nlu("build me a complete expense tracker app with a FastAPI backend", "hard")
        self.assertEqual(n.slots["tool"], "gace_build")
        n = commands.nlu("mute my mic", "hard")
        self.assertEqual(n.slots["tool"], "privacy_mic")

    def test_registered_with_right_risk(self):
        from genesis.v3.tools import TOOLS

        by = {t.name: t for t in TOOLS}
        for n in ("gace_build", "gace_resume", "gace_status", "gace_rollback", "privacy_mic", "privacy_speaker"):
            self.assertIn(n, by)
        self.assertTrue(by["gace_build"].always_confirm and by["gace_rollback"].always_confirm)
        self.assertEqual(by["privacy_mic"].risk.name, "LOW")


# ------------------------------------------------------------------ microphone / speaker mute (real VoiceManager)
class TestMute(unittest.TestCase):
    def setUp(self):
        from genesis.core.config import GenesisConfig
        from genesis.core.events import EventBus
        from genesis.core.session import SessionManager
        from genesis.core.settings import Settings
        from genesis.database.db import Database
        from genesis.database.repositories import Repositories
        from genesis.voice.manager import VoiceManager

        from .test_voice import WAKE_MARK, FakeMic, FakeSTT, FakeTTS, FakeWake, silence, tone

        self.WAKE, self.silence, self.tone = WAKE_MARK, silence, tone
        self.db = Database(":memory:")
        self.settings = Settings(Repositories(self.db).settings)
        self.settings.set("conversation.timeout_seconds", 2)
        bus = EventBus()
        self.session = SessionManager(self.settings, bus)
        self.heard = []

        def handler(text):
            self.heard.append(text)
            return "ok", False

        class VM(VoiceManager):
            pass

        privacy.patch_voice_manager(VM)
        privacy.GATE.mic_muted = privacy.GATE.speaker_muted = False
        self.mic, self.tts = FakeMic([]), FakeTTS()
        self.vm = VM(GenesisConfig(), self.settings, bus, handler, mic=self.mic, wake=FakeWake(refractory_s=0),
                     stt=FakeSTT(["open gmail", "open notepad", "again"]), tts=self.tts, session=self.session)
        self.addCleanup(self._reset)

    def _reset(self):
        self.vm.stop()
        privacy.GATE.mic_muted = privacy.GATE.speaker_muted = False
        privacy.GATE.vm = None

    @staticmethod
    def wait(cond, timeout=8.0):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.02)
        return False

    def say(self, n=1):
        for f in [self.WAKE] + self.tone(500) + self.silence(1300):
            self.mic.q.put(f)

    def test_mic_mute_stops_capture_and_wake_word(self):
        self.vm.start()
        self.assertTrue(self.wait(lambda: self.mic.is_open))
        snap = privacy.GATE.set_mic(True)
        self.assertTrue(self.wait(lambda: not self.mic.is_open), "the stream must really close")
        self.assertTrue(privacy.GATE.snapshot()["mic_really_off"])
        self.say()
        time.sleep(0.8)
        self.assertEqual(self.heard, [], "a muted microphone must not wake or record")
        self.assertFalse(self.mic.is_open)
        self.vm.push_to_talk()
        time.sleep(0.5)
        self.assertFalse(self.mic.is_open, "push-to-talk can't reopen a muted microphone")
        privacy.GATE.set_mic(False)
        self.assertTrue(self.wait(lambda: self.mic.is_open))

    def test_mic_unmute_listens_again(self):
        self.vm.start()
        privacy.GATE.set_mic(True)
        self.assertTrue(self.wait(lambda: not self.mic.is_open))
        privacy.GATE.set_mic(False)
        self.assertTrue(self.wait(lambda: self.mic.is_open))
        while not self.mic.q.empty():
            self.mic.q.get_nowait()
        self.say()
        self.assertTrue(self.wait(lambda: self.heard), "after unmuting, the wake word works again")

    def test_mute_survives_restart(self):
        privacy.GATE.attach(self.vm)
        privacy.GATE.set_mic(True)
        self.assertTrue(self.settings.get("privacy.mic_muted"))
        privacy.GATE.mic_muted = False  # simulate a fresh process
        privacy.GATE.preload(self.vm)
        self.assertTrue(privacy.GATE.mic_muted, "stays muted until the user turns it on")
        self.vm.start()
        time.sleep(0.5)
        self.assertFalse(self.mic.is_open)

    def test_speaker_mute_is_independent(self):
        self.vm.start()
        self.assertTrue(self.wait(lambda: self.mic.is_open))
        self.tts.spoken.clear()
        stopped = self.tts.stopped
        privacy.GATE.set_speaker(True)
        self.vm.speak("hello")
        self.tts.speak("direct")
        self.assertEqual(self.tts.spoken, [], "no speech while the speaker is muted")
        self.assertGreater(self.tts.stopped, stopped, "current speech is stopped")
        self.assertTrue(self.mic.is_open, "speaker mute doesn't touch the microphone")
        self.assertFalse(privacy.GATE.snapshot()["mic_muted"])
        privacy.GATE.set_speaker(False)
        self.vm.speak("hello again")
        self.assertIn("hello again", self.tts.spoken)

    def test_mic_mute_does_not_silence_speaker(self):
        self.vm.start()
        privacy.GATE.set_mic(True)
        self.tts.spoken.clear()
        self.vm.speak("still talking")
        self.assertIn("still talking", self.tts.spoken)

    def test_toggle_tools_and_state_reporting(self):
        ctx = Ctx(Path(tempfile.mkdtemp()), Model())
        privacy.GATE.attach(self.vm)
        res = coding.MicMute().run({"action": "toggle"}, ctx)
        self.assertTrue(res.data["mic_muted"])
        self.assertIn("muted", res.message)
        res = coding.MicMute().run({"action": "unmute"}, ctx)
        self.assertFalse(res.data["mic_muted"])
        res = coding.SpeakerMute().run({"action": "mute"}, ctx)
        self.assertTrue(res.data["speaker_muted"])
        self.assertEqual(self.vm.status()["speaker_muted"], True)
        self.assertEqual(self.vm.status()["mic_muted"], False)

    def test_listeners_and_log(self):
        seen = []
        privacy.GATE.listeners.append(seen.append)
        self.addCleanup(privacy.GATE.listeners.remove, seen.append)
        privacy.GATE.attach(self.vm)
        privacy.GATE.toggle_mic("hotkey")
        self.assertEqual(seen[-1]["mic_muted"], True)
        self.assertEqual(privacy.GATE.changes[-1]["source"], "hotkey")

    def test_hotkey_spec_parses(self):
        from genesis.ui.hotkey import parse_hotkey

        mods, vk = parse_hotkey(privacy.DEFAULT_HOTKEY)
        self.assertEqual(mods, 0x0002 | 0x0004)
        self.assertEqual(vk, ord("M"))


if __name__ == "__main__":
    unittest.main()
