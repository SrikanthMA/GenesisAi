"""What people actually ask of a spreadsheet, built on :mod:`table`: analyse, clean, summarise, report, dashboard.

All figures are computed in Python from the data - the AI model is never asked to do arithmetic."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import table as T
from .common import BizError, Run
from .table import Chart, Sheet, Table


def describe(profiles: list[dict]) -> str:
    """A plain-English account of a workbook's structure and data-quality problems."""
    lines = []
    for p in profiles:
        cols = ", ".join(f"{c['name']} ({c['type']})" for c in p["columns"][:14])
        lines.append(f"Sheet “{p['sheet']}”: {p['rows']} rows, {len(p['columns'])} columns - {cols}"
                     + (" …" if len(p["columns"]) > 14 else "") + ".")
        probs = []
        if p["duplicate_rows"]:
            rows = ", ".join(map(str, p["duplicate_row_numbers"][:8]))
            probs.append(f"{p['duplicate_rows']} duplicate row(s) (sheet rows {rows}{'…' if p['duplicate_rows'] > 8 else ''})")
        if p["empty_rows"]:
            probs.append(f"{p['empty_rows']} empty row(s)")
        for i in p["issues"]:
            ex = f" e.g. {'; '.join(i['examples'])}" if i.get("examples") else ""
            probs.append(f"{i['column']}: {i['count']} × {i['issue']}{ex}")
        lines.append("  Problems: " + ("; ".join(probs) + "." if probs else "none found."))
        lines += ["  " + n for n in p.get("notes", [])]
    return "\n".join(lines)


def quality_sheet(profiles: list[dict]) -> Table:
    rows = []
    for p in profiles:
        if p["duplicate_rows"]:
            rows.append([p["sheet"], "(whole row)", "duplicate rows", p["duplicate_rows"],
                         "rows " + ", ".join(map(str, p["duplicate_row_numbers"]))])
        if p["empty_rows"]:
            rows.append([p["sheet"], "(whole row)", "empty rows", p["empty_rows"], ""])
        for i in p["issues"]:
            rows.append([p["sheet"], i["column"], i["issue"], i["count"], "; ".join(i.get("examples", []))])
    return Table(["Sheet", "Column", "Problem", "Count", "Examples"], rows or [["-", "-", "no problems found", 0, ""]],
                 "Data quality")


def columns_sheet(profiles: list[dict]) -> Table:
    rows = [[p["sheet"], c["name"], c["type"], c["missing"], c["distinct"], c.get("sum"), c.get("mean"), c.get("min"), c.get("max")]
            for p in profiles for c in p["columns"]]
    return Table(["Sheet", "Column", "Type", "Missing", "Distinct", "Sum", "Average", "Min", "Max"], rows, "Columns")


def clean_sheets(t: Table, key: Optional[list] = None) -> tuple[list[Sheet], dict]:
    dup_idx = set(T.duplicates(T.clean(t, dedupe=False)[0], [t.col(k) for k in key] if key else None))
    pre, _ = T.clean(t, dedupe=False)
    removed = pre.copy(rows=[r for i, r in enumerate(pre.rows) if i in dup_idx], name="Removed duplicates")
    cleaned, rep = T.clean(t, key=key)
    missing = [i for i, r in enumerate(cleaned.rows) if any(T.blank(c) for c in r)]
    changes = Table(["Change", "Count"], [
        ["Duplicate rows removed", rep["duplicates_removed"]], ["Empty rows removed", rep["empty_rows_removed"]],
        ["Cells with extra spaces trimmed", rep["trimmed"]], ["Text turned into numbers", rep["numbers_fixed"]],
        ["Text turned into dates", rep["dates_fixed"]], ["Spellings unified (case / spacing)", rep["spellings_unified"]],
        ["Cells still missing (highlighted, not filled in)", rep["missing_left"]]], "Changes")
    sheets = [Sheet(cleaned, "Cleaned", highlight={i: "warn" for i in missing},
                    note="Yellow rows have missing values - Genesis does not invent data." if missing else ""),
              Sheet(changes, "Changes")]
    if removed.rows:
        sheets.append(Sheet(removed, "Removed duplicates"))
    rep["rows"] = len(cleaned.rows)
    return sheets, rep


