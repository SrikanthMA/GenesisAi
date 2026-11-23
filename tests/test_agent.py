"""Planner → Executor → Verifier tests."""
import json
import unittest

from genesis.agent.executor import UnresolvedTemplate, resolve_templates
from genesis.agent.plan import Plan, Reply, StepStatus
from genesis.agent.verifier import summarize
from genesis.nlp import rules
from genesis.nlp.intents import Intent
from genesis.tools.base import Tool, ToolResult, obj
from tests.helpers import GenesisTestCase, ScriptedLLM


class TestTemplates(unittest.TestCase):
    def test_resolution(self):
        plan = Plan("x")
        s1 = plan.add("compose_email", {})
        s1.status = StepStatus.SUCCEEDED
        s1.result = ToolResult.ok("ok", subject="Hi", to=["a@b.c"])
        self.assertEqual(resolve_templates({"s": "{{steps.1.data.subject}}", "t": "{{steps.1.data.to}}", "m": "Re: {{steps.1.data.subject}}"}, plan),
                         {"s": "Hi", "t": ["a@b.c"], "m": "Re: Hi"})

    def test_failed_dependency(self):
        plan = Plan("x")
        s1 = plan.add("a", {})
        s1.status = StepStatus.FAILED
        with self.assertRaises(UnresolvedTemplate):
            resolve_templates("{{steps.1.data.path}}", plan)


class FlakyTool(Tool):
    name = "flaky_tool"
    description = "test"
    parameters = obj({})

    def run(self, args, ctx):
        return ToolResult.ok("I did it.")

    def verify(self, args, result, ctx):
        return False  # post-condition not met


class CrashTool(Tool):
    name = "crash_tool"
    description = "test"
    parameters = obj({})

    def run(self, args, ctx):
        raise RuntimeError("kaboom")


class TestExecutor(GenesisTestCase):
    def test_plan_for_open_app(self):
        nlu = rules.parse("open chrome")
        plan = self.core.planner.build(nlu, self.core.dialogue.state("c"))
        self.assertIsInstance(plan, Plan)
        self.assertEqual([s.tool for s in plan.steps], ["open_application"])

    def test_verification_failure_is_reported_as_failure(self):
        self.core.registry.register(FlakyTool())
        plan = Plan("x")
        plan.add("flaky_tool", {}, "Do the flaky thing")
        out = self.core.executor.run(plan)
        self.assertEqual(out.status, "failed")
        self.assertIn("couldn't verify", summarize(plan).text)

    def test_tool_crash_never_claims_success(self):
        self.core.registry.register(CrashTool())
        plan = Plan("x")
        plan.add("crash_tool", {}, "Crash")
        plan.add("open_url", {"url": "https://example.com"}, "Open")
        out = self.core.executor.run(plan)
        self.assertEqual(out.status, "failed")
        self.assertEqual(plan.steps[1].status, StepStatus.SKIPPED)
        rep = summarize(plan)
        self.assertFalse(rep.success)
        self.assertIn("Something went wrong", rep.text)
        self.assertEqual(self.urls_opened(), [])

    def test_unknown_tool(self):
        plan = Plan("x")
        plan.add("format_disk", {}, "Format")
        self.assertEqual(self.core.executor.run(plan).status, "failed")

    def test_invalid_args_rejected_before_run(self):
        plan = Plan("x")
        plan.add("open_url", {"url": "https://x.com", "evil": "1"}, "Open")
        self.core.executor.run(plan)
        self.assertEqual(plan.steps[0].status, StepStatus.FAILED)
        self.assertEqual(self.urls_opened(), [])

    def test_denied_tool_is_blocked(self):
        from genesis.security.permissions import RiskLevel

        self.svc.permissions.set_policy("open_url", "deny", RiskLevel.LOW)
        r = self.say("go to example.com")
        self.assertIn("disabled", r.text)
        self.assertEqual(self.urls_opened(), [])

    def test_untrusted_content_escalates_following_steps(self):
        plan = Plan("x")
        (self.home / "Documents" / "a.txt").write_text("hello")
        plan.add("read_file", {"path": "a.txt"}, "Read")
        plan.add("open_url", {"url": "https://example.com"}, "Open")
        out = self.core.executor.run(plan)
        self.assertEqual(out.status, "awaiting_confirmation")
        self.assertEqual(out.confirmation.tool, "open_url")


