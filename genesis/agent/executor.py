"""Executor: runs a plan step by step through the permission layer.

For every step:
    resolve {{templates}} → validate schema → assess risk → permission check
    → (pause for confirmation if required) → run tool → verify → audit log
On failure it attempts a *safe* recovery (only LOW-risk recovery tools the
failing tool suggested), then stops the plan — it never pretends a failed
step succeeded.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from ..core.events import AssistantState, EventType
from ..core.logging_setup import get_logger
from ..security.confirmation import PendingConfirmation
from ..security.permissions import RiskLevel
from ..security.redaction import redact
from ..security.validators import ValidationError
from ..tools.base import ToolResult
from ..tools.registry import ToolRegistry
from .plan import Plan, PlanStep, StepStatus
from .planner import TEMPLATE_RE
from ..v2 import cancel as v2cancel, errors as v2errors, policy as v2policy  # v2 hooks (see genesis/v2/__init__.py)

if TYPE_CHECKING:  # pragma: no cover
    from ..core.container import ServiceContainer

log = get_logger("tools")


class UnresolvedTemplate(Exception):
    pass


@dataclass
class ExecOutcome:
    status: str  # completed | failed | awaiting_confirmation | blocked
    plan: Plan
    confirmation: Optional[PendingConfirmation] = None


def resolve_templates(value: Any, plan: Plan) -> Any:
    if isinstance(value, dict):
        return {k: resolve_templates(v, plan) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_templates(v, plan) for v in value]
    if not isinstance(value, str) or "{{" not in value:
        return value

    def lookup(m) -> Any:
        idx = int(m.group(1))
        ref = m.group(2)
        if idx < 1 or idx > len(plan.steps):
            raise UnresolvedTemplate(f"step {idx} does not exist")
        step = plan.steps[idx - 1]
        if step.status != StepStatus.SUCCEEDED or step.result is None:
            raise UnresolvedTemplate(f"step {idx} did not succeed")
        if ref == "message":
            return step.result.message
        key = ref.split(".", 1)[1]
        if key not in step.result.data:
            raise UnresolvedTemplate(f"step {idx} has no '{key}'")
        return step.result.data[key]

    full = TEMPLATE_RE.fullmatch(value.strip())
    if full:
        return lookup(full)  # keep native type (lists, ints)
    return TEMPLATE_RE.sub(lambda m: str(lookup(m)), value)


class Executor:
    def __init__(self, registry: ToolRegistry, services: "ServiceContainer") -> None:
        self.registry = registry
        self.svc = services

    def _emit_step(self, plan: Plan, step: PlanStep) -> None:
        self.svc.events.emit(EventType.STEP_UPDATED, plan_id=plan.id, step=step.to_dict())

    def run(self, plan: Plan, *, start: int = 0, confirmed_index: Optional[int] = None, task_id: Optional[str] = None) -> ExecOutcome:
        cap = int(self.svc.settings.get("agent.max_steps", 12) or 12)
        if len([s for s in plan.steps if not s.is_recovery]) > cap:  # runaway-plan guard
            for s in plan.steps[cap:]:
                if s.status == StepStatus.PENDING:
                    s.status = StepStatus.SKIPPED
            log.warning("plan truncated to %d steps", cap)
        if confirmed_index is not None and plan.steps[confirmed_index].confirm_group is not None:
            plan.confirmed_groups.add(plan.steps[confirmed_index].confirm_group)
        i = start
        while i < len(plan.steps):
            step = plan.steps[i]
            if step.status in (StepStatus.SUCCEEDED, StepStatus.SKIPPED, StepStatus.CANCELLED, StepStatus.FAILED):
                i += 1
                continue
            if v2cancel.requested(self.svc):  # v2 hook: the user cancelled — finish nothing new, keep what's done
                for later in plan.steps[i:]:
                    if later.status in (StepStatus.PENDING, StepStatus.AWAITING_CONFIRMATION):
                        later.status = StepStatus.CANCELLED
                        self._emit_step(plan, later)
                log.info("task cancelled by the user before step %d", i + 1)
                return ExecOutcome("cancelled", plan)
            outcome = self._run_step(plan, i, confirmed=(confirmed_index == i), task_id=task_id)
            if outcome is not None:
                return outcome
            fu = step.result.followup if step.status == StepStatus.SUCCEEDED and step.result else None
            if fu and self.registry.get(fu.get("tool", "")) is not None and fu.get("tool") != step.tool \
                    and sum(1 for s in plan.steps if s.is_followup) < 3:
                nxt = plan.insert_after(i, fu["tool"], dict(fu.get("args") or {}), fu.get("description") or fu["tool"],
                                        optional=bool(fu.get("optional")))
                nxt.is_followup = True
                self.svc.events.emit(EventType.PLAN_CREATED, plan=plan.to_dict())
            if step.status == StepStatus.FAILED:
                if step.result and step.result.recovery:
                    self._recover(plan, step, task_id)
                    rec = plan.steps[-1]
                    if step.result.recovery.get("complete") and rec.is_recovery and rec.status == StepStatus.SUCCEEDED:
                        step.optional = True  # the alternative method fully achieved this step → carry on with the plan
                        self._emit_step(plan, step)  # v2 hook: the task journal must see it's no longer a failure
                if not step.optional:
                    for later in plan.steps[i + 1:]:
                        if later.status == StepStatus.PENDING and not later.is_recovery:
                            later.status = StepStatus.SKIPPED
                            self._emit_step(plan, later)
                    return ExecOutcome("failed", plan)
            i += 1
        return ExecOutcome("completed" if plan.succeeded or all(
            s.status in (StepStatus.SUCCEEDED, StepStatus.SKIPPED) or s.optional for s in plan.steps) else "failed", plan)

    # ----------------------------------------------------------------- step
    def _run_step(self, plan: Plan, index: int, *, confirmed: bool, task_id: Optional[str]) -> Optional[ExecOutcome]:
        step = plan.steps[index]
        tool = self.registry.get(step.tool)
        if tool is None:
            step.status = StepStatus.FAILED
            step.result = ToolResult.fail(f"Unknown tool '{step.tool}'.")
            self._emit_step(plan, step)
            return None
        try:
            args = resolve_templates(step.args, plan)
        except UnresolvedTemplate as exc:
            step.status = StepStatus.SKIPPED if step.optional else StepStatus.FAILED
            step.result = ToolResult.fail(f"Skipped “{step.description}” because an earlier step didn't succeed.", error=str(exc))
            self._emit_step(plan, step)
            return None
        try:
            args = tool.validate(args)
        except ValidationError as exc:
            step.status = StepStatus.FAILED
            step.result = ToolResult.fail(f"I couldn't {step.description.lower() or step.tool}: {exc}")
            self._audit(task_id, step, args, RiskLevel.LOW, "invalid", False, step.result, 0)
            self._emit_step(plan, step)
            return None
        step.resolved_args = args
        if confirmed and not v2policy.approval_matches(step, tool.name, args):  # v2 hook: approval bound to exact args
            log.warning("arguments changed after approval — asking again", extra={"data": {"tool": tool.name}})
            confirmed = False
        risk = tool.risk_for(args, self.svc)
        step.risk = risk.label
        exempt = step.trusted_input and plan.source == "rules" and risk == RiskLevel.LOW and not step.is_recovery
        decision = self.svc.permissions.check(tool.name, risk, always_confirm=tool.always_confirm,
                                              untrusted_context=plan.untrusted and not exempt)
        if not decision.allowed:
            step.status = StepStatus.BLOCKED
            step.result = ToolResult.fail(decision.reason)
            self._audit(task_id, step, args, risk, "blocked", False, step.result, 0)
            self._emit_step(plan, step)
            return ExecOutcome("blocked", plan)
        group_ok = (step.confirm_group is not None and step.confirm_group in plan.confirmed_groups and risk < RiskLevel.HIGH
                    and not plan.untrusted and v2policy.group_approval_matches(step))  # v2 hook: bound group approval
        dup = "" if confirmed else v2policy.duplicate_note(self.svc, tool.name, args)  # v2 hook: never repeat by accident
        if ((decision.needs_confirmation and not group_ok) or dup) and not confirmed:
            step.status = StepStatus.AWAITING_CONFIRMATION
            prompt = tool.confirmation_prompt(args)
            group = [s for s in plan.steps if step.confirm_group is not None and s.confirm_group == step.confirm_group and s is not step
                     and s.status == StepStatus.PENDING]
            if group:
                prompt = prompt.rstrip("?") + ", and then " + ", ".join(s.description[:1].lower() + s.description[1:] for s in group) + "?"
            if plan.untrusted and risk < RiskLevel.MEDIUM:
                prompt += " (I'm asking because this was planned while reading external content.)"
            prompt = (v2policy.plan_preview(plan, index) + (dup + " " if dup else "") + prompt  # v2 hook
                      + v2policy.provenance_note(plan, step))
            pc = self.svc.confirmations.create(prompt=prompt, tool=tool.name, risk=risk.label, args_preview=redact(args),
                                               payload={"plan": plan, "index": index, "task_id": task_id})
            v2policy.bind_approval(step, tool.name, args, group)  # v2 hook
            self._emit_step(plan, step)
            self.svc.events.emit(EventType.STATE_CHANGED, state=AssistantState.WAITING_FOR_CONFIRMATION.value)
            return ExecOutcome("awaiting_confirmation", plan, pc)

        step.confirmed = confirmed or group_ok
        step.status = StepStatus.RUNNING
        self._emit_step(plan, step)
        self.svc.events.emit(EventType.STATE_CHANGED, state=AssistantState.EXECUTING.value, detail=step.description)
        t0 = time.perf_counter()
        max_retries = int(self.svc.settings.get("agent.max_retries", 1) or 0)
        crashed = False
        v2policy.ledger(self.svc).mark(tool.name, args, "attempted")  # v2 hook: a crash leaves "may have happened"
        while True:
            step.attempts += 1
            try:
                result = tool.run(args, self.svc)
            except Exception as exc:  # a tool bug must never crash Genesis or fake success
                log.exception("tool %s crashed", tool.name)
                crashed = True
                result = ToolResult.fail(f"Something went wrong while trying to {step.description.lower() or tool.name}.",
                                         error=f"{type(exc).__name__}: {exc}")
            if result.success or not result.retryable or step.attempts > max_retries \
                    or not v2policy.may_retry(tool.name, risk):  # v2 hook: only safe, repeatable steps are retried
                break
            log.info("retrying transient failure", extra={"data": {"tool": tool.name, "attempt": step.attempts,
                                                                   "error": (result.error or "")[:200]}})
            self.svc.events.emit(EventType.STATE_CHANGED, state=AssistantState.RETRYING.value,
                                 detail="That didn't work — trying again…")
            time.sleep(0.8)
        verified: Optional[bool] = None
        if result.success:
            self.svc.events.emit(EventType.STATE_CHANGED, state=AssistantState.VERIFYING.value, detail=step.description)
            try:
                verified = tool.verify(args, result, self.svc)
            except Exception:
                verified = None
            log.info("verification", extra={"data": {"tool": tool.name, "verified": verified}})
            if verified is False:
                result = ToolResult.fail(f"{result.message.rstrip('.')} — but I couldn't verify that it actually worked, "
                                         "so please check.", error="verification failed", **result.data)
        result.verified = verified
        if result.success:  # v2 hooks: remember what was done (duplicate guard); categorise failures
            v2policy.ledger(self.svc).record(tool.name, args)
        else:
            v2errors.annotate(result, crashed=crashed, risk=risk)
            if not crashed and result.data.get("error_category") != "unknown_outcome":
                v2policy.ledger(self.svc).mark(tool.name, args, "failed")  # a clear failure may be retried freely
        duration = int((time.perf_counter() - t0) * 1000)
        step.result = result
        step.verified = verified
        step.status = StepStatus.SUCCEEDED if result.success else StepStatus.FAILED
        if result.untrusted_content:
            plan.untrusted = True
        self._audit(task_id, step, args, risk, step.status.value, confirmed, result, duration)
        self._emit_step(plan, step)
        return None

    def _recover(self, plan: Plan, failed: PlanStep, task_id: Optional[str]) -> None:
        rec = failed.result.recovery if failed.result else None
        if not rec:
            return
        tool = self.registry.get(rec.get("tool", ""))
        if tool is None or tool.risk > RiskLevel.LOW or tool.always_confirm:
            return  # only automatic, low-risk recoveries
        step = plan.add(tool.name, rec.get("args", {}), f"Fallback: {tool.name.replace('_', ' ')}", is_recovery=True,
                        recovery_reason=rec.get("reason", ""))
        self._run_step(plan, len(plan.steps) - 1, confirmed=False, task_id=task_id)
        if step.status == StepStatus.AWAITING_CONFIRMATION:
            # never leave a recovery waiting silently; drop it
            step.status = StepStatus.SKIPPED
            self.svc.confirmations.cancel_all()

    def _audit(self, task_id, step, args, risk, status, confirmed, result: ToolResult, duration_ms: int) -> None:
        verbose = bool(self.svc.settings.get("debug.log_content", False))
        record = {"task_id": task_id, "tool": step.tool, "status": status, "risk": risk.label if hasattr(risk, "label") else risk,
                  "confirmed": confirmed, "duration_ms": duration_ms,
                  "args": redact(args) if verbose else _content_free(args),
                  "message": result.message[:300] if verbose else f"<{len(result.message or '')} chars>",
                  "error": redact(result.error), "verified": result.verified}
        log.info("tool_execution", extra={"data": record})
        try:
            self.svc.repos.tool_executions.record(task_id=task_id, tool=step.tool, args=args or {}, risk=record["risk"], status=status,
                                                  confirmed=confirmed, message=result.message, error=result.error,
                                                  duration_ms=duration_ms)
        except Exception:
            log.exception("could not write audit record")


# argument names whose values are conversation content (message bodies, notes, search text …)
_CONTENT_KEYS = {"text", "body", "message", "content", "new_content", "query", "addition", "find", "replace", "subject",
                 "new_subject", "question", "summary", "issue", "note", "prompt", "error_text", "issues", "commands"}


def _content_free(args) -> dict:
    """Structured-log view of tool arguments: keeps names/paths/ids for debugging, replaces content with its length."""
    out = {}
    for k, v in (args or {}).items():
        if k in _CONTENT_KEYS and isinstance(v, str):
            out[k] = f"<{len(v)} chars>"
        elif isinstance(v, (list, tuple)) and k in _CONTENT_KEYS:
            out[k] = f"<{len(v)} items>"
        else:
            out[k] = redact(v)
    return out