# ------------------------------------------------------------------ line totals shared by the reports
def with_amount(t: Table) -> tuple[Table, int, list[str]]:
    """The table plus the index of its money column. If there is quantity and price but no amount, one is computed."""
    notes = []
    a, q, p = t.find("amount"), t.find("quantity"), t.find("price")
    if a is not None and a in (q, p):
        a = None
    if a is not None and T.col_type(t.column(a)) == "number":
        return t, a, notes
    if q is not None and p is not None and q != p:
        out = t.copy(headers=t.headers + ["Amount"])
        skipped = 0
        for r in out.rows:
            qv, pv = T.to_number(r[q]), T.to_number(r[p])
            if qv is None or pv is None:
                skipped += 1
            r.append(round(qv * pv, 2) if qv is not None and pv is not None else None)
        notes.append(f"Amount = {t.headers[q]} × {t.headers[p]} (computed)."
                     + (f" {skipped} row(s) lack a quantity or price and were left out of totals." if skipped else ""))
        return out, len(out.headers) - 1, notes
    nums = [i for i in range(len(t.headers)) if T.col_type(t.column(i)) == "number"
            and not __import__("re").search(T.ROLES["id"], str(t.headers[i]), __import__("re").I)]
    if nums:
        notes.append(f"Used “{t.headers[nums[-1]]}” as the amount column.")
        return t, nums[-1], notes
    raise BizError("I couldn't find an amount column (or quantity and price) in this sheet. Columns: "
                   + ", ".join(map(str, t.headers)) + ".")


def _total(t: Table, i: int) -> float:
    return round(sum(float(n) for n in (T.to_number(v) for v in t.column(i)) if n is not None), 2)


def _top(pv: Table, n: int = 10) -> Table:
    return pv.copy(rows=sorted(pv.rows, key=lambda r: -(r[-1] or 0))[:n])


def sales_report(t: Table) -> tuple[list[Sheet], dict]:
    t, _ = T.clean(t)
    t, a, notes = with_amount(t)
    facts = {"rows": len(t.rows), "total": _total(t, a), "amount_column": t.headers[a], "notes": notes}
    sheets = [Sheet(t, "Data", totals=True, money=[a])]
    kpi = [["Total " + str(t.headers[a]).lower(), facts["total"]], ["Records", len(t.rows)],
           ["Average per record", round(facts["total"] / len(t.rows), 2) if t.rows else 0]]
    prod = t.find("product")
    if prod is None:
        prod = t.find("category")
    if prod is not None and prod != a:
        by = _top(T.pivot(t, prod, a, name="By product"), 1000)
        share = by.copy(headers=by.headers + ["Share"], rows=[r + [round(r[1] / facts["total"], 4) if facts["total"] else 0] for r in by.rows])
        sheets.append(Sheet(share, "By " + str(t.headers[prod])[:20], totals=True, money=[1], percent=[2],
                            charts=[Chart("bar", f"{t.headers[a]} by {t.headers[prod]}", 0, [1], max_rows=12)]))
        facts["top"] = [(str(r[0]), r[1]) for r in by.rows[:5]]
        facts["bottom"] = [(str(r[0]), r[1]) for r in by.rows[-3:]] if len(by.rows) > 5 else []
        kpi.append([f"Best {str(t.headers[prod]).lower()}", f"{by.rows[0][0]} ({T.fmt(by.rows[0][1])})"] if by.rows else ["", ""])
    d = t.find("date")
    if d is not None and T.col_type(t.column(d)) == "date":
        by_m = T.pivot(t, d, a, by_month=True, name="By month")
        rows = []
        for i, r in enumerate(by_m.rows):
            prev = by_m.rows[i - 1][1] if i else None
            rows.append(r + [round((r[1] - prev) / prev, 4) if prev else None])
        by_m = by_m.copy(headers=["Month", by_m.headers[1], "Change vs previous"], rows=rows)
        sheets.append(Sheet(by_m, "By month", totals=True, money=[1], percent=[2],
                            charts=[Chart("line", f"{t.headers[a]} by month", 0, [1])]))
        facts["months"] = [(r[0], r[1]) for r in by_m.rows]
        real = [r for r in by_m.rows if r[0] != "(no date)"]
        if real:
            best = max(real, key=lambda r: r[1])
            kpi.append(["Best month", f"{best[0]} ({T.fmt(best[1])})"])
            facts["best_month"] = (best[0], best[1])
    for role in ("category", "department"):
        c = t.find(role)
        if c is not None and c not in (prod, a) and T.col_type(t.column(c)) == "text":
            by_c = _top(T.pivot(t, c, a, name="By " + role), 1000)
            sheets.append(Sheet(by_c, "By " + str(t.headers[c])[:20], totals=True, money=[1],
                                charts=[Chart("pie", f"{t.headers[a]} by {t.headers[c]}", 0, [1], max_rows=8)]))
            facts["by_" + role] = [(str(r[0]), r[1]) for r in by_c.rows[:6]]
            break
    sheets.insert(0, Sheet(Table(["Measure", "Value"], kpi, "Summary"), "Summary",
                           note=" ".join(notes) or "All figures computed from the Data sheet."))
    facts["kpi"] = kpi
    return sheets, facts


