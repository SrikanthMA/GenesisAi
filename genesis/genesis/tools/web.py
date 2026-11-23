"""Web search and browser automation tools."""
from __future__ import annotations

from ..security.injection import scan_for_injection
from ..security.permissions import RiskLevel
from ..security.validators import ValidationError, validate_url
from ..services import web as websvc
from ..services.browser import BrowserController, BrowserUnavailable
from .base import Tool, ToolResult, obj, s


class WebSearch(Tool):
    name = "web_search"
    category = "web"
    description = "Search the web: opens the results in the user's browser and (if available) returns top results as text."
    parameters = obj({"query": s("Search query", maxLength=500),
                      "open_browser": {"type": "boolean", "default": True}}, ["query"])
    remote = True

    def run(self, args, ctx):
        q = args["query"].strip()
        engine = ctx.settings.get("search.engine", "google")
        opened = False
        if args.get("open_browser", True):
            url = websvc.search_url(q, engine)
            opened = ctx.backend.open_url(url)
        results = websvc.fetch_results(q, max_results=5)
        if not opened and not results:
            return ToolResult.fail("I couldn't open the browser or fetch search results. Are you online?")
        msg = f"I searched {engine.capitalize()} for “{q}”" + (" — the results are open in your browser." if opened else ".")
        res = ToolResult.ok(msg, query=q, results=results, opened=opened, url=websvc.search_url(q, engine))
        res.untrusted_content = bool(results)
        return res


class BrowserOpen(Tool):
    name = "browser_open"
    category = "browser"
    description = "Open a URL in Genesis's visible automated browser window (for multi-step web tasks)."
    parameters = obj({"url": s("http(s) URL", maxLength=2000)}, ["url"])
    remote = True

    def run(self, args, ctx):
        try:
            url = validate_url(args["url"])
        except ValidationError as exc:
            return ToolResult.fail(f"I won't open that link: {exc}")
        if not BrowserController.available():
            ok = ctx.backend.open_url(url)
            return ToolResult.ok(f"Opened {url} in your browser (automation isn't installed, so I can't control the page).",
                                 url=url) if ok else ToolResult.fail("The browser didn't open.")
        try:
            info = ctx.browser.goto(url)
        except Exception as exc:
            return ToolResult.fail(f"The browser couldn't load {url}: {exc}")
        return ToolResult.ok(f"Opened {info.get('title') or url}.", **info)


class BrowserReadPage(Tool):
    name = "browser_read_page"
    category = "browser"
    description = "Read the text of a web page (the given URL, or the page open in Genesis's browser). Untrusted content."
    parameters = obj({"url": s("http(s) URL (optional)", maxLength=2000)})
    remote = True

    def run(self, args, ctx):
        url = args.get("url")
        try:
            if url:
                url = validate_url(url)
                if BrowserController.available() and ctx.browser.current_url():
                    info = ctx.browser.goto(url)
                    info.update(ctx.browser.read())
                    title, text = info.get("title", ""), info.get("text", "")
                else:
                    title, text = websvc.fetch_page_text(url)
            elif BrowserController.available() and ctx.browser.current_url():
                info = ctx.browser.read()
                url, title, text = info["url"], info.get("title", ""), info.get("text", "")
            else:
                return ToolResult.fail("Which page? Give me a URL, or open it with Genesis's browser first.")
        except ValidationError as exc:
            return ToolResult.fail(f"I won't open that link: {exc}")
        except Exception as exc:
            return ToolResult.fail(f"I couldn't read that page: {exc}")
        text = (text or "")[:60_000]
        scan = scan_for_injection(text)
        res = ToolResult.ok(f"Read “{title or url}” ({len(text):,} characters).", url=url, title=title, content=text,
                            suspicious=scan.suspicious)
        res.untrusted_content = True
        return res


class BrowserClick(Tool):
    name = "browser_click"
    category = "browser"
    description = "Click a button or link (by its visible text) in Genesis's visible browser window."
    parameters = obj({"target": s("Visible text of the button/link", maxLength=200)}, ["target"])
    risk = RiskLevel.MEDIUM
    remote = True

    def confirmation_prompt(self, args):
        return f"Click “{args['target']}” on the current page?"

    def run(self, args, ctx):
        try:
            info = ctx.browser.click(args["target"])
        except BrowserUnavailable as exc:
            return ToolResult.fail(str(exc))
        except Exception as exc:
            return ToolResult.fail(f"The click failed: {exc}")
        return ToolResult.ok(f"Clicked “{args['target']}”.", **info)


class BrowserFill(Tool):
    name = "browser_fill"
    category = "browser"
    description = "Type a value into a form field (by label/placeholder) in Genesis's visible browser window."
    parameters = obj({"target": s("Field label or placeholder", maxLength=200), "value": s("Text to enter", maxLength=5000)},
                     ["target", "value"])
    risk = RiskLevel.MEDIUM
    remote = True

    def confirmation_prompt(self, args):
        return f"Type “{args['value'][:60]}” into the “{args['target']}” field?"

    def run(self, args, ctx):
        try:
            ctx.browser.fill(args["target"], args["value"])
        except BrowserUnavailable as exc:
            return ToolResult.fail(str(exc))
        except Exception as exc:
            return ToolResult.fail(f"Filling the field failed: {exc}")
        return ToolResult.ok(f"Filled the “{args['target']}” field.", field=args["target"])


TOOLS = [WebSearch(), BrowserOpen(), BrowserReadPage(), BrowserClick(), BrowserFill()]
