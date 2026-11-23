"""Persistent task journal: typed task states, validated transitions, checkpoints and resume after a restart.

It listens to the events the executor already emits (PLAN_CREATED / STEP_UPDATED), so the 1.x execution code does not
change. State lives in its own table ``v2_task_state`` (created if missing — no migration of existing data). The existing
``tasks`` table keeps the request and the original plan exactly as before.

Resuming never re-uses an approval: the remaining steps are planned again and every confirmation is asked again.
"""
from __future__ import annotations

import json
import re
import threading
import time
from enum import Enum
from typing import Any, Optional

from ..core.events import EventType
from ..core.logging_setup import get_logger
from ..security.redaction import MASK

log = get_logger("v2.tasks")


class TaskState(str, Enum):
    PENDING = "pending"
    READY = "ready"
    AWAITING_APPROVAL = "awaiting_approval"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


S = TaskState
TRANSITIONS: dict[TaskState, set[TaskState]] = {
    S.PENDING: {S.READY, S.RUNNING, S.AWAITING_APPROVAL, S.SUCCEEDED, S.FAILED, S.CANCELLED, S.BLOCKED},
    S.READY: {S.RUNNING, S.AWAITING_APPROVAL, S.CANCELLED, S.BLOCKED},
    S.AWAITING_APPROVAL: {S.AWAITING_APPROVAL, S.RUNNING, S.SUCCEEDED, S.FAILED, S.CANCELLED, S.BLOCKED},
    S.RUNNING: {S.RUNNING, S.AWAITING_APPROVAL, S.SUCCEEDED, S.FAILED, S.CANCELLED, S.BLOCKED},
    S.SUCCEEDED: {S.SUCCEEDED, S.RUNNING, S.AWAITING_APPROVAL},  # a result can add a follow-up step (ToolResult.followup)
    S.FAILED: {S.FAILED, S.RUNNING},  # re-plan / retry
    S.CANCELLED: {S.CANCELLED},
    S.BLOCKED: {S.BLOCKED},
}
UNFINISHED = {S.FAILED, S.CANCELLED, S.AWAITING_APPROVAL, S.RUNNING, S.BLOCKED, S.PENDING}
DONE_STEP = {"succeeded"}
MAX_STORED_CHARS = 4000
STALE_S = 120  # a task still "running" after this long without progress was interrupted (crash, restart, cancel)

SCHEMA = """
CREATE TABLE IF NOT EXISTS v2_task_state (
    task_id TEXT PRIMARY KEY,
    request TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    steps TEXT NOT NULL DEFAULT '[]',
    checkpoint INTEGER NOT NULL DEFAULT 0,
    failure TEXT,
    resumed_by TEXT,
    untrusted INTEGER NOT NULL DEFAULT 0,
    violations INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
)
"""


def derive_state(steps: list[dict]) -> TaskState:
    core = [s for s in steps if not s.get("is_recovery")]
    st = [s.get("status") for s in core]
    if not st:
        return S.PENDING
    if "awaiting_confirmation" in st:
        return S.AWAITING_APPROVAL
    if "running" in st:
        return S.RUNNING
    if "blocked" in st:
        return S.BLOCKED
    if all(x == "pending" for x in st):
        return S.PENDING
    if "pending" in st:
        return S.RUNNING  # some done, some still to do
    # nothing left to run: the outcome is decided by the steps that mattered (optional ones never make a task unfinished)
    if any(s.get("status") == "cancelled" and not s.get("optional") for s in core):
        return S.CANCELLED
    if any(s.get("status") == "failed" and not s.get("optional") for s in core):
        return S.FAILED
    return S.SUCCEEDED


TRUNC = "…[truncated]"


