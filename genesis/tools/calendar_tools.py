"""Google Calendar tools: create, list/search, find, move/rename and delete events."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from ..security.permissions import RiskLevel
from ..security.validators import ValidationError, validate_email
from ..services.calendar import CalendarError
from ..services.google_auth import GoogleAuthError
from .base import Tool, ToolResult, obj, s
from .reminders import human_time

CONNECT_HINT = "Connect your Google account in Genesis → Connected services (Calendar permission)."


def _dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _local(dt: datetime) -> datetime:
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt


def _line(i: int, e: dict) -> str:
    st = _dt(e.get("start"))
    when = "all day" if e.get("all_day") or st is None else _local(st).strftime("%I:%M %p").lstrip("0")
    return f"{i}. {when} — {e['title']}"


def _day_bounds(day: Optional[str]) -> tuple[datetime, datetime]:
    d = datetime.fromisoformat(day).date() if day else datetime.now().date()
    start = datetime.combine(d, datetime.min.time())
    return start, start + timedelta(days=1)


def _fail(exc: Exception) -> ToolResult:
    if isinstance(exc, GoogleAuthError):
        return ToolResult.fail(f"I can't reach your Google Calendar: {exc}", hint=CONNECT_HINT)
    return ToolResult.fail(str(exc), retryable="reach" in str(exc))


class CreateCalendarEvent(Tool):
    name = "create_calendar_event"
    category = "calendar"
    description = "Create an event in the user's Google Calendar (asks first, showing the exact time)."
    parameters = obj({"title": s("Event title", maxLength=300), "start": s("ISO 8601 local start"),
                      "duration_minutes": {"type": "integer", "minimum": 5, "maximum": 1440, "default": 30},
                      "description": s("Details", maxLength=5000),
                      "attendees": {"type": "array", "items": {"type": "string"}, "maxItems": 20}}, ["title", "start"])
    risk = RiskLevel.MEDIUM
    remote = True

    def confirmation_prompt(self, args):
        st = _dt(args.get("start"))
        when = human_time(st) if st else args.get("start")
        return f"Add “{args['title']}” to your calendar {when} ({args.get('duration_minutes', 30)} min)?"

    def run(self, args, ctx):
        start = _dt(args["start"])
        if start is None:
            return ToolResult.fail("That start time isn't a valid date.")
        try:
            attendees = [validate_email(a) for a in args.get("attendees") or []]
            info = ctx.calendar.create_event(args["title"], start, args.get("duration_minutes", 30), args.get("description", ""),
                                             attendees=attendees or None)
        except ValidationError as exc:
            return ToolResult.fail(str(exc))
        except (GoogleAuthError, CalendarError) as exc:
            return _fail(exc)
        return ToolResult.ok(f"Added “{args['title']}” to your calendar {human_time(start)}.", **info)


class ListCalendarEvents(Tool):
    name = "list_calendar_events"
    category = "calendar"
    description = "List calendar events for a day (YYYY-MM-DD, default today) or a date range, optionally matching text."
    parameters = obj({"day": s("YYYY-MM-DD"), "start": s("ISO start of range"), "end": s("ISO end of range"),
                      "query": s("Text to match", maxLength=200), "label": s("How to describe the range, e.g. 'tomorrow'")})
    remote = True

    def run(self, args, ctx):
        try:
            if args.get("start") and args.get("end"):
                start, end = _dt(args["start"]), _dt(args["end"])
            else:
                start, end = _day_bounds(args.get("day"))
            events = ctx.calendar.list_events(start, end, args.get("query", ""))
        except ValueError:
            return ToolResult.fail("I couldn't understand that date.")
        except (GoogleAuthError, CalendarError) as exc:
            return _fail(exc)
        label = args.get("label") or ("today" if not args.get("day") else human_time(start).split(" at ")[0])
        if not events:
            return ToolResult.ok(f"Your calendar is clear {label}." if not args.get("query")
                                 else f"No events matching “{args['query']}” {label}.", events=[])
        lines = "\n".join(_line(i + 1, e) for i, e in enumerate(events[:15]))
        n = len(events)
        return ToolResult.ok(f"{label.capitalize()} you have {n} event{'s' if n != 1 else ''}:\n{lines}", events=events)


class FindCalendarEvent(Tool):
    name = "find_calendar_event"
    category = "calendar"
    description = "Find ONE calendar event by day, time (HH:MM) and/or text — used before moving or cancelling it."
    parameters = obj({"day": s("YYYY-MM-DD (default: today, then the next 14 days)"), "time": s("HH:MM"),
                      "query": s("Text in the title, e.g. 'Rahul'", maxLength=200)})
    remote = True

    def run(self, args, ctx):
        try:
            if args.get("day"):
                start, end = _day_bounds(args["day"])
            else:
                start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                end = start + timedelta(days=14)
            events = ctx.calendar.list_events(start, end)
        except (GoogleAuthError, CalendarError) as exc:
            return _fail(exc)
        q = (args.get("query") or "").lower().strip()
        stop = {"meeting", "the", "my", "a", "event", "appointment", "call", "with"}
        words = [w for w in q.split() if w not in stop]
        if words:
            events = [e for e in events if all(w in (e["title"] + " " + " ".join(e.get("attendees", []))).lower() for w in words)]
        if args.get("time"):
            hh, mm = (int(x) for x in args["time"].split(":"))
            events = [e for e in events if _dt(e.get("start")) and (_local(_dt(e["start"])).hour, _local(_dt(e["start"])).minute) == (hh, mm)]
        if not events:
            return ToolResult.fail("I couldn't find that event on your calendar.", hint="Say “what do I have tomorrow?” to see your events.")
        if len(events) > 1:
            lines = "\n".join(_line(i + 1, e) for i, e in enumerate(events[:6]))
            return ToolResult.fail(f"More than one event matches:\n{lines}\nWhich one — tell me its time or name?", events=events)
        e = events[0]
        return ToolResult.ok(f"Found “{e['title']}”.", **e)


class UpdateCalendarEvent(Tool):
    name = "update_calendar_event"
    category = "calendar"
    description = "Move and/or rename a calendar event. new_time (HH:MM) keeps the day; new_start (ISO) sets both."
    parameters = obj({"event_id": s("Event id", maxLength=300), "title": s("Current title (for the confirmation)"),
                      "current_start": s("Current start ISO (for the confirmation)"), "new_start": s("ISO 8601 local start"),
                      "new_time": s("HH:MM"), "new_date": s("YYYY-MM-DD"), "new_title": s("New title", maxLength=300),
                      "duration_minutes": {"type": "integer", "minimum": 5, "maximum": 1440}}, ["event_id"])
    risk = RiskLevel.MEDIUM
    remote = True

    def _target(self, args) -> Optional[datetime]:
        if args.get("new_start"):
            return _dt(args["new_start"])
        cur = _dt(args.get("current_start"))
        base = _local(cur) if cur else datetime.now()
        if args.get("new_date"):
            d = datetime.fromisoformat(args["new_date"]).date()
            base = base.replace(year=d.year, month=d.month, day=d.day)
        if args.get("new_time"):
            hh, mm = (int(x) for x in args["new_time"].split(":"))
            base = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return base if (args.get("new_time") or args.get("new_date")) else None

    def confirmation_prompt(self, args):
        new = self._target(args)
        what = f"“{args.get('title') or 'that event'}”"
        parts = []
        if new:
            parts.append(f"to {human_time(new)}")
        if args.get("new_title"):
            parts.append(f"and rename it “{args['new_title']}”")
        return f"Move {what} {' '.join(parts)}?" if new else f"Rename {what} to “{args.get('new_title')}”?"

    def run(self, args, ctx):
        new = self._target(args)
        if new is None and not args.get("new_title") and not args.get("duration_minutes"):
            return ToolResult.fail("What should I change about it?")
        try:
            e = ctx.calendar.update_event(args["event_id"], start=new, duration_minutes=args.get("duration_minutes"),
                                          title=args.get("new_title"))
        except (GoogleAuthError, CalendarError) as exc:
            return _fail(exc)
        st = _dt(e.get("start"))
        return ToolResult.ok(f"Moved “{e['title']}” to {human_time(_local(st))}." if new and st else f"Updated “{e['title']}”.", **e)


class DeleteCalendarEvent(Tool):
    name = "delete_calendar_event"
    category = "calendar"
    description = "Cancel (delete) a calendar event. Always asks first."
    parameters = obj({"event_id": s("Event id", maxLength=300), "title": s("Title (for the confirmation)"),
                      "start": s("Start ISO (for the confirmation)")}, ["event_id"])
    risk = RiskLevel.MEDIUM
    always_confirm = True
    remote = True

    def confirmation_prompt(self, args):
        st = _dt(args.get("start"))
        return f"Cancel “{args.get('title') or 'that event'}”" + (f" {human_time(_local(st))}" if st else "") + "?"

    def run(self, args, ctx):
        try:
            ctx.calendar.delete_event(args["event_id"])
        except (GoogleAuthError, CalendarError) as exc:
            return _fail(exc)
        return ToolResult.ok(f"Cancelled “{args.get('title') or 'the event'}”.", event_id=args["event_id"])


TOOLS = [CreateCalendarEvent(), ListCalendarEvents(), FindCalendarEvent(), UpdateCalendarEvent(), DeleteCalendarEvent()]
