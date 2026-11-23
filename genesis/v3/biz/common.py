"""Shared plumbing: where results go, which files the user means, and the run log."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from ..work import _llm, documents_dir, slug, unique  # noqa: F401  (re-exported)

KINDS = {
    "excel": (".xlsx", ".xlsm", ".csv", ".tsv"),
    "word": (".docx",),
    "pdf": (".pdf",),
    "text": (".txt", ".md"),
    "image": (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"),
}
KINDS["document"] = KINDS["pdf"] + KINDS["word"] + KINDS["text"] + KINDS["image"]
KINDS["any"] = KINDS["excel"] + KINDS["document"]
STAGES = ("UNDERSTAND", "PLAN", "EXECUTE", "VERIFY", "DELIVER")


class BizError(Exception):
    """A problem the user can act on (shown as-is)."""


def out_dir(ctx=None) -> Path:
    base = os.getenv("GENESIS_BUSINESS_DIR")
    p = Path(base) if base else documents_dir() / "Genesis Business"
    p.mkdir(parents=True, exist_ok=True)
    return p


def out_path(ctx, title: str, ext: str) -> Path:
    return unique(out_dir(ctx) / f"{slug(title, 60)}{ext if ext.startswith('.') else '.' + ext}")


def data_dir(ctx=None) -> Path:
    cfg = getattr(ctx, "config", None)
    base = getattr(getattr(cfg, "paths", None), "data_dir", None)
    p = Path(base) if base else Path.home() / ".genesis"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ------------------------------------------------------------------ which files does the user mean?
_ATTACHED: list[str] = []  # files dropped on the Business page (most recent last)
_LOCK = threading.Lock()


def attach(paths: Iterable[str]) -> list[str]:
    with _LOCK:
        for p in paths:
            p = str(Path(p))
            if p in _ATTACHED:
                _ATTACHED.remove(p)
            _ATTACHED.append(p)
        del _ATTACHED[:-40]
        return list(_ATTACHED)


def attached(kind: str = "any") -> list[str]:
    ext = KINDS.get(kind, KINDS["any"])
    with _LOCK:
        return [p for p in _ATTACHED if p.lower().endswith(ext) and Path(p).is_file()]


def clear_attached() -> None:
    with _LOCK:
        _ATTACHED.clear()


def search_roots() -> list[Path]:
    docs = documents_dir()
    roots = [docs, docs.parent / "Desktop", Path.home() / "Desktop", Path.home() / "Downloads", out_dir()]
    seen, out = set(), []
    for r in roots:
        if r.is_dir() and str(r).lower() not in seen:
            seen.add(str(r).lower())
            out.append(r)
    return out


_PATH_RE = re.compile(r'"([^"]+)"|\'([^\']+)\'|([A-Za-z]:\\[^\s,;]+(?: [^\s,;]+)*?\.\w{2,5})|((?:~|/)[^\s,;]+\.\w{2,5})')
_NAME_RE = re.compile(r"([\w][\w\-. ()]{0,80}\.(?:xlsx|xlsm|csv|tsv|docx|pdf|txt|md|png|jpe?g))\b", re.I)


def _walk(root: Path, ext: tuple, depth: int = 2, limit: int = 4000):
    stack, n = [(root, 0)], 0
    while stack:
        d, lvl = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            n += 1
            if n > limit:
                return
            try:
                if e.is_file() and e.name.lower().endswith(ext) and not e.name.startswith("~$"):
                    yield Path(e.path)
                elif e.is_dir() and lvl < depth and not e.name.startswith((".", "$")) and e.name not in (
                        "node_modules", "__pycache__", ".venv", "venv", "AppData"):
                    stack.append((Path(e.path), lvl + 1))
            except OSError:
                continue


def resolve_files(ref, kind: str = "any", many: bool = False, roots: Optional[list[Path]] = None) -> list[Path]:
    """The file(s) the user means. ``ref`` may be a path, a file name, part of a name, a folder, a list of those, or
    empty / "this" / "these" (= what was dropped on the Business page, otherwise the newest matching file).

    Raises BizError with a message the user can act on - it never guesses silently between several candidates."""
    ext = KINDS.get(kind, KINDS["any"])
    if isinstance(ref, (list, tuple)):
        out: list[Path] = []
        for r in ref:
            out += resolve_files(r, kind, many=True, roots=roots)
        if not out:
            raise BizError("I couldn't find those files.")
        return out
    text = str(ref or "").strip()
    found: list[Path] = []
    for m in _PATH_RE.finditer(text):
        raw = next(g for g in m.groups() if g)
        p = Path(os.path.expandvars(os.path.expanduser(raw)))
        if p.is_file():
            found.append(p)
        elif p.is_dir():
            found += sorted(_walk(p, ext, depth=0))
    if not found and text and Path(text).expanduser().is_dir():
        found = sorted(_walk(Path(text).expanduser(), ext, depth=0))
    if not found and text and Path(text).expanduser().is_file():
        found = [Path(text).expanduser()]
    roots = roots if roots is not None else search_roots()
    if not found:
        for name in _NAME_RE.findall(text):
            for r in roots:
                hits = [p for p in _walk(r, ext) if p.name.lower() == name.strip().lower()]
                if hits:
                    found.append(max(hits, key=lambda p: p.stat().st_mtime))
                    break
    vague = not text or re.fullmatch(r"(this|that|these|those|the|my|it|them|latest|last|recent|newest|current)?\s*"
                                     r"(excel|spreadsheet|workbook|sheet|file|files|document|documents|doc|pdf|pdfs|csv|"
                                     r"invoice|invoices|notes|report|word)?s?(\s+files?)?", text, re.I)
    if not found and vague:
        att = [Path(p) for p in attached(kind)]
        if att:
            found = att if many else att[-1:]
        else:
            cands = sorted((p for r in roots for p in _walk(r, ext, depth=1)), key=lambda p: p.stat().st_mtime, reverse=True)
            if cands:
                found = cands[:1]
    if not found and text and not vague:  # part of a name: "the sales workbook"
        words = [w for w in re.findall(r"[a-z0-9]{3,}", text.lower()) if w not in (
            "the", "this", "that", "excel", "file", "files", "workbook", "spreadsheet", "sheet", "document", "word", "pdf",
            "csv", "and", "from", "with", "into", "these", "those", "invoice", "invoices")]
        if words:
            scored = []
            for r in roots:
                for p in _walk(r, ext):
                    hit = sum(w in p.stem.lower() for w in words)
                    if hit:
                        scored.append((hit, p.stat().st_mtime, p))
            if scored:
                scored.sort(reverse=True)
                top = [s for s in scored if s[0] == scored[0][0]]
                if len(top) > 1 and not many:
                    names = ", ".join(s[2].name for s in top[:5])
                    raise BizError(f"More than one file matches “{text}”: {names}. Tell me which one (the full name).")
                found = [s[2] for s in top] if many else [top[0][2]]
    found = [p for p in dict.fromkeys(found) if p.suffix.lower() in ext]
    if not found:
        what = {"excel": "an Excel or CSV file", "word": "a Word document", "pdf": "a PDF"}.get(kind, "a file")
        raise BizError(f"I couldn't find {what}" + (f" matching “{text}”" if text and not vague else "") +
                       ". Drop it on the Business page, or give me its full path or file name.")
    return found if many else found[:1]


# ------------------------------------------------------------------ run log
@dataclass
class Run:
    """One piece of work, logged stage by stage. Nothing is hidden: every failure is recorded and reported."""
    task: str
    kind: str = "task"
    steps: list[dict] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    status: str = "running"
    on_step: Optional[Callable[[dict], None]] = None

    def log(self, stage: str, message: str, ok: bool = True) -> None:
        step = {"stage": stage, "message": message, "ok": bool(ok), "t": round(time.time() - self.started, 2)}
        self.steps.append(step)
        if self.on_step:
            try:
                self.on_step(step)
            except Exception:
                pass

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        self.log("VERIFY", "Note: " + message, ok=True)

    def fail(self, stage: str, message: str) -> None:
        self.log(stage, message, ok=False)
        self.status = "failed"

    def deliver(self, *paths) -> None:
        for p in paths:
            p = Path(p)
            if not p.is_file() or p.stat().st_size == 0:
                self.fail("VERIFY", f"{p.name} was not written correctly.")
                continue
            self.outputs.append(str(p))
            self.log("DELIVER", f"Saved {p}")
        if self.status == "running":
            self.status = "done" if not any(not s["ok"] for s in self.steps) else "done with problems"

    def finish(self) -> None:
        if self.status == "running":
            self.status = "done" if not any(not s["ok"] for s in self.steps) else "done with problems"

    @property
    def failed(self) -> list[str]:
        return [s["message"] for s in self.steps if not s["ok"]]

    def to_dict(self) -> dict:
        return {"task": self.task, "kind": self.kind, "status": self.status, "steps": self.steps, "outputs": self.outputs,
                "sources": self.sources, "warnings": self.warnings, "seconds": round(time.time() - self.started, 2)}

    def summary(self) -> str:
        lines = []
        if self.outputs:
            lines.append("Saved: " + "; ".join(self.outputs))
        if self.warnings:
            lines.append("Notes: " + " ".join(self.warnings[:6]))
        if self.failed:
            lines.append("Problems: " + " ".join(self.failed[:6]))
        return "\n".join(lines)


class History:
    """Task history and generated files (SQLite, local)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, "
                       "task TEXT, status TEXT, seconds REAL, outputs TEXT, sources TEXT, steps TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS llm_cache (key TEXT PRIMARY KEY, ts REAL, answer TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS llm_usage (ts REAL, purpose TEXT, prompt_chars INTEGER, "
                       "answer_chars INTEGER, seconds REAL, cached INTEGER, model TEXT)")

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def save(self, run: Run) -> int:
        d = run.to_dict()
        with self._db() as db:
            cur = db.execute("INSERT INTO runs (ts, kind, task, status, seconds, outputs, sources, steps) VALUES (?,?,?,?,?,?,?,?)",
                             (time.time(), d["kind"], d["task"][:500], d["status"], d["seconds"], json.dumps(d["outputs"]),
                              json.dumps(d["sources"]), json.dumps(d["steps"])))
            return int(cur.lastrowid)

    def recent(self, n: int = 50) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("outputs", "sources", "steps"):
                d[k] = json.loads(d[k] or "[]")
            out.append(d)
        return out

    def files(self, n: int = 100) -> list[dict]:
        out = []
        for r in self.recent(300):
            for p in r["outputs"]:
                out.append({"path": p, "name": Path(p).name, "task": r["task"], "ts": r["ts"], "exists": Path(p).is_file()})
        return out[:n]

    def usage(self) -> dict:
        with self._db() as db:
            r = db.execute("SELECT COUNT(*) n, COALESCE(SUM(cached),0) c, COALESCE(SUM(prompt_chars),0) p, "
                           "COALESCE(SUM(answer_chars),0) a, COALESCE(SUM(seconds),0) s FROM llm_usage").fetchone()
        return {"calls": r["n"], "cached": r["c"], "approx_prompt_tokens": r["p"] // 4, "approx_answer_tokens": r["a"] // 4,
                "seconds": round(r["s"], 1)}


