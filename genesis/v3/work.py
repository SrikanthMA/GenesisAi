"""Everyday work tools (3.0.1): Wikipedia -> Word, documents (incl. IEEE papers), Excel, code in VS Code / Colab,
and the digital-twin tools (remember a preference, recall it, order your usual, answer as you).

Built for small local models: the AI writes *plain text* (one section at a time, CSV for tables, a fenced code block for
code) and Python does all the formatting, so a 3-4B model produces clean files. Files are saved (never overwriting) in
Documents\\Genesis Documents or Documents\\Genesis Code and opened for you.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import time
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

from ..tools.base import Tool, ToolResult, obj, s
from . import twin

UA = {"User-Agent": "Genesis/3.0 (personal desktop assistant)"}
WIKI_API = "https://{lang}.wikipedia.org/w/api.php"


# ------------------------------------------------------------------ helpers
def documents_dir() -> Path:
    for base in (os.getenv("OneDrive"), os.getenv("OneDriveConsumer")):
        if base and (Path(base) / "Documents").is_dir():
            return Path(base) / "Documents"
    return Path.home() / "Documents"


def out_dir(ctx, sub: str) -> Path:
    office = getattr(ctx, "office", None)
    base = getattr(office, "out_dir", None) if sub == "Genesis Documents" else None
    p = Path(base) if base else documents_dir() / sub
    p.mkdir(parents=True, exist_ok=True)
    return p


def slug(text: str, n: int = 50) -> str:
    t = re.sub(r"[^\w\- ]+", "", text or "").strip()
    return (re.sub(r"\s+", " ", t)[:n].strip() or "Genesis").replace(" ", "_")


def unique(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 1000):
        p = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not p.exists():
            return p
    return path.with_name(f"{path.stem}-{int(time.time())}{path.suffix}")


def _open(ctx, path: Path) -> None:
    try:
        ctx.backend.open_path(str(path))
    except Exception:
        pass


def _llm(ctx):
    llm = getattr(ctx, "llm", None)
    if llm is None or not getattr(llm, "is_generative", False):
        return None
    return llm


def ask(ctx, prompt: str, *, system: str = "", max_tokens: int = 700, temperature: float = 0.4) -> str:
    llm = _llm(ctx)
    if llm is None:
        raise RuntimeError("no AI model is available")
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    return (llm.chat(msgs, max_tokens=max_tokens, temperature=temperature) or "").strip()


def clean_text(text: str) -> str:
    """Plain text from a model answer: no markdown symbols, no 'Sure, here is…' lead-in."""
    t = re.sub(r"^(sure|certainly|of course|here(?:'s| is| are))\b[^\n]*:\s*\n", "", text.strip(), flags=re.I)
    t = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), t)
    t = re.sub(r"(?m)^#{1,6}\s*", "", t)
    t = re.sub(r"(?m)^\s*[*\-•]\s+", "• ", t)
    return t.strip()


def code_block(text: str) -> str:
    m = re.search(r"```[\w+#.\-]*\s*\n(.*?)```", text or "", re.S)
    if m:
        return m.group(1).rstrip() + "\n"
    return re.sub(r"^```[\w+#.\-]*|```$", "", (text or "").strip()).strip() + "\n"


def _session(ctx):
    sess = getattr(ctx, "extras", {}).get("web_session") if getattr(ctx, "extras", None) else None
    if sess is None:
        import requests

        sess = requests.Session()
    return sess


def _docx():
    import docx  # python-docx (already a Genesis requirement)

    return docx


# ------------------------------------------------------------------ Wikipedia
def wiki_fetch(sess, topic: str, lang: str = "en") -> Optional[dict]:
    api = WIKI_API.format(lang=lang)
    r = sess.get(api, params={"action": "query", "list": "search", "srsearch": topic, "srlimit": 1, "format": "json"},
                 headers=UA, timeout=15)
    hits = ((r.json() or {}).get("query") or {}).get("search") or []
    if not hits:
        return None
    title = hits[0]["title"]
    r = sess.get(api, params={"action": "query", "prop": "extracts|info", "explaintext": 1, "inprop": "url",
                              "titles": title, "redirects": 1, "format": "json"}, headers=UA, timeout=20)
    pages = (((r.json() or {}).get("query") or {}).get("pages") or {}).values()
    page = next(iter(pages), None)
    if not page or not page.get("extract"):
        return None
    return {"title": page.get("title", title), "url": page.get("fullurl") or
            f"https://{lang}.wikipedia.org/wiki/{quote_plus(title.replace(' ', '_'))}", "text": page["extract"]}


SKIP_SECTIONS = {"see also", "references", "external links", "further reading", "notes", "bibliography", "sources",
                 "citations", "footnotes"}


def wiki_sections(text: str, max_words: int = 6000) -> list[tuple[int, str, list[str]]]:
    """[(level, heading, paragraphs)] from Wikipedia's plain-text extract ('== Heading ==' lines)."""
    out: list[tuple[int, str, list[str]]] = [(1, "", [])]
    words = 0
    skipping = False
    for line in text.splitlines():
        m = re.match(r"^(={2,6})\s*(.+?)\s*\1\s*$", line.strip())
        if m:
            level = len(m.group(1)) - 1
            skipping = m.group(2).strip().lower() in SKIP_SECTIONS or (skipping and level > 1)
            if not skipping:
                out.append((level, m.group(2).strip(), []))
            continue
        if skipping or not line.strip():
            continue
        if words >= max_words:
            break
        out[-1][2].append(line.strip())
        words += len(line.split())
    return [sec for sec in out if sec[2] or sec[1]]


