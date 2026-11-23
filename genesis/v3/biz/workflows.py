"""The jobs Genesis can do end to end. Every function follows UNDERSTAND > PLAN > EXECUTE > VERIFY > DELIVER, logs each
stage on a :class:`Run`, writes results to new files (originals are untouched) and returns a dict with a ``message``."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Optional

from . import docs, excel, extract, mail, research
from . import table as T
from .common import BizError, Run, ask, has_llm, json_from, out_path, resolve_files
from .table import Chart, Sheet, Table


def _names(paths) -> str:
    return ", ".join(Path(p).name for p in paths)


def _commentary(ctx, kind: str, bullets: list[str]) -> Optional[str]:
    """Two or three sentences of management commentary written from the computed facts only (optional, AI model)."""
    if not has_llm(ctx) or not bullets:
        return None
    try:
        text = ask(ctx, f"Write a 3-sentence management commentary for a {kind} report using ONLY these computed facts. Do not "
                        "add, change or estimate any number, and do not explain causes that are not in the facts.\n- "
                        + "\n- ".join(bullets[:14]), max_tokens=170, purpose="report")
    except BizError:
        return None
    facts = re.sub(r"[\s,]", "", " ".join(bullets))
    for m in research.NUM.finditer(text):  # a number that is not in the facts means the model made something up: drop it all
        v = re.sub(r"[\s,]", "", m.group(0))
        if len(re.sub(r"\D", "", v)) >= 2 and v not in facts and v.rstrip("%") not in facts and v.rstrip(".0") not in facts:
            return None
    return text


def _report_doc(ctx, run: Run, title: str, kind: str, facts: dict, sheets: list[Sheet], source: str) -> Path:
    bullets = excel.facts_text(kind, facts)
    secs = [{"heading": "Key figures", "bullets": bullets}]
    note = _commentary(ctx, kind, bullets)
    if note:
        secs.insert(0, {"heading": "Commentary", "paragraphs": [note, "(Written by the AI model from the figures below; the "
                                                                      "figures themselves were calculated directly from the data.)"]})
    for sh in sheets[1:6]:
        t = T.plain(sh.table)
        if sh.title in ("Data", "Transactions", "Inventory", "Employees") or not t.rows:
            continue
        secs.append({"heading": sh.title, "level": 2,
                     "table": {"headers": t.headers, "rows": [[v if not isinstance(v, date) else v.strftime("%d-%b-%Y") for v in r] for r in t.rows[:25]]}})
    secs.append({"heading": "Method", "paragraphs": [f"Source: {source}. All totals, averages and rankings were calculated from "
                                                     "the source rows after removing exact duplicate and empty rows. Missing "
                                                     "values were left blank and are listed above; nothing was estimated."]})
    path = out_path(ctx, title, ".docx")
    docs.build(path, {"title": title, "subtitle": f"Prepared {date.today().strftime('%d %B %Y')}", "sections": secs,
                      "header": title, "toc": False})
    run.deliver(path)
    return path


# ------------------------------------------------------------------ Excel jobs
def analyse_excel(ctx, run: Run, file="", save: bool = True) -> dict:
    path = resolve_files(file, "excel")[0]
    run.sources.append(str(path))
    run.log("UNDERSTAND", f"Reading {path.name}.")
    tables = T.read_tables(path)
    profiles = [T.profile(t) for t in tables]
    text = excel.describe(profiles)
    st = [dict(s, sheet=t.name) for t in tables for s in T.stats(t)]
    run.log("EXECUTE", f"Profiled {len(tables)} sheet(s), {sum(p['rows'] for p in profiles)} rows.")
    out = None
    if save:
        sheets = [Sheet(excel.quality_sheet(profiles), "Data quality"), Sheet(excel.columns_sheet(profiles), "Columns")]
        if st:
            sheets.append(Sheet(Table(["Sheet", "Column", "Count", "Sum", "Average", "Median", "Min", "Max", "Std dev"],
                                      [[s["sheet"], s["column"], s["count"], s["sum"], s["mean"], s["median"], s["min"], s["max"], s["stdev"]]
                                       for s in st], "Statistics"), "Statistics"))
        out = out_path(ctx, f"{path.stem} - analysis", ".xlsx")
        excel.write(run, out, sheets)
    run.finish()
    n_issues = sum(len(p["issues"]) + bool(p["duplicate_rows"]) + bool(p["empty_rows"]) for p in profiles)
    return {"message": f"{path.name}:\n{text}" + (f"\nFull analysis saved to {out}." if out else ""), "profiles": profiles,
            "stats": st, "path": str(out) if out else None, "issues": n_issues}


def clean_excel(ctx, run: Run, file="", key: Optional[list] = None, only_duplicates: bool = False) -> dict:
    path = resolve_files(file, "excel")[0]
    run.sources.append(str(path))
    tables = T.read_tables(path)
    run.log("PLAN", "Remove empty and duplicate rows, trim spaces, fix numbers/dates stored as text, unify spellings; keep "
                    "missing values blank and highlight them; save as a new workbook.")
    sheets, total = [], {"duplicates_removed": 0, "rows": 0, "missing_left": 0}
    for t in tables:
        sh, rep = excel.clean_sheets(t, key if key and len(tables) == 1 else None)
        for s in sh:
            s.title = s.title if len(tables) == 1 else f"{t.name[:18]} {s.title}"[:31]
        sheets += sh
        for k in total:
            total[k] += rep[k]
        run.log("EXECUTE", f"{t.name}: removed {rep['duplicates_removed']} duplicate and {rep['empty_rows_removed']} empty row(s), "
                           f"fixed {rep['numbers_fixed'] + rep['dates_fixed']} cell type(s), unified {rep['spellings_unified']} spelling(s).")
    out = out_path(ctx, f"{path.stem} - cleaned", ".xlsx")
    excel.write(run, out, sheets)
    again = T.read_tables(out)[0]
    left = len(T.duplicates(again))
    run.log("VERIFY", f"Re-read the cleaned sheet: {left} duplicate row(s) remain.", ok=left == 0)
    return {"message": f"Cleaned {path.name}: removed {total['duplicates_removed']} duplicate row(s); {total['rows']} rows remain"
                       + (f"; {total['missing_left']} cell(s) are still empty and highlighted (I don't invent data)" if total["missing_left"] else "")
                       + f". Saved as {out.name}; your original file is unchanged.", "path": str(out), **total}


OP_PATTERNS = [
    (r"\bsort(?:ed)?(?: (?:it|the (?:data|sheet|rows)))?(?: in (ascending|descending) order)? by (?P<col>.+?)(?: in)? ?(?P<dir>ascending|descending|desc|asc|"
     r"high(?:est)? to low(?:est)?|low(?:est)? to high(?:est)?|largest first|smallest first|newest first|oldest first|z to a|a to z)?(?: order)?$", "sort"),
    (r"\b(?:filter|show|keep|only)(?: (?:the )?(?:rows|records|data|entries))?(?: for| where| with| to)? (?P<cond>.+)$", "filter"),
    (r"\b(?:add|create|insert|make)(?: a| an)?(?: new)? (?:formula )?column(?: called| named)? (?P<name>[\w %()/-]+?)\s*(?:=|as|that is|equal to|which is|with(?: the)? formula)\s*(?P<expr>.+)$", "formula"),
    (r"\b(?:pivot|group|summari[sz]e|total|break ?down)(?: table)?(?: (?:it|the (?:data|sheet)))?(?: of| the)?\s*(?:(?P<agg>sum|average|mean|count|min|max|median)(?: of)? )?(?P<val>[\w %()/-]+?)? ?(?:by|per|for each) (?P<rows>[\w %()/-]+?)(?: and (?P<cols>[\w %()/-]+))?$", "pivot"),
    (r"\b(?P<kind>bar|line|pie|column)? ?(?:chart|graph|plot)(?: of| for| showing)? (?P<val>[\w %()/-]+?) (?:by|per|over|against|vs\.?) (?P<rows>[\w %()/-]+)$", "chart"),
]


def plan_excel(ctx, t: Table, request: str) -> list[dict]:
    """Turn a plain request into a list of operations. Rules first; the AI model only if the rules find nothing."""
    ops = []
    parts = [p.strip() for p in re.split(r"\s*(?:,? and then |; | then |\. )\s*", request.strip().rstrip(".")) if p.strip()]
    for part in parts:
        p = re.sub(r"^(?:please |and |also )+", "", part, flags=re.I)
        for pat, op in OP_PATTERNS:
            m = re.search(pat, p, re.I)
            if not m:
                continue
            g = {k: (v.strip() if v else v) for k, v in m.groupdict().items()}
            try:
                if op == "sort":
                    d = (g.get("dir") or m.group(1) or "").lower()
                    desc = bool(re.search(r"desc|high(?:est)? to low|largest|newest|z to a", d))
                    ops.append({"op": "sort", "by": t.headers[t.col(g["col"])], "descending": desc})
                elif op == "filter":
                    T.parse_condition(t, g["cond"])
                    ops.append({"op": "filter", "condition": g["cond"]})
                elif op == "formula":
                    T.formula_for(t, g["expr"], 2)
                    ops.append({"op": "formula", "name": g["name"].strip().title() if g["name"].islower() else g["name"], "expression": g["expr"]})
                elif op in ("pivot", "chart"):
                    val = g.get("val")
                    val = None if not val or val.lower() in ("rows", "records", "count", "the data", "data", "it") else t.headers[t.col(val)]
                    ops.append({"op": op, "rows": t.headers[t.col(g["rows"])], "values": val, "agg": (g.get("agg") or ("sum" if val else "count")).lower(),
                                "columns": t.headers[t.col(g["cols"])] if g.get("cols") else None,
                                "kind": {"column": "bar"}.get((g.get("kind") or "bar").lower(), (g.get("kind") or "bar").lower())})
                break
            except BizError:
                if op == "filter":
                    continue  # "show ..." may be a different kind of request
                raise
    if re.search(r"\b(duplicate|dedup|clean)", request, re.I):
        ops.insert(0, {"op": "clean"})
    if re.search(r"\b(total row|add totals?|sum (?:up )?(?:the |all )?columns?)\b", request, re.I):
        ops.append({"op": "totals"})
    if ops or not has_llm(ctx):
        return ops
    ans = ask(ctx, "Turn the request into spreadsheet operations. Columns: " + ", ".join(f'"{h}"' for h in t.headers) + ".\n"
                   'Allowed operations (JSON array, in order):\n{"op":"clean"}\n{"op":"sort","by":"<column>","descending":true|false}\n'
                   '{"op":"filter","condition":"<column> <op> <value>"}  ops: = != > < >= <= contains\n'
                   '{"op":"formula","name":"<new column>","expression":"<columns with + - * / ( )>"}\n'
                   '{"op":"pivot","rows":"<column>","values":"<column or null>","agg":"sum|average|count|min|max"}\n'
                   '{"op":"chart","rows":"<column>","values":"<column>","kind":"bar|line|pie"}\n{"op":"totals"}\n'
                   f"Use only the columns listed. Return ONLY the JSON array ([] if nothing fits).\nRequest: {request}",
              max_tokens=300, temperature=0.0, purpose="excel-plan")
    data = json_from(ans)
    good = []
    for o in data if isinstance(data, list) else []:
        try:  # validate every model-proposed step against the real columns before running it
            if o.get("op") in ("clean", "totals"):
                good.append({"op": o["op"]})
            elif o.get("op") == "sort":
                good.append({"op": "sort", "by": t.headers[t.col(o["by"])], "descending": bool(o.get("descending"))})
            elif o.get("op") == "filter":
                T.parse_condition(t, o["condition"])
                good.append({"op": "filter", "condition": o["condition"]})
            elif o.get("op") == "formula":
                T.formula_for(t, o["expression"], 2)
                good.append({"op": "formula", "name": str(o["name"])[:40], "expression": o["expression"]})
            elif o.get("op") in ("pivot", "chart"):
                good.append({"op": o["op"], "rows": t.headers[t.col(o["rows"])],
                             "values": t.headers[t.col(o["values"])] if o.get("values") else None,
                             "agg": str(o.get("agg") or "sum").lower(), "columns": None, "kind": str(o.get("kind") or "bar")})
        except (BizError, KeyError, TypeError, AttributeError):
            continue
    return good


def edit_excel(ctx, run: Run, file="", request: str = "", ops: Optional[list] = None, sheet: Optional[str] = None) -> dict:
    """Sort / filter / add formula columns / pivot / chart / totals, from a plain request or explicit ``ops``."""
    path = resolve_files(file, "excel")[0]
    run.sources.append(str(path))
    t = T.read_table(path, sheet)
    run.log("UNDERSTAND", f"{path.name} [{t.name}]: {len(t.rows)} rows; columns: {', '.join(map(str, t.headers))}.")
    ops = ops if ops is not None else plan_excel(ctx, t, request)
    if not ops:
        raise BizError(f"I couldn't work out what to do with {path.name} from “{request}”. Try e.g. “sort by Amount descending”, "
                       "“filter where Region is North”, “add a column Total = Qty * Price”, “pivot Amount by Region”, or "
                       f"“chart Amount by Month”. Columns: {', '.join(map(str, t.headers))}.")
    run.log("PLAN", "; ".join(_describe_op(o) for o in ops))
    extra: list[Sheet] = []
    totals, done = False, []
    for o in ops:
        before = len(t.rows)
        if o["op"] == "clean":
            t, rep = T.clean(t)
            done.append(f"removed {rep['duplicates_removed']} duplicate row(s)")
        elif o["op"] == "sort":
            t = T.sort(t, o["by"], o.get("descending", False))
            done.append(f"sorted by {o['by']}" + (" (largest first)" if o.get("descending") else ""))
        elif o["op"] == "filter":
            t = T.filter_rows(t, o["condition"])
            done.append(f"kept {len(t.rows)} of {before} rows where {o['condition']}")
        elif o["op"] == "formula":
            t = T.add_formula_column(t, o["name"], o["expression"])
            done.append(f"added “{o['name']}” = {o['expression']} as a live formula")
        elif o["op"] == "totals":
            totals = True
            done.append("added a totals row")
        elif o["op"] in ("pivot", "chart"):
            src = T.plain(t)
            by_month = T.col_type(src.column(src.col(o["rows"]))) == "date"
            pv = T.pivot(src, o["rows"], o.get("values"), o.get("agg") or "sum", o.get("columns"), by_month=by_month,
                         name=f"{o.get('values') or 'Count'} by {o['rows']}")
            charts = [Chart(o.get("kind") or "bar", pv.name, 0, list(range(1, len(pv.headers) - (1 if o.get("columns") else 0))))] \
                if o["op"] == "chart" or len(pv.rows) <= 30 else []
            extra.append(Sheet(pv, pv.name, totals=(o.get("agg") or "sum") in ("sum", "count") and not o.get("columns"), charts=charts))
            done.append(f"{'charted' if o['op'] == 'chart' else 'summarised'} {o.get('values') or 'row count'} by {o['rows']} ({len(pv.rows)} groups)")
        run.log("EXECUTE", done[-1])
    out = out_path(ctx, f"{path.stem} - result", ".xlsx")
    info = excel.write(run, out, [Sheet(t, t.name or "Data", totals=totals)] + extra)
    return {"message": f"Done with {path.name}: " + "; ".join(done) + f". Saved as {out.name} ({info['formulas']} formula(s), "
                       f"{info['charts']} chart(s)); the original is unchanged.", "path": str(out), "ops": ops, "rows": len(t.rows)}


def _describe_op(o: dict) -> str:
    return {"clean": "clean the data", "sort": f"sort by {o.get('by')}", "filter": f"filter: {o.get('condition')}",
            "formula": f"add column {o.get('name')} = {o.get('expression')}", "totals": "add a totals row",
            "pivot": f"summarise {o.get('values') or 'rows'} by {o.get('rows')}",
            "chart": f"{o.get('kind', 'bar')} chart of {o.get('values')} by {o.get('rows')}"}[o["op"]]


def merge_excel(ctx, run: Run, files="", mode: str = "append", key: Optional[str] = None) -> dict:
    paths = resolve_files(files, "excel", many=True)
    if len(paths) < 2:
        raise BizError("I need at least two Excel or CSV files to combine. Drop them on the Business page or name them.")
    run.sources += [str(p) for p in paths]
    tables = [T.read_table(p) for p in paths]
    run.log("PLAN", f"{'Join' if mode == 'join' else 'Stack'} {len(tables)} files ({_names(paths)}); keep each file on its own sheet too.")
    merged = T.merge(tables, mode, key)
    sheets = [Sheet(merged, "Combined")]
    for p, t in zip(paths, tables):
        sheets.append(Sheet(t, Path(p).stem[:31]))
    out = out_path(ctx, "Combined workbook", ".xlsx")
    excel.write(run, out, sheets)
    expected = sum(len([r for r in t.rows if not all(T.blank(c) for c in r)]) for t in tables)
    if mode != "join":
        run.log("VERIFY", f"Row count: {len(merged.rows)} combined = {expected} from the sources.", ok=len(merged.rows) == expected)
    dup = len(T.duplicates(merged.copy(headers=merged.headers[:-1], rows=[r[:-1] for r in merged.rows]))) if mode != "join" else 0
    return {"message": f"Combined {len(paths)} files into {out.name}: {len(merged.rows)} rows, {len(merged.headers)} columns"
                       + (f" (a Source column shows where each row came from; {dup} row(s) appear in more than one file)" if mode != "join" else "")
                       + ".", "path": str(out), "rows": len(merged.rows), "duplicates_across_files": dup}


def convert_excel(ctx, run: Run, file="", to: str = "xlsx") -> dict:
    path = resolve_files(file, "excel")[0]
    run.sources.append(str(path))
    to = to.lower().lstrip(".")
    tables = T.read_tables(path)
    if to == "csv":
        outs = []
        for t in tables:
            out = out_path(ctx, path.stem + ("" if len(tables) == 1 else f" - {t.name}"), ".csv")
            T.to_csv(t, out)
            outs.append(out)
        run.deliver(*outs)
        back = T.read_csv(outs[0])
        run.log("VERIFY", f"Re-read {outs[0].name}: {len(back.rows)} rows (source sheet had {len(tables[0].rows)}).", ok=len(back.rows) == len(tables[0].rows))
        return {"message": f"Exported {path.name} to CSV: {_names(outs)}.", "path": str(outs[0]), "paths": [str(o) for o in outs]}
    if to == "pdf":
        out = out_path(ctx, path.stem, ".pdf")
        T.to_pdf(tables, out, path.stem)
        run.deliver(out)
        return {"message": f"Exported {path.name} to PDF: {out.name}.", "path": str(out)}
    if to in ("xlsx", "excel"):
        out = out_path(ctx, path.stem, ".xlsx")
        excel.write(run, out, [Sheet(t, t.name) for t in tables])
        return {"message": f"Converted {path.name} to an Excel workbook: {out.name} ({len(tables[0].rows)} rows, formatted, with filters).",
                "path": str(out)}
    raise BizError("I can convert spreadsheets to xlsx, csv or pdf.")


def sheets_excel(ctx, run: Run, file="", add: Optional[str] = None, rename: Optional[list] = None, delete: Optional[str] = None) -> dict:
    path = resolve_files(file, "excel")[0]
    run.sources.append(str(path))
    out = out_path(ctx, f"{path.stem} - edited", path.suffix if path.suffix.lower() in (".xlsx", ".xlsm") else ".xlsx")
    info = T.edit_sheets(path, out, add=add, rename=tuple(rename) if rename else None, delete=delete)
    run.log("EXECUTE", "; ".join(info["done"]))
    run.deliver(out)
    return {"message": f"In a copy of {path.name} I {' and '.join(info['done'])}. Sheets now: {', '.join(info['sheets'])}. Saved as "
                       f"{out.name}; the original is unchanged.", "path": str(out), "sheets": info["sheets"]}


def report_excel(ctx, run: Run, file="", kind: str = "", word: bool = True, reorder_level: Optional[float] = None) -> dict:
    """Sales (C), inventory (F), employee (B) or financial report: an Excel dashboard plus a Word management report."""
    path = resolve_files(file, "excel")[0]
    run.sources.append(str(path))
    t = T.read_table(path)
    kind = (kind or excel.guess_report(t)).lower()
    if kind not in excel.REPORTS:
        raise BizError("I can make a sales, inventory, employee or financial report.")
    run.log("UNDERSTAND", f"{path.name} [{t.name}]: {len(t.rows)} rows; this looks like {kind} data.")
    run.log("PLAN", {"sales": "Clean the data, compute revenue, rank products, total by month, chart it, write the report.",
                     "inventory": "Clean the data, find low-stock items and inconsistencies, value the stock, suggest reorders.",
                     "employee": "Organise records, list missing fields, summarise by department.",
                     "financial": "Total income and expenses, break down by category and month."}[kind])
    sheets, facts = excel.inventory_report(t, reorder_level) if kind == "inventory" else excel.REPORTS[kind](t)
    title = f"{path.stem} - {kind} report"
    out = out_path(ctx, title, ".xlsx")
    excel.write(run, out, sheets)
    if kind in ("sales", "financial"):  # independent check: re-add the source column and compare with the report total
        t2, a, _ = excel.with_amount(T.clean(t)[0])
        want = round(sum(float(n) for n in (T.to_number(v) for v in t2.column(a)) if n is not None), 2)
        got = facts.get("total", None) if kind == "sales" else None
        if got is not None:
            run.log("VERIFY", f"Report total {got:,.2f} = sum of the source rows {want:,.2f}.", ok=abs(got - want) < 0.01)
    doc = _report_doc(ctx, run, title, kind, facts, sheets, path.name) if word else None
    bullets = excel.facts_text(kind, facts)
    return {"message": f"{kind.capitalize()} report for {path.name}:\n- " + "\n- ".join(bullets[:8]) + f"\nSaved {out.name}"
                       + (f" and {doc.name}" if doc else "") + ".", "path": str(out), "doc": str(doc) if doc else None,
            "facts": facts, "kind": kind}


# ------------------------------------------------------------------ A. invoices
def process_invoices(ctx, run: Run, files="") -> dict:
    paths = resolve_files(files, "document", many=True)
    run.sources += [str(p) for p in paths]
    run.log("PLAN", f"Read {len(paths)} invoice(s); extract vendor, number, dates, line items and totals; check the arithmetic; "
                    "enter everything into one workbook with a summary.")
    inv_rows, item_rows, check_rows, hl = [], [], [], {}
    ok_n = 0
    for p in paths:
        try:
            r = extract.invoice(extract.pages(p), ctx)
        except BizError as exc:
            run.fail("EXECUTE", f"{p.name}: {exc}")
            inv_rows.append([p.name, None, None, None, None, None, None, None, None, None, "Could not be read", str(exc)])
            hl[len(inv_rows) - 1] = "bad"
            continue
        status = "OK" if r["valid"] and not r["missing"] else ("Check: " + "; ".join(
            (["missing " + ", ".join(r["missing"])] if r["missing"] else []) + (["arithmetic does not add up"] if not r["valid"] else [])))
        ok_n += status == "OK"
        inv_rows.append([p.name, r["vendor"], r["invoice_number"], T.to_date(r["invoice_date"] or ""), T.to_date(r["due_date"] or ""),
                         r["customer"], r["currency"], r["subtotal"], r["tax"], r["total"], status,
                         "read by OCR - please double-check" if r["ocr"] else ""])
        if status != "OK":
            hl[len(inv_rows) - 1] = "warn"
        for it in r["items"]:
            item_rows.append([p.name, r["invoice_number"], it["description"], it["qty"], it["price"], it["amount"],
                              "" if it["ok"] else "qty × price ≠ amount", it["source"]])
        for c in r["checks"]:
            check_rows.append([p.name, c["check"], "OK" if c["ok"] else "MISMATCH"])
        if not r["checks"]:
            check_rows.append([p.name, "no totals or line items found to check", "NOT CHECKED"])
        run.log("EXECUTE", f"{p.name}: {r['vendor'] or 'vendor not found'}, no. {r['invoice_number'] or 'not found'}, "
                           f"total {r['total'] if r['total'] is not None else 'not found'}, {len(r['items'])} line item(s) - {status}.",
                ok=status != "Could not be read")
    inv = Table(["File", "Vendor", "Invoice no.", "Invoice date", "Due date", "Customer", "Currency", "Subtotal", "Tax", "Total",
                 "Status", "Note"], inv_rows, "Invoices")
    total = round(sum(r[9] for r in inv_rows if isinstance(r[9], (int, float))), 2)
    by_vendor = T.pivot(inv, "Vendor", "Total", name="By vendor") if any(r[1] for r in inv_rows) else None
    summary = Table(["Measure", "Value"], [["Invoices processed", len(paths)], ["Read without problems", ok_n],
                                           ["Need checking", len(paths) - ok_n], ["Sum of invoice totals", total],
                                           ["Line items extracted", len(item_rows)]], "Summary")
    sheets = [Sheet(summary, "Summary", note="Blank cells mean the value was not found in the invoice - nothing is guessed."),
              Sheet(inv, "Invoices", totals=True, money=[7, 8, 9], highlight=hl),
              Sheet(Table(["File", "Invoice no.", "Description", "Qty", "Unit price", "Amount", "Problem", "Source"], item_rows, "Line items"),
                    "Line items", money=[4, 5]),
              Sheet(Table(["File", "Check", "Result"], check_rows, "Checks"), "Checks",
                    highlight={i: "bad" for i, r in enumerate(check_rows) if r[2] == "MISMATCH"})]
    if by_vendor is not None and len(by_vendor.rows) > 1:
        sheets.append(Sheet(by_vendor, "By vendor", totals=True, money=[1], charts=[Chart("bar", "Invoice total by vendor", 0, [1])]))
    out = out_path(ctx, "Invoice summary", ".xlsx")
    excel.write(run, out, sheets)
    bad = [r[0] for r in inv_rows if r[10] != "OK"]
    return {"message": f"Processed {len(paths)} invoice(s): {ok_n} read cleanly, total {total:,.2f}"
                       + (f". Please check: {', '.join(bad[:6])} (see the Checks sheet)" if bad else "") + f". Saved {out.name}.",
            "path": str(out), "invoices": len(paths), "ok": ok_n, "total": total, "need_checking": bad}


# ------------------------------------------------------------------ D. meetings
def meeting_minutes(ctx, run: Run, file="", text: str = "", email: bool = True) -> dict:
    if not text.strip():
        path = resolve_files(file, "document")[0]
        run.sources.append(str(path))
        text = extract.text_of(path)
    run.log("PLAN", "Extract decisions, action items, owners and deadlines; write the minutes; prepare a follow-up e-mail draft.")
    m = extract.meeting(text, ctx)
    run.log("EXECUTE", f"Found {len(m['decisions'])} decision(s) and {len(m['actions'])} action item(s).")
    no_owner = sum(1 for a in m["actions"] if not a["owner"])
    no_due = sum(1 for a in m["actions"] if not a["due"])
    if no_owner or no_due:
        run.warn(f"{no_owner} action(s) have no owner and {no_due} have no deadline in the notes - marked as such, not guessed.")
    out = out_path(ctx, f"Minutes - {m['title'] or 'meeting'}", ".docx")
    docs.build(out, docs.minutes_spec(m))
    back = docs.read(out)
    run.log("VERIFY", f"Minutes re-opened: {back['words']} words, {len(back['tables'])} table(s).", ok=back["words"] > 5)
    run.deliver(out)
    lines = [f"Hi all,\n\nThanks for joining{(' ' + m['title']) if m['title'] else ' the meeting'}"
             f"{(' on ' + m['date']) if m['date'] else ''}. Here is a summary.\n"]
    if m["summary"]:
        lines.append(m["summary"] + "\n")
    if m["decisions"]:
        lines.append("Decisions:\n" + "\n".join(f"- {d}" for d in m["decisions"]) + "\n")
    if m["actions"]:
        lines.append("Action items:\n" + "\n".join(f"- {a['task']} - {a['owner'] or 'owner to be confirmed'}"
                                                   f"{(' (due ' + a['due'] + ')') if a['due'] else ''}" for a in m["actions"]) + "\n")
    lines.append("Please reply if anything is missing or wrong.\n\nRegards")
    body = "\n".join(lines)
    draft = None
    if email:
        txt = out_path(ctx, f"Follow-up email - {m['title'] or 'meeting'}", ".txt")
        txt.write_text(f"Subject: Follow-up: {m['title'] or 'meeting'}\n\n{body}\n", encoding="utf-8")
        run.deliver(txt)
        draft = str(txt)
    return {"message": f"Minutes written ({len(m['decisions'])} decision(s), {len(m['actions'])} action item(s)): {out.name}."
                       + (f" Follow-up e-mail draft saved as {Path(draft).name} - nothing was sent." if draft else ""),
            "path": str(out), "email_draft": draft, "email_subject": f"Follow-up: {m['title'] or 'meeting'}", "email_body": body,
            "meeting": m}


# ------------------------------------------------------------------ E. research
def research_report(ctx, run: Run, question: str, kind: str = "research report", pdf: bool = False, sources: int = 5, session=None) -> dict:
    run.log("UNDERSTAND", f"Research question: {question}")
    res = research.research(ctx, question, run, n=sources, kind=kind, session=session)
    out = out_path(ctx, question[:60], ".docx")
    docs.build(out, res["spec"])
    run.deliver(out)
    pdf_path = None
    if pdf:
        pdf_path = out.with_suffix(".pdf")
        info = docs.to_pdf(out, pdf_path)
        if info.get("note"):
            run.warn(info["note"])
        run.deliver(pdf_path)
    c = res["check"]
    return {"message": f"Research report written from {len(res['sources'])} source(s), with citations and a source list: {out.name}."
                       + (f" Unverified figures flagged: {', '.join(c['unverified_numbers'][:5])}." if c["unverified_numbers"] else "")
                       + (f" {c['uncited_sentences']} sentence(s) have no citation." if c["uncited_sentences"] else ""),
            "path": str(out), "pdf": str(pdf_path) if pdf_path else None, "sources": [s["url"] for s in res["sources"]], "check": c}


def compare_report(ctx, run: Run, items: list[str], criteria: Optional[list[str]] = None, session=None) -> dict:
    criteria = criteria or ["What it is", "Price", "Key strengths", "Key weaknesses", "Best for"]
    run.log("PLAN", f"Search each of {', '.join(items)}; fill a table of {', '.join(criteria)}; keep only values found in the sources.")
    res = research.comparison(ctx, items, criteria, run, session)
    out = out_path(ctx, "Comparison - " + " vs ".join(items)[:50], ".docx")
    docs.build(out, {"title": "Comparison: " + " vs ".join(items), "meta": [("Date", date.today().strftime("%d %B %Y"))],
                     "sections": [{"heading": "Comparison table", "table": {"headers": res["headers"], "rows": res["rows"]},
                                   "paragraphs": ["“not found” means the sources I read did not state it; nothing was estimated."]},
                                  {"heading": "Sources", "numbered": [f"{s['title']} - {s['url']}" for s in res["sources"]]}]})
    xl = out_path(ctx, "Comparison - " + " vs ".join(items)[:50], ".xlsx")
    excel.write(run, xl, [Sheet(Table(res["headers"], res["rows"], "Comparison"), "Comparison")])
    run.deliver(out)
    nf = sum(c == "not found" for r in res["rows"] for c in r)
    return {"message": f"Comparison of {', '.join(items)} saved as {out.name} and {xl.name} ({nf} cell(s) not found in the sources).",
            "path": str(out), "xlsx": str(xl), "not_found": nf}


# ------------------------------------------------------------------ documents
DOC_KINDS = {"proposal": ["Executive summary", "Understanding of your needs", "Proposed solution", "Scope and deliverables", "Timeline",
                          "Pricing", "Why us", "Next steps"],
             "project report": ["Summary", "Objectives", "Work completed", "Results", "Risks and issues", "Next steps"],
             "technical documentation": ["Overview", "Requirements", "Installation", "Usage", "Configuration", "Troubleshooting"],
             "report": ["Summary", "Background", "Findings", "Analysis", "Recommendations"],
             "resume": ["Profile", "Skills", "Experience", "Education", "Projects"]}


def create_document(ctx, run: Run, kind: str, topic: str = "", details: str = "", data: Optional[dict] = None, pdf: bool = False) -> dict:
    """Invoices, quotations, minutes and letters are built from the details you give (no AI needed for the layout or the
    sums). Proposals, reports, technical docs and resumes are written section by section by the AI model from your details."""
    kind = (kind or "report").lower().strip()
    data = dict(data or {})
    title = topic or kind.title()
    if kind in ("invoice", "quotation", "quote"):
        label = "Invoice" if kind == "invoice" else "Quotation"
        if not data.get("items"):
            data = {**_invoice_details(ctx, details or topic), **data}
        spec, totals = docs.invoice_spec(data, label)
        for p in totals["problems"]:
            run.warn(p)
        title = f"{label} {data.get('number') or ''} {data.get('buyer') or ''}".strip()
        msg = f"{label} created: {totals['items']} item(s), subtotal {totals['subtotal']:,.2f}, tax {totals['tax']:,.2f}, total {totals['total']:,.2f}."
    elif kind in ("minutes", "meeting minutes"):
        return meeting_minutes(ctx, run, text=details or topic)
    elif kind in ("letter", "cover letter"):
        if not data.get("body"):
            if not has_llm(ctx):
                raise BizError("Writing the letter needs an AI model and none is available right now.")
            data["body"] = ask(ctx, f"Write the body of a professional {kind} (3-4 short paragraphs, no greeting, no sign-off, no "
                                    f"placeholders unless a fact is unknown - then write [please fill in]).\nPurpose: {topic}\n"
                                    f"Details: {details}", max_tokens=450, temperature=0.4, purpose="document")
            data.setdefault("subject", topic[:100])
        spec, msg = docs.letter_spec(data), "Letter drafted."
    else:
        if not has_llm(ctx):
            raise BizError(f"Writing a {kind} needs an AI model and none is available right now (is Ollama running?).")
        heads = DOC_KINDS.get(kind) or DOC_KINDS["report"]
        run.log("PLAN", f"{kind.title()} on “{topic}” with sections: {', '.join(heads)}.")
        secs, written = [], []
        for h in heads:
            body = ask(ctx, f"You are writing a {kind} titled “{topic}”. Write the section “{h}” (90-150 words, plain "
                            "paragraphs or short bullet lines starting with “- ”). Use the details below; where a needed fact "
                            f"is not given write [to be confirmed] instead of inventing it. Do not repeat earlier sections.\n"
                            f"Details from the user: {details or '(none given)'}\nSections already written: {', '.join(written) or 'none'}",
                       max_tokens=330, temperature=0.4, purpose="document")
            body = re.sub(rf"^\s*(?:#+\s*)?\**{re.escape(h)}\**:?\s*\n", "", body.strip(), flags=re.I)
            bl = [re.sub(r"^[-*•]\s+", "", x.strip()) for x in body.splitlines() if re.match(r"^\s*[-*•]\s+", x)]
            pa = "\n\n".join(x.strip() for x in body.splitlines() if x.strip() and not re.match(r"^\s*[-*•]\s+", x))
            secs.append({"heading": h, "paragraphs": [pa] if pa else [], "bullets": bl})
            written.append(h)
            run.log("EXECUTE", f"Wrote “{h}” ({len(body.split())} words).")
        spec = {"title": topic or kind.title(), "subtitle": kind.title(), "toc": kind != "resume" and len(heads) >= 5,
                "header": topic[:80], "meta": [("Date", date.today().strftime("%d %B %Y"))], "sections": secs}
        tbc = sum(str(s).count("[to be confirmed]") for s in secs)
        if tbc:
            run.warn(f"{tbc} place(s) are marked [to be confirmed] because the details weren't given.")
        msg = f"{kind.title()} written ({len(heads)} sections)."
    out = out_path(ctx, title, ".docx")
    docs.build(out, spec)
    back = docs.read(out)
    run.log("VERIFY", f"{out.name} re-opened: {back['words']} words, {len(back['tables'])} table(s).", ok=back["words"] > 3)
    run.deliver(out)
    pdf_path = None
    if pdf:
        pdf_path = out.with_suffix(".pdf")
        info = docs.to_pdf(out, pdf_path)
        if info.get("note"):
            run.warn(info["note"])
        run.deliver(pdf_path)
    return {"message": f"{msg} Saved {out.name}" + (f" and {pdf_path.name}" if pdf_path else "") + ".", "path": str(out),
            "pdf": str(pdf_path) if pdf_path else None}


def _invoice_details(ctx, text: str) -> dict:
    """Invoice fields from a sentence like “invoice for Beta Ltd: 3 widgets at 1200, 1 install at 500, 18% GST”."""
    d: dict = {"items": []}
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:x|×|nos\.?|pcs|units? of)?\s+([A-Za-z][\w \-/]{1,50}?)\s+(?:at|@|for)\s+(?:rs\.?|inr|₹|\$)?\s*([\d,]+(?:\.\d+)?)(?:\s*each)?", text, re.I):
        d["items"].append({"description": m.group(2).strip().capitalize(), "qty": float(m.group(1)), "price": T.to_number(m.group(3))})
    m = re.search(r"(\d+(?:\.\d+)?)\s?%\s*(?:gst|tax|vat)|(?:gst|tax|vat)\s*(?:of|@|at)?\s*(\d+(?:\.\d+)?)\s?%", text, re.I)
    if m:
        d["tax_percent"] = float(m.group(1) or m.group(2))
    m = re.search(r"\b(?:for|to|bill(?:ed)? to|customer|client)\s+([A-Z][\w&.\- ]{2,50}?)(?=[:,.;]|\s+(?:for|from|with|\d)|$)", text)
    if m:
        d["buyer"] = m.group(1).strip()
    m = re.search(r"\bfrom\s+([A-Z][\w&.\- ]{2,50}?)(?=[:,.;]|\s+(?:for|to|with|\d)|$)", text)
    if m:
        d["seller"] = m.group(1).strip()
    m = re.search(r"\b(?:invoice|quotation|quote)\s*(?:no\.?|number|#)\s*[:#]?\s*([A-Z0-9][\w/\-]{1,20})", text, re.I)
    if m:
        d["number"] = m.group(1)
    if re.search(r"₹|\brs\.?\b|\binr\b|rupee", text, re.I):
        d["currency"] = "INR"
    elif "$" in text or re.search(r"\busd\b|dollar", text, re.I):
        d["currency"] = "USD"
    return d


def summarise_documents(ctx, run: Run, files="") -> dict:
    paths = resolve_files(files, "any", many=True)
    run.sources += [str(p) for p in paths]
    run.log("PLAN", f"Read {len(paths)} file(s), summarise each, and put the summaries in one Word document.")
    secs, done, method = [], 0, ""
    for p in paths:
        try:
            text = extract.text_of(p)
            s = research.summarise(ctx, text, title=p.name)
        except BizError as exc:
            run.fail("EXECUTE", f"{p.name}: {exc}")
            secs.append({"heading": p.name, "paragraphs": [f"Could not be summarised: {exc}"]})
            continue
        method = s["method"]
        note = [f"Only the first part of this long document was read ({len(text.split()):,} words in total)."] if s["truncated"] else []
        secs.append({"heading": p.name, "paragraphs": [s["summary"]] + note, "bullets": s["points"]})
        done += 1
        run.log("EXECUTE", f"{p.name}: {len(text.split()):,} words summarised.")
    if not done:
        raise BizError("None of those files could be read.")
    overall = None
    if done > 1 and has_llm(ctx):
        overall = ask(ctx, "These are summaries of several documents. In 4-5 sentences say what they have in common and how they "
                           "differ. Use only the summaries.\n\n" + "\n\n".join(f"{s['heading']}: {s['paragraphs'][0]}" for s in secs)[:7000],
                      max_tokens=260, purpose="summarise")
        secs.insert(0, {"heading": "Overview", "paragraphs": [overall]})
    title = f"Summary of {paths[0].stem}" if len(paths) == 1 else f"Summary of {len(paths)} documents"
    out = out_path(ctx, title, ".docx")
    docs.build(out, {"title": title, "subtitle": f"Method: {method}", "sections": secs, "toc": len(paths) > 4})
    run.deliver(out)
    first = next((s for s in secs if s["heading"] != "Overview"), secs[0])
    return {"message": (overall or first["paragraphs"][0]) + f"\n\nSummarised {done} of {len(paths)} file(s); saved {out.name}."
                       + ("" if method == "AI model" else " (No AI model was available, so these are the key sentences, not a rewritten summary.)"),
            "path": str(out), "summarised": done, "method": method}


def compare_documents(ctx, run: Run, files="") -> dict:
    paths = resolve_files(files, "word", many=True)
    if len(paths) != 2:
        raise BizError(f"I compare two Word documents at a time (found {len(paths)}). Name both, or drop exactly two on the Business page.")
    run.sources += [str(p) for p in paths]
    c = docs.compare(paths[0], paths[1])
    out = out_path(ctx, f"Comparison - {paths[0].stem} vs {paths[1].stem}", ".docx")
    docs.build(out, docs.comparison_spec(c))
    run.deliver(out)
    return {"message": f"{c['a']} vs {c['b']}: {c['ratio'] * 100:.0f}% similar - {len(c['changed'])} paragraph(s) changed, "
                       f"{len(c['added'])} added, {len(c['removed'])} removed. Details in {out.name}.", "path": str(out),
            "changed": len(c["changed"]), "added": len(c["added"]), "removed": len(c["removed"])}


def rewrite_document(ctx, run: Run, file="", section: str = "", instruction: str = "", new_text: str = "") -> dict:
    path = resolve_files(file, "word")[0]
    run.sources.append(str(path))
    d = docs.read(path)
    heads = [h for h, _ in d["sections"] if h]
    if not section:
        raise BizError(f"Which section of {path.name} should I rewrite? Sections: {', '.join(heads) or 'none found'}.")
    if not new_text:
        if not has_llm(ctx):
            raise BizError("Rewriting needs an AI model and none is available right now.")
        old = next((b for h, b in d["sections"] if h.lower() == section.lower() or section.lower() in h.lower()), None)
        if old is None:
            raise BizError(f"There is no section called “{section}” in {path.name}. Sections: {', '.join(heads)}.")
        new_text = ask(ctx, f"Rewrite this section of a document. Instruction: {instruction or 'make it clearer and more professional'}. "
                            "Keep every fact, name and number; do not add new facts. Return only the rewritten text.\n\n"
                            f"--- section “{section}” ---\n{old[:5000]}", max_tokens=700, temperature=0.4, purpose="document", cache=False)
        lost = [n for n in dict.fromkeys(research.NUM.findall(old)) if len(re.sub(r"\D", "", n)) >= 2 and n.replace(",", "") not in new_text.replace(",", "")]
        if lost:
            run.warn("The rewrite dropped these figures from the original, please check: " + ", ".join(lost[:8]) + ".")
    out = out_path(ctx, f"{path.stem} - revised", ".docx")
    info = docs.rewrite_section(path, out, section, new_text)
    back = docs.read(out)
    run.log("VERIFY", f"Other sections unchanged: {len(back['sections'])} sections before and after.", ok=len(back["sections"]) == len(d["sections"]))
    run.deliver(out)
    return {"message": f"Rewrote “{info['section']}” ({info['old_words']} → {info['new_words']} words) in a copy: {out.name}. "
                       "The original is unchanged.", "path": str(out)}


def document_to_pdf(ctx, run: Run, file="") -> dict:
    path = resolve_files(file, "any")[0]
    run.sources.append(str(path))
    if path.suffix.lower() in (".xlsx", ".xlsm", ".csv", ".tsv"):
        return convert_excel(ctx, run, str(path), "pdf")
    if path.suffix.lower() != ".docx":
        raise BizError("I can convert Word documents and spreadsheets to PDF.")
    out = out_path(ctx, path.stem, ".pdf")
    info = docs.to_pdf(path, out)
    if info.get("note"):
        run.warn(info["note"])
    run.deliver(out)
    return {"message": f"Converted {path.name} to PDF with {info['engine']}: {out.name}." + (f" {info['note']}" if info.get("note") else ""),
            "path": str(out)}


# ------------------------------------------------------------------ extraction
def extract_data(ctx, run: Run, files="", what: str = "", columns: Optional[list[str]] = None) -> dict:
    """Tables and key details (dates, amounts, e-mails, phone numbers…) from files into one workbook with source references.
    With ``columns`` the AI model builds a table of records with exactly those columns (unfound cells stay empty)."""
    paths = resolve_files(files, "any", many=True)
    run.sources += [str(p) for p in paths]
    run.log("PLAN", f"Read {len(paths)} file(s); pull out tables and {'the fields ' + ', '.join(columns) if columns else 'dates, amounts, e-mails, phone numbers and percentages'}; "
                    "record where each item came from; flag duplicates.")
    ent_rows, sheets, rec_tables, dropped = [], [], [], 0
    wanted = [k for k in ("date", "amount", "email", "phone", "percentage") if re.search(
        {"date": r"date|deadline|when", "amount": r"amount|price|cost|money|total|value", "email": r"e-?mail", "phone": r"phone|mobile|contact",
         "percentage": r"percent|%|rate"}[k], what or "", re.I)] or None
    for p in paths:
        try:
            pgs = extract.pages(p)
        except BizError as exc:
            run.fail("EXECUTE", f"{p.name}: {exc}")
            continue
        ents = extract.entities(pgs, wanted)
        ent_rows += [[e["type"], e["value"], e["normalised"] if e["type"] != "date" else T.to_date(e["normalised"]), e["source"], e["context"]] for e in ents]
        tabs = [] if p.suffix.lower() in (".xlsx", ".xlsm", ".csv", ".tsv") and not columns else extract.tables_of(p)
        for t in tabs[:10]:
            sheets.append(Sheet(t, f"{p.stem[:14]} {t.name}"[:31], note=f"Source: {t.source}"))
        if columns:
            if not has_llm(ctx):
                raise BizError("Building a table with your own columns needs an AI model and none is available right now.")
            t, d = extract.records(ctx, "\n".join(pg.text for pg in pgs), columns, p.name)
            rec_tables.append(t)
            dropped += d
        if any(pg.ocr for pg in pgs):
            run.warn(f"{p.name} was read by OCR - check the values.")
        run.log("EXECUTE", f"{p.name}: {len(ents)} item(s), {len(tabs)} table(s).")
    if columns and rec_tables:
        rec = rec_tables[0].copy(rows=[r for t in rec_tables for r in t.rows], name="Records")
        dup = T.duplicates(rec.copy(headers=rec.headers[:-1], rows=[r[:-1] for r in rec.rows]))
        miss = {i: "warn" for i, r in enumerate(rec.rows) if any(v is None for v in r[:-1])}
        miss.update({i: "bad" for i in dup})
        sheets.insert(0, Sheet(rec, "Records", highlight=miss,
                               note="Yellow = a value was not found in the source (left empty). Red = duplicate record."))
        if dropped:
            run.warn(f"{dropped} value(s) proposed by the AI model were not in the source text and were removed.")
    if ent_rows:
        ent = Table(["Type", "Value", "Normalised", "Source", "Context"], ent_rows, "Details")
        seen, hl = {}, {}
        for i, r in enumerate(ent_rows):
            k = (r[0], str(r[2]))
            if k in seen:
                hl[i] = "warn"
            seen[k] = i
        sheets.insert(1 if columns and rec_tables else 0, Sheet(ent, "Details found", highlight=hl,
                                                               note="Yellow = the same value also appears elsewhere."))
    if not sheets:
        raise BizError("I couldn't find any tables or details to extract in those files.")
    out = out_path(ctx, "Extracted data" if len(paths) > 1 else f"{paths[0].stem} - extracted", ".xlsx")
    excel.write(run, out, sheets)
    counts = {}
    for r in ent_rows:
        counts[r[0]] = counts.get(r[0], 0) + 1
    return {"message": f"Extracted from {len(paths)} file(s): " + (", ".join(f"{v} {k}(s)" for k, v in counts.items()) or "no loose details")
                       + f"; {len([s for s in sheets if s.title not in ('Details found', 'Records')])} table(s)"
                       + (f"; {len(rec_tables[0].rows) if len(rec_tables) == 1 else sum(len(t.rows) for t in rec_tables)} record(s)" if rec_tables else "")
                       + f". Each item has its source. Saved {out.name}.", "path": str(out), "counts": counts}


# ------------------------------------------------------------------ e-mail
def email_triage(ctx, run: Run, query: str = "newer_than:1d", n: int = 15, save: bool = False, unanswered: bool = False) -> dict:
    msgs = mail.unanswered(ctx, 7, n) if unanswered else mail.inbox(ctx, query, n)
    run.log("EXECUTE", f"Read {len(msgs)} message(s) and sorted them by priority.")
    if not msgs:
        run.finish()
        return {"message": "You have no unanswered e-mails from the last week." if unanswered else "No e-mails match that.", "messages": []}
    lines = [f"{i}. [{m['priority']}] {m['subject'][:70]} - {m.get('from_name') or m.get('from', '')} ({m['category']}"
             + (f"; {', '.join(m['reasons'][:2])}" if m["reasons"] else "") + ")" for i, m in enumerate(msgs, 1)]
    out = None
    if save:
        t = Table(["Priority", "Category", "From", "Subject", "Deadline", "Needs reply", "Why", "Date"],
                  [[m["priority"], m["category"], m.get("from_name"), m["subject"], m["deadline"], "yes" if m["needs_reply"] else "",
                    ", ".join(m["reasons"]), m.get("date")] for m in msgs], "Inbox")
        out = out_path(ctx, "Inbox triage", ".xlsx")
        excel.write(run, out, [Sheet(t, "Inbox", highlight={i: "bad" for i, m in enumerate(msgs) if m["priority"] == "Urgent"})])
    run.finish()
    urgent = sum(m["priority"] in ("Urgent", "Important") for m in msgs)
    return {"message": f"{len(msgs)} e-mail(s), {urgent} important or urgent:\n" + "\n".join(lines)
                       + (f"\nSaved {out.name}." if out else ""), "messages": msgs, "path": str(out) if out else None, "untrusted": True}


def email_drafts(ctx, run: Run, query: str = "is:unread newer_than:1d", n: int = 5, instruction: str = "", unanswered: bool = False) -> dict:
    """Write reply drafts (saved in Gmail Drafts) for the messages that need an answer. Nothing is sent."""
    msgs = mail.unanswered(ctx, 7, 15) if unanswered else [m for m in mail.inbox(ctx, query, 15) if m["needs_reply"]]
    msgs = [m for m in msgs if m["priority"] != "Low"][:max(1, min(n, 8))]
    if not msgs:
        run.finish()
        return {"message": "I found no e-mails that need a reply.", "drafts": []}
    run.log("PLAN", f"Write reply drafts for {len(msgs)} message(s) and save them in Gmail Drafts for review. Nothing will be sent.")
    drafts = []
    for m in msgs:
        try:
            full = ctx.gmail.get_message(m["id"])
            d = mail.draft_reply(ctx, {**m, **full}, instruction)
            drafts.append({"to": d["to"], "subject": d["subject"], "body": d["body"], "url": d.get("url"), "draft_id": d.get("draft_id")})
            run.log("EXECUTE", f"Draft saved: {d['subject'][:60]} → {d['to']}")
        except Exception as exc:
            run.fail("EXECUTE", f"{m['subject'][:50]}: {exc}")
    run.finish()
    if not drafts:
        raise BizError("I couldn't create any drafts: " + "; ".join(run.failed[:3]))
    listing = "\n".join(f"{i}. To {d['to']} - {d['subject']}\n   {d['body'][:160].replace(chr(10), ' ')}…" for i, d in enumerate(drafts, 1))
    return {"message": f"{len(drafts)} reply draft(s) are in your Gmail Drafts for review - nothing has been sent:\n{listing}",
            "drafts": drafts, "untrusted": True}


WORKFLOWS = {
    "invoices": ("Invoice processing", "Read invoices (PDF, Word, images), extract vendor, number, dates, line items and totals, "
                                       "check the arithmetic and enter everything into Excel.", "document", True),
    "employees": ("Employee management", "Organise employee records, list missing fields and summarise by department.", "excel", False),
    "sales": ("Sales analysis", "Revenue, best products, monthly performance and charts, plus a management report.", "excel", False),
    "meeting": ("Meeting automation", "Decisions, action items, owners and deadlines from notes or a transcript; minutes and a "
                                      "follow-up e-mail draft.", "document", False),
    "research": ("Research automation", "Search the web, compare sources and write a cited report.", None, False),
    "inventory": ("Inventory management", "Low-stock items, inconsistencies, stock value and reorder suggestions.", "excel", False),
}


def run_workflow(ctx, run: Run, name: str, files="", question: str = "", **kw) -> dict:
    name = name.lower().strip()
    if name == "invoices":
        return process_invoices(ctx, run, files)
    if name in ("employees", "sales", "inventory"):
        return report_excel(ctx, run, files, {"employees": "employee"}.get(name, name), **kw)
    if name == "meeting":
        return meeting_minutes(ctx, run, files, text=question)
    if name == "research":
        if not question.strip():
            raise BizError("What should I research? Type the question.")
        return research_report(ctx, run, question)
    raise BizError(f"I don't have a workflow called “{name}”. I have: {', '.join(WORKFLOWS)}.")