class TestLLMPlanner(GenesisTestCase):
    def _planner_llm(self, steps):
        return ScriptedLLM(lambda m, j: json.dumps({"steps": steps}))

    def test_valid_llm_plan_accepted(self):
        self.svc.llm.primary = self._planner_llm([{"tool": "open_folder", "args": {"folder": "downloads"}, "description": "Open"}])
        self.svc.llm.primary.is_generative = True
        plan = self.core.planner.plan_with_llm("show me where my downloads went")
        self.assertIsInstance(plan, Plan)
        self.assertEqual(plan.source, "llm")

    def test_unknown_or_forbidden_tool_rejected(self):
        for steps in ([{"tool": "run_shell", "args": {"cmd": "format c:"}}],
                      [{"tool": "shutdown_computer", "args": {}}],
                      [{"tool": "open_url", "args": {"url": "https://x.com", "extra": 1}}]):
            self.svc.llm.primary = self._planner_llm(steps)
            self.assertIsInstance(self.core.planner.plan_with_llm("do something"), Reply, steps)


class TestMultiStep(GenesisTestCase):
    connect_gmail = True

    def test_find_rename_attach_draft(self):
        pdf = self.home / "Downloads" / "nlp_assignment.pdf"
        pdf.write_bytes(b"%PDF-1.4 test")
        self.svc.repos.contacts.upsert("Professor Rao", email="rao@university.edu", alias="professor")
        r = self.say("Find the latest assignment PDF, rename it, open Gmail, attach it, and draft an email to my professor.")
        self.assertEqual(r.status, "awaiting_input")
        self.assertIn("rename it to", r.text)
        r = self.say("NLP_Assignment_Final")
        self.assertEqual(r.status, "awaiting_confirmation")  # rename is medium risk
        r = self.say("yes")
        self.assertEqual(r.status, "awaiting_input")  # continues the queue: needs the message body
        self.assertEqual(r.question, "What should I say?")
        self.assertTrue((self.home / "Downloads" / "NLP_Assignment_Final.pdf").exists())
        r = self.say("Please find my NLP assignment attached.")
        self.assertEqual(r.status, "done", r.text)
        self.assertEqual(len(self.google.drafts), 1)
        msg = self.google.parsed("r-1")
        self.assertEqual(msg["To"], "rao@university.edu")
        names = [part.get_filename() for part in msg.walk() if part.get_filename()]
        self.assertEqual(names, ["NLP_Assignment_Final.pdf"])
        self.assertIn("https://mail.google.com/mail/u/0/#inbox", self.urls_opened())
        self.assertTrue(any("#drafts?compose=" in u for u in self.urls_opened()))

    def test_failure_stops_remaining_requests(self):
        r = self.say("open photoshop and then open gmail")
        self.assertIn("couldn't find", r.text)
        self.assertIn("didn't continue", r.text)
        self.assertEqual(self.urls_opened(), [])


class TestIntentPlanCoverage(GenesisTestCase):
    def test_every_actionable_intent_plans(self):
        st = self.core.dialogue.state("c")
        samples = ["open chrome", "close chrome", "open my downloads folder", "go to example.com", "search for cats",
                   "remind me tomorrow at 9 am to call mom", "lock my computer", "shut down the computer", "restart my pc",
                   "open my genesis project", "turn off the microphone", "what do you remember about me", "check my email",
                   "create a folder called X", "turn the volume down"]
        for s in samples:
            nlu = rules.parse(s)
            self.assertNotEqual(nlu.intent, Intent.UNKNOWN, s)
            d = self.core.planner.build(nlu, st)
            self.assertIsInstance(d, Plan, s)
            for step in d.steps:
                self.assertIn(step.tool, self.core.registry, s)


if __name__ == "__main__":
    unittest.main()
