"""Computer-use tools: see and operate *any* desktop application through the accessibility tree, keyboard and mouse.

These are deliberately generic (observe / click / type / press / scroll / copy / paste / select / drag) so the planner
and the LLM can combine them for applications Genesis has never seen. Safety rules live in services/computer.py
(terminals, password fields and dangerous shortcuts are refused; send/submit/pay/delete clicks and Enter in chat apps
are HIGH risk → always confirmed).
"""
from __future__ import annotations

import time
from typing import Optional

from ..security.permissions import RiskLevel
from ..services.computer import ComputerRefused, ComputerUnavailable, Element, action_risk, is_messaging, parse_keys
from .base import Tool, ToolResult, obj, s

RISK = {"low": RiskLevel.LOW, "medium": RiskLevel.MEDIUM, "high": RiskLevel.HIGH}
TARGET = s("What to act on, by its visible name or description: 'Save', 'the search box', 'the second button'", maxLength=300)


def _ctl(ctx):
    c = getattr(ctx, "computer", None)
    if c is None or not c.available():
        raise ComputerUnavailable("Mouse and keyboard control is only available on Windows (and needs the Genesis app running).")
    return c


def _fail(exc: Exception) -> ToolResult:
    if isinstance(exc, ComputerRefused):
        return ToolResult.fail(str(exc))
    return ToolResult.fail(str(exc), hint="On Windows, `pip install uiautomation` lets me see the buttons and fields in apps.")


def _resolve(ctx, args) -> Optional[Element]:
    """The element to act on — always re-checked against the window as it is *now* (ids from an earlier look can go stale)."""
    c = _ctl(ctx)
    if args.get("element_id") is not None:
        old = next((e for e in c.last_elements if e.id == int(args["element_id"])), None)
        name = (args.get("target") or (old.name if old else "")).strip()
        if not name:
            raise ComputerRefused("That control isn't in the window any more — it may have changed.")
        c.observe()
        same = [e for e in c.last_elements if e.name.lower() == name.lower() and e.enabled]
        if not same:
            raise ComputerRefused(f"“{name}” isn't in the active window any more, so I didn't click anything.")
        return next((e for e in same if e.id == int(args["element_id"])), same[0])
    if not args.get("target") and args.get("nth") is None:
        return None
    return c.find(args.get("target") or "", role=args.get("role") or "", nth=args.get("nth"), refresh=True)


def _changed(ctx, before: dict) -> Optional[bool]:
    """Observe after acting: did the window or its controls change? (None = can't tell — never claimed as proof)."""
    try:
        time.sleep(0.25)
        after = _ctl(ctx).observe()
    except Exception:
        return None
    if after["window"] != before.get("window") or [e["name"] for e in after["elements"]] != before.get("names"):
        return True
    return None


class ComputerObserve(Tool):
    name = "computer_observe"
    category = "computer"
    description = ("Look at the active window: its title and the named buttons, links, fields and tabs in it (accessibility tree — "
                   "no screenshot). Use before clicking/typing in an unfamiliar app.")
    parameters = obj({"role": {"type": "string", "enum": ["all", "button", "link", "field", "tab", "checkbox", "list item", "menu item"],
                               "default": "all"}})
    output = {"window": "object(title, process)", "elements": "list[{id, name, role, enabled}]"}

    def run(self, args, ctx):
        try:
            info = _ctl(ctx).observe()
        except (ComputerUnavailable, ComputerRefused) as exc:
            return _fail(exc)
        role = args.get("role", "all")
        els = [e for e in info["elements"] if role == "all" or e["role"] == role]
        title = info["window"]["title"] or info["window"]["process"] or "the active window"
        if not info["accessible"]:
            return ToolResult.ok(f"The active window is “{title}”, but it doesn't expose its controls to me. I can still type, "
                                 "press keys and scroll, or look at the screen if you ask “what's on my screen?”.",
                                 window=info["window"], elements=[])
        shown = [e for e in els if e["role"] != "text"][:20] or els[:20]
        lines = "\n".join(f"{i + 1}. {e['name']} ({e['role']})" + ("" if e["enabled"] else " — disabled") for i, e in enumerate(shown))
        res = ToolResult.ok(f"In “{title}” I can see:\n{lines}", window=info["window"],
                            elements=[dict(e, source="desktop") for e in shown])
        res.untrusted_content = True  # names come from the app/web page
        return res


