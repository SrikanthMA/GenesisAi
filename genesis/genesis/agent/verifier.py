"""Verifier / response composer.

Builds the final answer strictly from step results, so the words Genesis
speaks can never claim more than the tools actually achieved.
"""
from __future__ import annotations

from dataclasses import dataclass

from .plan import Plan, StepStatus


@dataclass
class Report:
    text: str
    success: bool
    partial: bool


def summarize(plan: Plan) -> Report:
    lines: list[str] = []
    any_fail = False
    any_ok = False
    for step in plan.steps:
        r = step.result
        if step.status == StepStatus.SUCCEEDED and r:
            any_ok = True
            if step.is_recovery:
                lines.append(step.recovery_reason or r.message)
            elif not step.hidden:
                lines.append(r.message)
        elif step.status in (StepStatus.FAILED, StepStatus.BLOCKED) and r:
            if step.optional:
                continue
            any_fail = True
            msg = r.message
            if r.hint and not step_has_successful_recovery(plan, step):
                msg += f" {r.hint}"
            lines.append(msg)
        elif step.status == StepStatus.CANCELLED:
            lines.append(f"Cancelled — I didn't {step.description[:1].lower() + step.description[1:]}.")
    skipped = [s for s in plan.steps if s.status == StepStatus.SKIPPED and not s.optional and not s.is_recovery]
    if skipped and any_fail:
        lines.append(f"I stopped there and skipped {len(skipped)} remaining step{'s' if len(skipped) != 1 else ''}.")
    text = "\n".join(line.strip() for line in lines if line and line.strip())
    return Report(text=text or ("Done." if any_ok else "Nothing was done."), success=any_ok and not any_fail,
                  partial=any_ok and any_fail)


def step_has_successful_recovery(plan: Plan, step) -> bool:
    idx = plan.steps.index(step)
    return any(s.is_recovery and s.status == StepStatus.SUCCEEDED for s in plan.steps[idx + 1:])
