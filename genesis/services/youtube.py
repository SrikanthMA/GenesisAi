"""YouTube: search, pick a result, play it.

Search results are read from YouTube's own results page (the ``ytInitialData`` JSON embedded in the HTML), so
"search YouTube for X" → "play the second one" works without an API key. If the page can't be read (offline,
consent wall, layout change) the tool still opens the results page in the browser and says it couldn't read
the list — it never invents results.

Titles/channels are **untrusted** external text: they are shown to the user and used to pick a video id, but
never followed as instructions.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any, Optional

import requests

from ..core.logging_setup import get_logger
from .web import USER_AGENT

log = get_logger("youtube")

BASE = "https://www.youtube.com"
SORT_BY_UPLOAD_DATE = "CAI%3D"  # YouTube's "sort by upload date" search filter
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


class YouTubeError(RuntimeError):
    pass


def search_url(query: str, latest: bool = False) -> str:
    url = f"{BASE}/results?search_query={urllib.parse.quote_plus(query)}"
    return url + (f"&sp={SORT_BY_UPLOAD_DATE}" if latest else "")


def watch_url(video_id: str) -> str:
    if not VIDEO_ID_RE.match(video_id or ""):
        raise YouTubeError("That doesn't look like a YouTube video id.")
    return f"{BASE}/watch?v={video_id}"


def video_id_from_url(url: str) -> Optional[str]:
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else None


def _text(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    if "simpleText" in node:
        return str(node["simpleText"])
    return "".join(str(r.get("text", "")) for r in node.get("runs", []) if isinstance(r, dict))


def _walk(node: Any, key: str, out: list, limit: int) -> None:
    if len(out) >= limit:
        return
    if isinstance(node, dict):
        if key in node and isinstance(node[key], dict):
            out.append(node[key])
            return
        for v in node.values():
            _walk(v, key, out, limit)
    elif isinstance(node, list):
        for v in node:
            _walk(v, key, out, limit)


def parse_results(html_text: str, limit: int = 10) -> list[dict]:
    """Extract video results from a YouTube results page."""
    m = re.search(r"(?:var ytInitialData|window\[\"ytInitialData\"\])\s*=\s*(\{.*?\});\s*</script>", html_text, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    renderers: list = []
    _walk(data, "videoRenderer", renderers, limit * 2)
    out, seen = [], set()
    for r in renderers:
        vid = r.get("videoId")
        if not vid or vid in seen or not VIDEO_ID_RE.match(vid):
            continue
        seen.add(vid)
        out.append({
            "video_id": vid,
            "title": _text(r.get("title"))[:200],
            "channel": _text(r.get("ownerText") or r.get("longBylineText"))[:120],
            "duration": _text(r.get("lengthText")),
            "published": _text(r.get("publishedTimeText")),
            "views": _text(r.get("viewCountText")),
            "url": f"{BASE}/watch?v={vid}",
        })
        if len(out) >= limit:
            break
    return out


class YouTubeService:
    CACHE_TTL = 300.0

    def __init__(self, session: Any = None) -> None:
        self.session = session or requests.Session()
        self._cache: dict[tuple, tuple[float, list[dict]]] = {}

    def search(self, query: str, *, latest: bool = False, limit: int = 8) -> list[dict]:
        import time as _t

        key = (query.strip().lower(), latest, limit)
        hit = self._cache.get(key)
        if hit and _t.time() - hit[0] < self.CACHE_TTL:
            return [dict(r) for r in hit[1]]  # "play the second one" right after a search needs no second request
        results = self._search(query, latest=latest, limit=limit)
        if results:
            self._cache[key] = (_t.time(), results)
            if len(self._cache) > 50:
                self._cache.pop(next(iter(self._cache)))
        return results

    def _search(self, query: str, *, latest: bool = False, limit: int = 8) -> list[dict]:
        url = search_url(query, latest)
        try:
            resp = self.session.get(url, timeout=15, headers={
                "User-Agent": USER_AGENT.replace("Genesis-Desktop-Assistant/1.0", "Chrome/124.0 Safari/537.36"),
                "Accept-Language": "en-US,en;q=0.9"}, cookies={"CONSENT": "YES+1", "SOCS": "CAI"})
        except requests.RequestException as exc:
            raise YouTubeError(f"Could not reach YouTube ({type(exc).__name__}).") from exc
        if getattr(resp, "status_code", 200) >= 400:
            raise YouTubeError(f"YouTube returned HTTP {resp.status_code}.")
        results = parse_results(resp.text, limit)
        log.info("youtube search", extra={"data": {"results": len(results), "latest": latest}})
        return results