class ComputerClick(Tool):
    name = "computer_click"
    category = "computer"
    description = ("Click a control in the active window by name/description ('Save', 'the second button', 'Next'). Uses "
                   "accessibility first, the control's position second. x/y coordinates only as a last resort. Set "
                   "button='right' for a context menu or double=true to open items.")
    parameters = obj({"target": TARGET, "role": s("button/link/field/tab/checkbox (optional)", maxLength=30),
                      "nth": {"type": "integer", "minimum": -1, "maximum": 50}, "element_id": {"type": "integer", "minimum": 1},
                      "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                      "double": {"type": "boolean", "default": False},
                      "x": {"type": "integer", "minimum": 0}, "y": {"type": "integer", "minimum": 0}})
    output = {"method": "accessibility | element-position | coordinates", "element": "{id, name, role}"}

    def _target_name(self, args, ctx) -> str:
        try:
            el = _resolve(ctx, args)
            return el.name if el else ""
        except Exception:
            return args.get("target") or ""

    @staticmethod
    def _key(args) -> str:
        return repr(sorted((k, v) for k, v in args.items()))

    def risk_for(self, args, ctx=None):
        coords = args.get("x") is not None and not args.get("target") and args.get("element_id") is None
        name = self._target_name(args, ctx) if ctx is not None and not coords else (args.get("target") or "")
        risk = RISK[action_risk("click", target_name=name, coordinates=coords, double=bool(args.get("double")))]
        if ctx is not None and risk != RiskLevel.LOW:
            prev = ctx.extras.get("computer_click_window") or {}
            if prev.get("key") != self._key(args) or time.time() - prev.get("at", 0) > 300:
                try:  # remember which window the user is approving the click in (the first assessment, not a re-check)
                    w = _ctl(ctx).window()
                    ctx.extras["computer_click_window"] = {"key": self._key(args), "handle": w.handle, "title": w.title,
                                                           "at": time.time()}
                except Exception:
                    pass
        return risk

    def confirmation_prompt(self, args):
        if args.get("x") is not None and not args.get("target"):
            return f"Click the screen at ({args['x']}, {args['y']})? I can't see a named control there, so please make sure."
        what = args.get("target") or f"control #{args.get('element_id')}"
        return f"Click “{what}” in the active window?"

    def run(self, args, ctx):
        try:
            c = _ctl(ctx)
            approved = (getattr(ctx, "extras", None) or {}).pop("computer_click_window", None)
            if approved and approved.get("key") == self._key(args):
                now = c.window()
                if (approved.get("handle") and now.handle != approved["handle"]) or now.title != approved.get("title"):
                    return ToolResult.fail(f"The window changed since you approved the click (it was “{approved.get('title')}”), "
                                           "so I didn't click anything.")
            el = _resolve(ctx, args)
            if el is None and not c.last_elements:
                c.observe()
            before = {"window": (c.last_window or c.window()).public(), "names": [e.name for e in c.last_elements]}
            if el is None and args.get("x") is None:
                return ToolResult.fail("What should I click?")
            info = c.click(el, x=args.get("x"), y=args.get("y"), button=args.get("button", "left"),
                           count=2 if args.get("double") else 1)
        except (ComputerUnavailable, ComputerRefused) as exc:
            return _fail(exc)
        what = f"“{el.name}”" if el else f"({args['x']}, {args['y']})"
        verb = "Right-clicked" if args.get("button") == "right" else "Double-clicked" if args.get("double") else "Clicked"
        return ToolResult.ok(f"{verb} {what}.", before=before, **info)

    def verify(self, args, result, ctx):
        return _changed(ctx, result.data.get("before") or {}) if result.success else False