class WikipediaToWord(Tool):
    name = "wikipedia_to_word"
    category = "office"
    description = ("Look up a topic on Wikipedia, open the article in the browser, and copy its text (headings and "
                   "paragraphs, with the source link) into a new Word document that opens automatically.")
    parameters = obj({"topic": s("what to look up", maxLength=200),
                      "summary_only": {"type": "boolean", "default": False},
                      "to_word": {"type": "boolean", "default": True},
                      "open_page": {"type": "boolean", "default": True}}, ["topic"])
    remote = True  # reads wikipedia.org (no personal data is sent; not an AI service)

    def run(self, args, ctx):
        try:
            page = wiki_fetch(_session(ctx), args["topic"])
        except Exception as exc:
            return ToolResult.fail(f"I couldn't reach Wikipedia: {exc}", retryable=True)
        if not page:
            return ToolResult.fail(f"Wikipedia has no article matching “{args['topic']}”.")
        if args.get("open_page", True):
            try:
                ctx.backend.open_url(page["url"])
            except Exception:
                pass
        secs = wiki_sections(page["text"], 700 if args.get("summary_only") else 6000)
        if args.get("summary_only"):
            secs = secs[:1]
        if not args.get("to_word", True):
            intro = " ".join(secs[0][2])[:900] if secs else ""
            res = ToolResult.ok(f"Opened Wikipedia: {page['title']}.\n\n{intro}", **page)
            res.untrusted_content = True
            return res
        docx = _docx()
        doc = docx.Document()
        doc.add_heading(page["title"], 0)
        p = doc.add_paragraph()
        p.add_run(f"Source: Wikipedia, “{page['title']}”, {page['url']} (retrieved {date.today():%d %B %Y}). "
                  "Text available under CC BY-SA 4.0.").italic = True
        n_words = 0
        for level, heading, paras in secs:
            if heading:
                doc.add_heading(heading, min(level, 4))
            for para in paras:
                doc.add_paragraph(para)
                n_words += len(para.split())
        path = unique(out_dir(ctx, "Genesis Documents") / f"{slug(page['title'])} - Wikipedia.docx")
        doc.save(str(path))
        _open(ctx, path)
        res = ToolResult.ok(f"Copied the Wikipedia article “{page['title']}” ({n_words:,} words, {len(secs)} sections) "
                            f"into Word: {path.name}. The source link is at the top.", path=str(path), file_path=str(path),
                            url=page["url"], title=page["title"])
        res.untrusted_content = True
        return res

    def verify(self, args, result, ctx):
        return Path(result.data["path"]).is_file() if result.success and result.data.get("path") else None


