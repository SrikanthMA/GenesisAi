"""2.0 execution policy — called by the executor and planner through small ``v2 hook`` lines.

* **Approval binding.** An approval is bound to the exact tool + arguments it was given for (a fingerprint). If the
  arguments differ when the step actually runs, the approval no longer applies and Genesis asks again. Steps covered by a
  group approval are bound to the argument *templates* the user saw.
* **Duplicate guard.** Actions that must not happen twice by accident (sending, submitting, creating remotely) are
  remembered for 10 minutes; doing exactly the same one again always asks, and says it was already done.
* **Retry rule.** Automatic retries only for LOW-risk tools that are not in the never-repeat list — whatever the tool
  reports. An uncertain outcome is reported, not retried.
* **LLM plan checks.** Every ``{{steps.N…}}`` reference must point to an *earlier* step; at most
  ``MAX_CONSEQUENTIAL_LLM_STEPS`` consequential steps per model-written plan (a dangerous job can't be smuggled through as
  many small steps); before the first consequential step the user sees the whole plan.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from typing import Any, Optional

from ..security.permissions import RiskLevel

# never repeated automatically, and a repeat within DUPLICATE_WINDOW_S always asks first
NEVER_REPEAT = {"gmail_send_draft", "whatsapp_send", "form_submit", "github_create_repo", "github_delete_repo",
                "create_calendar_event", "delete_calendar_event", "update_calendar_event", "shutdown_computer",
                "restart_computer", "sleep_computer", "delete_path", "run_program", "run_project_command", "apply_patch",
                "apply_code_change", "write_code_files", "office_create", "move_path", "rename_path", "copy_path",
                "create_file", "colab_run"}
DUPLICATE_GUARDED = {"gmail_send_draft", "whatsapp_send", "form_submit", "github_create_repo", "create_calendar_event"}
DUPLICATE_WINDOW_S = 600
MAX_CONSEQUENTIAL_LLM_STEPS = 4
TEMPLATE = re.compile(r"\{\{\s*steps\.(\d+)\.")


def _canon(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _canon(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    if isinstance(value, str):
        return " ".join(value.split())
    return value


def fingerprint(tool: str, args: Optional[dict]) -> str:
    raw = json.dumps({"tool": tool, "args": _canon(args or {})}, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


# ----------------------------------------------------------------------------------------------- approval binding
def bind_approval(step, tool: str, args: dict, group_steps: list) -> None:
    """Called when a confirmation is created: remember exactly what the user is being asked to approve."""
    step.v2_approval = fingerprint(tool, args)
    for s in group_steps:
        s.v2_group_approval = fingerprint(s.tool, s.args)  # the template the user saw (values come from earlier steps)


def approval_matches(step, tool: str, args: dict) -> bool:
    fp = getattr(step, "v2_approval", None)
    return fp is None or fp == fingerprint(tool, args)


def group_approval_matches(step) -> bool:
    fp = getattr(step, "v2_group_approval", None)
    return fp is None or fp == fingerprint(step.tool, step.args)


# ----------------------------------------------------------------------------------------------- duplicates
class _Ledger:
    """fingerprint → (time, outcome) with outcome "done" | "attempted" (started, result unknown) | "failed" (known no)."""

    def __init__(self) -> None:
        self._done: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _guarded(tool: str, args: dict) -> bool:
        return tool in DUPLICATE_GUARDED and bool(args)  # without arguments two actions can't be told apart

    def mark(self, tool: str, args: dict, outcome: str, clock=time.time) -> None:
        if self._guarded(tool, args):
            with self._lock:
                self._done[fingerprint(tool, args)] = (clock(), outcome)

    def record(self, tool: str, args: dict, clock=time.time) -> None:  # backwards-compatible: a success
        self.mark(tool, args, "done", clock)

    def lookup(self, tool: str, args: dict, clock=time.time) -> Optional[tuple[float, str]]:
        if not self._guarded(tool, args):
            return None
        with self._lock:
            entry = self._done.get(fingerprint(tool, args))
        if entry is None or clock() - entry[0] > DUPLICATE_WINDOW_S or entry[1] == "failed":
            return None
        return clock() - entry[0], entry[1]

    def recent(self, tool: str, args: dict, clock=time.time) -> Optional[float]:
        hit = self.lookup(tool, args, clock)
        return hit[0] if hit else None

    def clear(self) -> None:
        with self._lock:
            self._done.clear()


def ledger(svc) -> _Ledger:
    """One ledger per running assistant (kept on the service container)."""
    led = svc.extras.get("v2_ledger")
    if led is None:
        led = svc.extras["v2_ledger"] = _Ledger()
    return led


def duplicate_note(svc, tool: str, args: dict) -> str:
    hit = ledger(svc).lookup(tool, args)
    if hit is None:
        return ""
    ago, outcome = hit
    when = "just now" if ago < 60 else f"{int(ago // 60)} minute{'s' if ago >= 120 else ''} ago"
    if outcome == "attempted":
        return f"I already tried exactly this {when} and couldn't confirm whether it went through — please check first. Try again?"
    return f"I already did exactly this {when}. Do it again?"


# ----------------------------------------------------------------------------------------------- retries
def may_retry(tool: str, risk: RiskLevel) -> bool:
    return risk == RiskLevel.LOW and tool not in NEVER_REPEAT


# ----------------------------------------------------------------------------------------------- LLM plans
def check_llm_plan(plan, registry) -> Optional[str]:
    """Return a reason to reject a model-written plan, or None."""
    consequential = 0
    for idx, step in enumerate(plan.steps, 1):
        for ref in TEMPLATE.findall(json.dumps(step.args, default=str)):
            if not 1 <= int(ref) < idx:
                return f"step {idx} uses the result of step {ref}, which doesn't run before it"
        tool = registry.get(step.tool)
        if tool is not None and (tool.risk >= RiskLevel.MEDIUM or tool.always_confirm or tool.remote
                                 or step.tool in NEVER_REPEAT):
            consequential += 1
    if consequential > MAX_CONSEQUENTIAL_LLM_STEPS:
        return f"{consequential} steps change things — more than {MAX_CONSEQUENTIAL_LLM_STEPS} in one go"
    return None


def plan_preview(plan, index: int) -> str:
    """Before the first step that needs approval in a model-written plan, show the whole plan once."""
    if getattr(plan, "source", "") != "llm" or len(plan.steps) < 2 or getattr(plan, "v2_previewed", False):
        return ""
    plan.v2_previewed = True
    lines = [f"{n}. {s.description or s.tool}" + ("  ← now" if n - 1 == index else "") for n, s in enumerate(plan.steps, 1)]
    return "Here's my plan:\n" + "\n".join(lines) + "\n"


def provenance_note(plan, step) -> str:
    """When the plan uses a file picked from several search matches, say which one (so the user can stop a wrong pick)."""
    # only the results this step actually depends on (directly or through earlier steps)
    used: set[int] = set()
    todo = [step]
    while todo:
        cur = todo.pop()
        for r in TEMPLATE.findall(json.dumps(cur.args, default=str)):
            k = int(r)
            if k not in used and 0 < k <= len(plan.steps):
                used.add(k)
                todo.append(plan.steps[k - 1])
    for k in sorted(used):
        src = plan.steps[k - 1] if 0 < k <= len(plan.steps) else None
        if src is None or src is step or src.result is None or not src.result.success:
            continue
        data = src.result.data or {}
        results = data.get("results")
        if isinstance(results, list) and len(results) > 1 and data.get("path"):
            from pathlib import Path

            return f" (I used “{Path(str(data['path'])).name}” — the best of {len(results)} matches; say no if it's the wrong one.)"
    return ""
