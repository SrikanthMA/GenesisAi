"""Application and URL tools."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from ..security.permissions import RiskLevel
from ..security.validators import ValidationError, validate_url
from ..services.platform import PlatformError
from ..services.app_catalog import lookup, web_version

BROWSER_KEYS = {"chrome", "edge", "firefox", "brave"}
from .base import Tool, ToolResult, obj, s


BROWSER_PROCESSES = ["chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "chrome", "firefox", "msedge"]


def _wait_for(ctx, names: list[str], pid: Optional[int], seconds: float = 2.5) -> bool:
    """Observe: did the program actually start (or hand off to an already-running instance)?"""
    deadline = time.time() + seconds
    while True:
        if names and ctx.backend.find_processes(names):
            return True
        if pid and ctx.backend.process_alive(pid):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.25)


class OpenApplication(Tool):
    name = "open_application"
    category = "apps"
    description = "Launch an installed desktop application or a web app (e.g. chrome, vscode, spotify, gmail, youtube)."
    parameters = obj({"app": s("Application name, e.g. 'chrome', 'vs code', 'gmail'", maxLength=100)}, ["app"])
    risk = RiskLevel.LOW

    def run(self, args, ctx):
        name = args["app"].strip()
        resolver = ctx.apps
        candidates = resolver.resolve_all(name) if hasattr(resolver, "resolve_all") else [r for r in [resolver.resolve(name)] if r]
        if not candidates:
            web = web_version(name)
            if web:
                res = ToolResult.fail(f"{web[1]} isn't installed on this computer.")
                res.recovery = {"tool": "open_url", "args": {"url": web[2]}, "complete": True,
                                "reason": f"{web[1]} isn't installed here, so I opened the web version instead."}
                return res
            found = lookup(name)
            if found and found[0] in BROWSER_KEYS:
                res = ToolResult.fail(f"{found[1]['name']} isn't installed on this computer.")
                res.recovery = {"tool": "open_url", "args": {"url": "https://www.google.com/"}, "complete": True,
                                "reason": f"{found[1]['name']} isn't installed, so I opened your default browser instead."}
                return res
            return ToolResult.fail(
                f"I couldn't find an application called '{name}' on this computer — it doesn't seem to be installed.",
                hint=f"Check the name — or say “search the web for how to install {name}”.",
                diagnosis={"cause": "not_installed", "detail": f"{name} isn't installed", "fix": ""},
            )
        errors: list[str] = []
        for attempt, resolved in enumerate(candidates):
            try:
                if resolved.kind == "url":
                    url = validate_url(resolved.target)
                    if not ctx.backend.open_url(url):
                        errors.append("the default browser didn't respond")
                        continue
                    return ToolResult.ok(f"Done — {resolved.display_name} is open in your browser.", app=resolved.key, url=url,
                                         kind="url")
                pid = None
                if resolved.kind == "exe":
                    pid = ctx.backend.launch([resolved.target])
                elif resolved.kind == "uri":
                    ctx.backend.open_uri(resolved.target)
                else:
                    ctx.backend.open_path(resolved.target)
                if resolved.kind == "exe" and not _wait_for(ctx, resolved.process_names or [Path(resolved.target).name], pid):
                    errors.append(f"{Path(resolved.target).name} exited immediately")
                    continue
                prefix = "That didn't work the first time, so I tried another way — " if attempt else "Done — "
                return ToolResult.ok(f"{prefix}{resolved.display_name} is open.", app=resolved.key, pid=pid, kind=resolved.kind,
                                     target=resolved.target, process_names=resolved.process_names, attempts=attempt + 1)
            except (PlatformError, ValidationError) as exc:
                errors.append(str(exc))
                continue
        from ..services.diagnostics import diagnose_app

        disp = candidates[0].display_name
        diag = diagnose_app(disp, backend=ctx.backend, errors=errors, target=candidates[0].target)
        res = ToolResult.fail(f"I couldn't open {disp}. Let me figure out why… {diag['detail']}",
                              error="; ".join(errors[-2:]) or "it did not start", hint=diag.get("fix") or None, diagnosis=diag)
        web = web_version(name)
        if web:
            res.recovery = {"tool": "open_url", "args": {"url": web[2]}, "complete": True,
                            "reason": f"The {web[1]} app wouldn't start, so I opened the web version instead."}
        return res

    def verify(self, args, result, ctx):
        if not result.success:
            return False
        if result.data.get("kind") == "url":
            # the page itself can't be inspected, but a browser process must exist after opening it
            return True if ctx.backend.find_processes(BROWSER_PROCESSES) else None
        names = result.data.get("process_names") or []
        pid = result.data.get("pid")
        if (names and ctx.backend.find_processes(names)) or (pid and ctx.backend.process_alive(pid)):
            return True
        return None if result.data.get("kind") in ("uri", "shortcut") else False


class CloseApplication(Tool):
    name = "close_application"
    category = "apps"
    description = "Close a running application gracefully (unsaved work may prompt)."
    parameters = obj({"app": s("Application name", maxLength=100)}, ["app"])
    risk = RiskLevel.MEDIUM

    def confirmation_prompt(self, args):
        return f"This will close {args.get('app')} — any unsaved work in it could be lost. Continue?"

    def _process_names(self, name, ctx):
        from ..services.app_catalog import lookup

        found = lookup(name)
        if found and found[1].get("process"):
            return found[1]["process"], found[1].get("name", name)
        if found and found[1].get("url"):
            return None, found[1].get("name", name)
        base = name.strip().lower()
        return [f"{base}.exe", base], name

    def run(self, args, ctx):
        names, disp = self._process_names(args["app"], ctx)
        if names is None:
            return ToolResult.fail(f"{disp} is a website; close its browser tab instead.")
        if not ctx.backend.find_processes(names):
            return ToolResult.fail(f"{disp} doesn't seem to be running.")
        try:
            closed = ctx.backend.terminate_processes(names)
        except PlatformError as exc:
            return ToolResult.fail(f"I couldn't close {disp}: {exc}")
        if closed == 0:
            return ToolResult.fail(f"{disp} didn't close (it may be waiting for you to save something).")
        return ToolResult.ok(f"Done — {disp} is closed.", app=args["app"], processes=closed, names=names)

    def verify(self, args, result, ctx):
        if not result.success:
            return False
        return not ctx.backend.find_processes(result.data.get("names", []))


class OpenUrl(Tool):
    name = "open_url"
    category = "web"
    description = "Open an http(s) URL in the user's default browser."
    parameters = obj({"url": s("http(s) URL", maxLength=2000)}, ["url"])
    risk = RiskLevel.LOW

    def run(self, args, ctx):
        try:
            url = validate_url(args["url"])
        except ValidationError as exc:
            return ToolResult.fail(f"I won't open that link: {exc}")
        if not ctx.backend.open_url(url):
            return ToolResult.fail("Your default browser didn't respond.")
        return ToolResult.ok(f"Done — {url.split('//', 1)[-1].rstrip('/')} is open.", url=url)


TOOLS = [OpenApplication(), CloseApplication(), OpenUrl()]