# ------------------------------------------------------------------ documents and IEEE papers
IEEE_SECTIONS = ["Introduction", "Related Work", "Methodology", "Results and Discussion", "Conclusion"]
ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"]
WRITER = ("You write clear, accurate, well-organised documents. Plain text only: no markdown, no headings, no "
          "lists unless asked, no lead-in like 'Sure'. If you are not certain of a fact, number or citation, leave it "
          "out rather than inventing it.")


def outline(ctx, kind: str, topic: str, requirements: str) -> list[str]:
    raw = ask(ctx, f"Plan the section headings for a {kind} about: {topic}.\n"
                   + (f"Requirements: {requirements}\n" if requirements else "")
                   + "Return ONLY 4 to 7 short section headings, one per line, no numbering, nothing else.",
              system=WRITER, max_tokens=150, temperature=0.3)
    heads = []
    for line in raw.splitlines():
        h = re.sub(r"^\s*(?:\d+[.)]|[IVX]+\.|[*\-•#]+)\s*", "", line).strip(" :*#\"")
        if 2 <= len(h) <= 80 and not h.lower().startswith(("here", "sure", "section headings")):
            heads.append(h)
    return heads[:8] or ["Introduction", "Background", "Main Points", "Discussion", "Conclusion"]


def write_section(ctx, kind: str, topic: str, heading: str, requirements: str, words: int, done: list[str]) -> str:
    prev = f"Sections already written: {', '.join(done)}. Don't repeat them.\n" if done else ""
    txt = ask(ctx, f"Write the “{heading}” section of a {kind} about: {topic}.\n"
                   + (f"Requirements: {requirements}\n" if requirements else "") + prev
                   + f"About {words} words, in paragraphs. Plain text only.",
              system=WRITER, max_tokens=min(1400, int(words * 2.2) + 100), temperature=0.5)
    return clean_text(txt)


def _set_columns(section, num: int) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    sect = section._sectPr
    for old in sect.findall(qn("w:cols")):
        sect.remove(old)
    cols = OxmlElement("w:cols")
    cols.set(qn("w:num"), str(num))
    cols.set(qn("w:space"), "360")
    sect.append(cols)


def build_ieee(path: Path, title: str, abstract: str, keywords: str, sections: list[tuple[str, str]],
               author: str = "") -> None:
    docx = _docx()
    from docx.enum.section import WD_SECTION
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Inches, Pt

    doc = docx.Document()
    st = doc.styles["Normal"]
    st.font.name, st.font.size = "Times New Roman", Pt(10)
    sec = doc.sections[0]
    sec.top_margin, sec.bottom_margin = Inches(0.75), Inches(1)
    sec.left_margin = sec.right_margin = Inches(0.625)
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run(title)
    r.font.size, r.font.name = Pt(24), "Times New Roman"
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.add_run(author or "Author Name\nDepartment, Institution\nCity, Country\nemail@example.com").font.size = Pt(11)
    body = doc.add_section(WD_SECTION.CONTINUOUS)
    _set_columns(body, 2)

    def para(text: str, *, bold_lead: str = "", italic_lead: bool = False, size: int = 10, bold: bool = False):
        q = doc.add_paragraph()
        q.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        q.paragraph_format.first_line_indent = Inches(0.125) if not bold_lead else None
        q.paragraph_format.space_after = Pt(2)
        if bold_lead:
            lr = q.add_run(bold_lead)
            lr.bold, lr.italic, lr.font.size = True, italic_lead, Pt(size)
        tr = q.add_run(text)
        tr.font.size, tr.bold = Pt(size), bold
        return q

    para(abstract, bold_lead="Abstract—", italic_lead=True, size=9, bold=True)
    para(keywords, bold_lead="Index Terms—", italic_lead=True, size=9, bold=True)
    for i, (heading, text) in enumerate(sections):
        h = doc.add_paragraph()
        h.alignment = WD_ALIGN_PARAGRAPH.CENTER
        h.paragraph_format.space_before = Pt(8)
        hr = h.add_run(f"{ROMAN[i] if i < len(ROMAN) else i + 1}. {heading.upper()}")
        hr.font.size = Pt(10)
        hr.font.small_caps = True
        for chunk in [c for c in re.split(r"\n\s*\n|\n", text) if c.strip()]:
            para(chunk.strip())
    h = doc.add_paragraph()
    h.alignment = WD_ALIGN_PARAGRAPH.CENTER
    h.add_run("REFERENCES").font.small_caps = True
    for i in range(1, 4):
        q = doc.add_paragraph()
        q.add_run(f"[{i}] Add a verified reference here (Author, “Title,” Journal/Conference, vol., no., pp., year).")\
            .font.size = Pt(8)
    doc.save(str(path))