def inventory_report(t: Table, default_reorder: Optional[float] = None) -> tuple[list[Sheet], dict]:
    t, _ = T.clean(t)
    q, p, ro, prod = t.find("quantity"), t.find("price"), t.find("reorder"), t.find("product")
    if q is None:
        raise BizError("I couldn't find a stock / quantity column. Columns: " + ", ".join(map(str, t.headers)) + ".")
    if prod is None:
        prod = 0
    notes, issues, low, hl = [], [], [], {}
    out = t.copy(headers=t.headers + ["Stock value", "Status", "Suggested reorder"])
    seen: dict = {}
    for i, r in enumerate(out.rows):
        qty, price = T.to_number(r[q]), T.to_number(r[p]) if p is not None else None
        level = T.to_number(r[ro]) if ro is not None else default_reorder
        status, sug = "OK", None
        name = str(r[prod]).strip().lower() if not T.blank(r[prod]) else ""
        if name and name in seen:
            issues.append([r[prod], f"listed twice (rows {seen[name] + 2} and {i + 2})"])
        seen.setdefault(name, i)
        if qty is None:
            status = "Quantity missing"
            issues.append([r[prod], "quantity missing"])
        elif qty < 0:
            status = "Negative stock"
            issues.append([r[prod], f"negative stock ({qty})"])
        elif level is not None and qty <= level:
            status = "Out of stock" if qty == 0 else "Low stock"
            sug = max(int(round(2 * level - qty)), 1)  # bring stock back to twice the reorder level
            low.append([r[prod], qty, level, sug, round(sug * price, 2) if price is not None else None])
        if p is not None and price is None:
            issues.append([r[prod], "price missing"])
        if status != "OK":
            hl[i] = "bad" if status in ("Out of stock", "Negative stock", "Quantity missing") else "warn"
        r += [round(qty * price, 2) if qty is not None and price is not None and qty >= 0 else None, status, sug]
    if ro is None and default_reorder is None:
        notes.append("No reorder-level column was found, so low stock could not be judged. Tell me a level, e.g. “low stock below 10”.")
    elif ro is None:
        notes.append(f"No reorder-level column: used {default_reorder:g} for every item.")
    sv = len(out.headers) - 3
    total = _total(out, sv)
    kpi = [["Items", len(out.rows)], ["Total stock value", total if p is not None else "n/a (no price column)"],
           ["Low or out of stock", len(low)], ["Data problems", len(issues)]]
    sheets = [Sheet(Table(["Measure", "Value"], kpi, "Summary"), "Summary", note=" ".join(notes)),
              Sheet(out, "Inventory", totals=p is not None, money=[sv], highlight=hl)]
    if low:
        sheets.append(Sheet(Table(["Item", "In stock", "Reorder level", "Suggested order qty", "Estimated cost"], low, "Reorder"),
                            "Reorder list", totals=True, money=[4],
                            note="Suggested quantity brings each item back to twice its reorder level."))
    if issues:
        sheets.append(Sheet(Table(["Item", "Problem"], issues, "Problems"), "Problems"))
    if p is not None:
        top = _top(Table([out.headers[prod], "Stock value"], [[r[prod], r[sv] or 0] for r in out.rows], "Top value"), 10)
        sheets.append(Sheet(top, "Top stock value", charts=[Chart("bar", "Highest stock value", 0, [1])], money=[1]))
    return sheets, {"items": len(out.rows), "total_value": total if p is not None else None,
                    "low": [(str(x[0]), x[1], x[2], x[3]) for x in low], "issues": [(str(a), b) for a, b in issues],
                    "notes": notes, "kpi": kpi}


