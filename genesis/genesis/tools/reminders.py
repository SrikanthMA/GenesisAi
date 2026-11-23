"""Reminder and calendar tools."""
from __future__ import annotations

from datetime import datetime

from ..services.calendar import CalendarError
from ..services.google_auth import GoogleAuthError
from ..services.scheduler import from_utc_iso
from .base import Tool, ToolResult, obj, s


def human_time(dt: datetime) -> str:
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    day = dt.date()
    if day == now.date():
        d = "today"
    elif (day - now.date()).days == 1:
        d = "tomorrow"
    elif 0 < (day - now.date()).days < 7:
        d = dt.strftime("%A")
    else:
        d = dt.strftime("%a %d %b")
    t = dt.strftime("%I:%M %p").lstrip("0")
    return f"{d} at {t}"


class CreateReminder(Tool):
    name = "create_reminder"
    category = "productivity"
    description = "Create a local reminder that pops up (and is spoken) at the given local date/time."
    parameters = obj({"text": s("What to remind about", maxLength=500), "due_at": s("ISO 8601 local datetime")}, ["text", "due_at"])

    def run(self, args, ctx):
        try:
            due = datetime.fromisoformat(args["due_at"])
        except ValueError:
            return ToolResult.fail("That reminder time isn't a valid date.")
        now = datetime.now(due.tzinfo) if due.tzinfo else datetime.now()
        if due <= now:
            return ToolResult.fail("That time is in the past — when should I remind you?")
        if ctx.scheduler is None:
            return ToolResult.fail("The reminder service isn't running.")
        rid = ctx.scheduler.add(args["text"], due)
        return ToolResult.ok(f"Okay — I'll remind you {human_time(due)} to {args['text']}.", reminder_id=rid,
                             due_at=due.isoformat(), text=args["text"])

    def verify(self, args, result, ctx):
        if not result.success:
            return False
        row = ctx.repos.reminders.get(result.data["reminder_id"])
        return bool(row and row["status"] == "pending")


class ListReminders(Tool):
    name = "list_reminders"
    category = "productivity"
    description = "List pending reminders."
    parameters = obj({})

    def run(self, args, ctx):
        rows = ctx.repos.reminders.pending()
        if not rows:
            return ToolResult.ok("You have no pending reminders.", reminders=[])
        lines = [f"• {human_time(from_utc_iso(r['due_at']))} — {r['text']}" for r in rows[:15]]
        return ToolResult.ok("Your reminders:\n" + "\n".join(lines), reminders=rows)


class CreateCalendarEvent(Tool):
    name = "create_calendar_event"
    category = "productivity"
    description = "Create an event in the user's Google Calendar."
    parameters = obj({"title": s("Event title", maxLength=300), "start": s("ISO 8601 local start"),
                      "duration_minutes": {"type": "integer", "minimum": 5, "maximum": 1440, "default": 30},
                      "description": s("Details", maxLength=5000)}, ["title", "start"])
    remote = True

    def run(self, args, ctx):
        try:
            start = datetime.fromisoformat(args["start"])
            info = ctx.calendar.create_event(args["title"], start, args.get("duration_minutes", 30), args.get("description", ""))
        except ValueError:
            return ToolResult.fail("That start time isn't a valid date.")
        except GoogleAuthError as exc:
            return ToolResult.fail(f"I couldn't add it to Google Calendar: {exc}", hint="Connect Google in Genesis → Services.")
        except CalendarError as exc:
            return ToolResult.fail(str(exc))
        return ToolResult.ok(f"Added “{args['title']}” to your calendar {human_time(start)}.", **info)


TOOLS = [CreateReminder(), ListReminders(), CreateCalendarEvent()]
