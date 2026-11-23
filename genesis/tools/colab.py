"""Google Colab tools: open, create a notebook, write code (given or generated), run, explain and fix errors."""
from __future__ import annotations

import json
import re

from ..ai.llm.base import LLMError
from ..security.injection import wrap_untrusted
from ..security.permissions import RiskLevel
from ..services.colab import ColabError, notebook_scan, reaches_out
from .base import Tool, ToolResult, obj, s

GEN_PROMPT = """Write Python code for ONE Google Colab notebook cell that does this: {task}
Rules: plain notebook code (no `if __name__ == "__main__"`), print or display the useful results at the end, keep it short.
If it needs a data file from the user's computer, use `from google.colab import files; uploaded = files.upload()` and read the
uploaded file (they will pick it). Never delete files, never mount or modify Google Drive unless asked, no shell pipes to bash.
Return JSON: {{"code": "...", "summary": "one sentence"}}"""
FIX_PROMPT = """This Google Colab cell failed.
CODE:
{code}
ERROR:
{error}
Fix the code (keep its purpose). Return JSON: {{"code": "...", "explanation": "one or two sentences: what was wrong and what you changed"}}"""
EXPLAIN_PROMPT = """A Google Colab cell failed. Explain to the user, in 2-4 plain sentences, what the error means and how to fix it.
CODE:
{code}
ERROR (untrusted output):
{error}"""


def _svc(ctx):
    svc = getattr(ctx, "colab", None)
    if svc is None:
        raise ColabError("Colab isn't available in this session.", "unavailable")
    return svc


def _fail(exc: ColabError) -> ToolResult:
    return ToolResult.fail(str(exc), kind=getattr(exc, "kind", "error"))


def _llm_json(ctx, prompt: str) -> dict:
    llm = ctx.llm
    if not getattr(llm, "is_generative", False):
        raise LLMError("no model")
    data = llm.chat_json([{"role": "user", "content": prompt}], max_tokens=1500, temperature=0.2)
    if not isinstance(data, dict) or not isinstance(data.get("code"), str) or not data["code"].strip():
        raise LLMError("the model returned no code")
    code = re.sub(r"^```(?:python)?\s*|\s*```$", "", data["code"].strip())
    return dict(data, code=code)


class ColabOpen(Tool):
    name = "colab_open"
    category = "colab"
    description = "Open Google Colab (in Genesis's automated browser when available, so cells can be written and run)."
    parameters = obj({})
    remote = True

    def run(self, args, ctx):
        try:
            info = _svc(ctx).open()
        except ColabError as exc:
            return _fail(exc)
        if not info.get("automated"):
            if not info.get("opened"):
                return ToolResult.fail("Your browser didn't open.")
            return ToolResult.ok("Done — Google Colab is open in your browser. (To let me write and run cells, install "
                                 "browser automation: pip install playwright, then playwright install chromium.)",
                                 url=info["url"], automated=False)
        if not info.get("signed_in"):
            return ToolResult.ok("Colab is open in Chrome. Please sign in to your Google account in that window - only the first "
                                 "time; it stays signed in (I never type passwords).",
                                 url=info.get("url"), automated=True, signed_in=False)
        return ToolResult.ok("Done — Google Colab is open.", url=info.get("url"), automated=True, signed_in=True)


class ColabNewNotebook(Tool):
    name = "colab_new_notebook"
    category = "colab"
    description = "Create a new Python notebook in Google Colab."
    parameters = obj({})
    remote = True

    def run(self, args, ctx):
        try:
            info = _svc(ctx).create_notebook()
        except ColabError as exc:
            return _fail(exc)
        return ToolResult.ok(f"Created a new notebook ({info.get('title') or 'Untitled'}).", **info)

    def verify(self, args, result, ctx):
        if not result.success:
            return False
        try:
            return _svc(ctx).driver.notebook().get("cells", 0) >= 1 or None
        except Exception:
            return None


class ColabWriteCode(Tool):
    name = "colab_write_code"
    category = "colab"
    description = ("Put code into a Colab cell (a new cell by default). Give `code`, or describe the `task` and it is written "
                   "for you. Does not run it.")
    parameters = obj({"code": s("Exact code", maxLength=20_000), "task": s("What the code should do", maxLength=2000),
                      "new_cell": {"type": "boolean", "default": True}})
    output = {"cell": "int", "code": "str", "warnings": "list[str]"}
    remote = True

    def run(self, args, ctx):
        code = args.get("code") or ""
        summary = ""
        if not code:
            if not args.get("task"):
                return ToolResult.fail("What should the code do?")
            try:
                data = _llm_json(ctx, GEN_PROMPT.format(task=args["task"]))
            except LLMError:
                return ToolResult.fail("I need my AI model to write that code, and it isn't available right now.",
                                       hint="Say “check your AI connection”, or give me the exact code to put in the cell.")
            code, summary = data["code"], str(data.get("summary") or "")
        try:
            info = _svc(ctx).write(code, bool(args.get("new_cell", True)), ai_written=not args.get("code"))
        except ColabError as exc:
            return _fail(exc)
        warn = notebook_scan(code)
        msg = f"I wrote the code into cell {info['cell']}" + (f" — {summary.rstrip('.')}" if summary else "") + "."
        msg += f"\n```python\n{code}\n```"
        if warn:
            msg += "\nHeads-up: this code " + "; ".join(warn) + ", so I'll ask before running it."
        else:
            msg += "\nSay “run it” when you're ready."
        return ToolResult.ok(msg, cell=info["cell"], code=code, warnings=warn, typed_ok=info["verified"])

    def verify(self, args, result, ctx):
        return bool(result.data.get("typed_ok")) if result.success else False