class ComputerType(Tool):
    name = "computer_type"
    category = "computer"
    description = ("Type text into the active window — into a named field if `field` is given, otherwise where the cursor is. "
                   "Never presses Enter by itself (use computer_press). replace=true clears the field first.")
    parameters = obj({"text": s("Text to type", maxLength=20_000), "field": s("Field name/description (optional)", maxLength=200),
                      "replace": {"type": "boolean", "default": False}}, ["text"])
    output = {"method": "accessibility-value | keystrokes", "field": "str", "chars": "int"}

    def confirmation_prompt(self, args):
        return f"Type “{args['text'][:80]}” into {args.get('field') or 'the active window'}?"

    def run(self, args, ctx):
        try:
            c = _ctl(ctx)
            el = c.find(args["field"], role="field") if args.get("field") else None
            info = c.type(args["text"], el, replace=bool(args.get("replace")))
        except (ComputerUnavailable, ComputerRefused) as exc:
            return _fail(exc)
        where = f" into “{info['field']}”" if info.get("field") else ""
        return ToolResult.ok(f"Typed it{where}.", **info)

    def verify(self, args, result, ctx):
        if not result.success or result.data.get("method") != "accessibility-value":
            return None
        try:
            el = _ctl(ctx).find(args["field"], role="field")
            return (el.value or "").strip() == args["text"].strip()[:200] or None
        except Exception:
            return None


class ComputerPress(Tool):
    name = "computer_press"
    category = "computer"
    description = ("Press a key or shortcut in the active window: 'enter', 'esc', 'tab', 'ctrl+s', 'alt+left' (back), "
                   "'ctrl+t'. Enter in chat/e-mail apps sends a message, so it is confirmed first.")
    parameters = obj({"keys": s("Key or combination, e.g. 'ctrl+s'", maxLength=60),
                      "times": {"type": "integer", "minimum": 1, "maximum": 20, "default": 1}}, ["keys"])
    output = {"keys": "str", "window": "str"}

    def risk_for(self, args, ctx=None):
        win = None
        try:
            win = _ctl(ctx).window() if ctx is not None else None
        except Exception:
            pass
        self._last_window = win
        return RISK[action_risk("press", window=win, keys=parse_keys(args["keys"]))]

    def confirmation_prompt(self, args):
        keys = parse_keys(args["keys"])
        if "enter" in keys or set(keys) in ({"alt", "s"}, {"ctrl", "enter"}):
            win = getattr(self, "_last_window", None)
            if win is not None and is_messaging(win):
                return f"Press {'+'.join(k.capitalize() for k in keys)} in “{win.title}”? In this app that sends the message."
            where = f" in “{win.title}”" if win is not None and win.title else " in the active window"
            return f"Press {'+'.join(k.capitalize() for k in keys)}{where}? That may submit or confirm something there."
        return f"Press {'+'.join(k.capitalize() for k in keys)} in the active window?"

    def run(self, args, ctx):
        keys = parse_keys(args["keys"])
        if not keys:
            return ToolResult.fail("Which key should I press?")
        try:
            c = _ctl(ctx)
            info = {}
            for _ in range(int(args.get("times", 1))):
                info = c.press(keys)
                time.sleep(0.05)
        except (ComputerUnavailable, ComputerRefused) as exc:
            return _fail(exc)
        return ToolResult.ok(f"Pressed {'+'.join(k.capitalize() if len(k) > 1 else k.upper() for k in keys)}"
                             + (f" {args['times']} times" if int(args.get("times", 1)) > 1 else "") + ".", **info)


