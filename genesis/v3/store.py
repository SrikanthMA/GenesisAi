"""Local store for 3.0 (SQLite file ``genesis_local.db`` in Genesis's data folder; nothing in the cloud).

Kept separate from Genesis's main database so the existing schema and migrations stay untouched.

Tables
* ``model_usage``     - one row per model call: agent, model, level, latency, tokens, success (performance log).
* ``agent_config``    - per-agent settings changed from Settings (model, temperature).
* ``session_summary`` - short summaries of past conversations (the *temporary* context is the live chat; summaries and
                        Genesis's existing facts/preferences tables are the *persistent* memory).
* ``slm_examples``    - conversation examples the user explicitly approved for training the Genesis SLM.
* ``slm_versions``    - every Genesis SLM build (Modelfile / adapter), its evaluation score and which one is active.
* ``memory_vectors``  - reserved for vector memory (embedding per memory item); created empty, filled in a later version.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS model_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, agent TEXT, model TEXT, level INTEGER, kind TEXT,
  latency_ms INTEGER, load_ms INTEGER, prompt_tokens INTEGER, eval_tokens INTEGER, ok INTEGER, error TEXT,
  external INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_usage_ts ON model_usage(ts);
CREATE TABLE IF NOT EXISTS agent_config (agent TEXT PRIMARY KEY, model TEXT, temperature REAL, updated REAL);
CREATE TABLE IF NOT EXISTS session_summary (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, session TEXT, summary TEXT NOT NULL, turns INTEGER);
CREATE TABLE IF NOT EXISTS slm_examples (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, agent TEXT, prompt TEXT NOT NULL, answer TEXT NOT NULL,
  source TEXT DEFAULT 'approved', approved INTEGER DEFAULT 1, note TEXT);
CREATE TABLE IF NOT EXISTS slm_versions (
  version INTEGER PRIMARY KEY, ts REAL NOT NULL, model_name TEXT NOT NULL, base_model TEXT NOT NULL,
  method TEXT NOT NULL, examples INTEGER DEFAULT 0, score REAL, active INTEGER DEFAULT 0, notes TEXT);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS missions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, objective TEXT, status TEXT, steps TEXT, report_path TEXT,
  updated REAL);
CREATE TABLE IF NOT EXISTS specialists (
  name TEXT PRIMARY KEY, model TEXT NOT NULL, capabilities TEXT NOT NULL, notes TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS memory_vectors (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ref_table TEXT, ref_id INTEGER, model TEXT, dim INTEGER, vector BLOB);
"""


class LocalStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    # ------------------------------------------------------------ helpers
    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    def _rows(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ------------------------------------------------------------ usage log
    def log_usage(self, *, agent: str, model: str, level: int, kind: str, latency_ms: int, load_ms: int = 0,
                  prompt_tokens: int = 0, eval_tokens: int = 0, ok: bool = True, error: str = "",
                  external: bool = False) -> None:
        self._exec("INSERT INTO model_usage(ts,agent,model,level,kind,latency_ms,load_ms,prompt_tokens,eval_tokens,ok,"
                   "error,external) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (time.time(), agent, model, level, kind, latency_ms, load_ms, prompt_tokens, eval_tokens,
                    int(ok), error[:200], int(external)))

    def usage_stats(self, since_s: float = 7 * 86400) -> list[dict]:
        """Per model: calls, failures, average latency and tokens/second over the last week."""
        return self._rows(
            "SELECT model, agent, COUNT(*) calls, SUM(1-ok) failures, CAST(AVG(latency_ms) AS INTEGER) avg_ms, "
            "ROUND(SUM(eval_tokens)*1000.0/NULLIF(SUM(CASE WHEN ok THEN latency_ms-load_ms END),0),1) tok_per_s, "
            "SUM(external) external_calls FROM model_usage WHERE ts>=? GROUP BY model, agent ORDER BY calls DESC",
            (time.time() - since_s,))

    def recent_usage(self, n: int = 20) -> list[dict]:
        return self._rows("SELECT * FROM model_usage ORDER BY id DESC LIMIT ?", (n,))

    # ------------------------------------------------------------ agent config
    def agent_overrides(self) -> dict:
        return {r["agent"]: r["model"] for r in self._rows("SELECT agent, model FROM agent_config") if r["model"]}

    def set_agent_model(self, agent: str, model: str) -> None:
        self._exec("INSERT INTO agent_config(agent,model,updated) VALUES (?,?,?) "
                   "ON CONFLICT(agent) DO UPDATE SET model=excluded.model, updated=excluded.updated",
                   (agent, model, time.time()))

    # ------------------------------------------------------------ summaries
    def add_summary(self, session: str, summary: str, turns: int) -> None:
        self._exec("INSERT INTO session_summary(ts,session,summary,turns) VALUES (?,?,?,?)",
                   (time.time(), session, summary.strip(), turns))

    def recent_summaries(self, n: int = 3) -> list[dict]:
        return self._rows("SELECT * FROM session_summary ORDER BY id DESC LIMIT ?", (n,))

    # ------------------------------------------------------------ SLM dataset
    def add_example(self, prompt: str, answer: str, *, agent: str = "", source: str = "approved",
                    note: str = "") -> int:
        if not prompt.strip() or not answer.strip():
            raise ValueError("an example needs both a prompt and an answer")
        return self._exec("INSERT INTO slm_examples(ts,agent,prompt,answer,source,note) VALUES (?,?,?,?,?,?)",
                          (time.time(), agent, prompt.strip(), answer.strip(), source, note)).lastrowid

    def examples(self, *, approved_only: bool = True) -> list[dict]:
        where = "WHERE approved=1" if approved_only else ""
        return self._rows(f"SELECT * FROM slm_examples {where} ORDER BY id")

    def delete_example(self, ex_id: int) -> bool:
        return self._exec("DELETE FROM slm_examples WHERE id=?", (ex_id,)).rowcount > 0

    def clear_examples(self) -> int:
        return self._exec("DELETE FROM slm_examples").rowcount

    # ------------------------------------------------------------ SLM versions
    def add_version(self, *, model_name: str, base_model: str, method: str, examples: int,
                    score: Optional[float] = None, notes: str = "") -> int:
        row = self._rows("SELECT COALESCE(MAX(version),0)+1 v FROM slm_versions")[0]
        v = int(row["v"])
        self._exec("INSERT INTO slm_versions(version,ts,model_name,base_model,method,examples,score,notes) "
                   "VALUES (?,?,?,?,?,?,?,?)", (v, time.time(), model_name, base_model, method, examples, score, notes))
        return v

    def versions(self) -> list[dict]:
        return self._rows("SELECT * FROM slm_versions ORDER BY version")

    def set_score(self, version: int, score: float) -> None:
        self._exec("UPDATE slm_versions SET score=? WHERE version=?", (score, version))

    def activate(self, version: int) -> Optional[dict]:
        rows = self._rows("SELECT * FROM slm_versions WHERE version=?", (version,))
        if not rows:
            return None
        with self._lock:
            self.conn.execute("UPDATE slm_versions SET active=0")
            self.conn.execute("UPDATE slm_versions SET active=1 WHERE version=?", (version,))
            self.conn.commit()
        return rows[0]

    def active_version(self) -> Optional[dict]:
        rows = self._rows("SELECT * FROM slm_versions WHERE active=1")
        return rows[0] if rows else None

    # ------------------------------------------------------------ small settings (mode, …)
    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        rows = self._rows("SELECT value FROM kv WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set(self, key: str, value: str) -> None:
        self._exec("INSERT INTO kv(key,value,updated) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                   "updated=excluded.updated", (key, value, time.time()))

    # ------------------------------------------------------------ missions
    def mission_start(self, objective: str) -> int:
        return self._exec("INSERT INTO missions(ts,objective,status,steps,updated) VALUES (?,?,?,?,?)",
                          (time.time(), objective, "running", "[]", time.time())).lastrowid

    def mission_update(self, mid: int, *, status: Optional[str] = None, steps: Optional[list] = None,
                       report_path: Optional[str] = None) -> None:
        sets, vals = ["updated=?"], [time.time()]
        if status is not None:
            sets.append("status=?")
            vals.append(status)
        if steps is not None:
            sets.append("steps=?")
            vals.append(json.dumps(steps))
        if report_path is not None:
            sets.append("report_path=?")
            vals.append(report_path)
        self._exec(f"UPDATE missions SET {', '.join(sets)} WHERE id=?", (*vals, mid))

    def missions(self, n: int = 10) -> list[dict]:
        rows = self._rows("SELECT * FROM missions ORDER BY id DESC LIMIT ?", (n,))
        for r in rows:
            r["steps"] = json.loads(r["steps"] or "[]")
        return rows

    # ------------------------------------------------------------ specialist models (e.g. TwinSim SLMs)
    def add_specialist(self, name: str, model: str, capabilities: list[str], notes: str = "") -> None:
        self._exec("INSERT INTO specialists(name,model,capabilities,notes,ts) VALUES (?,?,?,?,?) ON CONFLICT(name) DO UPDATE "
                   "SET model=excluded.model, capabilities=excluded.capabilities, notes=excluded.notes",
                   (name.lower(), model, json.dumps([c.lower() for c in capabilities]), notes, time.time()))

    def specialists(self) -> list[dict]:
        rows = self._rows("SELECT * FROM specialists ORDER BY name")
        for r in rows:
            r["capabilities"] = json.loads(r["capabilities"] or "[]")
        return rows

    def remove_specialist(self, name: str) -> bool:
        return self._exec("DELETE FROM specialists WHERE name=?", (name.lower(),)).rowcount > 0

    # ------------------------------------------------------------ privacy
    def export(self) -> dict:
        return {t: self._rows(f"SELECT * FROM {t}") for t in
                ("model_usage", "agent_config", "session_summary", "slm_examples", "slm_versions", "missions", "specialists")}

    def export_json(self, path: Path | str) -> Path:
        p = Path(path)
        p.write_text(json.dumps(self.export(), indent=2, ensure_ascii=False), encoding="utf-8")
        return p

    def wipe(self, *, keep_versions: bool = True) -> None:
        """Delete local AI data (usage log, summaries, approved examples; versions too unless kept)."""
        tables = ["model_usage", "session_summary", "slm_examples", "memory_vectors"]
        if not keep_versions:
            tables.append("slm_versions")
        with self._lock:
            for t in tables:
                self.conn.execute(f"DELETE FROM {t}")
            self.conn.commit()
