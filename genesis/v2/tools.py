"""2.0 tools: inspect and clear the persistent task journal (memory controls for task history)."""
from __future__ import annotations

from ..security.permissions import RiskLevel
from ..tools.base import Tool, ToolResult, obj
from . import tasks as v2tasks


def _journal(ctx):
    return ctx.extras.get("v2_journal") or v2tasks.JOURNAL


class ListUnfinishedTasks(Tool):
    name = "list_unfinished_tasks"
    category = "tasks"
    description = "List multi-step tasks that were interrupted, cancelled, failed or are waiting for approval (they can be resumed)."
    parameters = obj({})

    def run(self, args, ctx):
        j = _journal(ctx)
        if j is None:
            return ToolResult.fail("Task history isn't available in this session.")
        return ToolResult.ok(j.describe_unfinished(), count=len(j.unfinished()))

    def verify(self, args, result, ctx):
        return result.success


class ClearTaskHistory(Tool):
    name = "clear_task_history"
    category = "tasks"
    description = "Delete the saved progress of past multi-step tasks (the audit log of actions is kept)."
    parameters = obj({})
    risk = RiskLevel.MEDIUM

    def confirmation_prompt(self, args):
        return "This deletes the saved progress of your past tasks, so they can't be resumed. Continue?"

    def run(self, args, ctx):
        j = _journal(ctx)
        if j is None:
            return ToolResult.fail("Task history isn't available in this session.")
        n = j.clear()
        return ToolResult.ok(f"Cleared the saved progress of {n} task{'s' if n != 1 else ''}.", cleared=n)

    def verify(self, args, result, ctx):
        j = _journal(ctx)
        return bool(result.success and j is not None and not j.unfinished(limit=1))


TOOLS = [ListUnfinishedTasks(), ClearTaskHistory()]

from .meet_tools import TOOLS as _TOOLS_21  # noqa: E402  (2.1: Google Meet, meeting minutes, voice chat)

TOOLS += _TOOLS_21

from .coding_tools import TOOLS as _TOOLS_22  # noqa: E402  (2.2: active window, code in any window, Colab extras)

TOOLS += _TOOLS_22