class ComputerScroll(Tool):
    name = "computer_scroll"
    category = "computer"
    description = "Scroll the active window up, down, left or right."
    parameters = obj({"direction": {"type": "string", "enum": ["up", "down", "left", "right"], "default": "down"},
                      "amount": {"type": "integer", "minimum": 1, "maximum": 30, "default": 5}})

    def run(self, args, ctx):
        try:
            info = _ctl(ctx).scroll(args.get("direction", "down"), int(args.get("amount", 5)))
        except (ComputerUnavailable, ComputerRefused) as exc:
            return _fail(exc)
        return ToolResult.ok(f"Scrolled {info['direction']}.", **info)


class ComputerCopy(Tool):
    name = "computer_copy"
    category = "computer"
    description = "Copy the selected text in the active window (returns it), or put the given `text` on the clipboard."
    parameters = obj({"text": s("Text to place on the clipboard instead of copying the selection", maxLength=20_000)})
    output = {"text": "str (clipboard contents)"}

    def run(self, args, ctx):
        try:
            c = _ctl(ctx)
            if args.get("text"):
                c.driver.clipboard_set(args["text"])
                return ToolResult.ok("Copied to the clipboard.", text=args["text"], chars=len(args["text"]))
            info = c.copy()
        except (ComputerUnavailable, ComputerRefused) as exc:
            return _fail(exc)
        if not info["text"]:
            return ToolResult.fail("Nothing was selected, so there was nothing to copy.", hint="Select some text first, "
                                   "or say “select all” and then “copy”.")
        res = ToolResult.ok(f"Copied {len(info['text'])} characters.", **info)
        res.untrusted_content = True
        return res


class ComputerPaste(Tool):
    name = "computer_paste"
    category = "computer"
    description = "Paste the clipboard (or the given `text`) at the cursor in the active window."
    parameters = obj({"text": s("Text to paste (optional)", maxLength=20_000)})

    def run(self, args, ctx):
        try:
            info = _ctl(ctx).paste(args.get("text"))
        except (ComputerUnavailable, ComputerRefused) as exc:
            return _fail(exc)
        return ToolResult.ok("Pasted.", **info)


class ComputerSelect(Tool):
    name = "computer_select"
    category = "computer"
    description = "Select text: everything in the active control ('all'), or all text in a named field."
    parameters = obj({"field": s("Field to select in (optional)", maxLength=200)})

    def run(self, args, ctx):
        try:
            c = _ctl(ctx)
            if args.get("field"):
                el = c.find(args["field"], role="field")
                c.driver.focus_element(el) or c.click(el)
            info = c.select_all()
        except (ComputerUnavailable, ComputerRefused) as exc:
            return _fail(exc)
        return ToolResult.ok("Selected all the text" + (f" in “{args['field']}”." if args.get("field") else "."), **info)


class ComputerDrag(Tool):
    name = "computer_drag"
    category = "computer"
    description = "Drag one control onto another in the active window (e.g. a file onto a folder), by their names."
    parameters = obj({"source": TARGET, "destination": TARGET}, ["source", "destination"])
    risk = RiskLevel.MEDIUM

    def confirmation_prompt(self, args):
        return f"Drag “{args['source']}” onto “{args['destination']}”?"

    def run(self, args, ctx):
        try:
            c = _ctl(ctx)
            src = c.find(args["source"])
            dst = c.find(args["destination"], refresh=False)
            info = c.drag(src, dst)
        except (ComputerUnavailable, ComputerRefused) as exc:
            return _fail(exc)
        return ToolResult.ok(f"Dragged “{info['from']}” onto “{info['to']}”.", **info)


def messaging_foreground(ctx) -> bool:
    try:
        return is_messaging(_ctl(ctx).window())
    except Exception:
        return False


TOOLS = [ComputerObserve(), ComputerClick(), ComputerType(), ComputerPress(), ComputerScroll(), ComputerCopy(), ComputerPaste(),
         ComputerSelect(), ComputerDrag()]
