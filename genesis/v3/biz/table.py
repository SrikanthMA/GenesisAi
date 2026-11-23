"""Excel / CSV engine. Pure Python + openpyxl: reads, profiles, cleans, reshapes and writes workbooks.

A :class:`Table` is headers + rows + where it came from. Every operation returns a *new* table; originals on disk are
never modified (results are written to new files by :func:`save_workbook`)."""
from __future__ import annotations

import csv
import re
import shutil
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from .common import BizError

NUM_RE = re.compile(r"^\(?\s*[-+]?\s*(?:[$€£₹]|rs\.?|inr|usd)?\s*[-+]?\d[\d,]*(?:\.\d+)?\s*%?\s*\)?$", re.I)
DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y",
                "%Y/%m/%d", "%d-%b-%Y", "%d-%b-%y", "%d/%m/%y", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M")
ROLES = {  # column roles by header words (first match wins)
    "amount": r"\b(amount|total|revenue|sales?|value|net|gross|price total|line total|turnover|income|expense|cost|spend|paid)\b",
    "quantity": r"\b(qty|quantity|units?|stock|on hand|count|pieces|pcs|sold)\b",
    "price": r"\b(unit price|price|rate|unit cost|cost per|mrp)\b",
    "date": r"\b(date|day|month|period|invoice date|order date|joined|doj|hired)\b",
    "product": r"\b(product|item|sku|description|article|part)\b",
    "category": r"\b(category|type|segment|group|class|region|account)\b",
    "department": r"\b(department|dept|team|division|unit)\b",
    "name": r"\b(name|employee|staff|customer|client|vendor|supplier)\b",
    "reorder": r"\b(reorder|re-order|min(?:imum)?(?: stock| level)?|threshold|safety stock)\b",
    "salary": r"\b(salary|ctc|pay|wage|compensation)\b",
    "id": r"\b(id|code|number|no\.?|#)\b",
}


@dataclass
class Table:
    headers: list[str]
    rows: list[list]
    name: str = "Sheet1"
    source: str = ""
    header_row: int = 1  # 1-based row of the headers in the source sheet
    notes: list[str] = field(default_factory=list)

    def col(self, ref) -> int:
        """Index of a column from its name (exact, case-insensitive, then closest), letter or number. BizError if unknown."""
        if isinstance(ref, int):
            if 0 <= ref < len(self.headers):
                return ref
            raise BizError(f"There is no column {ref + 1}.")
        want = str(ref).strip().lower()
        low = [str(h).strip().lower() for h in self.headers]
        if want in low:
            return low.index(want)
        squash = lambda t: re.sub(r"[^a-z0-9]", "", t)  # noqa: E731
        sq = [squash(h) for h in low]
        if squash(want) and squash(want) in sq:
            return sq.index(squash(want))
        part = [i for i, h in enumerate(low) if want and (want in h or h in want) and h]
        if len(part) == 1:
            return part[0]
        if want.rstrip("s") != want:
            try:
                return self.col(want.rstrip("s"))
            except BizError:
                pass
        raise BizError(f"I couldn't find a column called “{ref}”. The columns are: {', '.join(map(str, self.headers))}.")

    def find(self, role: str) -> Optional[int]:
        pat = re.compile(ROLES[role], re.I)
        for i, h in enumerate(self.headers):
            if pat.search(str(h).replace("_", " ")):
                return i
        return None

    def column(self, i: int) -> list:
        return [r[i] if i < len(r) else None for r in self.rows]

    def copy(self, rows=None, headers=None, name=None) -> "Table":
        return Table(list(headers if headers is not None else self.headers),
                     [list(r) for r in (rows if rows is not None else self.rows)], name or self.name, self.source,
                     self.header_row, list(self.notes))


# ------------------------------------------------------------------ values
def blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def to_number(v) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str) and NUM_RE.match(v.strip()) and re.search(r"\d", v):
        t = v.strip()
        neg = t.startswith("(") and t.endswith(")")
        pct = "%" in t
        t = re.sub(r"[^\d.\-]", "", t.replace(",", ""))
        try:
            n = float(t)
        except ValueError:
            return None
        n = -abs(n) if neg else n
        n = n / 100 if pct else n
        return int(n) if n == int(n) and not pct and "." not in t else n
    return None


def to_date(v) -> Optional[date]:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and 6 <= len(v.strip()) <= 20 and re.search(r"\d", v):
        for f in DATE_FORMATS:
            try:
                return datetime.strptime(v.strip(), f).date()
            except ValueError:
                continue
    return None


def col_type(values: list) -> str:
    vals = [v for v in values if not blank(v)]
    if not vals:
        return "empty"
    n = len(vals)
    if sum(isinstance(v, (date, datetime)) or (isinstance(v, str) and to_date(v) is not None) for v in vals) >= 0.8 * n:
        return "date"
    if sum(to_number(v) is not None for v in vals) >= 0.8 * n:
        return "number"
    return "text"


