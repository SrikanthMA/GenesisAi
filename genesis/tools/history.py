"""Task memory: search earlier conversations and completed tasks ("that Python tutorial we talked about yesterday")."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from .base import Tool, ToolResult, obj, s

STOP = {"that", "this", "the", "a", "an", "we", "i", "you", "were", "was", "talking", "talked", "about", "discussed", "mentioned",
        "earlier", "yesterday", "today", "last", "week", "find", "show", "open", "me", "my", "one", "thing", "from", "on", "of",
        "looked", "at", "saw", "watched", "said", "before", "again", "remember", "what", "which", "did"}
RANGES = {"today": 0, "yesterday": 1, "this_week": 7, "last_week": 14, "any": 3650}


def keywords(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9+#.]{2,}", (text or "").lower()) if w not in STOP][:6]


def _window(when: str) -> tuple[str, str]:
    now = datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if when == "today":
        start, end = today, now + timedelta(minutes=1)
    elif when == "yesterday":
        start, end = today - timedelta(days=1), today
    elif when == "last_week":
        start, end = today - timedelta(days=14), today - timedelta(days=6)
    else:
        start, end = today - timedelta(days=RANGES.get(when, 3650)), now + timedelta(minutes=1)
    utc = lambda d: d.astimezone(timezone.utc).isoformat(timespec="seconds")  # noqa: E731 (stored timestamps are UTC)
    return utc(start), utc(end)


def _items_from_plan(plan: dict) -> list[dict]:
    out = []
    for st in plan.get("steps", []):
        data = ((st.get("result") or {}).get("data") or {})
        if st.get("status") != "succeeded":
            continue
        if data.get("video_id") and data.get("url"):
            out.append({"url": data["url"], "label": data.get("title") or "YouTube video", "kind": "video"})
        elif st.get("tool") in ("youtube_search", "web_search") and data.get("results"):
            for r in data["results"][:5]:
                if r.get("url"):
                    out.append({"url": r["url"], "label": r.get("title") or r["url"],
                                "kind": "video" if r.get("video_id") else "web"})
        elif data.get("url") and st.get("tool") in ("open_url", "browser_open", "browser_read_page"):
            out.append({"url": data["url"], "label": data.get("title") or data["url"], "kind": "web"})
        elif data.get("path") and st.get("tool") in ("open_file", "read_file", "create_file", "write_code_files"):
            out.append({"path": data["path"], "label": data["path"].replace("\\", "/").rsplit("/", 1)[-1], "kind": "file"})
    return out


class SearchHistory(Tool):
    name = "search_history"
    category = "memory"
    description = "Search earlier conversations and completed tasks (links, videos, files) by topic and time."
    parameters = obj({"query": s("What to look for", maxLength=300),
                      "when": {"type": "string", "enum": list(RANGES), "default": "any"}}, ["query"])

    def run(self, args, ctx):
        if not ctx.memory.enabled:
            return ToolResult.fail("Memory is turned off, so I don't keep past conversations.")
        words = keywords(args["query"])
        if not words:
            return ToolResult.fail("What was it about?")
        start, end = _window(args.get("when", "any"))
        like = " AND ".join(["lower(request) LIKE ?"] * len(words))
        tasks = ctx.db.query(f"SELECT request, plan, created_at FROM tasks WHERE created_at >= ? AND created_at < ? AND ({like}) "
                             "ORDER BY created_at DESC LIMIT 20", (start, end, *[f"%{w}%" for w in words]))
        items: list[dict] = []
        seen = set()
        for t in tasks:
            try:
                plan = json.loads(t["plan"] or "{}")
            except json.JSONDecodeError:
                continue
            for it in _items_from_plan(plan):
                key = it.get("url") or it.get("path")
                if key and key not in seen:
                    seen.add(key)
                    items.append(dict(it, when=t["created_at"], request=t["request"]))
        like_m = " AND ".join(["lower(content) LIKE ?"] * len(words))
        msgs = ctx.db.query(f"SELECT role, content, created_at FROM messages WHERE created_at >= ? AND created_at < ? AND ({like_m}) "
                            "ORDER BY created_at DESC LIMIT 10", (start, end, *[f"%{w}%" for w in words]))
        for m in msgs:
            for url in re.findall(r"https?://[^\s)\]>\"']+", m["content"]):
                if url not in seen:
                    seen.add(url)
                    items.append({"url": url, "label": url, "kind": "web", "when": m["created_at"]})
        when = {"today": " today", "yesterday": " yesterday", "this_week": " this week", "last_week": " last week"}.get(
            args.get("when", "any"), "")
        if not items and not msgs:
            return ToolResult.ok(f"I couldn't find anything about “{' '.join(words)}”{when} in our past conversations.", items=[])
        if not items:
            snippet = msgs[0]["content"][:200]
            return ToolResult.ok(f"We talked about it{when}, but there was no link or file. You said/I said: “{snippet}”",
                                 items=[], messages=msgs[:5])
        lines = "\n".join(f"{i + 1}. {it['label']}" + (" (YouTube)" if it["kind"] == "video" else "") for i, it in enumerate(items[:6]))
        return ToolResult.ok(f"Here's what I found{when}:\n{lines}\nSay “open the first one”.", items=items[:10])


TOOLS = [SearchHistory()]
