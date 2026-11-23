"""Plan data structures."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from ..tools.base import ToolResult


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


@dataclass
class PlanStep:
    id: int
    tool: str
    args: dict[str, Any]
    description: str = ""
    optional: bool = False  # failure does not abort the rest of the plan
    hidden: bool = False  # intermediate step whose message isn't spoken (e.g. compose_email)
    is_recovery: bool = False
    recovery_reason: str = ""
    status: StepStatus = StepStatus.PENDING
    result: Optional[ToolResult] = None
    resolved_args: Optional[dict] = None
    risk: Optional[str] = None
    confirmed: bool = False
    verified: Optional[bool] = None

    def to_dict(self) -> dict:
        from ..security.redaction import redact

        return {
            "id": self.id, "tool": self.tool, "description": self.description, "status": self.status.value,
            "args": redact(self.resolved_args or self.args), "risk": self.risk, "confirmed": self.confirmed,
            "verified": self.verified, "optional": self.optional, "hidden": self.hidden, "is_recovery": self.is_recovery,
            "result": self.result.to_dict() if self.result else None,
        }


@dataclass
class Plan:
    request: str
    steps: list[PlanStep] = field(default_factory=list)
    intent: str = ""
    source: str = "rules"  # rules | llm
    untrusted: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def add(self, tool: str, args: dict, description: str = "", **kw: Any) -> PlanStep:
        step = PlanStep(id=len(self.steps) + 1, tool=tool, args=args, description=description, **kw)
        self.steps.append(step)
        return step

    @property
    def finished(self) -> bool:
        return all(s.status not in (StepStatus.PENDING, StepStatus.RUNNING, StepStatus.AWAITING_CONFIRMATION) for s in self.steps)

    @property
    def succeeded(self) -> bool:
        core = [s for s in self.steps if not s.is_recovery and not s.optional]
        return bool(core) and all(s.status == StepStatus.SUCCEEDED for s in core)

    def to_dict(self) -> dict:
        return {"id": self.id, "request": self.request, "intent": self.intent, "source": self.source,
                "untrusted": self.untrusted, "steps": [s.to_dict() for s in self.steps]}


@dataclass
class Clarify:
    question: str
    slot: str


@dataclass
class Reply:
    text: str
    ui_action: Optional[str] = None
    data: dict = field(default_factory=dict)
    use_llm: bool = False  # generate a conversational answer with the LLM