# ------------------------------------------------------------------ reading
def _header_index(grid: list[list]) -> int:
    for i, row in enumerate(grid[:15]):
        cells = [c for c in row if not blank(c)]
        if len(cells) >= 2 and sum(isinstance(c, str) for c in cells) >= 0.7 * len(cells):
            nxt = grid[i + 1] if i + 1 < len(grid) else []
            if len([c for c in nxt if not blank(c)]) >= max(1, len(cells) // 2) or i + 1 >= len(grid):
                return i
    return 0


def _from_grid(grid: list[list], name: str, source: str) -> Optional[Table]:
    grid = [list(r) for r in grid]
    while grid and all(blank(c) for c in grid[-1]):
        grid.pop()
    if not grid:
        return None
    h = _header_index(grid)
    width = max(len(r) for r in grid[h:])
    keep = [j for j in range(width) if any(j < len(r) and not blank(r[j]) for r in grid[h:])]
    headers, seen = [], {}
    for j in keep:
        v = grid[h][j] if j < len(grid[h]) else None
        t = str(v).strip() if not blank(v) else f"Column {j + 1}"
        seen[t.lower()] = seen.get(t.lower(), 0) + 1
        headers.append(t if seen[t.lower()] == 1 else f"{t} ({seen[t.lower()]})")
    rows = [[(r[j] if j < len(r) else None) for j in keep] for r in grid[h + 1:]]
    return Table(headers, rows, name, source, h + 1)


def read_csv(path: Path) -> Table:
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeError:
            continue
    try:
        dialect = csv.Sniffer().sniff(text[:4000], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel_tab if Path(path).suffix.lower() == ".tsv" else csv.excel
    grid = [row for row in csv.reader(text.splitlines(), dialect)]
    t = _from_grid(grid, Path(path).stem[:31], str(path))
    if t is None:
        raise BizError(f"{Path(path).name} is empty.")
    for i in range(len(t.headers)):  # CSV has no types: turn clean number columns into numbers
        if col_type(t.column(i)) == "number":
            for r in t.rows:
                n = to_number(r[i])
                if n is not None:
                    r[i] = n
    return t


def read_tables(path) -> list[Table]:
    """Every non-empty sheet of a workbook (or the one table of a CSV). Formulas are read as their last saved value."""
    path = Path(path)
    if not path.is_file():
        raise BizError(f"I can't find {path}.")
    if path.suffix.lower() in (".csv", ".tsv", ".txt"):
        return [read_csv(path)]
    try:
        from openpyxl import load_workbook

        wb_v = load_workbook(path, data_only=True, read_only=True)
        wb_f = load_workbook(path, data_only=False, read_only=True)
    except Exception as exc:
        raise BizError(f"I couldn't open {path.name}: {exc}") from exc
    out = []
    for ws in wb_v.worksheets:
        grid = [list(r) for r in ws.iter_rows(values_only=True)]
        fgrid = [list(r) for r in wb_f[ws.title].iter_rows(values_only=True)]
        uncached = 0
        for i, row in enumerate(grid):
            for j, v in enumerate(row):
                if v is None and i < len(fgrid) and j < len(fgrid[i]) and isinstance(fgrid[i][j], str) \
                        and fgrid[i][j].startswith("="):
                    uncached += 1
        t = _from_grid(grid, ws.title, str(path))
        if t is not None:
            if uncached:
                t.notes.append(f"{uncached} formula cell(s) in “{ws.title}” have no saved value (open and save the file in "
                               "Excel once to calculate them).")
            out.append(t)
    wb_v.close()
    wb_f.close()
    if not out:
        raise BizError(f"{path.name} has no data.")
    return out


def read_table(path, sheet: Optional[str] = None) -> Table:
    tables = read_tables(path)
    if sheet:
        for t in tables:
            if t.name.lower() == str(sheet).lower():
                return t
        raise BizError(f"There is no sheet “{sheet}”. Sheets: {', '.join(t.name for t in tables)}.")
    return max(tables, key=lambda t: len(t.rows) * len(t.headers))


# ------------------------------------------------------------------ profile & clean
def _norm(v) -> str:
    return re.sub(r"\s+", " ", str(v)).strip().lower() if not blank(v) else ""


def _row_key(row: list, cols: Optional[list[int]] = None) -> tuple:
    idx = cols if cols is not None else range(len(row))
    out = []
    for i in idx:
        v = row[i] if i < len(row) else None
        n = to_number(v) if not isinstance(v, str) else None
        out.append(round(float(n), 6) if n is not None else _norm(v))
    return tuple(out)


def duplicates(t: Table, cols: Optional[list[int]] = None) -> list[int]:
    """Indexes of rows that repeat an earlier row (all columns, or just ``cols``). Blank rows are ignored."""
    seen, dup = set(), []
    for i, r in enumerate(t.rows):
        if all(blank(c) for c in r):
            continue
        k = _row_key(r, cols)
        if k in seen:
            dup.append(i)
        seen.add(k)
    return dup


def profile(t: Table) -> dict:
    cols = []
    issues = []
    for i, h in enumerate(t.headers):
        vals = t.column(i)
        kind = col_type(vals)
        missing = sum(blank(v) for v in vals)
        info = {"name": h, "type": kind, "missing": missing, "distinct": len({_norm(v) for v in vals if not blank(v)})}
        present = [v for v in vals if not blank(v)]
        if kind == "number":
            nums = [to_number(v) for v in present]
            as_text = sum(isinstance(v, str) for v, n in zip(present, nums) if n is not None)
            bad = [v for v, n in zip(present, nums) if n is None]
            if as_text:
                issues.append({"column": h, "issue": "numbers stored as text", "count": as_text})
            if bad:
                issues.append({"column": h, "issue": "not a number", "count": len(bad), "examples": [str(b) for b in bad[:3]]})
            good = [float(n) for n in nums if n is not None]
            if good:
                info.update(sum=round(sum(good), 4), mean=round(statistics.fmean(good), 4), min=min(good), max=max(good))
        elif kind == "date":
            bad = [v for v in present if to_date(v) is None]
            texty = sum(isinstance(v, str) for v in present)
            if bad:
                issues.append({"column": h, "issue": "not a date", "count": len(bad), "examples": [str(b) for b in bad[:3]]})
            if texty:
                issues.append({"column": h, "issue": "dates stored as text", "count": texty})
        elif kind == "text":
            ws = sum(isinstance(v, str) and (v != v.strip() or "  " in v) for v in present)
            if ws:
                issues.append({"column": h, "issue": "extra spaces", "count": ws})
            forms: dict[str, set] = {}
            for v in present:
                forms.setdefault(_norm(v), set()).add(re.sub(r"\s+", " ", str(v)).strip())
            variants = {k: sorted(s) for k, s in forms.items() if len(s) > 1}
            if variants and info["distinct"] <= max(50, len(present) // 2):
                issues.append({"column": h, "issue": "same value written differently", "count": len(variants),
                               "examples": [" / ".join(v) for v in list(variants.values())[:3]]})
        if missing:
            issues.append({"column": h, "issue": "missing values", "count": missing})
        cols.append(info)
    dup = duplicates(t)
    empty = sum(all(blank(c) for c in r) for r in t.rows)
    return {"sheet": t.name, "rows": len(t.rows), "columns": cols, "duplicate_rows": len(dup),
            "duplicate_row_numbers": [d + t.header_row + 1 for d in dup[:20]], "empty_rows": empty, "issues": issues,
            "notes": list(t.notes)}


def clean(t: Table, *, dedupe: bool = True, key: Optional[list] = None) -> tuple[Table, dict]:
    """Trim spaces, turn text numbers/dates into real ones, unify spellings that differ only by case/spacing, drop empty
    rows and (optionally) duplicate rows. Missing values are reported, never invented."""
    out = t.copy()
    rep = {"trimmed": 0, "numbers_fixed": 0, "dates_fixed": 0, "spellings_unified": 0, "empty_rows_removed": 0,
           "duplicates_removed": 0, "missing_left": 0}
    before = len(out.rows)
    out.rows = [r for r in out.rows if not all(blank(c) for c in r)]
    rep["empty_rows_removed"] = before - len(out.rows)
    for i in range(len(out.headers)):
        vals = out.column(i)
        kind = col_type(vals)
        canon: dict[str, str] = {}
        if kind == "text":
            counts: dict[str, dict[str, int]] = {}
            for v in vals:
                if not blank(v):
                    s = re.sub(r"\s+", " ", str(v)).strip()
                    counts.setdefault(s.lower(), {}).setdefault(s, 0)
                    counts[s.lower()][s] += 1
            canon = {k: max(d.items(), key=lambda kv: (kv[1], not kv[0].islower(), kv[0]))[0] for k, d in counts.items()}
        for r in out.rows:
            v = r[i]
            if blank(v):
                r[i] = None
                rep["missing_left"] += 1
                continue
            if isinstance(v, str):
                s = re.sub(r"\s+", " ", v).strip()
                if s != v:
                    rep["trimmed"] += 1
                if kind == "number" and to_number(s) is not None:
                    r[i] = to_number(s)
                    rep["numbers_fixed"] += 1
                elif kind == "date" and to_date(s) is not None:
                    r[i] = to_date(s)
                    rep["dates_fixed"] += 1
                elif kind == "text":
                    c = canon.get(s.lower(), s)
                    if c != s:
                        rep["spellings_unified"] += 1
                    r[i] = c
                else:
                    r[i] = s
            elif isinstance(v, datetime) and v.time() == datetime.min.time():
                r[i] = v.date()
    if dedupe:
        cols = [out.col(k) for k in key] if key else None
        dup = set(duplicates(out, cols))
        rep["duplicates_removed"] = len(dup)
        out.rows = [r for i, r in enumerate(out.rows) if i not in dup]
    return out, rep


# ------------------------------------------------------------------ sort / filter / stats / pivot / merge
def _sort_key(v):
    if blank(v):
        return (3, "")
    d = v if isinstance(v, (date, datetime)) else None
    if d is not None:
        return (1, datetime(d.year, d.month, d.day).timestamp())
    n = to_number(v)
    if n is not None:
        return (0, float(n))
    return (2, str(v).lower())


def sort(t: Table, by, descending: bool = False) -> Table:
    cols = [t.col(b) for b in (by if isinstance(by, (list, tuple)) else [by])]
    rows = [r for r in t.rows if not all(blank(c) for c in r)]
    for c in reversed(cols):
        filled = sorted((r for r in rows if not blank(r[c])), key=lambda r: _sort_key(r[c]), reverse=descending)
        rows = filled + [r for r in rows if blank(r[c])]  # blanks always last
    return t.copy(rows=rows)


COND_RE = re.compile(r"^\s*(.+?)\s*(>=|<=|!=|<>|=|==|>|<|\bcontains\b|\bdoes not contain\b|\bstarts with\b|\bends with\b|"
                     r"\bis empty\b|\bis not empty\b|\bis blank\b|\bis not blank\b|\bafter\b|\bbefore\b|\bis not\b|\bis\b|"
                     r"\bequals\b|\bover\b|\babove\b|\bmore than\b|\bgreater than\b|\bunder\b|\bbelow\b|\bless than\b|"
                     r"\bat least\b|\bat most\b)\s*(.*?)\s*$", re.I)
_OPS = {"over": ">", "above": ">", "more than": ">", "greater than": ">", "under": "<", "below": "<", "less than": "<",
        "at least": ">=", "at most": "<=", "is": "=", "equals": "=", "==": "=", "<>": "!=", "is not": "!=", "after": ">",
        "before": "<", "is blank": "is empty", "is not blank": "is not empty"}


def parse_condition(t: Table, text: str) -> tuple[int, str, Any]:
    m = COND_RE.match(text or "")
    if not m:
        raise BizError(f"I didn't understand the filter “{text}”. Try e.g. “Amount > 500” or “City is Delhi”.")
    col, op, val = t.col(m.group(1).strip().strip("\"'")), m.group(2).lower().strip(), m.group(3).strip().strip("\"'")
    return col, _OPS.get(op, op), val


def _test(v, op: str, val: str) -> bool:
    if op == "is empty":
        return blank(v)
    if op == "is not empty":
        return not blank(v)
    if blank(v):
        return op == "!="
    if op in ("contains", "does not contain", "starts with", "ends with"):
        s, w = str(v).lower(), val.lower()
        return {"contains": w in s, "does not contain": w not in s, "starts with": s.startswith(w), "ends with": s.endswith(w)}[op]
    a, b = to_number(v), to_number(val)
    if a is None or b is None:
        da, db = to_date(v), to_date(val)
        if da is not None and db is not None:
            a, b = da.toordinal(), db.toordinal()
        else:
            a, b = _norm(v), _norm(val)
            if op not in ("=", "!="):
                return False
    return {"=": a == b, "!=": a != b, ">": a > b, "<": a < b, ">=": a >= b, "<=": a <= b}[op]


def filter_rows(t: Table, conditions) -> Table:
    conds = [parse_condition(t, c) for c in ([conditions] if isinstance(conditions, str) else conditions)]
    return t.copy(rows=[r for r in t.rows if all(_test(r[c], op, val) for c, op, val in conds)])


def stats(t: Table) -> list[dict]:
    out = []
    for i, h in enumerate(t.headers):
        if col_type(t.column(i)) != "number":
            continue
        nums = [float(n) for n in (to_number(v) for v in t.column(i)) if n is not None]
        if nums:
            out.append({"column": h, "count": len(nums), "sum": round(sum(nums), 4), "mean": round(statistics.fmean(nums), 4),
                        "median": round(statistics.median(nums), 4), "min": min(nums), "max": max(nums),
                        "stdev": round(statistics.stdev(nums), 4) if len(nums) > 1 else 0.0})
    return out


AGGS = {"sum": sum, "average": statistics.fmean, "mean": statistics.fmean, "count": len, "min": min, "max": max,
        "median": statistics.median}


def _bucket(v, by_month: bool):
    if blank(v):
        return "(blank)"
    if by_month:
        d = to_date(v)
        return d.strftime("%Y-%m") if d else "(no date)"
    return v if isinstance(v, (int, float)) else str(v).strip()


def pivot(t: Table, rows, values=None, agg: str = "sum", columns=None, by_month: bool = False, name: str = "Pivot") -> Table:
    """Group ``rows`` (optionally across ``columns``) and aggregate ``values``. With no values column, counts rows."""
    agg = (agg or "sum").lower()
    if agg not in AGGS:
        raise BizError(f"I can do {', '.join(sorted(set(AGGS)))} - not “{agg}”.")
    r_i = t.col(rows)
    v_i = t.col(values) if values is not None else None
    c_i = t.col(columns) if columns is not None else None
    if v_i is None:
        agg = "count"
    row_month = by_month and col_type(t.column(r_i)) == "date"
    cells: dict = {}
    col_keys: list = []
    for r in t.rows:
        if all(blank(c) for c in r):
            continue
        rk = _bucket(r[r_i], row_month)
        ck = _bucket(r[c_i], False) if c_i is not None else "_"
        if ck not in col_keys:
            col_keys.append(ck)
        if agg == "count":
            cells.setdefault(rk, {}).setdefault(ck, []).append(1)
        else:
            n = to_number(r[v_i])
            if n is not None:
                cells.setdefault(rk, {}).setdefault(ck, []).append(float(n))
            else:
                cells.setdefault(rk, {}).setdefault(ck, [])
    fn = AGGS[agg]
    val = lambda xs: (round(fn(xs), 4) if xs else 0)  # noqa: E731
    label = "Count" if v_i is None else f"{agg.title()} of {t.headers[v_i]}"
    keys = sorted(cells, key=lambda k: _sort_key(k))
    if c_i is None:
        out = [[k, val(cells[k].get("_", []))] for k in keys]
        return Table([t.headers[r_i] + (" (month)" if row_month else ""), label], out, name[:31], t.source)
    col_keys = sorted(col_keys, key=_sort_key)
    out = []
    for k in keys:
        vals = [val(cells[k].get(c, [])) for c in col_keys]
        allv = [x for c in col_keys for x in cells[k].get(c, [])]
        out.append([k] + vals + [val(allv)])
    return Table([t.headers[r_i]] + [str(c) for c in col_keys] + ["Total"], out, name[:31], t.source)


def merge(tables: list[Table], mode: str = "append", key=None) -> Table:
    """``append``: stack the tables (columns matched by name, a Source column records where each row came from).
    ``join``: one row per key value, columns from every table side by side."""
    if len(tables) < 2:
        raise BizError("I need at least two tables to combine.")
    norm = lambda h: re.sub(r"[^a-z0-9]", "", str(h).lower())  # noqa: E731
    if mode == "join":
        if key is None:
            common = set(norm(h) for h in tables[0].headers)
            for t in tables[1:]:
                common &= {norm(h) for h in t.headers}
            cand = [h for h in tables[0].headers if norm(h) in common]
            if not cand:
                raise BizError("These files share no column to join on. Tell me which column to match.")
            key = next((h for h in cand if re.search(ROLES["id"], str(h), re.I)), cand[0])
        headers = [tables[0].headers[tables[0].col(key)]]
        data: dict = {}
        order: list = []
        for t in tables:
            k_i = t.col(key)
            extra = [i for i in range(len(t.headers)) if i != k_i]
            start = len(headers)
            for i in extra:
                h = t.headers[i]
                headers.append(h if h not in headers else f"{h} ({Path(t.source).stem or t.name})")
            for r in t.rows:
                if blank(r[k_i]):
                    continue
                kk = _row_key(r, [k_i])
                if kk not in data:
                    data[kk] = {0: r[k_i]}
                    order.append(kk)
                for off, i in enumerate(extra):
                    data[kk].setdefault(start + off, r[i])
        rows = [[data[k].get(j) for j in range(len(headers))] for k in order]
        return Table(headers, rows, "Combined", "; ".join(t.source for t in tables))
    headers: list[str] = []
    for t in tables:
        for h in t.headers:
            if norm(h) not in [norm(x) for x in headers]:
                headers.append(h)
    pos = {norm(h): i for i, h in enumerate(headers)}
    rows = []
    for t in tables:
        label = Path(t.source).name if t.source else t.name
        if sum(Path(x.source).name == label for x in tables) > 1:
            label += f" / {t.name}"
        for r in t.rows:
            if all(blank(c) for c in r):
                continue
            new = [None] * len(headers)
            for i, h in enumerate(t.headers):
                new[pos[norm(h)]] = r[i]
            rows.append(new + [label])
    return Table(headers + ["Source"], rows, "Combined", "; ".join(t.source for t in tables))


# ------------------------------------------------------------------ formulas
def col_letter(i: int) -> str:
    from openpyxl.utils import get_column_letter

    return get_column_letter(i + 1)


def formula_for(t: Table, expression: str, row: int) -> str:
    """Turn “Quantity * Unit Price” into ``=C5*D5`` for sheet row ``row``. Column names are matched longest first; only
    arithmetic, numbers, brackets and a few safe functions are allowed."""
    expr = expression.strip().lstrip("=")
    names = sorted(((str(h), i) for i, h in enumerate(t.headers)), key=lambda x: -len(x[0]))
    marks: dict[str, str] = {}
    for h, i in names:
        pat = re.compile(r"(?<![\w])\[?" + re.escape(h) + r"\]?(?![\w])", re.I)
        token = f"\x00{len(marks)}\x00"
        if pat.search(expr):
            expr = pat.sub(token, expr)
            marks[token] = f"{col_letter(i)}{row}"
    left = re.sub(r"\x00\d+\x00", "", expr)
    left = re.sub(r"\b(SUM|AVERAGE|MIN|MAX|ROUND|ABS|IF|AND|OR)\b", "", left, flags=re.I)
    if re.search(r"[^\d\s.+\-*/(),%<>=]", left):
        bad = re.findall(r"[A-Za-z_][\w ]*", left)
        raise BizError(f"I couldn't turn “{expression}” into a formula" + (f" (unknown: {', '.join(b.strip() for b in bad[:3])})" if bad else "") +
                       f". Columns: {', '.join(map(str, t.headers))}.")
    if not marks:
        raise BizError(f"“{expression}” doesn't mention any column. Columns: {', '.join(map(str, t.headers))}.")
    for token, ref in marks.items():
        expr = expr.replace(token, ref)
    return "=" + re.sub(r"\s+", "", expr)


@dataclass
class Formula:
    """A column whose cells are Excel formulas built from ``expression`` (column names)."""
    expression: str


def add_formula_column(t: Table, name: str, expression: str) -> Table:
    formula_for(t, expression, 2)  # validate now
    out = t.copy(headers=t.headers + [name])
    for r in out.rows:
        r.append(Formula(expression))
    return out


# ------------------------------------------------------------------ writing
@dataclass
class Chart:
    kind: str  # bar | line | pie
    title: str
    labels: int  # column index of the categories
    values: list[int]  # column indexes of the series
    anchor: str = ""
    max_rows: int = 30


@dataclass
class Sheet:
    table: Table
    title: str = ""
    totals: bool = False  # add a TOTAL row with =SUM() under number columns
    charts: list[Chart] = field(default_factory=list)
    note: str = ""
    highlight: dict = field(default_factory=dict)  # {row index: "bad" | "warn"}
    money: list[int] = field(default_factory=list)
    percent: list[int] = field(default_factory=list)


def _safe(v):
    """Text from data must never become a live formula (CSV injection)."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") and to_number(v) is None:
        return "'" + v
    return v


def save_workbook(path, sheets: list[Sheet]) -> dict:
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, LineChart, PieChart, Reference
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)
    used: set[str] = set()
    n_formulas = 0
    head_fill, head_font = PatternFill("solid", fgColor="1F4E78"), Font(bold=True, color="FFFFFF")
    fills = {"bad": PatternFill("solid", fgColor="F8CBAD"), "warn": PatternFill("solid", fgColor="FFE699"),
             "good": PatternFill("solid", fgColor="C6E0B4")}
    for sh in sheets:
        t = sh.table
        title = re.sub(r"[\[\]:*?/\\]", " ", (sh.title or t.name or "Sheet"))[:31].strip() or "Sheet"
        base, k = title, 2
        while title.lower() in used:
            title = f"{base[:28]} {k}"
            k += 1
        used.add(title.lower())
        ws = wb.create_sheet(title)
        top = 1
        if sh.note:
            ws.cell(1, 1, sh.note).font = Font(italic=True, color="595959")
            top = 3
        for j, h in enumerate(t.headers, 1):
            c = ws.cell(top, j, str(h))
            c.fill, c.font, c.alignment = head_fill, head_font, Alignment(vertical="center", wrap_text=True)
        types = [col_type(t.column(j)) for j in range(len(t.headers))]
        for i, r in enumerate(t.rows):
            row = top + 1 + i
            for j, v in enumerate(r[:len(t.headers)], 1):
                if isinstance(v, Formula):
                    shifted = Table(t.headers, [], t.name)
                    v = formula_for(shifted, v.expression, row)
                    n_formulas += 1
                else:
                    v = _safe(v)
                c = ws.cell(row, j, v)
                if isinstance(v, (date, datetime)):
                    c.number_format = "DD-MMM-YYYY"
                elif (j - 1) in sh.money or (isinstance(v, float) and (j - 1) not in sh.percent):
                    c.number_format = "#,##0.00"
                if (j - 1) in sh.percent:
                    c.number_format = "0.0%"
                if i in sh.highlight:
                    c.fill = fills[sh.highlight[i]]
        last = top + len(t.rows)
        if sh.totals and t.rows:
            ws.cell(last + 1, 1, "TOTAL").font = Font(bold=True)
            for j, kind in enumerate(types, 1):
                has_formula = any(isinstance(r[j - 1], Formula) for r in t.rows if j - 1 < len(r))
                idish = re.search(r"\b(id|no\.?|number|code|year|phone|zip|pin|price|rate|reorder|level|age|%)\b", str(t.headers[j - 1]), re.I)
                if j > 1 and (kind == "number" or has_formula) and not idish and (j - 1) not in sh.percent:
                    L = get_column_letter(j)
                    c = ws.cell(last + 1, j, f"=SUM({L}{top + 1}:{L}{last})")
                    c.font, c.number_format = Font(bold=True), "#,##0.00"
                    n_formulas += 1
        for j, h in enumerate(t.headers, 1):
            width = max([len(str(h))] + [len(str(v)) if not isinstance(v, (Formula, float)) else 12
                                         for v in t.column(j - 1)[:200] if v is not None] or [8])
            ws.column_dimensions[get_column_letter(j)].width = max(9, min(width + 3, 48))
        ws.freeze_panes = ws.cell(top + 1, 1)
        if t.rows:
            ws.auto_filter.ref = f"A{top}:{get_column_letter(len(t.headers))}{last}"
        anchor_col = len(t.headers) + 2
        for n, ch in enumerate(sh.charts):
            rows_n = min(len(t.rows), ch.max_rows)
            if rows_n < 1:
                continue
            obj = {"bar": BarChart, "line": LineChart, "pie": PieChart}.get(ch.kind, BarChart)()
            obj.title, obj.height, obj.width = ch.title, 8.5, 16
            for v in ch.values[:1] if ch.kind == "pie" else ch.values:
                obj.add_data(Reference(ws, min_col=v + 1, min_row=top, max_row=top + rows_n), titles_from_data=True)
            obj.set_categories(Reference(ws, min_col=ch.labels + 1, min_row=top + 1, max_row=top + rows_n))
            if ch.kind != "pie":
                obj.y_axis.title = str(t.headers[ch.values[0]]) if len(ch.values) == 1 else None
                obj.legend = None if len(ch.values) == 1 else obj.legend
            ws.add_chart(obj, ch.anchor or f"{get_column_letter(anchor_col)}{2 + n * 18}")
    path = Path(path)
    wb.save(path)
    return {"path": str(path), "sheets": [s for s in wb.sheetnames], "formulas": n_formulas}


def validate_workbook(path) -> dict:
    """Re-open a workbook we wrote and check it: sheets exist, formulas parse, their ranges are inside the sheet, no
    error values. Returns {"ok", "sheets", "formulas", "charts", "problems"}."""
    from openpyxl import load_workbook
    from openpyxl.formula import Tokenizer
    from openpyxl.utils import range_boundaries

    problems: list[str] = []
    try:
        wb = load_workbook(path)
    except Exception as exc:
        return {"ok": False, "sheets": [], "formulas": 0, "charts": 0, "problems": [f"cannot be opened: {exc}"]}
    formulas = charts = 0
    for ws in wb.worksheets:
        charts += len(getattr(ws, "_charts", []))
        for row in ws.iter_rows():
            for c in row:
                v = c.value
                if isinstance(v, str) and v.startswith("#") and v.rstrip("!?/0").upper() in ("#REF", "#DIV", "#NAME", "#VALUE", "#N/A", "#NUM"):
                    problems.append(f"{ws.title}!{c.coordinate} holds the error {v}")
                if isinstance(v, str) and v.startswith("=") and len(v) > 1:
                    formulas += 1
                    try:
                        for tok in Tokenizer(v).items:
                            if tok.type == "OPERAND" and tok.subtype == "RANGE" and "!" not in tok.value:
                                c1, r1, c2, r2 = range_boundaries(tok.value.replace("$", ""))
                                if (r2 or r1) > ws.max_row or (c2 or c1) > ws.max_column:
                                    problems.append(f"{ws.title}!{c.coordinate}: {tok.value} points outside the data")
                                if c.row in range(r1 or 0, (r2 or r1 or 0) + 1) and c.column in range(c1 or 0, (c2 or c1 or 0) + 1):
                                    problems.append(f"{ws.title}!{c.coordinate} refers to itself")
                            if tok.type == "OPERAND" and tok.subtype == "ERROR":
                                problems.append(f"{ws.title}!{c.coordinate}: {v}")
                    except Exception:
                        problems.append(f"{ws.title}!{c.coordinate}: formula could not be read ({v[:40]})")
    return {"ok": not problems, "sheets": wb.sheetnames, "formulas": formulas, "charts": charts, "problems": problems[:20]}


def evaluate(t: Table, expression: str, row: list):
    """The value a formula column will show for one row (used to verify formulas and for PDF/CSV exports)."""
    f = formula_for(t, expression, 1)[1:]
    for i in sorted(range(len(t.headers)), key=lambda i: -len(col_letter(i))):
        n = to_number(row[i]) if i < len(row) and not isinstance(row[i], Formula) else None
        f = re.sub(rf"(?<![A-Z]){col_letter(i)}1(?!\d)", repr(float(n)) if n is not None else "0.0", f)
    if not re.fullmatch(r"[\d\s.+\-*/()e]+", f):
        return None
    try:
        return round(eval(f, {"__builtins__": {}}, {}), 6)  # noqa: S307 - digits and arithmetic only (checked above)
    except Exception:
        return None


def plain(t: Table) -> Table:
    """The table with formula cells replaced by their values."""
    rows = [[evaluate(t, v.expression, r) if isinstance(v, Formula) else v for v in r] for r in t.rows]
    return t.copy(rows=rows)


def to_csv(t: Table, path) -> str:
    t = plain(t)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(t.headers)
        for r in t.rows:
            w.writerow(["" if v is None else (v.isoformat() if isinstance(v, (date, datetime)) else _safe(v)) for v in r])
    return str(path)


def fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (date, datetime)):
        return v.strftime("%d-%b-%Y")
    if isinstance(v, float):
        return f"{v:,.2f}"
    if isinstance(v, int) and not isinstance(v, bool):
        return f"{v:,}" if abs(v) >= 10000 else str(v)
    return str(v)


def to_pdf(tables: list[Table], path, title: str = "") -> str:
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
        from reportlab.platypus import Table as RTable
        from reportlab.platypus import TableStyle
    except ImportError as exc:
        raise BizError("PDF export needs the “reportlab” package (run Update_Genesis_3.4.bat, or: pip install reportlab).") from exc
    from xml.sax.saxutils import escape

    styles = getSampleStyleSheet()
    wide = any(len(t.headers) > 6 for t in tables)
    doc = SimpleDocTemplate(str(path), pagesize=landscape(A4) if wide else A4, leftMargin=28, rightMargin=28, topMargin=32,
                            bottomMargin=32, title=title or "Genesis report")
    small = styles["BodyText"].clone("small")
    small.fontSize, small.leading = 7.5, 9
    flow = []
    if title:
        flow += [Paragraph(escape(title), styles["Title"]), Spacer(1, 8)]
    for t in tables:
        t = plain(t)
        flow.append(Paragraph(escape(t.name), styles["Heading2"]))
        data = [[Paragraph(f"<b>{escape(str(h))}</b>", small) for h in t.headers]]
        data += [[Paragraph(escape(fmt(v)), small) for v in r[:len(t.headers)]] for r in t.rows[:2000]]
        rt = RTable(data, repeatRows=1)
        rt.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#D9E2F3")),
                                ("GRID", (0, 0), (-1, -1), 0.3, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        flow += [rt, Spacer(1, 14)]
    doc.build(flow)
    return str(path)


# ------------------------------------------------------------------ worksheets of an existing workbook (on a copy)
def edit_sheets(src, dst, *, add: Optional[str] = None, rename: Optional[tuple] = None, delete: Optional[str] = None) -> dict:
    """Add / rename / delete a worksheet. Works on a copy (``dst``); the original file is left untouched."""
    from openpyxl import load_workbook

    src, dst = Path(src), Path(dst)
    if src.suffix.lower() not in (".xlsx", ".xlsm"):
        raise BizError("Worksheets can only be changed in an .xlsx workbook (convert the CSV first).")
    shutil.copy2(src, dst)
    wb = load_workbook(dst, keep_vba=dst.suffix.lower() == ".xlsm")
    names = {n.lower(): n for n in wb.sheetnames}
    done = []
    if add:
        if add.lower() in names:
            raise BizError(f"There is already a sheet called “{names[add.lower()]}”.")
        wb.create_sheet(add[:31])
        done.append(f"added “{add[:31]}”")
    if rename:
        old, new = rename
        if old.lower() not in names:
            raise BizError(f"There is no sheet “{old}”. Sheets: {', '.join(wb.sheetnames)}.")
        if new.lower() in names:
            raise BizError(f"There is already a sheet called “{new}”.")
        wb[names[old.lower()]].title = new[:31]
        done.append(f"renamed “{names[old.lower()]}” to “{new[:31]}”")
    if delete:
        if delete.lower() not in names:
            raise BizError(f"There is no sheet “{delete}”. Sheets: {', '.join(wb.sheetnames)}.")
        if len(wb.sheetnames) == 1:
            raise BizError("A workbook must keep at least one sheet.")
        del wb[names[delete.lower()]]
        done.append(f"deleted “{names[delete.lower()]}”")
    wb.save(dst)
    return {"path": str(dst), "sheets": wb.sheetnames, "done": done}