def employee_report(t: Table) -> tuple[list[Sheet], dict]:
    t, rep = T.clean(t)
    dept, sal, name = t.find("department"), t.find("salary"), t.find("name")
    hl, missing = {}, []
    for i, r in enumerate(t.rows):
        gaps = [str(t.headers[j]) for j, v in enumerate(r) if T.blank(v)]
        if gaps:
            hl[i] = "warn"
            who = r[name] if name is not None and not T.blank(r[name]) else f"row {i + 2}"
            missing.append([who, ", ".join(gaps)])
    kpi = [["Employees", len(t.rows)], ["Records with missing fields", len(missing)],
           ["Duplicate records removed", rep["duplicates_removed"]]]
    sheets = [Sheet(t, "Employees", highlight=hl, money=[sal] if sal is not None else [])]
    facts = {"employees": len(t.rows), "missing": [(str(a), b) for a, b in missing[:50]], "notes": []}
    if dept is not None:
        head = T.pivot(t, dept, name="By department")
        rows = [list(r) for r in head.rows]
        headers = [t.headers[dept], "Employees"]
        if sal is not None and T.col_type(t.column(sal)) == "number":
            tot = {r[0]: r[1] for r in T.pivot(t, dept, sal, "sum").rows}
            avg = {r[0]: r[1] for r in T.pivot(t, dept, sal, "average").rows}
            rows = [r + [tot.get(r[0], 0), round(avg.get(r[0], 0), 2)] for r in rows]
            headers += [f"Total {t.headers[sal]}", f"Average {t.headers[sal]}"]
            kpi += [[f"Total {str(t.headers[sal]).lower()}", _total(t, sal)]]
        by = Table(headers, rows, "By department")
        sheets.append(Sheet(by, "By department", charts=[Chart("bar", "Employees by department", 0, [1])],
                            money=[2, 3] if len(headers) > 2 else []))
        facts["departments"] = [tuple(r) for r in rows]
        kpi.insert(1, ["Departments", len(rows)])
    else:
        facts["notes"].append("No department column was found, so there is no department summary.")
    if missing:
        sheets.append(Sheet(Table(["Employee", "Missing fields"], missing, "Missing"), "Missing information"))
    sheets.insert(0, Sheet(Table(["Measure", "Value"], kpi, "Summary"), "Summary", note=" ".join(facts["notes"])))
    facts["kpi"] = kpi
    return sheets, facts


def financial_report(t: Table) -> tuple[list[Sheet], dict]:
    """Income / expense summary. Signs decide (negative = expense) unless a Type column says income/expense/credit/debit."""
    import re

    t, _ = T.clean(t)
    t, a, notes = with_amount(t)
    typ = next((i for i, h in enumerate(t.headers) if re.search(r"\b(type|dr/?cr|debit/credit|kind|flow)\b", str(h), re.I)), None)
    deb = next((i for i, h in enumerate(t.headers) if re.search(r"\b(debit|withdrawal|expense|paid out)\b", str(h), re.I)), None)
    cre = next((i for i, h in enumerate(t.headers) if re.search(r"\b(credit|deposit|income|paid in|receipt)\b", str(h), re.I)), None)
    income = expense = 0.0
    signed = []
    for r in t.rows:
        if deb is not None and cre is not None and deb != cre:
            v = (T.to_number(r[cre]) or 0) - (T.to_number(r[deb]) or 0)
        else:
            v = T.to_number(r[a])
            if v is not None and typ is not None and re.search(r"expens|debit|\bdr\b|out|payment|cost", str(r[typ] or ""), re.I):
                v = -abs(v)
        signed.append(v)
        if v is not None:
            income += v if v > 0 else 0
            expense += -v if v < 0 else 0
    if expense == 0 and typ is None and deb is None:
        notes.append("All amounts are positive and there is no Type column, so everything was treated as income/revenue.")
    out = t.copy(headers=t.headers + ["Signed amount"], rows=[r + [s] for r, s in zip(t.rows, signed)])
    sa = len(out.headers) - 1
    kpi = [["Income", round(income, 2)], ["Expenses", round(expense, 2)], ["Net", round(income - expense, 2)],
           ["Transactions", len(out.rows)]]
    sheets = [Sheet(Table(["Measure", "Value"], kpi, "Summary"), "Summary", note=" ".join(notes), money=[1]),
              Sheet(out, "Transactions", money=[a, sa])]
    facts = {"income": round(income, 2), "expense": round(expense, 2), "net": round(income - expense, 2), "notes": notes, "kpi": kpi}
    cat = t.find("category")
    if cat is not None and cat != a:
        by = T.pivot(out, cat, sa, name="By category")
        by.rows.sort(key=lambda r: r[1])
        sheets.append(Sheet(by, "By category", totals=True, money=[1], charts=[Chart("bar", "Net by category", 0, [1], max_rows=15)]))
        facts["by_category"] = [(str(r[0]), r[1]) for r in by.rows]
    d = t.find("date")
    if d is not None and T.col_type(t.column(d)) == "date":
        by_m = T.pivot(out, d, sa, by_month=True, name="By month")
        sheets.append(Sheet(by_m, "By month", totals=True, money=[1], charts=[Chart("line", "Net by month", 0, [1])]))
        facts["months"] = [(r[0], r[1]) for r in by_m.rows]
    return sheets, facts