_HIST: dict[str, History] = {}


def history(ctx=None) -> History:
    path = data_dir(ctx) / "business.db"
    if str(path) not in _HIST:
        _HIST[str(path)] = History(path)
    return _HIST[str(path)]


# ------------------------------------------------------------------ the AI model (through Genesis's own router)
def has_llm(ctx) -> bool:
    return _llm(ctx) is not None


def ask(ctx, prompt: str, *, system: str = "", max_tokens: int = 700, temperature: float = 0.2, purpose: str = "business",
        cache: bool = True, retries: int = 1) -> str:
    """Ask Genesis's model (the existing router picks the smallest capable local model; an external model is used only
    if the user enabled one). Answers are cached, calls are counted, and one retry is made on a transient failure."""
    llm = _llm(ctx)
    if llm is None:
        raise BizError("This step needs an AI model and none is available right now (is Ollama running?).")
    hist = history(ctx)
    key = hashlib.sha256(json.dumps([system, prompt, max_tokens, temperature]).encode()).hexdigest()
    if cache:
        with hist._db() as db:
            row = db.execute("SELECT answer FROM llm_cache WHERE key=? AND ts>?", (key, time.time() - 86400)).fetchone()
        if row:
            with hist._db() as db:
                db.execute("INSERT INTO llm_usage VALUES (?,?,?,?,?,?,?)",
                           (time.time(), purpose, len(prompt), len(row["answer"]), 0.0, 1, ""))
            return row["answer"]
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            answer = (llm.chat(msgs, max_tokens=max_tokens, temperature=temperature) or "").strip()
            if not answer:
                raise RuntimeError("empty answer")
            model = str((getattr(llm, "last_chat_route", None) or {}).get("model", "") or getattr(llm, "name", ""))
            with hist._db() as db:
                db.execute("INSERT INTO llm_usage VALUES (?,?,?,?,?,?,?)",
                           (time.time(), purpose, len(prompt), len(answer), time.time() - t0, 0, model))
                if cache:
                    db.execute("INSERT OR REPLACE INTO llm_cache VALUES (?,?,?)", (key, time.time(), answer))
            return answer
        except Exception as exc:  # retry once, then report
            last = exc
            time.sleep(0.2 * (attempt + 1))
    raise BizError(f"The AI model didn't answer ({last}).")


def json_from(text: str):
    """The first JSON object/array in a model answer, or None."""
    t = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = t.find(opener), t.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except ValueError:
                continue
    return None
