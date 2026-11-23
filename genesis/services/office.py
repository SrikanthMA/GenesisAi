"""Microsoft Office: create Word / Excel / PowerPoint files, and write into the document that is open right now.

* New files are built with python-docx / openpyxl / python-pptx (no Office needed to create them), saved to
  ``Documents/Genesis Documents`` with a name that never overwrites an existing file, then opened in Office.
* Writing into the open document uses Office's own automation interface (COM, Windows): text is *added at the end*
  (Word), in the next empty rows (Excel) or as new slides (PowerPoint). Genesis never saves or closes your document —
  you can undo with Ctrl+Z like any edit.
* Spreadsheet formulas coming from the AI model are allowed only if they use plain arithmetic and a short list of safe
  functions; anything else (e.g. DDE/`cmd|` payloads, WEBSERVICE, HYPERLINK) is written as text.
"""
from __future__ import annotations

import re
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional

from ..core.logging_setup import get_logger

log = get_logger("office")
KINDS = {"word": ".docx", "excel": ".xlsx", "powerpoint": ".pptx"}
APP_NAMES = {"word": "Word", "excel": "Excel", "powerpoint": "PowerPoint"}
SAFE_FUNCS = {"SUM", "AVERAGE", "AVG", "MIN", "MAX", "COUNT", "COUNTA", "ROUND", "ROUNDUP", "ROUNDDOWN", "IF", "ABS", "SUMIF",
              "COUNTIF", "AVERAGEIF", "PRODUCT", "MEDIAN", "TODAY", "AND", "OR", "NOT", "INT", "MOD", "POWER", "SQRT"}