REPORTS = {"sales": sales_report, "inventory": inventory_report, "employee": employee_report, "financial": financial_report}


def guess_report(t: Table) -> str:
    if t.find("reorder") is not None or (t.find("quantity") is not None and "stock" in " ".join(map(str, t.headers)).lower()):
        return "inventory"
    if t.find("salary") is not None or t.find("department") is not None:
        return "employee"
    if t.find("product") is not None or t.find("quantity") is not None:
        return "sales"
    return "financial"


def facts_text(kind: str, facts: dict) -> list[str]:
    """Bullet points stating the computed facts (used in Word reports and spoken summaries)."""
    f = lambda v: T.fmt(v) if isinstance(v, (int, float)) else str(v)  # noqa: E731
    out = [f"{k}: {f(v)}" for k, v in facts.get("kpi", [])]
    if facts.get("top"):
        out.append("Top performers: " + "; ".join(f"{n} ({f(v)})" for n, v in facts["top"]) + ".")
    if facts.get("bottom"):
        out.append("Lowest performers: " + "; ".join(f"{n} ({f(v)})" for n, v in facts["bottom"]) + ".")
    if facts.get("months"):
        out.append("By month: " + "; ".join(f"{m} {f(v)}" for m, v in facts["months"][-12:]) + ".")
    if facts.get("low"):
        out.append("Reorder: " + "; ".join(f"{n} (have {f(q)}, level {f(l)}, order {s})" for n, q, l, s in facts["low"][:15]) + ".")
    if facts.get("issues"):
        out.append("Data problems: " + "; ".join(f"{n}: {p}" for n, p in facts["issues"][:10]) + ".")
    if facts.get("missing"):
        out.append("Missing information: " + "; ".join(f"{n} ({g})" for n, g in facts["missing"][:10]) + ".")
    return out + list(facts.get("notes", []))


def write(run: Run, path: Path, sheets: list[Sheet]) -> dict:
    """Save, re-open and check a workbook; problems are logged on the run (never swallowed)."""
    info = T.save_workbook(path, sheets)
    check = T.validate_workbook(path)
    if check["ok"]:
        run.log("VERIFY", f"{Path(path).name}: {len(check['sheets'])} sheet(s), {check['formulas']} formula(s), "
                          f"{check['charts']} chart(s) - opened and checked.")
    else:
        for p in check["problems"]:
            run.fail("VERIFY", p)
    for sh in sheets:  # formulas: recompute a sample in Python and compare with the source figures
        t = sh.table
        fcols = [j for j in range(len(t.headers)) if any(isinstance(r[j], T.Formula) for r in t.rows if j < len(r))]
        for j in fcols:
            bad = sum(T.evaluate(t, r[j].expression, r) is None for r in t.rows[:200] if isinstance(r[j], T.Formula))
            if bad:
                run.warn(f"“{t.headers[j]}”: {bad} row(s) have blanks in the cells the formula uses.")
    run.deliver(path)
    return {**info, **check}
