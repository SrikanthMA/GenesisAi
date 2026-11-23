"""Desktop window management and screenshots."""
from __future__ import annotations

import time
from pathlib import Path

from ..services.app_catalog import lookup
from ..services.platform import PlatformError
from ..services.screen import ScreenUnavailable
from .base import Tool, ToolResult, obj, s


def _match(windows: list[dict], name: str) -> list[dict]:
    """Find windows by app name or title fragment ("chrome", "vs code", "notepad", "the gmail one")."""
    n = name.lower().strip().removeprefix("the ").removesuffix(" window").removesuffix(" one").strip()
    words = {n}
    found = lookup(n)
    if found:
        words.add(str(found[1].get("name", "")).lower())
        words.update(a.lower() for a in found[1].get("aliases", []))
    words.discard("")
    return [w for w in windows if any(x in w["title"].lower() for x in words)]


class ListWindows(Tool):
    name = "list_windows"
    category = "windows"
    description = "List the open windows on the desktop."
    parameters = obj({})

    def run(self, args, ctx):
        try:
            wins = ctx.backend.list_windows()
        except PlatformError as exc:
            return ToolResult.fail(str(exc))
        if not wins:
            return ToolResult.ok("No windows are open.", windows=[])
        lines = "\n".join(f"{i + 1}. {w['title']}" + ("  (active)" if w.get("active") else "") for i, w in enumerate(wins[:15]))
        return ToolResult.ok(f"Open windows:\n{lines}", windows=wins)


class ManageWindow(Tool):
    name = "manage_window"
    category = "windows"
    description = "Switch to, minimize, maximize or restore a window, found by app name or title (e.g. 'chrome', 'notepad')."
    parameters = obj({"window": s("App name or part of the window title", maxLength=200),
                      "action": {"type": "string", "enum": ["focus", "minimize", "maximize", "restore"]},
                      "window_id": {"type": "integer"}}, ["action"])

    def run(self, args, ctx):
        try:
            wins = ctx.backend.list_windows()
            if args.get("window_id"):
                matches = [w for w in wins if w["id"] == args["window_id"]]
            elif args.get("window"):
                matches = _match(wins, args["window"])
            else:
                matches = [w for w in wins if w.get("active")]
            if not matches:
                return ToolResult.fail(f"I can't find an open window for “{args.get('window') or 'that'}”.",
                                       hint="Say “list my windows” to see what's open.")
            # windows are listed in Z-order, so the first match is the one used most recently
            target = next((m for m in matches if m.get("active")), matches[0])
            ctx.backend.window_action(target["id"], args["action"])
        except PlatformError as exc:
            return ToolResult.fail(str(exc))
        verb = {"focus": "Switched to", "minimize": "Minimized", "maximize": "Maximized", "restore": "Restored"}[args["action"]]
        return ToolResult.ok(f"{verb} “{target['title']}”.", window_id=target["id"], title=target["title"], action=args["action"])

    def verify(self, args, result, ctx):
        if not result.success or args["action"] not in ("focus", "maximize"):
            return None
        try:
            return any(w["id"] == result.data["window_id"] and w.get("active") for w in ctx.backend.list_windows()) or None
        except PlatformError:
            return None


class ShowDesktop(Tool):
    name = "show_desktop"
    category = "windows"
    description = "Minimize all windows (show the desktop)."
    parameters = obj({})

    def run(self, args, ctx):
        try:
            ctx.backend.minimize_all()
        except PlatformError as exc:
            return ToolResult.fail(str(exc))
        return ToolResult.ok("Minimized everything — here's your desktop.")


class TakeScreenshot(Tool):
    name = "take_screenshot"
    category = "windows"
    description = "Take a screenshot and save it as a PNG in Pictures/Screenshots (stays on this PC)."
    parameters = obj({"open_after": {"type": "boolean", "default": False}})

    def run(self, args, ctx):
        if getattr(ctx, "screen", None) is None:
            return ToolResult.fail("Screen capture isn't available in this build.")
        folder = Path.home() / "Pictures" / "Screenshots"
        path = folder / f"Genesis_{time.strftime('%Y%m%d_%H%M%S')}.png"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            size = ctx.screen.save_screenshot(path)
        except ScreenUnavailable as exc:
            return ToolResult.fail(str(exc))
        except OSError as exc:
            return ToolResult.fail(f"I couldn't save the screenshot: {exc}")
        if args.get("open_after"):
            ctx.backend.open_path(str(path))
        return ToolResult.ok(f"Saved a screenshot to {path.name} in Pictures\\Screenshots.", path=str(path), size=list(size))

    def verify(self, args, result, ctx):
        return bool(result.success and Path(result.data["path"]).is_file()) if result.success else False


class OpenTerminal(Tool):
    name = "open_terminal"
    category = "windows"
    description = "Open a terminal window (Windows Terminal / PowerShell) in a folder, e.g. the current project."
    parameters = obj({"path": s("Folder to open the terminal in", maxLength=1000)})

    def run(self, args, ctx):
        import shutil
        import sys

        from ..security.validators import ValidationError, validate_path

        try:
            folder = validate_path(args.get("path") or str(Path.home()), allowed_roots=ctx.allowed_roots(), must_exist=True)
        except ValidationError as exc:
            return ToolResult.fail(str(exc))
        if folder.is_file():
            folder = folder.parent
        if sys.platform.startswith("win"):
            wt = shutil.which("wt")
            argv = [wt, "-d", "."] if wt else [shutil.which("powershell") or "powershell.exe", "-NoExit"]
        else:
            argv = [shutil.which("x-terminal-emulator") or shutil.which("gnome-terminal") or "xterm"]
        try:
            ctx.backend.launch(argv, cwd=str(folder))  # the folder is passed as the working directory, never parsed as a command
        except PlatformError as exc:
            return ToolResult.fail(f"I couldn't open a terminal: {exc}")
        return ToolResult.ok(f"Opened a terminal in {folder.name}.", path=str(folder))


TOOLS = [ListWindows(), ManageWindow(), ShowDesktop(), TakeScreenshot(), OpenTerminal()]