class OfficeError(Exception):
    def __init__(self, message: str, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind  # no_document | unavailable | invalid | error


# ------------------------------------------------------------------ helpers
def safe_cell(value: Any) -> Any:
    """Numbers stay numbers; formulas only if harmless; everything else is text."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return float(text) if "." in text else int(text)
    if text[:1] in ("=", "+", "-", "@") and len(text) > 1:
        if text.startswith("=") and _safe_formula(text):
            return text
        return "'" + text if not re.fullmatch(r"-?\d[\d,.]*", text) else text  # shown as text, never evaluated
    return text[:32000]


def _safe_formula(f: str) -> bool:
    body = f[1:]
    if re.search(r"[|!\[\]{}\"'`@]|\b(?:cmd|dde|msexcel|powershell)\b", body, re.I):
        return False
    funcs = set(re.findall(r"([A-Za-z_][A-Za-z0-9_.]*)\s*\(", body))
    if any(fn.upper() not in SAFE_FUNCS for fn in funcs):
        return False
    rest = re.sub(r"[A-Za-z_][A-Za-z0-9_.]*\s*\(", "(", body)
    rest = re.sub(r"\$?[A-Z]{1,3}\$?\d{1,7}", "0", rest)  # cell references
    return bool(re.fullmatch(r"[0-9.\s+\-*/^%(),:<>=&]*", rest))


def unique_path(folder: Path, title: str, ext: str) -> Path:
    stem = re.sub(r"[^\w\s-]", "", title or "Genesis document").strip()
    stem = re.sub(r"\s+", " ", stem)[:60] or "Genesis document"
    p = folder / f"{stem}{ext}"
    n = 2
    while p.exists():
        p = folder / f"{stem} ({n}){ext}"
        n += 1
    return p


# ------------------------------------------------------------------ builders (new files)
def build_docx(content: dict, path: Path) -> dict:
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    st = doc.styles["Normal"]
    st.font.name, st.font.size = "Calibri", Pt(11)
    if content.get("title"):
        doc.add_heading(str(content["title"])[:200], level=0)
    n_par = 0
    for sec in content.get("sections") or []:
        if sec.get("heading"):
            doc.add_heading(str(sec["heading"])[:200], level=1)
        for para in sec.get("paragraphs") or []:
            if str(para).strip():
                doc.add_paragraph(str(para).strip())
                n_par += 1
        for b in sec.get("bullets") or []:
            if str(b).strip():
                doc.add_paragraph(str(b).strip(), style="List Bullet")
                n_par += 1
    for para in content.get("paragraphs") or []:  # simple documents: just paragraphs
        doc.add_paragraph(str(para))
        n_par += 1
    doc.save(str(path))
    return {"paragraphs": n_par, "sections": len(content.get("sections") or [])}


def build_xlsx(content: dict, path: Path) -> dict:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)
    sheets = content.get("sheets") or [{"name": "Sheet1", "headers": content.get("headers") or [], "rows": content.get("rows") or []}]
    total_rows = 0
    for sh in sheets[:10]:
        name = re.sub(r"[\\/*?:\[\]]", "", str(sh.get("name") or "Sheet"))[:31] or "Sheet"
        ws = wb.create_sheet(name)
        headers = [str(h) for h in (sh.get("headers") or [])][:50]
        r0 = 1
        if headers:
            ws.append(headers)
            for c in range(1, len(headers) + 1):
                cell = ws.cell(row=1, column=c)
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="1F4E79")
                cell.alignment = Alignment(vertical="center")
            ws.freeze_panes = "A2"
            r0 = 2
        rows = [list(r)[:50] for r in (sh.get("rows") or [])[:5000] if isinstance(r, (list, tuple))]
        for row in rows:
            ws.append([safe_cell(v) for v in row])
        total_rows += len(rows)
        if sh.get("total_row") and rows and headers:
            last = r0 + len(rows) - 1
            tot = ["Total"] + [None] * (len(headers) - 1)
            for c in range(2, len(headers) + 1):
                col = [row[c - 1] for row in rows if len(row) >= c]
                if col and all(isinstance(safe_cell(v), (int, float)) for v in col):
                    L = get_column_letter(c)
                    tot[c - 1] = f"=SUM({L}{r0}:{L}{last})"
            ws.append(tot)
            for c in range(1, len(headers) + 1):
                ws.cell(row=last + 1, column=c).font = Font(bold=True)
        for c in range(1, (len(headers) or max((len(r) for r in rows), default=1)) + 1):
            L = get_column_letter(c)
            width = max([len(str(ws.cell(row=i, column=c).value or "")) for i in range(1, min(ws.max_row, 200) + 1)] + [8])
            ws.column_dimensions[L].width = min(60, width + 2)
    wb.save(str(path))
    return {"sheets": len(wb.sheetnames), "rows": total_rows}


def build_pptx(content: dict, path: Path) -> dict:
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Inches, Pt

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    accent = RGBColor(0x1F, 0x4E, 0x79)
    white = RGBColor(0xFF, 0xFF, 0xFF)

    def place(shape, x, y, w, h):  # the default template is laid out for 4:3 — stretch placeholders to 16:9
        shape.left, shape.top, shape.width, shape.height = Inches(x), Inches(y), Inches(w), Inches(h)

    s = prs.slides.add_slide(prs.slide_layouts[0])
    s.background.fill.solid()
    s.background.fill.fore_color.rgb = RGBColor(0x14, 0x23, 0x3A)
    place(s.shapes.title, 0.9, 2.3, 11.5, 1.6)
    s.shapes.title.text = str(content.get("title") or "Presentation")[:200]
    for run in s.shapes.title.text_frame.paragraphs[0].runs:
        run.font.color.rgb, run.font.bold, run.font.size = white, True, Pt(48)
    if len(s.placeholders) > 1:
        place(s.placeholders[1], 0.9, 4.0, 11.5, 1.2)
        s.placeholders[1].text = str(content.get("subtitle") or "")[:300]
        for run in s.placeholders[1].text_frame.paragraphs[0].runs:
            run.font.color.rgb, run.font.size = RGBColor(0xC9, 0xD6, 0xE6), Pt(22)
    n = 1
    for sl in (content.get("slides") or [])[:40]:
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        place(slide.shapes.title, 0.7, 0.4, 11.9, 1.1)
        place(slide.placeholders[1], 0.7, 1.7, 11.9, 5.2)
        slide.shapes.title.text = str(sl.get("title") or "")[:200]
        for run in slide.shapes.title.text_frame.paragraphs[0].runs:
            run.font.color.rgb, run.font.bold = accent, True
        body = slide.placeholders[1].text_frame
        bullets = [str(b) for b in (sl.get("bullets") or []) if str(b).strip()][:8]
        body.text = bullets[0] if bullets else ""
        for b in bullets[1:]:
            body.add_paragraph().text = b
        for p in body.paragraphs:
            for run in p.runs:
                run.font.size = Pt(22 if len(bullets) <= 4 else 18)
        if sl.get("notes"):
            slide.notes_slide.notes_text_frame.text = str(sl["notes"])[:3000]
        n += 1
    prs.save(str(path))
    return {"slides": n}


BUILDERS = {"word": build_docx, "excel": build_xlsx, "powerpoint": build_pptx}


def check_file(kind: str, path: Path) -> dict:
    """Re-open a saved file to prove it is a valid document (used for verification)."""
    if kind == "word":
        from docx import Document

        d = Document(str(path))
        return {"paragraphs": sum(1 for p in d.paragraphs if p.text.strip())}
    if kind == "excel":
        from openpyxl import load_workbook

        wb = load_workbook(str(path))
        return {"sheets": len(wb.sheetnames), "rows": sum(max(0, ws.max_row - 1) for ws in wb.worksheets)}
    from pptx import Presentation

    return {"slides": len(Presentation(str(path)).slides)}


# ------------------------------------------------------------------ live documents (COM)
class LiveOffice(ABC):
    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def active(self, kind: str) -> Optional[str]:
        """Name of the document open in that app right now, or None."""

    @abstractmethod
    def write(self, kind: str, content: dict) -> dict: ...

    @abstractmethod
    def tail(self, kind: str) -> str:
        """Text at the end of the active document (for verification)."""


class WindowsLiveOffice(LiveOffice):
    PROGIDS = {"word": "Word.Application", "excel": "Excel.Application", "powerpoint": "PowerPoint.Application"}

    def available(self) -> bool:
        if not sys.platform.startswith("win"):
            return False
        try:
            import win32com.client  # noqa: F401

            return True
        except ImportError:
            return False

    def _app(self, kind: str):
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore

        try:
            pythoncom.CoInitialize()  # tools run on worker threads
        except Exception:
            pass
        try:
            return win32com.client.GetActiveObject(self.PROGIDS[kind])
        except Exception:
            return None

    def _doc(self, kind: str):
        app = self._app(kind)
        if app is None:
            return None, None
        try:
            if kind == "word":
                return app, (app.ActiveDocument if app.Documents.Count else None)
            if kind == "excel":
                return app, (app.ActiveWorkbook if app.Workbooks.Count else None)
            return app, (app.ActivePresentation if app.Presentations.Count else None)
        except Exception:
            return app, None

    def active(self, kind: str) -> Optional[str]:
        if not self.available():
            return None
        _, doc = self._doc(kind)
        try:
            return str(doc.Name) if doc is not None else None
        except Exception:
            return None

    def write(self, kind: str, content: dict) -> dict:
        app, doc = self._doc(kind)
        if doc is None:
            raise OfficeError(f"There's no {APP_NAMES[kind]} document open.", "no_document")
        if kind == "word":
            return self._word(doc, content)
        if kind == "excel":
            return self._excel(app, content)
        return self._ppt(doc, content)

    @staticmethod
    def _word(doc, content: dict) -> dict:
        blocks: list[tuple[str, int]] = []  # (text, style) — Word's built-in style ids work in every language
        if content.get("heading"):
            blocks.append((str(content["heading"]), -2))  # wdStyleHeading1
        for p in content.get("paragraphs") or []:
            blocks.append((str(p), -1))  # wdStyleNormal
        for b in content.get("bullets") or []:
            blocks.append((str(b), -49))  # wdStyleListBullet
        for text, style in blocks:
            has_text = len(doc.Content.Text.strip()) > 0
            end = doc.Content.End - 1
            rng = doc.Range(end, end)
            rng.InsertAfter(("\r" if has_text else "") + text)
            try:
                doc.Paragraphs(doc.Paragraphs.Count).Style = style
            except Exception:
                pass
        return {"document": str(doc.Name), "added": len(blocks)}

    @staticmethod
    def _excel(app, content: dict) -> dict:
        ws = app.ActiveSheet
        used = ws.UsedRange
        empty = used.Rows.Count == 1 and used.Columns.Count == 1 and ws.Cells(1, 1).Value in (None, "")
        row = 1 if empty else used.Row + used.Rows.Count
        start = row
        lines = ([content["headers"]] if content.get("headers") and empty else []) + list(content.get("rows") or [])
        for r in lines[:2000]:
            for c, v in enumerate(list(r)[:50], start=1):
                val = safe_cell(v)
                cell = ws.Cells(row, c)
                if isinstance(val, str) and val.startswith("="):
                    cell.Formula = val
                else:
                    cell.Value = val
            row += 1
        return {"document": str(app.ActiveWorkbook.Name), "sheet": str(ws.Name), "rows": row - start, "first_row": start}

    @staticmethod
    def _ppt(pres, content: dict) -> dict:
        added = 0
        try:
            layout = pres.SlideMaster.CustomLayouts(2)  # "Title and Content" in the default master
        except Exception:
            layout = None
        for sl in (content.get("slides") or [])[:30]:
            idx = pres.Slides.Count + 1
            slide = pres.Slides.AddSlide(idx, layout) if layout is not None else pres.Slides.Add(idx, 2)  # ppLayoutText
            shapes = slide.Shapes
            try:
                shapes(1).TextFrame.TextRange.Text = str(sl.get("title") or "")
                shapes(2).TextFrame.TextRange.Text = "\r".join(str(b) for b in (sl.get("bullets") or [])[:8])
            except Exception:
                pass
            if sl.get("notes"):
                try:
                    slide.NotesPage.Shapes.Placeholders(2).TextFrame.TextRange.Text = str(sl["notes"])
                except Exception:
                    pass
            added += 1
        return {"document": str(pres.Name), "added": added, "slides": int(pres.Slides.Count)}

    def tail(self, kind: str) -> str:
        app, doc = self._doc(kind)
        if doc is None:
            return ""
        try:
            if kind == "word":
                return str(doc.Content.Text)[-3000:]
            if kind == "excel":
                ws = app.ActiveSheet
                used = ws.UsedRange
                last = used.Row + used.Rows.Count - 1
                return " ".join(str(ws.Cells(last, c).Value or "") for c in range(1, min(used.Columns.Count, 30) + 1))
            s = doc.Slides(doc.Slides.Count)
            return " ".join(str(sh.TextFrame.TextRange.Text) for sh in s.Shapes if sh.HasTextFrame)
        except Exception:
            return ""


class FakeLiveOffice(LiveOffice):
    """Test double: documents are plain lists."""

    def __init__(self, open_docs: Optional[dict] = None) -> None:
        self.docs = {k: {"name": v, "items": []} for k, v in (open_docs or {}).items()}

    def available(self) -> bool:
        return True

    def active(self, kind):
        return self.docs[kind]["name"] if kind in self.docs else None

    def write(self, kind, content):
        if kind not in self.docs:
            raise OfficeError(f"There's no {APP_NAMES[kind]} document open.", "no_document")
        items = self.docs[kind]["items"]
        if kind == "word":
            new = ([content["heading"]] if content.get("heading") else []) + list(content.get("paragraphs") or []) + \
                  list(content.get("bullets") or [])
        elif kind == "excel":
            new = [" ".join(str(safe_cell(v)) for v in r) for r in (content.get("rows") or [])]
        else:
            new = [str(s.get("title")) + ": " + " ".join(s.get("bullets") or []) for s in content.get("slides") or []]
        items.extend(new)
        return {"document": self.docs[kind]["name"], "added": len(new), "rows": len(new), "slides": len(items)}

    def tail(self, kind):
        return " ".join(self.docs.get(kind, {}).get("items", [])[-3:])


class OfficeService:
    def __init__(self, backend: Any = None, live: Optional[LiveOffice] = None, out_dir: Optional[Path] = None) -> None:
        self.backend = backend
        self.live = live if live is not None else WindowsLiveOffice()
        self.out_dir = out_dir or (Path.home() / "Documents" / "Genesis Documents")

    def create(self, kind: str, content: dict, title: str, open_after: bool = True) -> dict:
        if kind not in KINDS:
            raise OfficeError("I can create Word, Excel or PowerPoint files.", "invalid")
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = unique_path(self.out_dir, title, KINDS[kind])
        try:
            stats = BUILDERS[kind](content, path)
        except ImportError as exc:
            raise OfficeError(f"Creating {APP_NAMES[kind]} files needs a package that isn't installed ({exc.name}). "
                              "Run: .\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt", "unavailable") from exc
        opened = False
        if open_after and self.backend is not None:
            try:
                self.backend.open_path(str(path))
                opened = True
            except Exception as exc:
                log.info("couldn't open %s: %s", path.suffix, type(exc).__name__)
        return dict(stats, path=str(path), opened=opened)

    def open_document(self, kind: str) -> Optional[str]:
        try:
            return self.live.active(kind) if self.live.available() else None
        except Exception:
            return None

    def write_open(self, kind: str, content: dict) -> dict:
        if not self.live.available():
            raise OfficeError("Writing into an open Office document works on Windows with Office installed.", "unavailable")
        try:
            return self.live.write(kind, content)
        except OfficeError:
            raise
        except Exception as exc:
            log.warning("office write failed: %s", type(exc).__name__)
            raise OfficeError(f"{APP_NAMES[kind]} didn't accept the text ({type(exc).__name__}). If a dialog is open in "
                              f"{APP_NAMES[kind]}, close it and try again.", "error") from exc


__all__ = ["OfficeService", "OfficeError", "LiveOffice", "WindowsLiveOffice", "FakeLiveOffice", "safe_cell", "check_file",
           "KINDS", "APP_NAMES"]
