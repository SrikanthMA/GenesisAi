"""Routines: explicitly created, inspectable, deletable automations.

A routine is a stored natural-language command sequence ("open VS Code and then
open my Genesis project") with an optional weekly schedule. When it runs, the
commands go through the *normal* pipeline — NLU, planner, permission checks and
confirmations — so a routine can never do anything the user couldn't do by
speaking, and risky steps still wait for confirmation.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from ..security.permissions import RiskLevel
from .base import Tool, ToolResult, obj, s

DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def describe_schedule(days: list[int], time_of_day: Optional[str]) -> str:
    if not time_of_day:
        return "on request"
    if sorted(days) == list(range(7)):
        when = "every day"
    elif sorted(days) == list(range(5)):
        when = "every weekday"
    elif sorted(days) == [5, 6]:
        when = "every weekend day"
    else:
        when = "every " + ", ".join(DAY_NAMES[d].capitalize() for d in sorted(days))
    hh, mm = (int(x) for x in time_of_day.split(":"))
    t = datetime(2000, 1, 1, hh, mm).strftime("%I:%M %p").lstrip("0")
    return f"{when} at {t}"


class CreateRoutine(Tool):
    name = "create_routine"
    category = "routines"
    description = "Save a routine (a sequence of spoken commands), optionally scheduled weekly at a time."
    parameters = obj({
        "name": s("Routine name", maxLength=80),
        "commands": s("Commands, e.g. 'open VS Code and then open my Genesis project'", maxLength=1000),
        "days": {"type": "array", "items": {"type": "integer"}, "maxItems": 7},
        "time_of_day": s("HH:MM 24h", maxLength=5),
    }, ["name", "commands"])
    risk = RiskLevel.MEDIUM  # schedules future automated actions → the user confirms exactly what will run

    def confirmation_prompt(self, args):
        return (f"Save the routine “{args['name']}” — {describe_schedule(args.get('days') or [], args.get('time_of_day'))}: "
                f"{args['commands']}. Risky steps will still ask you first. Save it?")

    def run(self, args, ctx):
        days = sorted({d for d in (args.get("days") or []) if 0 <= d <= 6})
        tod = args.get("time_of_day")
        if tod and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", tod):
            return ToolResult.fail("That routine time isn't valid.")
        if tod and not days:
            days = list(range(7))
        rid = ctx.repos.routines.add(args["name"], args["commands"], days, tod)
        return ToolResult.ok(f"Saved the “{args['name']}” routine — {describe_schedule(days, tod)}. "
                             "You can see or delete it on the Memory page.", routine_id=rid, name=args["name"])

    def verify(self, args, result, ctx):
        return bool(result.success and ctx.repos.routines.get(result.data["routine_id"]))


class ListRoutines(Tool):
    name = "list_routines"
    category = "routines"
    description = "List saved routines."
    parameters = obj({})

    def run(self, args, ctx):
        rows = ctx.repos.routines.list()
        if not rows:
            return ToolResult.ok("You don't have any routines yet. Try “every Monday at 8 AM, open VS Code and my Genesis project”.",
                                 routines=[])
        lines = [f"• {r['name']} — {describe_schedule(r['days'], r['time_of_day'])}: {r['commands']}"
                 + ("" if r["enabled"] else " (paused)") for r in rows]
        return ToolResult.ok("Your routines:\n" + "\n".join(lines), routines=rows)


class DeleteRoutine(Tool):
    name = "delete_routine"
    category = "routines"
    description = "Delete a saved routine by name."
    parameters = obj({"name": s("Routine name", maxLength=80)}, ["name"])

    def run(self, args, ctx):
        r = ctx.repos.routines.find(args["name"])
        if not r:
            return ToolResult.fail(f"I don't have a routine called “{args['name']}”.")
        ctx.repos.routines.delete(r["id"])
        return ToolResult.ok(f"Deleted the “{r['name']}” routine.", routine_id=r["id"])


TOOLS = [CreateRoutine(), ListRoutines(), DeleteRoutine()]
