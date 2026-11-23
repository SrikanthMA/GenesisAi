"""Web search and browser automation tools."""
from __future__ import annotations

import re

from ..security.injection import scan_for_injection
from ..security.permissions import RiskLevel
from ..security.validators import ValidationError, validate_url
from ..services import web as websvc
from ..services.browser import BrowserUnavailable
from ..core.logging_setup import get_logger
from .base import Tool, ToolResult, obj, s


log = get_logger("tools.web")


class WebSearch(Tool):
    name = "web_search"
    category = "web"
    description = "Search the web: opens the results in the user's browser and (if available) returns top results as text."
    parameters = obj({"query": s("Search query", maxLength=500),
                      "open_browser": {"type": "boolean", "default": True},
                      "read_pages": {"type": "integer", "minimum": 0, "maximum": 5, "default": 0,
                                     "description": "Also read the top N result pages (in parallel) for better answers"}}, ["query"])
    remote = True

    def run(self, args, ctx):
        q = args["query"].strip()
        engine = ctx.settings.get("search.engine", "google")
        opened = False
        if args.get("open_browser", True):
            url = websvc.search_url(q, engine)
            opened = ctx.backend.open_url(url)
        results = websvc.fetch_results(q, max_results=5, session=ctx.extras.get("web_session"))
        if results and args.get("read_pages"):
            results = websvc.read_pages(results, int(args["read_pages"]), session=ctx.extras.get("web_session"))
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
        if not ctx.browser.available():
            ok = ctx.backend.open_url(url)
            return ToolResult.ok(f"Opened {url} in your browser (automation isn't installed, so I can't control the page).",
                                 url=url) if ok else ToolResult.fail("The browser didn't open.")
        try:
            info = ctx.browser.goto(url)
        except Exception as exc:
            from ..services.diagnostics import diagnose_url, explain

            diag = diagnose_url(url, session=ctx.extras.get("web_session"))
            res = ToolResult.fail(f"The page didn't load. Let me figure out why… {explain(diag)}", error=f"{type(exc).__name__}",
                                  diagnosis=diag)
            res.retryable = diag["cause"] in ("timeout", "site_ok")
            if diag["cause"] == "site_ok":
                res.recovery = {"tool": "open_url", "args": {"url": url}, "complete": True,
                                "reason": "The site is fine but my automated browser had trouble, so I opened it in your normal browser."}
            return res
        return ToolResult.ok(f"Opened {info.get('title') or url}.", **info)

    def verify(self, args, result, ctx):
        if not result.success or not ctx.browser.available():
            return None
        cur = ctx.browser.current_url()
        return bool(cur) and not cur.startswith(("chrome-error://", "about:blank")) or None


class DiagnoseConnection(Tool):
    name = "diagnose_connection"
    category = "web"
    description = ("Work out why a website (or the internet) isn't working: no internet, DNS problem, website down, timeout, or "
                   "a browser problem.")
    parameters = obj({"url": s("Website to check (optional — without it, checks the internet connection)", maxLength=2000)})
    output = {"cause": "online|offline|dns_failed|site_down|timeout|http_error|site_ok|invalid_url", "detail": "str", "fix": "str"}

    def run(self, args, ctx):
        from ..services.diagnostics import diagnose_network, diagnose_url, explain

        url = (args.get("url") or "").strip()
        diag = diagnose_url(url, session=ctx.extras.get("web_session")) if url else diagnose_network()
        ok = diag["cause"] in ("online", "site_ok")
        return ToolResult.ok(explain(diag), **diag) if ok else ToolResult.ok(explain(diag), problem=True, **diag)


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
                title = text = ""
                if ctx.browser.available() and ctx.browser.current_url():
                    try:
                        info = ctx.browser.goto(url)
                        info.update(ctx.browser.read())
                        title, text = info.get("title", ""), info.get("text", "")
                    except Exception as exc:  # recover: the automated browser failed → plain download
                        log.info("browser read failed, falling back to HTTP: %s", exc)
                if not text:
                    title, text = websvc.fetch_page_text(url)
            elif ctx.browser.available() and ctx.browser.current_url():
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


_SYNONYMS = {"apply": {"application", "apply", "submit"}, "application": {"apply", "application"}, "login": {"log", "sign", "signin"},
             "signin": {"sign", "log", "login"}, "search": {"search", "find"}, "next": {"next", "continue", "more"},
             "buy": {"buy", "purchase", "checkout", "cart"}}


def _tokens(text: str) -> set[str]:
    return {w.rstrip("s") for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 1}


def rank_elements(elements: list[dict], description: str, role: str = "") -> list[tuple[float, dict]]:
    """Score page/screen elements against a natural description ("the internship application button")."""
    want = _tokens(description) - {"the", "a", "an", "on", "page", "this", "button", "link", "field", "box", "tab", "one", "to", "for"}
    expanded = set(want)
    for w in want:
        expanded |= _SYNONYMS.get(w, set())
    out = []
    for el in elements:
        have = _tokens(el.get("name", ""))
        if not have:
            continue
        overlap = len(want & have) + 0.5 * len((expanded - want) & have)
        score = overlap / max(1, len(want))
        if role and el.get("role") == role:
            score += 0.25
        if score > 0:
            out.append((round(score, 3), el))
    return sorted(out, key=lambda x: -x[0])


