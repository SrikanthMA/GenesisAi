"""Local to-do list and notes (stored in Genesis's SQLite database — private, on this PC)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from ..security.permissions import RiskLevel
from .base import Tool, ToolResult, obj, s
from .reminders import human_time


def _pick(rows: list[dict], ref: str, key: str = "text") -> tuple[Optional[dict], list[dict]]:
    ref = (ref or "").strip()
    if ref.isdigit():
        row = next((r for r in rows if r["id"] == int(ref)), None)
        return row, [row] if row else []
    words = [w for w in ref.lower().split() if len(w) > 2 and w not in ("the", "task", "note", "about", "my")]
    hits = [r for r in rows if all(w in (r.get(key, "") + " " + r.get("title", "")).lower() for w in words)] if words else []
    return (hits[0] if len(hits) == 1 else None), hits


class AddTask(Tool):
    name = "add_task"
    category = "productivity"
    description = "Add an item to the user's to-do list (optionally with a due date)."
    parameters = obj({"text": s("The task", maxLength=500), "due": s("ISO 8601 due date/time")}, ["text"])

    def run(self, args, ctx):
        tid = ctx.repos.todos.add(args["text"].strip(), args.get("due"))
        due = f" (due {human_time(datetime.fromisoformat(args['due']))})" if args.get("due") else ""
        return ToolResult.ok(f"Added “{args['text'].strip()}” to your to-do list{due}.", task_id=tid, text=args["text"])

    def verify(self, args, result, ctx):
        return bool(result.success and ctx.repos.todos.get(result.data["task_id"]))


class ListTasks(Tool):
    name = "list_tasks"
    category = "productivity"
    description = "Show the to-do list."
    parameters = obj({"include_done": {"type": "boolean", "default": False}})

    def run(self, args, ctx):
        rows = ctx.repos.todos.list(bool(args.get("include_done")))
        if not rows:
            return ToolResult.ok("Your to-do list is empty.", tasks=[])
        lines = [f"{i + 1}. {'✓ ' if r['done'] else ''}{r['text']}" +
                 (f" — due {human_time(datetime.fromisoformat(r['due']))}" if r.get("due") else "") for i, r in enumerate(rows[:20])]
        return ToolResult.ok("Your to-do list:\n" + "\n".join(lines), tasks=rows)


class CompleteTask(Tool):
    name = "complete_task"
    category = "productivity"
    description = "Mark a to-do item as done (by id or by words from it)."
    parameters = obj({"ref": s("Task id or words from it", maxLength=300)}, ["ref"])

    def run(self, args, ctx):
        row, hits = _pick(ctx.repos.todos.list(), args["ref"])
        if row is None:
            if len(hits) > 1:
                return ToolResult.fail("Several tasks match: " + "; ".join(h["text"] for h in hits[:4]) + ". Which one?")
            return ToolResult.fail(f"I couldn't find a to-do matching “{args['ref']}”.")
        ctx.repos.todos.set_done(row["id"])
        return ToolResult.ok(f"Nice — marked “{row['text']}” as done.", task_id=row["id"])


class DeleteTask(Tool):
    name = "delete_task"
    category = "productivity"
    description = "Remove an item from the to-do list."
    parameters = obj({"ref": s("Task id or words from it", maxLength=300)}, ["ref"])
    risk = RiskLevel.MEDIUM

    def confirmation_prompt(self, args):
        return f"Remove “{args['ref']}” from your to-do list?"

    def run(self, args, ctx):
        row, hits = _pick(ctx.repos.todos.list(include_done=True), args["ref"])
        if row is None:
            return ToolResult.fail("Which task? " + ("; ".join(h["text"] for h in hits[:4]) if hits else "I couldn't find it."))
        ctx.repos.todos.delete(row["id"])
        return ToolResult.ok(f"Removed “{row['text']}”.", task_id=row["id"])


class AddNote(Tool):
    name = "add_note"
    category = "productivity"
    description = "Save a note (local, private)."
    parameters = obj({"content": s("Note text", maxLength=20_000), "title": s("Short title", maxLength=200)}, ["content"])

    def run(self, args, ctx):
        title = (args.get("title") or " ".join(args["content"].split()[:6])).strip()
        nid = ctx.repos.notes.add(title, args["content"].strip())
        return ToolResult.ok(f"Noted: “{title}”.", note_id=nid, title=title)


class FindNotes(Tool):
    name = "find_notes"
    category = "productivity"
    description = "List notes, or find notes containing some words, and read the best match."
    parameters = obj({"query": s("Words to look for (empty = all notes)", maxLength=300)})

    def run(self, args, ctx):
        rows = ctx.repos.notes.search(args.get("query", ""))
        if not rows:
            return ToolResult.ok("I don't have any notes" + (f" about “{args['query']}”." if args.get("query") else " yet."), notes=[])
        if args.get("query") and len(rows) == 1:
            n = rows[0]
            return ToolResult.ok(f"Your note “{n['title']}”:\n{n['content']}", notes=rows, note_id=n["id"])
        lines = [f"{i + 1}. {n['title']}" for i, n in enumerate(rows[:15])]
        return ToolResult.ok("Your notes:\n" + "\n".join(lines), notes=rows)


class DeleteNote(Tool):
    name = "delete_note"
    category = "productivity"
    description = "Delete a note."
    parameters = obj({"ref": s("Note id or words from it", maxLength=300)}, ["ref"])
    risk = RiskLevel.MEDIUM

    def confirmation_prompt(self, args):
        return f"Delete the note “{args['ref']}”?"

    def run(self, args, ctx):
        row, hits = _pick(ctx.repos.notes.list(), args["ref"], key="content")
        if row is None:
            return ToolResult.fail("Which note? " + ("; ".join(h["title"] for h in hits[:4]) if hits else "I couldn't find it."))
        ctx.repos.notes.delete(row["id"])
        return ToolResult.ok(f"Deleted the note “{row['title']}”.", note_id=row["id"])


TOOLS = [AddTask(), ListTasks(), CompleteTask(), DeleteTask(), AddNote(), FindNotes(), DeleteNote()]
