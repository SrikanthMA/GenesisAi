"""Word engine (python-docx): build formatted documents from a plain structure, read them back, rewrite a section,
compare two documents, and convert to PDF. Money is added up in Python; missing details are marked, never invented."""
from __future__ import annotations

import difflib
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path
from typing import Optional

from .common import BizError

MISSING = "[not provided]"


def _docx():
    try:
        import docx  # noqa: F401
        return docx
    except ImportError as exc:  # pragma: no cover
        raise BizError("Word documents need the “python-docx” package (pip install python-docx).") from exc


def tidy(text: str) -> str:
    """Mechanical consistency fixes: doubled spaces and words, space before punctuation, stray markdown."""
    t = re.sub(r"[ \t]+", " ", str(text or "")).strip()
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)
    t = re.sub(r"\b(\w+)( \1\b)+", r"\1", t, flags=re.I) if not re.search(r"\b(had had|that that)\b", t, re.I) else t
    t = re.sub(r"^\s*#+\s*", "", t)
    return re.sub(r"\*\*(.+?)\*\*", r"\1", t)


def _field(paragraph, code: str) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    run = paragraph.add_run()
    for kind, text in (("begin", None), (None, code), ("separate", None), (None, " "), ("end", None)):
        if kind:
            el = OxmlElement("w:fldChar")
            el.set(qn("w:fldCharType"), kind)
            if kind == "begin" and code.strip().startswith("TOC"):
                el.set(qn("w:dirty"), "true")
        elif text == " ":
            el = OxmlElement("w:t")
            el.text = "Right-click and choose Update Field to build the table of contents." if code.strip().startswith("TOC") else "1"
        else:
            el = OxmlElement("w:instrText")
            el.set(qn("xml:space"), "preserve")
            el.text = text
        run._r.append(el)


def _table(doc, headers: list, rows: list, widths: Optional[list] = None, right: Optional[list] = None):
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt

    tb = doc.add_table(rows=1, cols=len(headers))
    tb.style = "Table Grid"
    for i, h in enumerate(headers):
        cell = tb.rows[0].cells[i]
        cell.text = ""
        r = cell.paragraphs[0].add_run(str(h))
        r.bold, r.font.size = True, Pt(10)
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:fill"), "D9E2F3")
        cell._tc.get_or_add_tcPr().append(shd)
    for row in rows:
        cells = tb.add_row().cells
        for i, v in enumerate(list(row)[:len(headers)]):
            cells[i].text = ""
            p = cells[i].paragraphs[0]
            txt = f"{v:,.2f}" if isinstance(v, float) else ("" if v is None else str(v))
            run = p.add_run(txt)
            run.font.size = Pt(10)
            if isinstance(v, (int, float)) or (right and i in right):
                p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    return tb


