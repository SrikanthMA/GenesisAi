"""Word / Excel / PowerPoint tools.

office_create — writes a new document from the user's request (content by the AI model, or the exact text given), saves
                it without overwriting anything and opens it in Office.
office_write  — adds content to the document that is open right now (end of the Word document, next empty rows in
                Excel, new slides in PowerPoint). If nothing is open, it creates a new file instead. Never saves/closes.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..ai.llm.base import LLMError
from ..services.office import APP_NAMES, OfficeError, check_file
from .base import Tool, ToolResult, obj, s

APP = {"type": "string", "enum": ["word", "excel", "powerpoint"]}

CREATE_PROMPTS = {
    "word": """Write the content of a Word document for this request: {request}
Return JSON only: {{"title": "...", "sections": [{{"heading": "...", "paragraphs": ["..."], "bullets": ["..."]}}]}}
Rules: well-structured and accurate; 3-6 sections unless the request implies otherwise (a letter or a single paragraph needs
one section without a heading); plain text (no markdown); "bullets" may be empty.""",
    "excel": """Design a spreadsheet for this request: {request}
Return JSON only: {{"title": "...", "sheets": [{{"name": "...", "headers": ["..."], "rows": [["...", 0]], "total_row": false}}]}}
Rules: realistic, useful example rows (5-15) the user can edit; numbers as numbers (not strings); you may use simple
formulas like "=B2*C2" or "=SUM(B2:B10)" only; set total_row true when a totals line makes sense.""",
    "powerpoint": """Write a presentation for this request: {request}
Return JSON only: {{"title": "...", "subtitle": "...", "slides": [{{"title": "...", "bullets": ["..."], "notes": "..."}}]}}
Rules: exactly {slides} content slides; 3-5 short bullets per slide (max ~12 words each); speaker notes of 1-3 sentences;
accurate, plain text (no markdown).""",
}
WRITE_PROMPTS = {
    "word": """Write this for a Word document: {request}
Return JSON only: {{"heading": "" , "paragraphs": ["..."], "bullets": []}}
Plain text, no markdown. Use "heading" only if a title helps.""",
    "excel": """Produce spreadsheet rows for: {request}
Return JSON only: {{"headers": ["..."], "rows": [["...", 0]]}}  (numbers as numbers; simple formulas like "=B2*C2" only)""",
    "powerpoint": """Write {slides} slide(s) for: {request}