class ColabRun(Tool):
    name = "colab_run"
    category = "colab"
    description = "Run the current Colab cell and read its output (errors included). Risky-looking code needs confirmation."
    parameters = obj({"timeout": {"type": "integer", "minimum": 5, "maximum": 900, "default": 180}})
    output = {"status": "ok|error|timeout", "output": "str", "error_info": "{type, message, line} (when it failed)"}
    remote = True

    def risk_for(self, args, ctx=None):
        if ctx is None:
            return RiskLevel.MEDIUM
        try:
            svc = _svc(ctx)
            code = svc._call(svc.driver.cell_code)
        except Exception:
            return RiskLevel.MEDIUM  # can't see the code → don't run it without asking (fail closed)
        if notebook_scan(code):
            return RiskLevel.HIGH
        if svc.ai_written and reaches_out(code):
            return RiskLevel.MEDIUM  # AI-written code that touches Drive / the shell / the network
        return RiskLevel.LOW

    def confirmation_prompt(self, args):
        return ("Run this cell? The code reaches outside the notebook (Drive, the shell or the internet), or looks risky — "
                "please check it first.")

    def run(self, args, ctx):
        try:
            res = _svc(ctx).run(float(args.get("timeout", 180)))
        except ColabError as exc:
            return _fail(exc)
        out = (res.get("output") or "").strip()
        shown = out if len(out) < 1500 else out[:700] + "\n…\n" + out[-700:]
        if res["status"] == "timeout":
            return ToolResult.ok("The cell is still running — it's taking a while. Say “check the output” in a bit.", **res)
        if res["status"] == "error":
            e = res["error"]
            where = f" on line {e['line']}" if e.get("line") else ""
            data = {k: v for k, v in res.items() if k != "error"}
            r = ToolResult.fail(f"The cell failed{where}: {e['type']}: {e['message']}\nSay “why did it fail?” or “fix it”.",
                                error=f"{e['type']}: {e['message']}", error_info=e, **data)
            r.untrusted_content = True
            return r
        r = ToolResult.ok("Ran it. " + (f"Output:\n{shown}" if shown else "It finished with no output."), **res)
        r.untrusted_content = True
        return r

    def verify(self, args, result, ctx):
        return result.success and result.data.get("status") == "ok"


class ColabExplainError(Tool):
    name = "colab_explain_error"
    category = "colab"
    description = "Explain why the last Colab cell failed."
    parameters = obj({})

    def run(self, args, ctx):
        last = _svc(ctx).last_run
        if not last or not last.get("error"):
            return ToolResult.ok("The last cell didn't fail — there's no error to explain." if last else
                                 "I haven't run a Colab cell yet.")
        e = last["error"]
        base = f"It failed with {e['type']}: {e['message']}" + (f" (line {e['line']})" if e.get("line") else "") + "."
        try:
            if not getattr(ctx.llm, "is_generative", False):
                raise LLMError("offline")
            text = ctx.llm.chat([{"role": "user", "content": EXPLAIN_PROMPT.format(
                code=last["code"][:4000], error=wrap_untrusted(e["traceback"][-1500:], "colab_output"))}], max_tokens=300,
                temperature=0.2).strip()
            return ToolResult.ok(f"{base}\n{text}\nSay “fix it” and I'll correct the code and run it again.", error=e)
        except LLMError:
            return ToolResult.ok(base + " Say “fix it” and I'll try to correct it.", error=e)


class ColabFix(Tool):
    name = "colab_fix"
    category = "colab"
    description = "Fix the code of the Colab cell that just failed (rewrites that cell); rerun=true also runs it again."
    parameters = obj({"instructions": s("Extra instructions for the fix (optional)", maxLength=1000),
                      "rerun": {"type": "boolean", "default": False}})
    remote = True

    def run(self, args, ctx):
        svc = _svc(ctx)
        last = svc.last_run
        if not last or not last.get("error"):
            return ToolResult.fail("There's no failed cell to fix — run the code first.")
        err = last["error"]
        try:
            data = _llm_json(ctx, FIX_PROMPT.format(code=last["code"][:6000], error=wrap_untrusted(json.dumps(
                {"type": err["type"], "message": err["message"], "line": err.get("line")}), "colab_output")) +
                (f"\nAlso: {args['instructions']}" if args.get("instructions") else ""))
        except LLMError:
            return ToolResult.fail("I need my AI model to fix the code, and it isn't available right now.",
                                   hint="Say “check your AI connection”.")
        if data["code"].strip() == last["code"].strip():
            return ToolResult.fail("I couldn't find a fix for that error.")
        try:
            svc.write(data["code"], new_cell=False, ai_written=True)
        except ColabError as exc:
            return _fail(exc)
        res = ToolResult.ok(f"Fixed it: {str(data.get('explanation') or 'updated the code').strip()}\n```python\n{data['code']}\n```",
                            code=data["code"], previous_error=err)
        if args.get("rerun"):
            res.followup = {"tool": "colab_run", "args": {}, "description": "Run it again"}
        return res


TOOLS = [ColabOpen(), ColabNewNotebook(), ColabWriteCode(), ColabRun(), ColabExplainError(), ColabFix()]