def build(path, spec: dict) -> dict:
    """Write a .docx from ``spec``:

    {title, subtitle, meta: [(label, value)], toc: bool, header: str, footer: str, page_numbers: bool,
     sections: [{heading, level, paragraphs: [..], bullets: [..], numbered: [..], table: {headers, rows}, image: path,
                 caption}]}"""
    _docx()
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Inches, Pt

    doc = Document()
    st = doc.styles["Normal"]
    st.font.name, st.font.size = "Calibri", Pt(11)
    sec = doc.sections[0]
    sec.left_margin = sec.right_margin = Inches(1)
    if spec.get("header"):
        hp = sec.header.paragraphs[0]
        hp.text = str(spec["header"])
        hp.runs[0].font.size = Pt(9)
    if spec.get("footer") or spec.get("page_numbers", True):
        fp = sec.footer.paragraphs[0]
        fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        if spec.get("footer"):
            fp.add_run(str(spec["footer"]) + "   ").font.size = Pt(9)
        if spec.get("page_numbers", True):
            fp.add_run("Page ").font.size = Pt(9)
            _field(fp, " PAGE ")
    if spec.get("title"):
        doc.add_heading(tidy(spec["title"]), 0)
    if spec.get("subtitle"):
        p = doc.add_paragraph()
        r = p.add_run(tidy(spec["subtitle"]))
        r.italic = True
    for label, value in spec.get("meta") or []:
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(0)
        p.add_run(f"{label}: ").bold = True
        p.add_run(str(value) if value not in (None, "") else MISSING)
    if spec.get("toc"):
        doc.add_heading("Contents", 1)
        _field(doc.add_paragraph(), ' TOC \\o "1-3" \\h \\z \\u ')
        doc.add_page_break()
    n_tables = n_images = 0
    for s in spec.get("sections") or []:
        if s.get("heading"):
            doc.add_heading(tidy(s["heading"]), max(1, min(int(s.get("level", 1)), 3)))
        for para in s.get("paragraphs") or []:
            for chunk in re.split(r"\n\s*\n", str(para)):
                if chunk.strip():
                    p = doc.add_paragraph(tidy(chunk.replace("\n", " ")))
                    p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY if len(chunk) > 200 else None
        for b in s.get("bullets") or []:
            doc.add_paragraph(tidy(b), style="List Bullet")
        for b in s.get("numbered") or []:
            doc.add_paragraph(tidy(b), style="List Number")
        tb = s.get("table")
        if tb and tb.get("headers"):
            _table(doc, tb["headers"], tb.get("rows") or [], right=tb.get("right"))
            doc.add_paragraph()
            n_tables += 1
        if s.get("image") and Path(s["image"]).is_file():
            doc.add_picture(str(s["image"]), width=Inches(float(s.get("width", 6))))
            doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
            n_images += 1
            if s.get("caption"):
                c = doc.add_paragraph()
                c.alignment = WD_ALIGN_PARAGRAPH.CENTER
                c.add_run(tidy(s["caption"])).italic = True
    doc.save(str(path))
    return {"path": str(path), "tables": n_tables, "images": n_images}


def read(path) -> dict:
    """{title, text, blocks: [{kind: heading|para|bullet, level, text}], tables: [[[cell]]], sections: [(heading, text)]}"""
    _docx()
    from docx import Document

    path = Path(path)
    try:
        doc = Document(str(path))
    except Exception as exc:
        raise BizError(f"I couldn't open {path.name}: {exc}") from exc
    blocks = []
    for p in doc.paragraphs:
        text = p.text.strip()
        if not text:
            continue
        style = (p.style.name or "").lower() if p.style is not None else ""
        m = re.match(r"heading (\d)", style)
        if m or style == "title":
            blocks.append({"kind": "heading", "level": int(m.group(1)) if m else 0, "text": text})
        elif "list" in style:
            blocks.append({"kind": "bullet", "level": 0, "text": text})
        else:
            blocks.append({"kind": "para", "level": 0, "text": text})
    tables = [[[c.text.strip() for c in r.cells] for r in t.rows] for t in doc.tables]
    sections, head, buf = [], "", []
    for b in blocks:
        if b["kind"] == "heading":
            if buf or head:
                sections.append((head, "\n".join(buf)))
            head, buf = b["text"], []
        else:
            buf.append(("• " if b["kind"] == "bullet" else "") + b["text"])
    if buf or head:
        sections.append((head, "\n".join(buf)))
    title = next((b["text"] for b in blocks if b["kind"] == "heading"), path.stem)
    text = "\n".join(("\n" + b["text"] if b["kind"] == "heading" else b["text"]) for b in blocks)
    for t in tables:
        text += "\n" + "\n".join(" | ".join(r) for r in t)
    return {"title": title, "text": text.strip(), "blocks": blocks, "tables": tables, "sections": sections,
            "words": len(text.split())}