def build_docx(path: Path, title: str, sections: list[tuple[str, str]], kind: str) -> None:
    docx = _docx()
    doc = docx.Document()
    if kind == "letter":
        doc.add_paragraph(date.today().strftime("%d %B %Y"))
        for _, text in sections:
            for chunk in [c for c in text.split("\n") if c.strip()]:
                doc.add_paragraph(chunk.strip())
    else:
        doc.add_heading(title, 0)
        for heading, text in sections:
            if heading:
                doc.add_heading(heading, 1)
            for chunk in [c for c in text.split("\n") if c.strip()]:
                c = chunk.strip()
                if c.startswith("• "):
                    doc.add_paragraph(c[2:], style="List Bullet")
                else:
                    doc.add_paragraph(c)
    doc.save(str(path))


class DraftDocument(Tool):
    name = "draft_document"
    category = "office"
    description = ("Draft a Word document as per the user's requirements: kind = ieee (two-column IEEE conference "
                   "paper), report, essay, letter, proposal, assignment, article, notes or document. Written section "
                   "by section, saved and opened in Word.")
    parameters = obj({"topic": s("topic or title", maxLength=500),
                      "kind": {"type": "string", "enum": ["ieee", "report", "essay", "letter", "proposal", "assignment",
                                                          "article", "notes", "document"], "default": "document"},
                      "requirements": s("extra requirements: length, audience, points to cover …", maxLength=3000),
                      "words": {"type": "integer", "default": 0}}, ["topic"])

    def run(self, args, ctx):
        if _llm(ctx) is None:
            return ToolResult.fail("I need an AI model to write that, and none is available right now.",
                                   hint="Say “check my local AI setup”.")
        kind, topic, req = args.get("kind") or "document", args["topic"].strip(), (args.get("requirements") or "").strip()
        folder = out_dir(ctx, "Genesis Documents")
        try:
            if kind == "ieee":
                heads = IEEE_SECTIONS
                per = max(150, int((args.get("words") or 2200) / (len(heads) + 1)))
                abstract = clean_text(ask(ctx, f"Write the abstract (150-200 words) of an IEEE conference paper about: "
                                               f"{topic}. {req} Plain text, one paragraph.", system=WRITER,
                                          max_tokens=400))
                kw = clean_text(ask(ctx, f"Give 4 to 6 IEEE index terms (keywords) for a paper about: {topic}. "
                                         "Return them on one line, comma-separated, nothing else.", system=WRITER,
                                    max_tokens=60, temperature=0.2)).splitlines()[0]
                done: list[str] = []
                secs = []
                for h in heads:
                    secs.append((h, write_section(ctx, "IEEE conference paper", topic, h, req, per, done)))
                    done.append(h)
                title = topic if len(topic) < 120 else topic[:117] + "…"
                path = unique(folder / f"{slug(title)} - IEEE.docx")
                build_ieee(path, title, abstract, kw, secs)
                note = (" References are placeholders — add real, verified sources (I don't invent citations). Fill in "
                        "the author block under the title.")
            else:
                total = int(args.get("words") or {"letter": 250, "notes": 400, "essay": 900}.get(kind, 1100))
                if kind == "letter":
                    heads = [""]
                else:
                    heads = outline(ctx, kind, topic, req)
                per = max(80, int(total / max(1, len(heads))))
                secs, done = [], []
                for h in heads:
                    secs.append((h, write_section(ctx, kind, topic, h or "full text", req, per if h else total, done)))
                    done.append(h)
                title = topic[:1].upper() + topic[1:]
                path = unique(folder / f"{slug(title)} - {kind}.docx")
                build_docx(path, title, secs, kind)
                note = " Check facts and figures before you share it."
        except Exception as exc:
            return ToolResult.fail(f"I couldn't finish the document: {exc}")
        _open(ctx, path)
        words = sum(len(t.split()) for _, t in secs)
        return ToolResult.ok(f"Drafted your {('IEEE paper' if kind == 'ieee' else kind)} “{title}” ({words:,} words, "
                             f"{len(secs)} sections) and opened it in Word: {path.name}.{note}",
                             path=str(path), file_path=str(path), sections=[h for h, _ in secs])

    def verify(self, args, result, ctx):
        return Path(result.data["path"]).is_file() if result.success else None