def _shrink(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= MAX_STORED_CHARS else value[:MAX_STORED_CHARS] + TRUNC
    if isinstance(value, dict):
        return {k: _shrink(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_shrink(v) for v in value[:200]] + ([TRUNC] if len(value) > 200 else [])
    return value


class TaskJournal:
    def __init__(self, db, events, clock=time.time) -> None:
        self.db = db
        self.clock = clock
        self._lock = threading.Lock()
        db.execute(SCHEMA)
        self._unsub = [events.subscribe(self._on_plan, EventType.PLAN_CREATED),
                       events.subscribe(self._on_step, EventType.STEP_UPDATED)]

    def close(self) -> None:
        for u in self._unsub:
            u()

    # ------------------------------------------------------------------ event handlers
    def _on_plan(self, event) -> None:
        plan = event.payload.get("plan") or {}
        if not plan.get("id"):
            return
        steps = [_shrink(s) for s in plan.get("steps", [])]
        now = self.clock()
        with self._lock:
            row = self.db.query_one("SELECT state FROM v2_task_state WHERE task_id=?", (plan["id"],))
            if row is None:
                self.db.execute("INSERT INTO v2_task_state(task_id,request,source,state,steps,untrusted,created_at,updated_at) "
                                "VALUES(?,?,?,?,?,?,?,?)", (plan["id"], str(plan.get("request", ""))[:500],
                                                           plan.get("source", ""), derive_state(steps).value,
                                                           json.dumps(steps, default=str), int(bool(plan.get("untrusted"))),
                                                           now, now))
            else:  # the plan grew (follow-up / recovery step inserted)
                if plan.get("untrusted"):
                    self.db.execute("UPDATE v2_task_state SET untrusted=1 WHERE task_id=?", (plan["id"],))
                self._store(plan["id"], steps, TaskState(row["state"]))

    def _on_step(self, event) -> None:
        pid, step = event.payload.get("plan_id"), event.payload.get("step") or {}
        if not pid:
            return
        with self._lock:
            row = self.db.query_one("SELECT state, steps FROM v2_task_state WHERE task_id=?", (pid,))
            if row is None:
                return
            steps = json.loads(row["steps"] or "[]")
            for i, s in enumerate(steps):
                if s.get("id") == step.get("id") and s.get("tool") == step.get("tool"):
                    steps[i] = _shrink(step)
                    break
            else:
                steps.append(_shrink(step))
            self._store(pid, steps, TaskState(row["state"]))

    def _store(self, pid: str, steps: list[dict], old: TaskState) -> None:
        new = derive_state(steps)
        bad = new not in TRANSITIONS.get(old, set())
        if bad:
            log.warning("task %s: unexpected state change %s → %s", pid, old.value, new.value)
        done = [i for i, s in enumerate(steps) if s.get("status") in DONE_STEP]
        checkpoint = (max(done) + 1) if done else 0
        failure = next((((s.get("result") or {}).get("message") or "")[:300] for s in steps
                        if s.get("status") in ("failed", "blocked")), None)
        self.db.execute("UPDATE v2_task_state SET state=?, steps=?, checkpoint=?, failure=?, violations=violations+?, "
                        "updated_at=? WHERE task_id=?",
                        (new.value, json.dumps(steps, default=str), checkpoint, failure, 1 if bad else 0, self.clock(), pid))

    # ------------------------------------------------------------------ queries
    def get(self, task_id: str) -> Optional[dict]:
        row = self.db.query_one("SELECT * FROM v2_task_state WHERE task_id=?", (task_id,))
        if row:
            row["steps"] = json.loads(row["steps"] or "[]")
        return row

    def unfinished(self, *, within_s: float = 7 * 24 * 3600, limit: int = 5) -> list[dict]:
        rows = self.db.query("SELECT * FROM v2_task_state WHERE resumed_by IS NULL AND updated_at >= ? "
                             "ORDER BY updated_at DESC LIMIT 20", (self.clock() - within_s,))
        out = []
        for r in rows:
            live = r["state"] == "running" and self.clock() - r["updated_at"] < STALE_S  # still in progress right now
            if TaskState(r["state"]) in UNFINISHED and r["state"] != "pending" and not live:
                r["steps"] = json.loads(r["steps"] or "[]")
                out.append(r)
        return out[:limit]

    def clear(self) -> int:
        n = self.db.query_one("SELECT COUNT(*) AS n FROM v2_task_state")["n"]
        self.db.execute("DELETE FROM v2_task_state")
        return int(n)

    # ------------------------------------------------------------------ resume
    def resume_slots(self) -> dict:
        """The remaining steps of the latest unfinished task, with references to earlier results filled in. Read-only:
        call ``mark_resumed`` once a plan was actually built. Approvals are never carried over, and the resumed plan keeps
        the original's protection (model-written source, untrusted content seen)."""
        tasks = self.unfinished(limit=1)
        if not tasks:
            return {"v2_reply": "There's no unfinished task to continue."}
        task = tasks[0]
        steps = task["steps"]
        done = {i + 1: s for i, s in enumerate(steps) if s.get("status") in DONE_STEP}
        remaining = []
        for s in steps:
            if s.get("status") in DONE_STEP or s.get("is_recovery"):
                continue
            try:
                args = _fill(s.get("args") or {}, done)
            except LookupError:
                return {"v2_reply": f"I can't safely continue “{task['request'][:80]}” — part of it depended on a result I no "
                                    "longer have. Please ask me again."}
            if _redacted(args) or TRUNC in json.dumps(args, default=str, ensure_ascii=False):
                return {"v2_reply": f"I can't continue “{task['request'][:80]}” automatically because part of it was hidden "
                                    "for privacy or too long to keep. Please ask me again."}
            remaining.append({"tool": s["tool"], "args": args, "description": s.get("description") or s["tool"]})
        if not remaining:
            return {"v2_reply": "That task already finished."}
        untrusted = any((s.get("result") or {}).get("untrusted_content") for s in steps) or bool(task.get("untrusted"))
        return {"steps": remaining, "description": f"Continue “{task['request'][:80]}”", "v2_resumed": task["task_id"],
                "v2_source": task.get("source") or "rules", "v2_untrusted": untrusted}

    def mark_resumed(self, task_id: str, by: str = "resume") -> None:
        self.db.execute("UPDATE v2_task_state SET resumed_by=? WHERE task_id=?", (by, task_id))

    def describe_unfinished(self) -> str:
        tasks = self.unfinished()
        if not tasks:
            return "You have no unfinished tasks."
        lines = []
        for t in tasks:
            todo = sum(1 for s in t["steps"] if s.get("status") not in DONE_STEP and not s.get("is_recovery"))
            why = f" — {t['failure']}" if t.get("failure") else ""
            lines.append(f"• “{t['request'][:70]}”: {t['state'].replace('_', ' ')}, {todo} step(s) left{why}")
        return "Unfinished tasks:\n" + "\n".join(lines) + "\nSay “continue where you stopped” to pick up the latest one."


_REF = re.compile(r"\{\{\s*steps\.(\d+)\.(data\.([\w]+)|message)\s*\}\}")


def _fill(value: Any, done: dict[int, dict]) -> Any:
    if isinstance(value, dict):
        return {k: _fill(v, done) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, done) for v in value]
    if not isinstance(value, str) or "{{" not in value:
        return value

    def look(m) -> Any:
        step = done.get(int(m.group(1)))
        if step is None:
            raise LookupError(m.group(0))
        res = step.get("result") or {}
        val = res.get("message") if m.group(2) == "message" else (res.get("data") or {}).get(m.group(3))
        if val is None or TRUNC in json.dumps(val, default=str, ensure_ascii=False):
            raise LookupError(m.group(0))
        return val

    full = _REF.fullmatch(value.strip())
    if full:
        return look(full)
    return _REF.sub(lambda m: str(look(m)), value)


def _redacted(args: Any) -> bool:
    """Stored step arguments are redacted (secrets masked) — such a step can't be replayed."""
    return MASK in json.dumps(args, default=str)


JOURNAL: Optional[TaskJournal] = None  # the running assistant's journal (set by GenesisCore)


def attach(svc) -> TaskJournal:
    global JOURNAL
    j = svc.extras.get("v2_journal")
    if j is None:
        j = svc.extras["v2_journal"] = TaskJournal(svc.db, svc.events)
    JOURNAL = j
    return j