class BrowserElements(Tool):
    name = "browser_list_elements"
    category = "browser"
    description = "See the current page in Genesis's browser: title, headings, buttons, links and fields (by name)."
    parameters = obj({"kind": {"type": "string", "enum": ["all", "buttons", "links", "fields", "headings"], "default": "all"}})
    remote = True

    def run(self, args, ctx):
        if not ctx.browser.available() or not ctx.browser.current_url():
            return ToolResult.fail("Nothing is open in Genesis's browser yet.", hint="Say “open <website> in your browser” first.")
        try:
            info = ctx.browser.elements()
        except Exception as exc:
            return ToolResult.fail(f"I couldn't read the page: {exc}", retryable=True)
        kind = args.get("kind", "all")
        role = {"buttons": "button", "links": "link", "fields": "field", "headings": "heading"}.get(kind)
        els = [e for e in info["elements"] if not role or e["role"] == role]
        shown = [e for e in els if e["role"] != "heading"][:15] if kind == "all" else els[:15]
        heads = [e["name"] for e in info["elements"] if e["role"] == "heading"][:4]
        lines = "\n".join(f"{i + 1}. {e['name']} ({e['role']})" for i, e in enumerate(shown))
        msg = f"On “{info['title']}”" + (f" — {'; '.join(heads)}" if heads and kind == "all" else "") + f":\n{lines or '(nothing clickable)'}"
        res = ToolResult.ok(msg, url=info["url"], title=info["title"], elements=[dict(e, source="browser") for e in shown])
        res.untrusted_content = True
        return res


class BrowserFind(Tool):
    name = "browser_find"
    category = "browser"
    description = "Find the element on the current page that best matches a description, e.g. 'the internship application button'."
    parameters = obj({"description": s("What to look for", maxLength=300)}, ["description"])
    remote = True

    def run(self, args, ctx):
        if not ctx.browser.available() or not ctx.browser.current_url():
            return ToolResult.fail("Nothing is open in Genesis's browser yet.", hint="Open the page first.")
        try:
            info = ctx.browser.elements(limit=150)
        except Exception as exc:
            return ToolResult.fail(f"I couldn't read the page: {exc}", retryable=True)
        role = "button" if "button" in args["description"].lower() else "link" if "link" in args["description"].lower() else ""
        ranked = rank_elements(info["elements"], args["description"], role)
        if not ranked:
            return ToolResult.fail(f"I don't see anything like “{args['description']}” on this page.",
                                   hint="Say “scroll down” and ask again, or “what's on this page?”.")
        best = ranked[0][1]
        others = [e for _, e in ranked[1:4]]
        msg = f"Found “{best['name']}” ({best['role']})" + (" — it's below the visible area." if not best.get("in_view", True) else ".")
        msg += " Say “click it” to press it."
        res = ToolResult.ok(msg, element=dict(best, source="browser"), alternatives=others, url=info["url"])
        res.untrusted_content = True
        return res


class BrowserScroll(Tool):
    name = "browser_scroll"
    category = "browser"
    description = "Scroll the page in Genesis's browser (down, up, top, bottom)."
    parameters = obj({"direction": {"type": "string", "enum": ["down", "up", "top", "bottom"], "default": "down"},
                      "amount": {"type": "integer", "minimum": 1, "maximum": 10, "default": 1}})

    def run(self, args, ctx):
        if not ctx.browser.available() or not ctx.browser.current_url():
            return ToolResult.fail("Nothing is open in Genesis's browser to scroll.")
        try:
            info = ctx.browser.scroll(args.get("direction", "down"), int(args.get("amount", 1)))
        except Exception as exc:
            return ToolResult.fail(f"Scrolling failed: {exc}")
        return ToolResult.ok("That's the bottom of the page." if info.get("at_bottom") and args.get("direction") != "up"
                             else f"Scrolled {args.get('direction', 'down')}.", **info)


class BrowserPress(Tool):
    name = "browser_press"
    category = "browser"
    description = "Press a key in Genesis's browser (Enter submits the focused form)."
    parameters = obj({"key": {"type": "string", "enum": ["Enter", "Tab", "Escape", "PageDown", "PageUp"]}}, ["key"])
    risk = RiskLevel.MEDIUM

    def confirmation_prompt(self, args):
        return f"Press {args['key']} on the current page?" + (" (this may submit a form)" if args["key"] == "Enter" else "")

    def run(self, args, ctx):
        if not ctx.browser.available() or not ctx.browser.current_url():
            return ToolResult.fail("Nothing is open in Genesis's browser.")
        try:
            info = ctx.browser.press(args["key"])
        except Exception as exc:
            return ToolResult.fail(f"That didn't work: {exc}")
        return ToolResult.ok(f"Pressed {args['key']}.", **info)


