"""Business Automation tools (3.4) - the bridge between Genesis's planner / voice / Business page and ``v3.biz``.

Every tool: finds the files (inside the folders Genesis is allowed to read), runs the job with a stage-by-stage log,
saves results as NEW files in Documents\\Genesis Business, records the run in the task history, and reports problems
plainly. Nothing here sends e-mail, deletes anything or overwrites a source file, so the tools are LOW risk; sending a
draft still goes through the existing ``gmail_send_draft`` tool, which always asks first."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Callable

from ..tools.base import Tool, ToolResult, obj, s
from .biz import common, workflows as W
from .biz.common import BizError, Run

log = logging.getLogger("genesis.v3.business")
FILE = s("File name, path, or part of the name. Leave empty for the file(s) dropped on the Business page / the newest one.",
         maxLength=600)
FILES = {"type": "array", "items": {"type": "string", "maxLength": 600}, "maxItems": 60,
         "description": "File names, paths or a folder. Empty = the files dropped on the Business page."}


def _files(ctx, ref, kind: str, many: bool = False) -> list[str]:
    """Resolve what the user named and make sure every file is somewhere Genesis may read."""
    paths = common.resolve_files(ref if ref not in (None, [], "") else "", kind, many=many)
    roots = None
    try:
        roots = ctx.allowed_roots()
    except Exception:
        roots = None
    if roots:
        from ..security.validators import ValidationError, validate_path

        for p in paths:
            try:
                validate_path(str(p), allowed_roots=list(roots) + [common.out_dir(ctx)], must_exist=True)
            except ValidationError as exc:
                raise BizError(f"I'm not allowed to read {p.name}: {exc} (add the folder in Settings → Security if you want me to).") from exc
    return [str(p) for p in paths]


STEP_LISTENERS: list = []  # the Business page listens here to show progress


def _notify(step: dict) -> None:
    for fn in list(STEP_LISTENERS):
        try:
            fn(step)
        except Exception:
            pass


def run_job(ctx, kind: str, task: str, fn: Callable[[Run], dict], open_after: bool = True, on_step=None) -> ToolResult:
    """Run one job with full logging; used by every tool and by the Business page."""
    run = Run(task, kind, on_step=on_step or _notify)
    run.log("UNDERSTAND", task)
    try:
        data = fn(run)
        run.finish()
    except BizError as exc:
        run.fail("EXECUTE", str(exc))
        data = None
        err = str(exc)
    except Exception as exc:  # unexpected: log it, tell the user, keep Genesis running
        log.exception("business job failed: %s", task)
        run.fail("EXECUTE", f"Unexpected error: {exc}")
        data = None
        err = f"That didn't work: {exc}"
    try:
        common.history(ctx).save(run)
    except Exception:  # pragma: no cover
        log.exception("could not save business history")
    if data is None:
        res = ToolResult.fail(err, steps=run.steps, outputs=run.outputs)
        return res
    msg = data.get("message", "Done.")
    if run.failed:
        msg += "\nProblems: " + " ".join(run.failed[:5])
    if run.warnings:
        msg += "\nNotes: " + " ".join(dict.fromkeys(run.warnings[:5]))
    if run.outputs:
        msg += "\nFolder: " + str(Path(run.outputs[0]).parent)
    clean = {k: v for k, v in data.items() if k not in ("message", "profiles", "meeting", "messages", "facts", "untrusted")}
    res = ToolResult.ok(msg, **clean, outputs=run.outputs, steps=run.steps, warnings=run.warnings, file_path=(run.outputs or [None])[0])
    res.untrusted_content = bool(data.get("untrusted"))
    res.verified = bool(run.outputs) and all(Path(p).is_file() for p in run.outputs) if run.outputs else None
    if open_after and run.outputs and getattr(ctx, "backend", None) is not None:
        try:
            ctx.backend.open_path(run.outputs[0])
        except Exception:
            pass
    return res


class _Biz(Tool):
    category = "business"
    kind = "task"

    def job(self, args: dict, ctx, run: Run) -> dict:  # pragma: no cover - overridden
        raise NotImplementedError

    def task(self, args: dict) -> str:
        bits = [f"{k}={v}" for k, v in args.items() if v not in (None, "", [], False)]
        return self.name.replace("business_", "").replace("_", " ") + (": " + ", ".join(map(str, bits))[:300] if bits else "")

    def run(self, args, ctx):
        return run_job(ctx, self.kind, self.task(args), lambda run: self.job(args, ctx, run))

    def verify(self, args, result, ctx):
        return result.verified


class ExcelAnalyse(_Biz):
    name, kind = "business_excel_analyse", "excel"
    description = ("Analyse an Excel/CSV file: structure, column types, duplicates, missing values, inconsistent data and "
                   "statistics. Saves an analysis workbook. Use for 'analyse this workbook', 'find duplicates', 'check this data'.")
    parameters = obj({"file": FILE})

    def job(self, a, ctx, run):
        return W.analyse_excel(ctx, run, _files(ctx, a.get("file"), "excel")[0])


class ExcelClean(_Biz):
    name, kind = "business_excel_clean", "excel"
    description = ("Clean an Excel/CSV file into a NEW workbook: remove duplicate and empty rows, trim spaces, fix numbers/dates "
                   "stored as text, unify spellings. Missing values are highlighted, never filled in.")
    parameters = obj({"file": FILE, "key": {"type": "array", "items": {"type": "string"}, "maxItems": 6,
                                            "description": "Columns that identify a record (duplicates = same values here)"}})

    def job(self, a, ctx, run):
        return W.clean_excel(ctx, run, _files(ctx, a.get("file"), "excel")[0], a.get("key") or None)


class ExcelEdit(_Biz):
    name, kind = "business_excel_edit", "excel"
    description = ("Change a spreadsheet from a plain request: sort, filter, add a formula column, totals row, pivot/summary "
                   "by a column, chart. E.g. 'sort by Amount descending', 'add a column Total = Qty * Price', 'chart Sales by Month'.")
    parameters = obj({"file": FILE, "request": s("What to do, in plain words", maxLength=1000), "sheet": s("Sheet name", maxLength=60)},
                     ["request"])

    def job(self, a, ctx, run):
        return W.edit_excel(ctx, run, _files(ctx, a.get("file"), "excel")[0], a["request"], sheet=a.get("sheet") or None)


class ExcelMerge(_Biz):
    name, kind = "business_excel_merge", "excel"
    description = ("Combine several Excel/CSV files into one workbook. mode 'append' stacks rows (with a Source column); "
                   "'join' matches rows on a key column.")
    parameters = obj({"files": FILES, "mode": {"type": "string", "enum": ["append", "join"], "default": "append"},
                      "key": s("Key column for join", maxLength=80)})

    def job(self, a, ctx, run):
        return W.merge_excel(ctx, run, _files(ctx, a.get("files"), "excel", many=True), a.get("mode") or "append", a.get("key") or None)


class ExcelConvert(_Biz):
    name, kind = "business_excel_convert", "excel"
    description = "Convert a CSV to a formatted Excel workbook, or export a spreadsheet to CSV or PDF."
    parameters = obj({"file": FILE, "to": {"type": "string", "enum": ["xlsx", "csv", "pdf"], "default": "xlsx"}})

    def job(self, a, ctx, run):
        return W.convert_excel(ctx, run, _files(ctx, a.get("file"), "excel")[0], a.get("to") or "xlsx")


class ExcelSheets(_Biz):
    name, kind = "business_excel_sheets", "excel"
    description = "Add, rename or delete a worksheet - done in a copy of the workbook; the original is not changed."
    parameters = obj({"file": FILE, "add": s("Name of a sheet to add", maxLength=31), "rename_from": s("Sheet to rename", maxLength=31),
                      "rename_to": s("New name", maxLength=31), "delete": s("Sheet to delete", maxLength=31)})

    def job(self, a, ctx, run):
        if not (a.get("add") or a.get("delete") or (a.get("rename_from") and a.get("rename_to"))):
            raise BizError("Tell me which sheet to add, rename or delete.")
        rn = [a["rename_from"], a["rename_to"]] if a.get("rename_from") and a.get("rename_to") else None
        return W.sheets_excel(ctx, run, _files(ctx, a.get("file"), "excel")[0], a.get("add") or None, rn, a.get("delete") or None)


class Report(_Biz):
    name, kind = "business_report", "workflow"
    description = ("Sales / financial / inventory / employee report from a spreadsheet: an Excel dashboard with summaries and "
                   "charts plus a Word management report. Use for 'monthly sales dashboard', 'financial report from this "
                   "spreadsheet', 'inventory report', 'employee report'.")
    parameters = obj({"file": FILE, "kind": {"type": "string", "enum": ["", "sales", "financial", "inventory", "employee"], "default": ""},
                      "reorder_level": {"type": "number", "description": "Low-stock level when the sheet has no reorder column"}})

    def job(self, a, ctx, run):
        return W.report_excel(ctx, run, _files(ctx, a.get("file"), "excel")[0], a.get("kind") or "", reorder_level=a.get("reorder_level"))


class Invoices(_Biz):
    name, kind = "business_process_invoices", "workflow"
    description = ("Read invoices (PDF, Word, text, scanned images), extract vendor, invoice number, dates, line items and "
                   "totals, check the arithmetic, and enter everything into one Excel workbook with a summary.")
    parameters = obj({"files": FILES})

    def job(self, a, ctx, run):
        return W.process_invoices(ctx, run, _files(ctx, a.get("files"), "document", many=True))


class Minutes(_Biz):
    name, kind = "business_meeting_minutes", "workflow"
    description = ("Turn meeting notes or a transcript (a file, or pasted text) into minutes: decisions, action items, owners, "
                   "deadlines, plus a follow-up e-mail draft saved as a text file. Nothing is sent.")
    parameters = obj({"file": FILE, "text": s("The notes themselves, if pasted", maxLength=40_000)})

    def job(self, a, ctx, run):
        if (a.get("text") or "").strip():
            return W.meeting_minutes(ctx, run, text=a["text"])
        return W.meeting_minutes(ctx, run, _files(ctx, a.get("file"), "document")[0])


class Research(_Biz):
    name, kind = "business_research", "research"
    remote = True  # searches the web (only when you ask for research)
    description = ("Research a question on the web and write a cited report (Word, optionally PDF): executive summary, findings, "
                   "conflicting information, assumptions, sources. Use for market research, competitor analysis, technical research.")
    parameters = obj({"question": s("What to research", maxLength=600),
                      "kind": s("e.g. market research report, competitor analysis, technical report", maxLength=60),
                      "pdf": {"type": "boolean", "default": False}}, ["question"])

    def job(self, a, ctx, run):
        return W.research_report(ctx, run, a["question"], a.get("kind") or "research report", bool(a.get("pdf")))


class Compare(_Biz):
    name, kind = "business_compare", "research"
    remote = True
    description = "Compare companies, products or services in a table (Word + Excel) using web sources; unknown cells say 'not found'."
    parameters = obj({"items": {"type": "array", "items": {"type": "string", "maxLength": 80}, "minItems": 2, "maxItems": 6},
                      "criteria": {"type": "array", "items": {"type": "string", "maxLength": 60}, "maxItems": 8}}, ["items"])

    def job(self, a, ctx, run):
        return W.compare_report(ctx, run, a["items"], a.get("criteria") or None)


class CreateDocument(_Biz):
    name, kind = "business_create_document", "document"
    description = ("Create a formatted Word document: invoice, quotation, proposal, project report, technical documentation, "
                   "report, letter, resume. For invoices/quotations give items like '3 widgets at 1200, 18% GST'.")
    parameters = obj({"kind": s("invoice | quotation | proposal | project report | technical documentation | report | letter | resume", maxLength=40),
                      "topic": s("Title / what it is about / who it is for", maxLength=300),
                      "details": s("Facts to include (items and prices, names, dates, points to cover)", maxLength=6000),
                      "pdf": {"type": "boolean", "default": False}}, ["kind"])

    def job(self, a, ctx, run):
        return W.create_document(ctx, run, a["kind"], a.get("topic") or "", a.get("details") or "", pdf=bool(a.get("pdf")))


class Summarise(_Biz):
    name, kind = "business_summarise", "document"
    description = "Summarise one or many documents (PDF, Word, text, spreadsheets) into a single Word summary with key points."
    parameters = obj({"files": FILES})

    def job(self, a, ctx, run):
        return W.summarise_documents(ctx, run, _files(ctx, a.get("files"), "any", many=True))


class CompareDocuments(_Biz):
    name, kind = "business_compare_documents", "document"
    description = "Compare two Word documents and list what was changed, added and removed."
    parameters = obj({"files": FILES})

    def job(self, a, ctx, run):
        return W.compare_documents(ctx, run, _files(ctx, a.get("files"), "word", many=True))


class RewriteSection(_Biz):
    name, kind = "business_rewrite_section", "document"
    description = "Rewrite one section of a Word document (in a copy), keeping its facts and the rest of the document as is."
    parameters = obj({"file": FILE, "section": s("Heading of the section", maxLength=200),
                      "instruction": s("How to rewrite it", maxLength=600)}, ["section"])

    def job(self, a, ctx, run):
        return W.rewrite_document(ctx, run, _files(ctx, a.get("file"), "word")[0], a["section"], a.get("instruction") or "")


class ToPdf(_Biz):
    name, kind = "business_to_pdf", "document"
    description = "Convert a Word document or spreadsheet to PDF."
    parameters = obj({"file": FILE})

    def job(self, a, ctx, run):
        return W.document_to_pdf(ctx, run, _files(ctx, a.get("file"), "any")[0])


class Extract(_Biz):
    name, kind = "business_extract", "extract"
    description = ("Extract structured data from files (PDF, Word, Excel, CSV, text, images via OCR) into Excel with source "
                   "references: tables, dates, amounts, e-mails, phone numbers; or give 'columns' to build a table of records.")
    parameters = obj({"files": FILES, "what": s("What to look for (optional)", maxLength=200),
                      "columns": {"type": "array", "items": {"type": "string", "maxLength": 50}, "maxItems": 12}})

    def job(self, a, ctx, run):
        return W.extract_data(ctx, run, _files(ctx, a.get("files"), "any", many=True), a.get("what") or "", a.get("columns") or None)


class EmailTriage(_Biz):
    name, kind = "business_email_triage", "email"
    remote = True
    description = ("Read recent Gmail and sort it by priority (urgent / important / normal / low) and category, with deadlines "
                   "and who needs a reply. Read-only.")
    parameters = obj({"query": s("Gmail search, default today's mail", maxLength=200), "save": {"type": "boolean", "default": False},
                      "unanswered": {"type": "boolean", "default": False, "description": "Only mail from the last week you haven't replied to"}})

    def run(self, args, ctx):
        return run_job(ctx, self.kind, self.task(args), lambda run: W.email_triage(
            ctx, run, args.get("query") or "newer_than:1d", save=bool(args.get("save")), unanswered=bool(args.get("unanswered"))),
            open_after=bool(args.get("save")))


class EmailDrafts(_Biz):
    name, kind = "business_email_drafts", "email"
    remote = True
    description = ("Write reply drafts for e-mails that need an answer (or for unanswered e-mails) and save them in Gmail "
                   "Drafts for review. Never sends.")
    parameters = obj({"query": s("Gmail search, default unread mail from today", maxLength=200),
                      "instruction": s("How to reply (optional)", maxLength=400), "unanswered": {"type": "boolean", "default": False}})

    def run(self, args, ctx):
        return run_job(ctx, self.kind, self.task(args), lambda run: W.email_drafts(
            ctx, run, args.get("query") or "is:unread newer_than:1d", instruction=args.get("instruction") or "",
            unanswered=bool(args.get("unanswered"))), open_after=False)


class History(Tool):
    name = "business_history"
    category = "business"
    description = "List recent Business Automation tasks and the files they produced."
    parameters = obj({"limit": {"type": "integer", "default": 8}})

    def run(self, args, ctx):
        runs = common.history(ctx).recent(max(1, min(int(args.get("limit") or 8), 30)))
        if not runs:
            return ToolResult.ok("No business tasks have been run yet.", runs=[])
        lines = [f"{i}. {r['task'][:70]} - {r['status']}" + (f" → {', '.join(Path(p).name for p in r['outputs'][:2])}" if r["outputs"] else "")
                 for i, r in enumerate(runs, 1)]
        use = common.history(ctx).usage()
        return ToolResult.ok("Recent business tasks:\n" + "\n".join(lines) + f"\nAI model calls so far: {use['calls']} "
                             f"({use['cached']} answered from cache, about {use['approx_prompt_tokens'] + use['approx_answer_tokens']:,} tokens).",
                             runs=[{k: r[k] for k in ("task", "status", "outputs", "seconds")} for r in runs], usage=use)


class OpenFolder(Tool):
    name = "business_open_folder"
    category = "business"
    description = "Open the folder where Business Automation saves its results."
    parameters = obj({})

    def run(self, args, ctx):
        p = common.out_dir(ctx)
        try:
            ctx.backend.open_path(str(p))
        except Exception as exc:
            return ToolResult.fail(f"I couldn't open {p}: {exc}")
        return ToolResult.ok(f"Opened {p}.", path=str(p))


TOOLS = [ExcelAnalyse(), ExcelClean(), ExcelEdit(), ExcelMerge(), ExcelConvert(), ExcelSheets(), Report(), Invoices(), Minutes(),
         Research(), Compare(), CreateDocument(), Summarise(), CompareDocuments(), RewriteSection(), ToPdf(), Extract(),
         EmailTriage(), EmailDrafts(), History(), OpenFolder()]


# ------------------------------------------------------------------ natural-language commands
_XL = r"(?:excel(?: file| workbook| sheet)?|spreadsheets?|workbooks?|worksheets?|csv(?: file)?|sheets?|data(?:set)?)"
_REF = r"(?P<f>.+?)"
_CMD: list[tuple[re.Pattern, str, Callable]] = []


def _c(pattern: str, tool: str, args=lambda m: {}):
    _CMD.append((re.compile(pattern, re.I), tool, args))


def _f(m, key: str = "f") -> str:
    v = (m.groupdict().get(key) or "").strip(" .\"'")
    return "" if re.fullmatch(r"(?:this|that|these|those|the|my|it|them|attached|uploaded|dropped)?\s*(?:\d+|two|three|four|five|several)?\s*"
                              r"(?:excel|word|pdf|csv)?\s*(?:files?|workbooks?|spreadsheets?|sheets?|documents?|docs?|pdfs?|invoices?|notes|"
                              r"data|reports?|attachments?)?", v, re.I) else v


_c(rf"^(?:analy[sz]e|check|inspect|profile|review|look at|examine)\s+{_REF}(?:\s+{_XL})?(?:\s+and (?:find|show|list|tell me)(?: me)?(?: the| any)? (?:duplicates?(?: entries| rows| records)?|problems|issues|missing values|errors))?$",
   "business_excel_analyse", lambda m: {"file": _f(m)})
_c(rf"^(?:find|show|list|detect|check for)(?: me)?(?: the| any| all)? (?:duplicates?(?: entries| rows| records)?|missing values|data (?:quality )?(?:problems|issues)|inconsistent data|errors) in\s+{_REF}$",
   "business_excel_analyse", lambda m: {"file": _f(m)})
_c(rf"^(?:clean(?: up)?|tidy(?: up)?|de-?duplicate|dedupe|fix)\s+{_REF}$|^remove(?: the| all)? duplicates?(?: entries| rows| records)? (?:from|in)\s+(?P<f2>.+)$",
   "business_excel_clean", lambda m: {"file": _f(m) if m.group("f") else _f(m, "f2")})
_c(rf"^(?:combine|merge|join|consolidate|append)\s+{_REF}(?:\s+(?:in)?to (?:one|a single)(?: \w+)*)?$",
   "business_excel_merge", lambda m: {"files": [x.strip() for x in re.split(r"\s*(?:,| and )\s*", _f(m)) if x.strip()] if _f(m) else [],
                                      "mode": "join" if re.search(r"\bjoin\b", m.group(0), re.I) else "append"})
_c(rf"^(?:convert|export|save|turn)\s+{_REF}\s+(?:in)?to\s+(?:an? )?(?P<to>excel|xlsx|csv|pdf)(?: file| workbook)?$",
   "__convert__", lambda m: {"file": _f(m), "to": {"excel": "xlsx"}.get(m.group("to").lower(), m.group("to").lower())})
_c(rf"^(?:add|create|insert)(?: a| an)?(?: new)? (?:work)?sheet (?:called|named) (?P<n>[\w \-]{{1,31}}?)(?: (?:to|in)\s+(?P<f>.+))?$",
   "business_excel_sheets", lambda m: {"file": _f(m), "add": m.group("n").strip()})
_c(rf"^rename (?:the )?(?:work)?sheet (?P<a>[\w \-]{{1,31}}?) to (?P<b>[\w \-]{{1,31}}?)(?: in\s+(?P<f>.+))?$",
   "business_excel_sheets", lambda m: {"file": _f(m), "rename_from": m.group("a").strip(), "rename_to": m.group("b").strip()})
_c(rf"^(?:delete|remove) (?:the )?(?:work)?sheet (?P<a>[\w \-]{{1,31}}?)(?: (?:from|in)\s+(?P<f>.+))?$",
   "business_excel_sheets", lambda m: {"file": _f(m), "delete": m.group("a").strip()})
_c(r"^(?:create|make|generate|prepare|build|produce)(?: me)?(?: a| an| the)?(?: professional| monthly| weekly| quarterly| annual| detailed)*\s*"
   r"(?P<k>sales|financial|finance|inventory|stock|employee|staff|hr|revenue)\s+(?:report|dashboard|summary|analysis)(?:\s+(?:using|from|for|with|of|on)\s+(?P<f>.+))?$",
   "business_report", lambda m: {"file": _f(m), "kind": {"finance": "financial", "stock": "inventory", "staff": "employee", "hr": "employee",
                                                         "revenue": "sales"}.get(m.group("k").lower(), m.group("k").lower())})
_c(rf"^(?:analy[sz]e|summari[sz]e)(?: the| my)? (?P<k>sales|inventory|stock|employee|financial)(?: data| figures| records)?(?:\s+(?:in|from|using)\s+(?P<f>.+))?$",
   "business_report", lambda m: {"file": _f(m), "kind": {"stock": "inventory"}.get(m.group("k").lower(), m.group("k").lower())})
_c(r"^(?:read|process|extract|scan|go through|enter)\s+(?P<f>.*?invoices?.*?)(?:\s+and (?:extract|put|enter|add|save).*| (?:in)?to excel.*)?$",
   "business_process_invoices", lambda m: {"files": [_f(m)] if _f(m) else []})
_c(r"^(?:prepare|create|write|make|generate|draft)(?: the| me)? (?:meeting )?minutes(?: of(?: the)? meeting)?(?:\s+(?:from|using|for|of)\s+(?P<f>.+))?$",
   "business_meeting_minutes", lambda m: {"file": _f(m)})
_c(r"^(?:research|investigate)\s+(?P<q>.{4,400}?)(?:\s+and (?:create|write|make|prepare|give me)(?: me)?(?: a| an)? (?P<k>[\w ]*?report|comparison(?: report)?|summary))?$",
   "business_research", lambda m: {"question": m.group("q").strip(), **({"kind": m.group("k").strip(), "_hard": True} if m.group("k") else {})})
_c(r"^(?:write|create|prepare|make)(?: me)?(?: a| an)? (?P<k>market research|competitor analysis|research|technical research)(?: report)? (?:on|about|for)\s+(?P<q>.{4,400})$",
   "business_research", lambda m: {"question": m.group("q").strip(), "kind": m.group("k").strip() + " report", "_hard": True})
_c(r"^compare\s+(?P<a>[^,]{2,60}?)(?:\s*,\s*(?P<b>[^,]{2,60}?))?(?:\s*,\s*(?P<c2>[^,]{2,60}?))?\s*(?:,? and | vs\.? | versus | with )\s*(?P<d>[^,]{2,60}?)(?:\s+(?:on|by|for)\s+(?P<cr>.+?))?(?P<h>\s+(?:in a table|and (?:create|make|write|give me)(?: me)? (?:a |an )?(?:comparison )?(?:table|report)))?$",
   "__compare__", lambda m: {"items": [x.strip() for x in (m.group("a"), m.group("b"), m.group("c2"), m.group("d")) if x and x.strip()],
                             "criteria": [x.strip().capitalize() for x in re.split(r"\s*(?:,| and )\s*", m.group("cr") or "") if x.strip()],
                             "hard": bool(m.group("h"))})
_c(r"^(?:create|make|generate|prepare|write|draft)(?: me)?(?: a| an)? (?P<k>invoice|quotation|quote)(?:\s+(?P<d>.+))?$",
   "business_create_document", lambda m: {"kind": "quotation" if m.group("k").lower() != "invoice" else "invoice",
                                          "topic": "", "details": (m.group("d") or "").strip()})
_c(r"^(?:create|make|generate|prepare|write|draft)(?: me)?(?: a| an)?(?: professional| business| formal)* (?P<k>business proposal|proposal|project report|technical documentation|"
   r"technical document|resume|cv|cover letter|professional letter|letter)(?:\s+(?:on|about|for|to|regarding)\s+(?P<t>.+))?$",
   "business_create_document", lambda m: {"kind": {"business proposal": "proposal", "technical document": "technical documentation", "cv": "resume",
                                                   "professional letter": "letter"}.get(m.group("k").lower(), m.group("k").lower()),
                                          "topic": (m.group("t") or "").strip(), "details": (m.group("t") or "").strip()})
_c(r"^summari[sz]e\s+(?P<f>.*?(?:pdfs?|documents?|docs?|files?|papers?|reports?|\.\w{2,5}).*?)$", "business_summarise",
   lambda m: {"files": [_f(m)] if _f(m) else []})
_c(r"^compare\s+(?P<f>.*?(?:documents?|docs?|versions?|\.docx).*?)$", "business_compare_documents",
   lambda m: {"files": [x.strip() for x in re.split(r"\s*(?:,| and | with | vs\.? )\s*", _f(m)) if x.strip()] if _f(m) else []})
_c(r"^rewrite(?: the)? (?P<s>.+?) section(?: (?:of|in)\s+(?P<f>.+?))?(?:\s+to (?:be |make it |sound )?(?P<i>.+))?$", "business_rewrite_section",
   lambda m: {"file": _f(m), "section": m.group("s").strip(), "instruction": (m.group("i") or "").strip()})
_c(r"^(?:extract|pull|get)(?: the| all)? (?P<w>.+?) (?:from|out of|in)\s+(?P<f>.+?)(?:\s+(?:in)?to (?:excel|a spreadsheet|csv))?$", "business_extract",
   lambda m: {"files": [_f(m)] if _f(m) else [], "what": m.group("w").strip()})
_c(r"^(?:find|show|list|check|what are)(?: me)?(?: the| my)? (?:important|urgent|priority)(?: and urgent)? e-?mails?(?: (?:from|for|of) (?P<w>today|yesterday|this week))?"
   r"(?P<d>\s+and (?:prepare|write|draft|create)(?: the| me)? (?:response|reply)(?: drafts?)?| and draft (?:replies|responses))?$",
   "__email__", lambda m: {"when": (m.group("w") or "today").lower(), "drafts": bool(m.group("d"))})
_c(r"^(?:triage|sort|categori[sz]e|classify|organi[sz]e)(?: my)? (?:inbox|e-?mails?)$", "business_email_triage", lambda m: {})
_c(r"^(?:find|show|list|which)(?: me)?(?: my| the)? (?:unanswered|unreplied) e-?mails?(?P<d>.*draft.*)?$", "__email__",
   lambda m: {"when": "week", "drafts": bool(m.group("d")), "unanswered": True})
_c(r"^(?:prepare|write|draft|create)(?: me)? (?:reply|response|follow-?up) drafts?(?: for(?: my)?(?: (?P<u>unanswered|unread|important|today's))? e-?mails?)?$",
   "business_email_drafts", lambda m: {"unanswered": (m.group("u") or "").lower() == "unanswered"})
_c(r"^(?:show|list|what are)(?: me)?(?: my| the)? (?:business |recent )?(?:task history|business tasks|generated files|business history)$", "business_history")
_c(r"^open(?: the| my)? (?:business|genesis business|generated files|results) folder$", "business_open_folder")

ALWAYS_HARD = {"business_report", "business_email_drafts", "business_email_triage", "business_excel_clean", "business_excel_sheets",
               "business_compare_documents", "business_process_invoices", "business_meeting_minutes", "business_excel_merge"}
_EDIT = re.compile(r"\b(sort(?:ed)? (?:\w+ )*by|filter|pivot|group by|\bchart\b|\bgraph\b|add (?:a |an )?(?:new )?(?:formula )?column|totals? row|"
                   r"summari[sz]e .+ by )", re.I)
_WHEN = {"today": "newer_than:1d", "yesterday": "newer_than:2d older_than:1d", "this week": "newer_than:7d", "week": "newer_than:7d"}


def match(text: str):
    """(tool, args) for a Business Automation command, or None. Tried only after Genesis's own commands found nothing."""
    t = " ".join((text or "").strip().split()).rstrip(".!?")
    t = re.sub(r"^(?:hey |ok |okay )?(?:genesis|suzanne|andrew)[, ]+", "", t, flags=re.I)
    t = re.sub(r"^(?:please |can you |could you |i want you to |i need you to |would you )+", "", t, flags=re.I)
    for pat, tool, args in _CMD:
        m = pat.match(t)
        if not m:
            continue
        a = args(m)
        if tool == "__convert__":
            return "business_to_pdf" if a["to"] == "pdf" and not re.search(_XL + r"|\.(?:xlsx|csv)", m.group("f"), re.I) \
                else "business_excel_convert", ({"file": a["file"]} if a["to"] == "pdf" and not re.search(
                    _XL + r"|\.(?:xlsx|csv)", m.group("f"), re.I) else a)
        if tool == "__compare__":
            if any(re.search(r"\.(?:docx|pdf|xlsx)$|\b(?:documents?|files?|versions?|docs?)\b", i, re.I) for i in a["items"]):
                continue
            return "business_compare", {"items": a["items"], **({"criteria": a["criteria"]} if a["criteria"] else {}),
                                        **({"_hard": True} if a["hard"] else {})}
        if tool == "__email__":
            q = _WHEN.get(a["when"], "newer_than:1d")
            if a["drafts"]:
                return "business_email_drafts", {"_hard": True, **({"unanswered": True} if a.get("unanswered") else {"query": q})}
            if a.get("unanswered"):
                return "business_email_triage", {"unanswered": True, "_hard": True}
            return "business_email_triage", {"query": q, "_hard": True}
        if tool == "business_excel_analyse" and _EDIT.search(t):
            continue
        out = {k: v for k, v in a.items() if v not in ("", None)}
        if tool in ALWAYS_HARD:
            out["_hard"] = True
        return tool, out
    fm = re.search(r"(?:\b(?:in|of|for|from|on|using)\s+)?(\S+\.(?:xlsx|csv|xlsm|tsv))\b", t, re.I)
    if fm:
        t = (t[:fm.start()] + " " + t[fm.end():]).strip() + f" in {fm.group(1)}"
        t = re.sub(r"\s+", " ", t)
    else:
        vm = re.search(rf"\s(?:in |of |for |from |on |using )?(?:this|that|the|my) {_XL}(?= |$)", t, re.I)
        if vm and _EDIT.search(t):
            t = re.sub(r"\s+", " ", t[:vm.start()] + " " + t[vm.end():]).strip()
    m = re.match(rf"^(?P<r>.*?{_EDIT.pattern}.*?)(?:\s+(?:in|of|for|from|on|using)\s+(?P<f>(?:this|that|the|my)\s.+|\S+\.(?:xlsx|csv|xlsm|tsv)))?$", t, re.I)
    if m and (fm or re.search(_XL + r"|\bcolumn\b|\brows?\b", text, re.I)):
        req = m.group("r").strip()
        return "business_excel_edit", {"request": req, **({"file": _f(m)} if _f(m) else {})}
    return None
