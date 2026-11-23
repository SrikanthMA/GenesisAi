"""Application and URL tools."""
from __future__ import annotations

import time

from ..security.permissions import RiskLevel
from ..security.validators import ValidationError, validate_url
from ..services.platform import PlatformError
from .base import Tool, ToolResult, obj, s


class OpenApplication(Tool):
    name = "open_application"
    category = "apps"
    description = "Launch an installed desktop application or a web app (e.g. chrome, vscode, spotify, gmail, youtube)."
    parameters = obj({"app": s("Application name, e.g. 'chrome', 'vs code', 'gmail'", maxLength=100)}, ["app"])
    risk = RiskLevel.LOW

    def run(self, args, ctx):
        name = args["app"].strip()
        resolved = ctx.apps.resolve(name)
        if resolved is None:
            return ToolResult.fail(
                f"I couldn't find an application called '{name}' on this computer.",
                hint="Check the name, or tell me the full path to the program.",
            )
        try:
            if resolved.kind == "url":
                url = validate_url(resolved.target)
                if not ctx.backend.open_url(url):
                    return ToolResult.fail(f"Your default browser didn't open {resolved.display_name}.")
                return ToolResult.ok(f"Opened {resolved.display_name}.", app=resolved.key, url=url, kind="url")
            if resolved.kind == "exe":
                pid = ctx.backend.launch([resolved.target])
                return ToolResult.ok(f"Opened {resolved.display_name}.", app=resolved.key, pid=pid, kind="exe",
                                     target=resolved.target)
            if resolved.kind in ("shortcut", "uri"):
                ctx.backend.open_uri(resolved.target) if resolved.kind == "uri" else ctx.backend.open_path(resolved.target)
                return ToolResult.ok(f"Opened {resolved.display_name}.", app=resolved.key, kind=resolved.kind)
        except (PlatformError, ValidationError) as exc:
            return ToolResult.fail(f"I couldn't open {resolved.display_name}: {exc}")
        return ToolResult.fail(f"I don't know how to launch {resolved.display_name}.")

    def verify(self, args, result, ctx):
        if not result.success:
            return False
        pid = result.data.get("pid")
        if pid:
            time.sleep(0.4)
            # Many launchers (Chrome, Code.exe) hand off to an existing instance and exit quickly;
            # an exited launcher is not a failure, so only a positive check counts as verified.
            return True if ctx.backend.process_alive(pid) else None
        return None


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
        return ToolResult.ok(f"Closed {disp}.", app=args["app"], processes=closed, names=names)

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
        return ToolResult.ok(f"Opened {url.split('//', 1)[-1].rstrip('/')}.", url=url)


TOOLS = [OpenApplication(), CloseApplication(), OpenUrl()]
