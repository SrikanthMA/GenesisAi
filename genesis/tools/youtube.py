"""YouTube tools: search, play (by result / id / query / "latest"), and media control for what's playing."""
from __future__ import annotations

from ..security.permissions import RiskLevel
from ..security.validators import ValidationError, validate_url
from ..services.platform import PlatformError
from ..services.youtube import YouTubeError, search_url, video_id_from_url, watch_url
from .apps import BROWSER_PROCESSES
from .base import Tool, ToolResult, obj, s


def _fmt(i: int, r: dict) -> str:
    extra = " · ".join(x for x in (r.get("channel"), r.get("duration"), r.get("published")) if x)
    return f"{i}. {r.get('title') or r['video_id']}" + (f" — {extra}" if extra else "")


class YouTubeSearch(Tool):
    name = "youtube_search"
    category = "youtube"
    description = "Search YouTube: opens the results in the browser and returns the list of videos (so 'play the 2nd one' works)."
    parameters = obj({"query": s("What to search for", maxLength=300),
                      "latest": {"type": "boolean", "default": False, "description": "Sort by upload date"},
                      "open_browser": {"type": "boolean", "default": True}}, ["query"])
    remote = True

    def run(self, args, ctx):
        q = args["query"].strip()
        url = search_url(q, bool(args.get("latest")))
        opened = ctx.backend.open_url(url) if args.get("open_browser", True) else False
        try:
            results = ctx.youtube.search(q, latest=bool(args.get("latest")))
        except YouTubeError as exc:
            results = []
            reason = str(exc)
        else:
            reason = "" if results else "the page didn't contain a readable list"
        if not results:
            if opened:
                return ToolResult.ok(f"I opened the YouTube results for “{q}”, but I couldn't read the list ({reason}), "
                                     "so pick a video there.", query=q, results=[], url=url, opened=True)
            return ToolResult.fail(f"I couldn't search YouTube: {reason}.", retryable="reach" in reason)
        lines = "\n".join(_fmt(i + 1, r) for i, r in enumerate(results[:5]))
        msg = (f"Here's what YouTube has for “{q}”" + (" (newest first)" if args.get("latest") else "") + f":\n{lines}\n"
               "Say “play the first one” (or the second…).")
        res = ToolResult.ok(msg, query=q, results=results, url=url, opened=opened)
        res.untrusted_content = True
        return res


class YouTubePlay(Tool):
    name = "youtube_play"
    category = "youtube"
    description = ("Play a YouTube video: by video_id/url, or search for `query` and play result number `index` "
                   "(1 = first; set latest=true for the newest upload).")
    parameters = obj({"video_id": s("11-character YouTube id", maxLength=20), "url": s("YouTube video URL", maxLength=500),
                      "query": s("Search terms", maxLength=300), "index": {"type": "integer", "minimum": 1, "maximum": 20, "default": 1},
                      "latest": {"type": "boolean", "default": False}, "title": s("Title (for the reply)", maxLength=300)})
    remote = True

    def run(self, args, ctx):
        vid = args.get("video_id") or video_id_from_url(args.get("url", ""))
        title, channel = args.get("title", ""), ""
        if not vid:
            q = (args.get("query") or "").strip()
            if not q:
                return ToolResult.fail("What should I play?")
            try:
                results = ctx.youtube.search(q, latest=bool(args.get("latest")))
            except YouTubeError as exc:
                results, err = [], str(exc)
            else:
                err = ""
            idx = int(args.get("index") or 1) - 1
            if not results or idx >= len(results):
                url = search_url(q, bool(args.get("latest")))
                if ctx.backend.open_url(url):
                    return ToolResult.ok(f"I couldn't pick a video automatically ({err or 'no readable results'}), so I opened "
                                         f"the YouTube results for “{q}” — click the one you want.", url=url, played=False)
                return ToolResult.fail(f"I couldn't reach YouTube{': ' + err if err else ''}.", retryable=True)
            pick = results[idx]
            vid, title, channel = pick["video_id"], pick.get("title", ""), pick.get("channel", "")
        try:
            url = validate_url(watch_url(vid))
        except (YouTubeError, ValidationError) as exc:
            return ToolResult.fail(str(exc))
        if not ctx.backend.open_url(url):
            return ToolResult.fail("The browser didn't open.", retryable=True)
        ctx.extras["media_playing"] = True
        who = f" by {channel}" if channel else ""
        return ToolResult.ok(f"Playing “{title}”{who} on YouTube." if title else "Playing it on YouTube.",
                             video_id=vid, url=url, title=title, channel=channel, played=True)

    def verify(self, args, result, ctx):
        if not result.success or not result.data.get("played"):
            return None
        return True if ctx.backend.find_processes(BROWSER_PROCESSES) else None


class MediaControl(Tool):
    name = "media_control"
    category = "media"
    description = ("Control whatever is playing (YouTube, Spotify, …) with the system media keys: pause, resume, "
                   "play_pause, next, previous, volume_up, volume_down, mute.")
    parameters = obj({"action": {"type": "string", "enum": ["pause", "resume", "play_pause", "next", "previous", "volume_up",
                                                             "volume_down", "mute"]},
                      "steps": {"type": "integer", "minimum": 1, "maximum": 25, "default": 5}}, ["action"])
    risk = RiskLevel.LOW

    def run(self, args, ctx):
        a = args["action"]
        playing = ctx.extras.get("media_playing")
        try:
            if a == "pause" and playing is False:
                return ToolResult.ok("It's already paused.", action=a)
            if a == "resume" and playing is True:
                return ToolResult.ok("It's already playing.", action=a)
            if a in ("pause", "resume", "play_pause"):
                ctx.backend.media_key("play_pause")
                ctx.extras["media_playing"] = {"pause": False, "resume": True}.get(a, None if playing is None else not playing)
            elif a in ("next", "previous"):
                ctx.backend.media_key("next" if a == "next" else "prev")
                ctx.extras["media_playing"] = True
            elif a == "mute":
                ctx.backend.media_key("mute")
            else:
                ctx.backend.media_key(a, int(args.get("steps", 5)))
        except PlatformError as exc:
            return ToolResult.fail(str(exc))
        words = {"pause": "Paused.", "resume": "Resumed.", "play_pause": "Toggled play/pause.", "next": "Skipped to the next one.",
                 "previous": "Went back to the previous one.", "volume_up": "Turned it up.", "volume_down": "Turned it down.",
                 "mute": "Toggled mute."}
        return ToolResult.ok(words[a], action=a)


TOOLS = [YouTubeSearch(), YouTubePlay(), MediaControl()]