class BrowserClick(Tool):
    name = "browser_click"
    category = "browser"
    description = "Click a button or link in Genesis's visible browser window — by its visible text, or by element_id from browser_list_elements/browser_find."
    parameters = obj({"target": s("Visible text of the button/link", maxLength=200), "element_id": {"type": "integer", "minimum": 1},
                      "nth": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Click the Nth element of `kind`"},
                      "kind": {"type": "string", "enum": ["button", "link", "field", "any"], "default": "any"}})
    risk = RiskLevel.MEDIUM
    remote = True

    def confirmation_prompt(self, args):
        if args.get("nth") and not args.get("target"):
            words = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth"}
            return f"Click the {words.get(args['nth'], str(args['nth']) + 'th')} {args.get('kind', 'element').replace('any', 'item')} on the page?"
        return f"Click “{args.get('target') or 'that'}” on the current page?"

    def run(self, args, ctx):
        if not args.get("target") and not args.get("element_id") and not args.get("nth"):
            return ToolResult.fail("What should I click?")
        if args.get("nth") and not args.get("element_id"):
            try:
                els = ctx.browser.elements()["elements"]
            except Exception as exc:
                return ToolResult.fail(f"I couldn't read the page: {exc}")
            kind = args.get("kind", "any")
            pool = [e for e in els if (kind == "any" and e["role"] != "heading") or e["role"] == kind]
            if len(pool) < int(args["nth"]):
                return ToolResult.fail(f"There are only {len(pool)} {kind if kind != 'any' else 'item'}s on this part of the page.")
            pick = pool[int(args["nth"]) - 1]
            args = dict(args, element_id=pick["id"], target=pick["name"])
        try:
            if args.get("element_id"):
                info = ctx.browser.click_element(int(args["element_id"]))
                info["clicked"] = args.get("target") or "it"
                return ToolResult.ok(f"Clicked “{info['clicked']}” — now on “{info.get('title') or info.get('url')}”.", **info)
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


class BrowserBack(Tool):
    name = "browser_back"
    category = "browser"
    description = "Go back one page in Genesis's automated browser window."
    parameters = obj({})

    def run(self, args, ctx):
        if not ctx.browser.available() or not ctx.browser.current_url():
            return ToolResult.fail("I can only go back in pages I opened with Genesis's own browser. Press Alt+← in your browser.")
        try:
            info = ctx.browser.go_back()
        except Exception as exc:
            return ToolResult.fail(f"Going back didn't work: {exc}")
        return ToolResult.ok(f"Went back to {info.get('title') or info.get('url')}.", **info)


class AnalyzeContent(Tool):
    name = "llm_analyze"
    category = "reasoning"
    description = ("Reason over content gathered by earlier steps (compare options, extract requirements, summarise). "
                   "No side effects; content is treated as untrusted data.")
    parameters = obj({"instruction": s("What to do with the content", maxLength=2000),
                      "content": s("Material from earlier steps", maxLength=60_000),
                      "allow_own_knowledge": {"type": "boolean", "default": False,
                                              "description": "If the material is empty, answer from general knowledge (and say so)"}},
                     ["instruction", "content"])
    remote = True

    def run(self, args, ctx):
        import re

        from ..ai.llm.base import LLMError
        from ..security.injection import wrap_untrusted

        if not getattr(ctx.llm, "is_generative", False):
            return ToolResult.fail("Comparing and summarising needs the language model, which is offline right now.",
                                   hint="I opened the search results in your browser instead.")
        thin = len(re.sub(r"^(Search results:)?\s*(\[\])?\s*$", "", args["content"].strip())) < 40
        if thin and not args.get("allow_own_knowledge"):
            return ToolResult.fail("I didn't get enough information from the search to compare anything.",
                                   hint="Install the optional 'ddgs' package so I can read search results.")
        if thin:
            try:
                out = ctx.llm.chat([{"role": "system", "content": "Be concise and practical."},
                                    {"role": "user", "content": args["instruction"].replace("using ONLY the search results", "")}],
                                   max_tokens=600, temperature=0.3)
            except LLMError as exc:
                return ToolResult.fail(f"The language model couldn't answer: {exc}", retryable=True)
            text = out.strip() + "\n(I couldn't read the search results, so this is from my own knowledge — the results are open in your browser.)"
            return ToolResult.ok(text, analysis=text, from_knowledge=True)
        try:
            out = ctx.llm.chat([{"role": "system", "content": "Be concise and factual. Use bullet points. Say when information is missing."},
                                {"role": "user", "content": args["instruction"] + "\n\n" + wrap_untrusted(args["content"], "search results")}],
                               max_tokens=800, temperature=0.3)
        except LLMError as exc:
            res = ToolResult.fail(f"The language model couldn't answer: {exc}")
            res.retryable = True
            return res
        return ToolResult.ok(out.strip(), analysis=out.strip())


TOOLS = [DiagnoseConnection(), WebSearch(), BrowserOpen(), BrowserReadPage(), BrowserClick(), BrowserFill(), BrowserBack(), AnalyzeContent(),
         BrowserElements(), BrowserFind(), BrowserScroll(), BrowserPress()]