# ------------------------------------------------------------------ Excel
def parse_table(text: str) -> list[list]:
    body = text
    m = re.search(r"```(?:csv)?\s*\n(.*?)```", text, re.S)
    if m:
        body = m.group(1)
    lines = [ln for ln in body.strip().splitlines() if ln.count(",") >= 1 and not ln.lower().startswith(("here", "sure"))]
    rows = list(csv.reader(io.StringIO("\n".join(lines))))
    width = max((len(r) for r in rows), default=0)
    out = []
    for r in rows:
        cells = []
        for c in (r + [""] * width)[:width]:
            c = c.strip()
            num = c.replace(",", "").replace("₹", "").replace("$", "").strip()
            if c.startswith("="):
                cells.append(c)
            elif re.fullmatch(r"-?\d+(\.\d+)?", num):
                cells.append(float(num) if "." in num else int(num))
            else:
                cells.append(c)
        out.append(cells)
    return out


def build_xlsx(path: Path, title: str, rows: list[list], total: bool) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = slug(title, 28).replace("_", " ") or "Sheet1"
    for r in rows:
        ws.append(r)
    head_fill = PatternFill("solid", fgColor="DDEBF7")
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = head_fill
    ws.freeze_panes = "A2"
    n = len(rows)
    if total and n > 2:
        tot = ["Total"]
        for col in range(2, len(rows[0]) + 1):
            vals = [rows[i][col - 1] for i in range(1, n)]
            letter = get_column_letter(col)
            numeric = all(isinstance(v, (int, float)) or (isinstance(v, str) and v.startswith("=")) for v in vals if v != "")
            tot.append(f"=SUM({letter}2:{letter}{n})" if numeric and any(v != "" for v in vals) else "")
        ws.append(tot)
        for cell in ws[ws.max_row]:
            cell.font = Font(bold=True)
    for col in range(1, (len(rows[0]) if rows else 0) + 1):
        letter = get_column_letter(col)
        width = max(len(str(ws.cell(row=r, column=col).value or "")) for r in range(1, ws.max_row + 1))
        ws.column_dimensions[letter].width = min(50, max(10, width + 2))
    wb.save(str(path))


class ExcelCreate(Tool):
    name = "excel_create"
    category = "office"
    description = ("Create an Excel spreadsheet for the user's request (a tracker, budget, timetable, marks list, "
                   "data table…) with headers, example rows, formulas and totals; saves and opens it in Excel.")
    parameters = obj({"request": s("what the sheet is for", maxLength=2000),
                      "rows": {"type": "integer", "default": 10}}, ["request"])

    def run(self, args, ctx):
        if _llm(ctx) is None:
            return ToolResult.fail("I need an AI model to design the sheet, and none is available right now.")
        req = args["request"].strip()
        n = max(3, min(int(args.get("rows") or 10), 60))
        prompt = (f"Create a spreadsheet table for: {req}\nReturn ONLY CSV: the first line is the column headers, then "
                  f"{n} realistic example rows. Numbers as plain numbers (no currency symbols or thousands separators). "
                  "You may use simple Excel formulas such as =B2*C2. No explanation.")
        try:
            rows = parse_table(ask(ctx, prompt, max_tokens=1500, temperature=0.3))
            if len(rows) < 2:
                rows = parse_table(ask(ctx, prompt + "\nRemember: CSV only.", max_tokens=1500, temperature=0.2))
        except Exception as exc:
            return ToolResult.fail(f"I couldn't design the sheet: {exc}")
        if len(rows) < 2 or len(rows[0]) < 2:
            return ToolResult.fail("The AI model didn't return a usable table. Try describing the columns, e.g. "
                                   "“an expense sheet with date, item, category and amount”.")
        total = bool(re.search(r"\b(budget|expens|cost|price|sales|total|mark|score|bill|invoice|amount|spend|salar|fee)",
                               req, re.I))
        path = unique(out_dir(ctx, "Genesis Documents") / f"{slug(req, 40)}.xlsx")
        build_xlsx(path, req, rows, total)
        _open(ctx, path)
        return ToolResult.ok(f"Created the spreadsheet {path.name} ({len(rows) - 1} rows × {len(rows[0])} columns: "
                             f"{', '.join(str(h) for h in rows[0][:6])}) and opened it in Excel.",
                             path=str(path), file_path=str(path), headers=rows[0])

    def verify(self, args, result, ctx):
        return Path(result.data["path"]).is_file() if result.success else None