Return JSON only: {{"slides": [{{"title": "...", "bullets": ["..."], "notes": "..."}}]}}  (3-5 short bullets each)""",
}


def _svc(ctx):
    svc = getattr(ctx, "office", None)
    if svc is None:
        raise OfficeError("Office integration isn't available in this session.", "unavailable")
    return svc


def _ask_model(ctx, prompt: str, max_tokens: int) -> dict:
    llm = ctx.llm
    if not getattr(llm, "is_generative", False):
        raise LLMError("no model")
    data = llm.chat_json([{"role": "user", "content": prompt}], max_tokens=max_tokens, temperature=0.4)
    if not isinstance(data, dict):
        raise LLMError("the model returned no content")
    return data


def _literal(kind: str, text: str) -> dict:
    text = text.strip()
    if kind == "excel":
        rows = [[c.strip() for c in re.split(r"\s*[,;\t]\s*", line) if c.strip()] for line in re.split(r"\s*(?:\n|\|)\s*", text)
                if line.strip()]
        return {"rows": rows or [[text]]}
    if kind == "powerpoint":
        title, _, rest = text.partition(":")
        bullets = [b.strip() for b in re.split(r"\s*(?:\n|;|•)\s*", rest) if b.strip()] if rest else []
        return {"slides": [{"title": title.strip()[:120], "bullets": bullets}]}
    return {"paragraphs": [p.strip() for p in re.split(r"\n{2,}|\r", text) if p.strip()] or [text]}


def _title_from(request: str, kind: str) -> str:
    t = re.sub(r"^(?:an?|the|some|my)\s+", "", (request or "").strip(), flags=re.I)
    t = re.sub(r"^(?:essay|report|article|presentation|document|spreadsheet|sheet)\s+(?:on|about|for)\s+", "", t, flags=re.I)
    return (t[:1].upper() + t[1:])[:60] if t else {"word": "Document", "excel": "Spreadsheet", "powerpoint": "Presentation"}[kind]


def _counts_text(kind: str, stats: dict) -> str:
    if kind == "powerpoint":
        return f"{stats.get('slides', 0)} slides"
    if kind == "excel":
        return f"{stats.get('rows', 0)} rows"
    return f"{stats.get('paragraphs', 0)} paragraphs"


def _need_model_fail(kind: str) -> ToolResult:
    return ToolResult.fail(f"I need my AI model to write that {APP_NAMES[kind]} content, and it isn't available right now.",
                           hint="Say “check your AI connection” — or tell me the exact text to put in.")


class OfficeCreate(Tool):
    name = "office_create"
    category = "office"
    description = ("Create a NEW Word document, Excel spreadsheet or PowerPoint presentation and open it. Describe what it "
                   "should contain in `request` (the content is written for you), or give the exact `text`.")
    parameters = obj({"app": APP, "request": s("What the document should contain / be about", maxLength=2000),
                      "text": s("Exact text to put in (instead of generating)", maxLength=20000),
                      "title": s("Title / file name", maxLength=120),
                      "slides": {"type": "integer", "minimum": 1, "maximum": 30}}, ["app"])
    output = {"path": "str", "opened": "bool", "slides|rows|paragraphs": "int"}

    def run(self, args, ctx):
        kind = args["app"]
        request = (args.get("request") or "").strip()
        if args.get("text"):
            content = _literal(kind, args["text"])
            title = args.get("title") or _title_from(args["text"][:40], kind)
            if kind == "powerpoint":
                content["title"] = title
        elif request:
            slides = int(args.get("slides") or 6)
            try:
                content = _ask_model(ctx, CREATE_PROMPTS[kind].format(request=request, slides=slides),
                                     2600 if kind == "word" else 2200)
            except LLMError:
                return _need_model_fail(kind)
            if kind == "powerpoint" and isinstance(content.get("slides"), list):
                content["slides"] = content["slides"][:slides]
            title = args.get("title") or str(content.get("title") or "") or _title_from(request, kind)
        else:
            content = {"title": args.get("title") or ""} if kind != "excel" else {"sheets": [{"name": "Sheet1", "headers": [], "rows": []}]}
            title = args.get("title") or f"New {APP_NAMES[kind]} {'document' if kind == 'word' else 'file'}"
        try:
            info = _svc(ctx).create(kind, content, title, open_after=bool(ctx.settings.get("office.open_after_create", True)))
        except OfficeError as exc:
            return ToolResult.fail(str(exc), kind=exc.kind)
        except Exception as exc:  # malformed model output, disk errors …
            return ToolResult.fail(f"I couldn't build the {APP_NAMES[kind]} file ({type(exc).__name__}). Try asking again.")
        where = Path(info["path"]).name
        opened = f" and opened it in {APP_NAMES[kind]}" if info.get("opened") else ""
        body = f" ({_counts_text(kind, info)})" if request or args.get("text") else ""
        return ToolResult.ok(f"Done — I created “{where}”{body}{opened}. It's in Documents\\Genesis Documents.",
                             **{k: v for k, v in info.items()}, app=kind)

    def verify(self, args, result, ctx):
        if not result.success:
            return False
        try:
            stats = check_file(args["app"], Path(result.data["path"]))
        except Exception:
            return False
        if args["app"] == "powerpoint":
            return stats["slides"] == result.data.get("slides")
        return True


class OfficeWrite(Tool):
    name = "office_write"
    category = "office"
    description = ("Write into the Word document / Excel sheet / PowerPoint presentation that is OPEN right now: text is added "
                   "at the end, rows below the data, or new slides. Give exact `text`, or a `request` to have it written. "
                   "If none is open, a new file is created instead.")
    parameters = obj({"app": APP, "request": s("What to write (e.g. 'a paragraph about India', 'a slide about results')",
                                                maxLength=2000),
                      "text": s("Exact text / values to insert", maxLength=20000),
                      "slides": {"type": "integer", "minimum": 1, "maximum": 20}}, ["app"])
    output = {"document": "str", "added": "int"}

    def run(self, args, ctx):
        kind = args["app"]
        if not args.get("text") and not args.get("request"):
            return ToolResult.fail(f"What should I write in {APP_NAMES[kind]}?")
        svc = _svc(ctx)
        open_doc = svc.open_document(kind)
        if open_doc is None:
            res = OfficeCreate().run({"app": kind, "request": args.get("request"), "text": args.get("text"),
                                      "slides": args.get("slides")}, ctx)
            if res.success:
                res.message = f"No {APP_NAMES[kind]} document was open, so I made a new one. " + res.message
                res.data["created"] = True
            return res
        if args.get("text"):
            content = _literal(kind, args["text"])
        else:
            try:
                content = _ask_model(ctx, WRITE_PROMPTS[kind].format(request=args["request"], slides=int(args.get("slides") or 1)),
                                     1800)
            except LLMError:
                return _need_model_fail(kind)
        try:
            info = svc.write_open(kind, content)
        except OfficeError as exc:
            return ToolResult.fail(str(exc), kind=exc.kind)
        what = {"word": "the end of", "excel": "the next empty rows of", "powerpoint": "new slides at the end of"}[kind]
        return ToolResult.ok(f"Done — I wrote it into {what} “{info.get('document', open_doc)}”. It isn't saved yet; press "
                             f"Ctrl+S when you're happy (Ctrl+Z undoes it).", probe=_probe(kind, content), **info, app=kind)

    def verify(self, args, result, ctx):
        if not result.success:
            return False
        if result.data.get("created"):
            return OfficeCreate().verify(args, result, ctx)
        probe = result.data.get("probe") or ""
        try:
            tail = _svc(ctx).live.tail(args["app"])
        except Exception:
            return None
        return (probe.lower() in tail.lower()) if probe and tail else None


def _probe(kind: str, content: dict) -> str:
    """A short piece of what we inserted, to find again when verifying."""
    try:
        if kind == "word":
            items = (content.get("bullets") or []) or (content.get("paragraphs") or []) or [content.get("heading") or ""]
            return str(items[-1])[-40:].strip()
        if kind == "excel":
            row = (content.get("rows") or [[]])[-1]
            return str(row[0]) if row else ""
        return str((content.get("slides") or [{}])[-1].get("title") or "")[:40]
    except Exception:
        return ""


TOOLS = [OfficeCreate(), OfficeWrite()]
