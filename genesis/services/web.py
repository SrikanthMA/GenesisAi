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


def fetch_results(query: str, max_results: int = 5, session: Optional[requests.Session] = None) -> list[dict]:
    """Top search results as text. Uses the optional ``ddgs`` package when installed, otherwise DuckDuckGo's plain HTML
    results page (no extra dependency). Returns [] when offline — the browser tab is then the result."""
    try:
        try:
            from ddgs import DDGS  # type: ignore
        except ImportError:
            from duckduckgo_search import DDGS  # type: ignore
    except ImportError:
        DDGS = None  # noqa: N806
    import time as _t

    key = (query.strip().lower(), max_results)
    if session is None:
        hit = _RESULT_CACHE.get(key)
        if hit and _t.time() - hit[0] < 300:
            return [dict(r) for r in hit[1]]
    results = _fetch_results(query, max_results, session, DDGS)
    if session is None and results:
        _RESULT_CACHE[key] = (_t.time(), results)
        if len(_RESULT_CACHE) > 100:
            _RESULT_CACHE.pop(next(iter(_RESULT_CACHE)))
    return results


_RESULT_CACHE: dict = {}


def _fetch_results(query: str, max_results: int, session, DDGS) -> list[dict]:  # noqa: N803
    if DDGS is not None and session is None:
        try:
            with DDGS() as ddgs:
                return [{"title": r.get("title", ""), "url": r.get("href") or r.get("url", ""), "snippet": r.get("body", "")}
                        for r in ddgs.text(query, max_results=max_results)]
        except Exception as exc:
            log.info("ddgs search failed, trying the HTML endpoint: %s", exc)
    return _duckduckgo_html(query, max_results, session)


_DDG_RESULT = re.compile(r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>.*?'
                         r'(?:<a[^>]+class="result__snippet"[^>]*>(?P<snip>.*?)</a>)?', re.S)


def _duckduckgo_html(query: str, max_results: int, session: Optional[requests.Session] = None) -> list[dict]:
    sess = session or requests.Session()
    try:
        resp = sess.get("https://html.duckduckgo.com/html/", params={"q": query}, timeout=15,
                        headers={"User-Agent": USER_AGENT})
        if getattr(resp, "status_code", 200) >= 400:
            return []
        body = resp.text
    except Exception as exc:
        log.info("search result fetch failed: %s", exc)
        return []
    out = []
    for m in _DDG_RESULT.finditer(body):
        href = html.unescape(m.group("href"))
        if "uddg=" in href:
            href = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0])
        if href.startswith("//"):
            href = "https:" + href
        if not href.startswith("http") or "duckduckgo.com/y.js" in href:
            continue  # ads
        strip = lambda x: html.unescape(re.sub(r"<[^>]+>", "", x or "")).strip()  # noqa: E731
        out.append({"title": strip(m.group("title"))[:200], "url": href, "snippet": strip(m.group("snip"))[:400]})
        if len(out) >= max_results:
            break
    return out


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


def read_pages(results: list[dict], n: int = 3, session: Optional[requests.Session] = None, chars: int = 3000) -> list[dict]:
    """Download the top *n* result pages **in parallel** and attach their text (untrusted) as ``page_text``."""
    import concurrent.futures

    picked = [r for r in results if str(r.get("url", "")).startswith("http")][:n]

    def one(r):
        try:
            _title, text = fetch_page_text(r["url"], timeout=12, max_bytes=800_000, session=session)
            return r["url"], " ".join(text.split())[:chars]
        except Exception:
            return r["url"], ""

    if not picked:
        return results
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(picked))) as pool:
        texts = dict(pool.map(one, picked))
    return [dict(r, page_text=texts.get(r.get("url"), "")) if texts.get(r.get("url")) else r for r in results]