# ------------------------------------------------------------------ code in VS Code / Colab
LANGS = [("python", "py", r"python|py\b"), ("javascript", "js", r"javascript|node|js\b"), ("typescript", "ts", r"typescript"),
         ("html", "html", r"html|web ?page|website"), ("java", "java", r"\bjava\b"), ("cpp", "cpp", r"c\+\+|cpp"),
         ("c", "c", r"\bin c\b|\bc program"), ("csharp", "cs", r"c#|csharp"), ("sql", "sql", r"\bsql\b"),
         ("bash", "sh", r"bash|shell script"), ("powershell", "ps1", r"powershell")]
CODER = ("You are Andrew, an expert programmer. Write complete, working, well-commented code. Reply with ONE fenced "
         "code block containing the whole program, then at most two sentences on how to run it.")


def language_of(task: str) -> tuple[str, str]:
    for lang, ext, pat in LANGS:
        if re.search(pat, task, re.I):
            return lang, ext
    return "python", "py"


def write_code(ctx, task: str, lang: str, extra: str = "") -> tuple[str, str]:
    raw = ask(ctx, f"Write a {lang} program that does this: {task}\n{extra}".strip(), system=CODER, max_tokens=1800,
              temperature=0.2)
    code = code_block(raw)
    tail = raw.split("```")[-1].strip() if raw.count("```") >= 2 else ""
    return code, clean_text(tail)[:300]


class CodeInVSCode(Tool):
    name = "code_in_vscode"
    category = "code"
    description = ("Write a program for the user's task (Andrew, the technical agent), save it in "
                   "Documents\\Genesis Code and open it in VS Code.")
    parameters = obj({"task": s("what the program should do", maxLength=3000),
                      "language": s("python, javascript, html, java, c++ … (guessed if empty)", maxLength=30)}, ["task"])

    def run(self, args, ctx):
        if _llm(ctx) is None:
            return ToolResult.fail("I need an AI model to write code, and none is available right now.")
        task = args["task"].strip()
        lang, ext = language_of(args.get("language") or task)
        try:
            code, how = write_code(ctx, task, lang)
        except Exception as exc:
            return ToolResult.fail(f"I couldn't write the code: {exc}")
        if len(code.strip()) < 5:
            return ToolResult.fail("The AI model didn't return any code. Try describing the task in more detail.")
        folder = out_dir(ctx, "Genesis Code")
        path = unique(folder / f"{slug(task, 40).lower()}.{ext}")
        path.write_text(code, encoding="utf-8")
        opened = "VS Code"
        exe = None
        try:
            exe = ctx.apps.vscode_executable()
        except Exception:
            exe = None
        try:
            if exe:
                ctx.backend.launch([exe, str(folder), str(path)])
            else:
                import shutil

                code_cmd = shutil.which("code")
                if code_cmd:
                    ctx.backend.launch([code_cmd, str(folder), str(path)])
                else:
                    ctx.backend.open_path(str(path))
                    opened = "your default editor (VS Code wasn't found)"
        except Exception:
            opened = "nothing - open it yourself"
        preview = code if len(code) < 1800 else code[:1800] + "\n# … (rest in the file)"
        return ToolResult.ok(f"Wrote {path.name} ({len(code.splitlines())} lines of {lang}) and opened it in {opened}."
                             f"\n```{lang}\n{preview}```" + (f"\n{how}" if how else ""),
                             path=str(path), file_path=str(path), language=lang)

    def verify(self, args, result, ctx):
        return Path(result.data["path"]).is_file() if result.success else None