def rewrite_section(src, dst, heading: str, new_text: str) -> dict:
    """Replace the body under ``heading`` with ``new_text`` in a *copy* of the document (formatting elsewhere is kept)."""
    _docx()
    from docx import Document

    shutil.copy2(src, dst)
    doc = Document(str(dst))
    paras = doc.paragraphs
    is_head = lambda p: p.style is not None and re.match(r"(heading \d|title)", (p.style.name or "").lower())  # noqa: E731
    heads = [(i, p) for i, p in enumerate(paras) if is_head(p) and p.text.strip()]
    want = heading.strip().lower()
    hit = [(i, p) for i, p in heads if p.text.strip().lower() == want] or \
          [(i, p) for i, p in heads if want in p.text.strip().lower()]
    if not hit:
        Path(dst).unlink(missing_ok=True)
        raise BizError(f"There is no section called “{heading}”. Sections: {', '.join(p.text.strip() for _, p in heads) or 'none'}.")
    if len(hit) > 1:
        Path(dst).unlink(missing_ok=True)
        raise BizError(f"“{heading}” matches several sections: {', '.join(p.text.strip() for _, p in hit)}. Which one?")
    i, hp = hit[0]
    end = next((j for j, _ in heads if j > i), len(paras))
    body = paras[i + 1:end]
    old = "\n".join(p.text for p in body if p.text.strip())
    style = next((p.style for p in body if p.text.strip() and "list" not in (p.style.name or "").lower()), None)
    for p in body:
        p._element.getparent().remove(p._element)
    anchor = hp
    for chunk in [c for c in re.split(r"\n\s*\n|\n", new_text) if c.strip()]:
        new = doc.add_paragraph(tidy(chunk))
        if style is not None:
            new.style = style
        anchor._element.addnext(new._element)
        anchor = new
    doc.save(str(dst))
    return {"path": str(dst), "section": hp.text.strip(), "old_words": len(old.split()), "new_words": len(new_text.split())}


def _units(d: dict) -> list[str]:
    return [("# " if b["kind"] == "heading" else "") + b["text"] for b in d["blocks"]] + \
           [" | ".join(r) for t in d["tables"] for r in t]


def compare(a, b) -> dict:
    """Paragraph-level differences between two documents: {added, removed, changed: [(old, new)], same, ratio}."""
    da, db = read(a), read(b)
    ua, ub = _units(da), _units(db)
    sm = difflib.SequenceMatcher(None, ua, ub, autojunk=False)
    added, removed, changed, same = [], [], [], 0
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            same += i2 - i1
        elif op == "insert":
            added += ub[j1:j2]
        elif op == "delete":
            removed += ua[i1:i2]
        else:
            olds, news = ua[i1:i2], ub[j1:j2]
            for k in range(max(len(olds), len(news))):
                if k < len(olds) and k < len(news):
                    if difflib.SequenceMatcher(None, olds[k], news[k]).ratio() > 0.5:
                        changed.append((olds[k], news[k]))
                    else:
                        removed.append(olds[k])
                        added.append(news[k])
                elif k < len(olds):
                    removed.append(olds[k])
                else:
                    added.append(news[k])
    return {"a": Path(a).name, "b": Path(b).name, "added": added, "removed": removed, "changed": changed, "same": same,
            "ratio": round(sm.ratio(), 3), "words_a": da["words"], "words_b": db["words"]}


def word_diff(old: str, new: str) -> str:
    out = []
    a, b = old.split(), new.split()
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            out.append(" ".join(a[i1:i2]))
        else:
            if i2 > i1:
                out.append("[-" + " ".join(a[i1:i2]) + "-]")
            if j2 > j1:
                out.append("{+" + " ".join(b[j1:j2]) + "+}")
    return " ".join(out)


