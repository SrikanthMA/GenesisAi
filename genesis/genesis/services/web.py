"""Web search + page fetching helpers.

* ``search_url`` builds a search URL for the user's configured engine
  (opened visibly in their browser — the primary action).
* ``fetch_results`` optionally retrieves top results as text so Genesis can
  summarise them (uses the ``ddgs`` / ``duckduckgo_search`` package if
  installed; otherwise the browser tab is the result).
* ``fetch_page_text`` downloads a page and converts HTML to readable text.
All fetched content is untrusted and must be wrapped before reaching the LLM.
"""
from __future__ import annotations

import html
import re
import urllib.parse
from html.parser import HTMLParser
from typing import Optional

import requests

from ..core.logging_setup import get_logger

log = get_logger("web")

ENGINES = {
    "google": "https://www.google.com/search?q={q}",
    "bing": "https://www.bing.com/search?q={q}",
    "duckduckgo": "https://duckduckgo.com/?q={q}",
}
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Genesis-Desktop-Assistant/1.0"


def search_url(query: str, engine: str = "google") -> str:
    tpl = ENGINES.get(engine, ENGINES["google"])
    return tpl.format(q=urllib.parse.quote_plus(query))


def fetch_results(query: str, max_results: int = 5) -> list[dict]:
    try:
        try:
            from ddgs import DDGS  # type: ignore
        except ImportError:
            from duckduckgo_search import DDGS  # type: ignore
    except ImportError:
        return []
    try:
        with DDGS() as ddgs:
            return [{"title": r.get("title", ""), "url": r.get("href") or r.get("url", ""), "snippet": r.get("body", "")}
                    for r in ddgs.text(query, max_results=max_results)]
    except Exception as exc:
        log.info("search result fetch failed: %s", exc)
        return []


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form", "iframe"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip_depth = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip_depth:
            self.skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip_depth:
            self.parts.append(data)


def html_to_text(markup: str) -> tuple[str, str]:
    p = _TextExtractor()
    try:
        p.feed(markup)
    except Exception:
        pass
    text = html.unescape("".join(p.parts))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return p.title.strip(), text


def fetch_page_text(url: str, timeout: float = 20.0, max_bytes: int = 2_000_000,
                    session: Optional[requests.Session] = None) -> tuple[str, str]:
    sess = session or requests.Session()
    resp = sess.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT}, stream=True, allow_redirects=True)
    resp.raise_for_status()
    ctype = resp.headers.get("Content-Type", "")
    if "html" not in ctype and "text" not in ctype:
        raise ValueError(f"Page is not text/HTML ({ctype or 'unknown type'}).")
    chunks, size = [], 0
    for chunk in resp.iter_content(65536):
        chunks.append(chunk)
        size += len(chunk)
        if size > max_bytes:
            break
    raw = b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")
    return html_to_text(raw) if "html" in ctype else ("", raw)