class CodeInColab(Tool):
    name = "code_in_colab"
    category = "colab"
    description = ("Write Python code for the user's task and put it in a new Google Colab notebook cell (not run until "
                   "the user says so). Falls back to a notebook file + opening Colab if browser control isn't set up.")
    parameters = obj({"task": s("what the code should do", maxLength=3000)}, ["task"])
    remote = True

    def run(self, args, ctx):
        if _llm(ctx) is None:
            return ToolResult.fail("I need an AI model to write code, and none is available right now.")
        task = args["task"].strip()
        try:
            code, _ = write_code(ctx, task, "python", "It runs in ONE Google Colab notebook cell: no "
                                                     "`if __name__ == '__main__'`, print/display results at the end.")
        except Exception as exc:
            return ToolResult.fail(f"I couldn't write the code: {exc}")
        if getattr(ctx, "colab", None) is not None:
            try:
                from ..tools.colab import ColabNewNotebook, ColabWriteCode

                made = ColabNewNotebook().run({}, ctx)
                if made.success:
                    wrote = ColabWriteCode().run({"code": code, "new_cell": False}, ctx)
                    if wrote.success:
                        return ToolResult.ok(wrote.message.replace("I wrote the code", "I wrote your code"),
                                             code=code, **{k: v for k, v in wrote.data.items() if k != "code"})
            except Exception:
                pass
        nb = {"cells": [{"cell_type": "markdown", "metadata": {}, "source": [f"# {task}\nWritten by Genesis (Andrew)."]},
                        {"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [],
                         "source": code.splitlines(keepends=True)}],
              "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3"}}, "nbformat": 4,
              "nbformat_minor": 5}
        path = unique(out_dir(ctx, "Genesis Code") / f"{slug(task, 40).lower()}.ipynb")
        path.write_text(json.dumps(nb, indent=1), encoding="utf-8")
        try:
            ctx.backend.open_url("https://colab.research.google.com/#create=true")
        except Exception:
            pass
        return ToolResult.ok(f"I wrote the code and saved it as a notebook: {path}. Colab is opening — choose "
                             f"File → Upload notebook and pick that file (or paste the code below into a cell).\n"
                             f"```python\n{code}```", path=str(path), file_path=str(path), code=code)


# ------------------------------------------------------------------ digital twin tools
def _profile(ctx) -> Optional[twin.Profile]:
    from .provider import find_provider

    prov = find_provider(ctx)
    return getattr(prov, "profile", None) or twin.from_services(ctx)


class TwinRemember(Tool):
    name = "twin_remember"
    category = "memory"
    description = "Remember one of the user's preferences (favourite food, usual order, likes, dislikes…)."
    parameters = obj({"statement": s("the preference, e.g. “My favourite pizza is a Farmhouse from Domino's”",
                                     maxLength=400)}, ["statement"])

    def run(self, args, ctx):
        fact = twin.preference_from(args["statement"]) or args["statement"].strip()
        mem = getattr(ctx, "memory", None)
        saved = None
        if mem is not None and hasattr(mem, "remember"):
            saved = mem.remember(fact, kind="preference")
            if saved is None and getattr(mem, "enabled", True) is False:
                return ToolResult.fail("Memory is switched off, so I can't save that.",
                                       hint="Say “turn memory on” first.")
        elif getattr(ctx, "repos", None) is not None:
            saved = ctx.repos.memory.add(fact, kind="preference")
        said = re.sub(r"^User's ", "your ", fact)
        said = re.sub(r"^User ", "you ", said)
        return ToolResult.ok(f"Got it, Boss — I'll remember {said}.", fact=fact, id=saved)


class TwinRecall(Tool):
    name = "twin_recall"
    category = "memory"
    description = "Say what the user likes/usually wants for something (from their saved preferences only)."
    parameters = obj({"item": s("e.g. pizza, coffee, movie", maxLength=100)}, ["item"])

    def run(self, args, ctx):
        prof = _profile(ctx)
        found = twin.facts_about(args["item"], prof.facts() if prof else [])
        if not found:
            return ToolResult.ok(f"I don't know your {args['item']} preference yet, Boss. Tell me, e.g. “my favourite "
                                 f"{args['item']} is …”, and I'll remember it.", found=[])
        return ToolResult.ok("From what you've told me: " + "; ".join(f.replace("User's ", "your ").replace("User ", "you ")
                                                                   for f in found[:5]) + ".", found=found)