def comparison_spec(c: dict) -> dict:
    cut = lambda t: t if len(t) < 700 else t[:700] + " …"  # noqa: E731
    secs = [{"heading": "Summary", "bullets": [
        f"{c['a']}: {c['words_a']} words. {c['b']}: {c['words_b']} words.", f"Overall similarity: {c['ratio'] * 100:.0f}%.",
        f"{len(c['changed'])} paragraph(s) changed, {len(c['added'])} added, {len(c['removed'])} removed, {c['same']} identical."]}]
    if c["changed"]:
        secs.append({"heading": "Changed", "paragraphs": ["[-removed words-] {+added words+}"],
                     "numbered": [cut(word_diff(o, n)) for o, n in c["changed"][:200]]})
    if c["added"]:
        secs.append({"heading": f"Only in {c['b']}", "bullets": [cut(x) for x in c["added"][:200]]})
    if c["removed"]:
        secs.append({"heading": f"Only in {c['a']}", "bullets": [cut(x) for x in c["removed"][:200]]})
    return {"title": "Document comparison", "subtitle": f"{c['a']}  vs  {c['b']}", "sections": secs,
            "meta": [("Date", date.today().strftime("%d %B %Y"))]}


# ------------------------------------------------------------------ PDF
def to_pdf(src, dst) -> dict:
    """Convert a .docx to PDF. Uses Microsoft Word when installed (exact layout), then LibreOffice; if neither is
    available it writes a plain text-and-tables PDF and says so."""
    src, dst = Path(src).resolve(), Path(dst).resolve()
    try:
        import win32com.client  # type: ignore

        word = win32com.client.DispatchEx("Word.Application")
        try:
            word.Visible = False
            d = word.Documents.Open(str(src), ReadOnly=True)
            d.SaveAs(str(dst), FileFormat=17)
            d.Close(False)
        finally:
            word.Quit()
        if dst.is_file():
            return {"path": str(dst), "engine": "Microsoft Word", "exact": True}
    except Exception:
        pass
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    for cand in (r"C:\Program Files\LibreOffice\program\soffice.exe", r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"):
        if not exe and Path(cand).is_file():
            exe = cand
    if exe:
        try:
            subprocess.run([exe, "--headless", "--convert-to", "pdf", "--outdir", str(dst.parent), str(src)],
                           capture_output=True, timeout=120, check=False)
            made = dst.parent / (src.stem + ".pdf")
            if made.is_file():
                if made != dst:
                    made.replace(dst)
                return {"path": str(dst), "engine": "LibreOffice", "exact": True}
        except Exception:
            pass
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError as exc:
        raise BizError("To make a PDF I need Microsoft Word, LibreOffice or the “reportlab” package - none was found.") from exc
    from xml.sax.saxutils import escape

    d = read(src)
    styles = getSampleStyleSheet()
    flow = []
    for b in d["blocks"]:
        if b["kind"] == "heading":
            flow.append(Paragraph(escape(b["text"]), styles["Title" if b["level"] == 0 else f"Heading{min(b['level'], 3)}"]))
        elif b["kind"] == "bullet":
            flow.append(Paragraph("• " + escape(b["text"]), styles["BodyText"]))
        else:
            flow += [Paragraph(escape(b["text"]), styles["BodyText"]), Spacer(1, 4)]
    for t in d["tables"]:
        if t:
            rt = Table([[Paragraph(escape(c), styles["BodyText"]) for c in r] for r in t], repeatRows=1)
            rt.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
                                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#D9E2F3"))]))
            flow += [Spacer(1, 8), rt]
    SimpleDocTemplate(str(dst), pagesize=A4, title=d["title"]).build(flow)
    return {"path": str(dst), "engine": "built-in", "exact": False,
            "note": "Word isn't installed, so this PDF keeps the text and tables but not the exact Word layout "
                    "(tables are placed after the text)."}


# ------------------------------------------------------------------ ready-made business documents (no AI needed)
def money(v) -> str:
    return f"{float(v):,.2f}"


def invoice_spec(d: dict, kind: str = "Invoice") -> tuple[dict, dict]:
    """Invoice / quotation from {seller, buyer, number, date, due, currency, tax_percent, items: [{description, qty, price}],
    notes}. Totals are calculated here; anything not supplied is marked “[not provided]”."""
    cur = (d.get("currency") or "").strip()
    rows, sub, problems = [], 0.0, []
    for n, it in enumerate(d.get("items") or [], 1):
        try:
            qty, price = float(it.get("qty", 1) or 0), float(it.get("price", 0) or 0)
        except (TypeError, ValueError):
            problems.append(f"item {n} has a quantity or price that is not a number")
            continue
        if not str(it.get("description") or "").strip():
            problems.append(f"item {n} has no description")
        line = round(qty * price, 2)
        sub += line
        rows.append([n, str(it.get("description") or MISSING), f"{qty:g}", money(price), money(line)])
    if not rows:
        raise BizError(f"I need at least one line item (description, quantity, price) to make the {kind.lower()}.")
    tax_p = float(d.get("tax_percent") or 0)
    tax = round(sub * tax_p / 100, 2)
    total = round(sub + tax, 2)
    rows += [["", "", "", "Subtotal", money(sub)]]
    if tax_p:
        rows += [["", "", "", f"Tax ({tax_p:g}%)", money(tax)]]
    rows += [["", "", "", f"TOTAL {cur}".strip(), money(total)]]
    for k in ("seller", "buyer", "number"):
        if not str(d.get(k) or "").strip():
            problems.append(f"{k} not provided")
    quote = kind.lower().startswith("quot")
    spec = {"title": kind.upper(), "page_numbers": False,
            "meta": [(f"{kind} no.", d.get("number")), ("Date", d.get("date") or date.today().strftime("%d %B %Y")),
                     ("Valid until" if quote else "Due date", d.get("due")), ("From", d.get("seller")),
                     ("To", d.get("buyer"))],
            "sections": [{"heading": "Items", "level": 2,
                          "table": {"headers": ["#", "Description", "Qty", f"Unit price {cur}".strip(), f"Amount {cur}".strip()],
                                    "rows": rows, "right": [2, 3, 4]}},
                         {"heading": "Notes", "level": 2, "paragraphs": [d.get("notes") or (
                             "This quotation is not an invoice." if quote else "Thank you for your business.")]}]}
    return spec, {"subtotal": round(sub, 2), "tax": tax, "total": total, "items": len(rows) - (3 if tax_p else 2),
                  "problems": problems}


def minutes_spec(m: dict) -> dict:
    """Meeting minutes from {title, date, attendees, summary, decisions, actions: [{task, owner, due}], discussion}."""
    acts = [[a.get("task") or MISSING, a.get("owner") or "Not assigned", a.get("due") or "No date given"] for a in m.get("actions") or []]
    secs = []
    if m.get("summary"):
        secs.append({"heading": "Summary", "paragraphs": [m["summary"]]})
    if m.get("discussion"):
        secs.append({"heading": "Discussion", "bullets": list(m["discussion"])})
    secs.append({"heading": "Decisions", "bullets": list(m.get("decisions") or []) or ["No decisions were recorded in the notes."]})
    secs.append({"heading": "Action items", "table": {"headers": ["Task", "Owner", "Due"], "rows": acts}} if acts else
                {"heading": "Action items", "paragraphs": ["No action items were found in the notes."]})
    return {"title": m.get("title") or "Meeting minutes",
            "meta": [("Date", m.get("date")), ("Attendees", ", ".join(m.get("attendees") or []) or None)], "sections": secs}


def letter_spec(d: dict) -> dict:
    body = [p for p in re.split(r"\n\s*\n", d.get("body") or "") if p.strip()]
    lines = [d.get("sender") or MISSING, d.get("date") or date.today().strftime("%d %B %Y"), "", d.get("recipient") or MISSING]
    return {"page_numbers": False, "sections": [
        {"paragraphs": ["\n\n".join(x) for x in [[ln] for ln in lines if ln]]},
        {"paragraphs": ([f"Subject: {d['subject']}"] if d.get("subject") else []) + [d.get("greeting") or "Dear Sir or Madam,"] + body
                       + [d.get("closing") or "Yours sincerely,", d.get("sender_name") or d.get("sender") or MISSING]}]}