FOOD_APPS = {"swiggy": "https://www.swiggy.com/search?query={q}", "zomato": "https://www.zomato.com/search?q={q}",
             "dominos": "https://pizzaonline.dominos.co.in/menu", "domino's": "https://pizzaonline.dominos.co.in/menu"}


class TwinOrder(Tool):
    name = "twin_order"
    category = "web"
    description = ("Help order food: recall the user's usual/favourite for the item and open the ordering site "
                   "(Swiggy by default, or Zomato / Domino's) searched for it. Never places or pays for an order.")
    parameters = obj({"item": s("e.g. pizza", maxLength=100), "app": {"type": "string", "enum": list(FOOD_APPS)}},
                     ["item"])
    remote = True

    def run(self, args, ctx):
        prof = _profile(ctx)
        found = twin.facts_about(args["item"], prof.facts() if prof else [])
        app = (args.get("app") or "").lower()
        if not found:
            for f in (prof.facts() if prof else []):
                if re.search(r"\b(swiggy|zomato|domino'?s)\b", f, re.I) and args["item"].lower() in f.lower():
                    found.append(f)
        pick = ""
        if found:
            pick = re.sub(r"^User'?s? [\w' -]*?:\s*|^User (always|usually|normally) \w+ ", "", found[0]).strip()
            m = re.search(r"\b(swiggy|zomato|domino'?s)\b", found[0], re.I)
            if m and not app:
                app = m.group(1).lower()
        app = app or "swiggy"
        query = re.sub(r"^(?:an?|the)\s+", "", pick, flags=re.I) or args["item"]
        url = FOOD_APPS.get(app, FOOD_APPS["swiggy"]).format(q=quote_plus(re.sub(r"\bfrom \w+'?s?\b", "", query).strip()))
        try:
            ctx.backend.open_url(url)
        except Exception:
            pass
        site = {"dominos": "Domino's", "domino's": "Domino's"}.get(app, app.title())
        if pick:
            msg = (f"Your usual {args['item']} is {pick}, Boss. I've opened {site} with it — check the cart, address "
                   "and payment yourself; I never place or pay for orders.")
        else:
            msg = (f"I've opened {site} for {args['item']}. I don't know your usual yet — tell me “my favourite "
                   f"{args['item']} is …” and next time I'll pick it for you.")
        return ToolResult.ok(msg, url=url, pick=pick)


class TwinAnswer(Tool):
    name = "twin_answer"
    category = "memory"
    description = ("Answer a question or message the way the user would (digital twin): first person, their style, "
                   "only their saved facts.")
    parameters = obj({"question": s("what to answer as the user", maxLength=3000)}, ["question"])

    def run(self, args, ctx):
        llm = _llm(ctx)
        if llm is None:
            return ToolResult.fail("I need an AI model for that, and none is available right now.")
        from .provider import find_provider

        q = args["question"].strip()
        prov = find_provider(ctx)
        if prov is not None and (getattr(ctx, "llm", None) is prov or getattr(ctx.llm, "primary", None) is prov):
            msgs = [{"role": "user", "content": f"answer as me: {q}"}]  # the local router switches to twin mode
        else:
            prof = _profile(ctx)
            msgs = [{"role": "system", "content": (prof or twin.Profile(lambda: [])).twin_prompt(q)},
                    {"role": "user", "content": q}]
        try:
            text = llm.chat(msgs, max_tokens=500, temperature=0.7)
        except Exception as exc:
            return ToolResult.fail(f"I couldn't answer that: {exc}")
        return ToolResult.ok(clean_text(text) or "…", twin=True)


TOOLS = [WikipediaToWord(), DraftDocument(), ExcelCreate(), CodeInVSCode(), CodeInColab(), TwinRemember(),
         TwinRecall(), TwinOrder(), TwinAnswer()]
