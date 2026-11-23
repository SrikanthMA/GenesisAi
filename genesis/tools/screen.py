"""Screen-understanding tools. Capture only on explicit request; interaction only by element name, with confirmation."""
from __future__ import annotations

from ..security.injection import wrap_untrusted
from ..security.permissions import RiskLevel
from ..services.screen import ScreenUnavailable
from .base import Tool, ToolResult, obj, s


def _disabled(ctx) -> ToolResult | None:
    if not ctx.settings.get("screen.enabled", True):
        return ToolResult.fail("Screen analysis is turned off in Settings → Privacy.")
    if getattr(ctx, "screen", None) is None:
        return ToolResult.fail("Screen understanding isn't available in this build.")
    return None


class AnalyzeScreen(Tool):
    name = "analyze_screen"
    category = "screen"
    description = "Capture the screen once (only when asked) and describe/explain what's visible, e.g. an error message."
    parameters = obj({"question": s("What the user wants to know about the screen", maxLength=500)})

    def risk_for(self, args, ctx=None):
        # a screenshot sent to a cloud vision model is private data leaving the PC → ask first
        if ctx is not None and getattr(ctx, "screen", None) is not None and ctx.screen.capabilities().get("vision") \
                and ctx.screen.capabilities().get("vision_remote"):
            return RiskLevel.MEDIUM
        return RiskLevel.LOW

    def confirmation_prompt(self, args):
        return "To answer that I'll take one screenshot and send it to the vision model online. Is that okay?"

    def run(self, args, ctx):
        bad = _disabled(ctx)
        if bad:
            return bad
        q = args.get("question") or "Describe what is on the screen and point out any error messages."
        try:
            a = ctx.screen.analyze(q)
        except ScreenUnavailable as exc:
            return ToolResult.fail(str(exc))
        errors = [ln for ln in a.text_lines if any(w in ln.lower() for w in ("error", "exception", "failed", "traceback", "denied"))]
        parts = []
        if a.description:
            parts.append(a.description)
        elif errors:
            parts.append("I can see this on screen: " + " / ".join(errors[:3]))
        elif a.window_title:
            parts.append(f"The active window is “{a.window_title}”" + (f" with {len(a.elements)} controls." if a.elements else "."))
        elif a.text_lines:
            parts.append("Visible text: " + " / ".join(a.text_lines[:5]))
        else:
            parts.append("I captured the screen but couldn't read anything useful (install Tesseract OCR or set up a vision model).")
        explanation = None
        if not a.description and errors and getattr(ctx.llm, "is_generative", False):
            try:
                explanation = ctx.llm.chat([{"role": "user", "content": "Explain this error briefly and suggest a fix:\n"
                                             + wrap_untrusted("\n".join(errors[:10]), "screen")}], max_tokens=300)
                parts.append(explanation)
            except Exception:
                pass
        res = ToolResult.ok("\n".join(parts), **a.public(), errors=errors)
        res.untrusted_content = True
        return res


class ReadScreenText(Tool):
    name = "read_screen_text"
    category = "screen"
    description = "Read the text currently visible on screen (local OCR / UI Automation)."
    parameters = obj({})

    def run(self, args, ctx):
        bad = _disabled(ctx)
        if bad:
            return bad
        try:
            a = ctx.screen.analyze(use_vision=False)
        except ScreenUnavailable as exc:
            return ToolResult.fail(str(exc))
        if not a.text_lines:
            return ToolResult.fail("I couldn't read any text on the screen (OCR isn't installed).",
                                   hint="Install Tesseract OCR and run: pip install pytesseract")
        res = ToolResult.ok("Here's the text I can see:\n" + "\n".join(a.text_lines[:25]), text_lines=a.text_lines)
        res.untrusted_content = True
        return res


class ListScreenElements(Tool):
    name = "list_screen_elements"
    category = "screen"
    description = "List named buttons/links/fields in the active window (Windows UI Automation)."
    parameters = obj({})

    def run(self, args, ctx):
        bad = _disabled(ctx)
        if bad:
            return bad
        try:
            a = ctx.screen.analyze(use_vision=False)
        except ScreenUnavailable as exc:
            return ToolResult.fail(str(exc))
        if not a.elements:
            return ToolResult.fail("I couldn't list the controls in this window.", hint="pip install uiautomation")
        names = [f"{e.name} ({e.control_type})" for e in a.elements if e.control_type in ("Button", "Hyperlink", "MenuItem",
                                                                                          "TabItem", "CheckBox", "ListItem")]
        return ToolResult.ok(f"In “{a.window_title}” I can see: " + ", ".join(names[:25]),
                             elements=[e.public() for e in a.elements])


class ClickScreenElement(Tool):
    name = "click_screen_element"
    category = "screen"
    description = "Click a named control (button/link) in the active window via UI Automation. Never uses raw coordinates."
    parameters = obj({"name": s("Visible name of the control, e.g. 'Save'", maxLength=200),
                      "nth": {"type": "integer", "minimum": 1, "maximum": 50}, "kind": {"type": "string", "enum": ["button", "link", "any"],
                                                                                         "default": "button"}})
    risk = RiskLevel.MEDIUM
    always_confirm = True

    def confirmation_prompt(self, args):
        if args.get("nth") and not args.get("name"):
            return f"Click {args.get('kind', 'button')} number {args['nth']} in the active window?"
        return f"Click “{args['name']}” in the active window?"

    def run(self, args, ctx):
        bad = _disabled(ctx)
        if bad:
            return bad
        if not args.get("name") and not args.get("nth"):
            return ToolResult.fail("What should I click?")
        if args.get("nth") and not args.get("name"):
            try:
                a = ctx.screen.analyze(use_vision=False)
            except ScreenUnavailable as exc:
                return ToolResult.fail(str(exc))
            want = {"button": ("Button",), "link": ("Hyperlink",), "any": ("Button", "Hyperlink", "MenuItem", "TabItem")}[args.get("kind", "button")]
            pool = [e for e in a.elements if e.control_type in want and e.enabled]
            if len(pool) < int(args["nth"]):
                return ToolResult.fail(f"I can only see {len(pool)} of those in the active window.")
            args = dict(args, name=pool[int(args["nth"]) - 1].name)
        try:
            matches = ctx.screen.find_element(args["name"])
        except ScreenUnavailable as exc:
            return ToolResult.fail(str(exc))
        buttons = [m for m in matches if m.enabled]
        if not buttons:
            return ToolResult.fail(f"I can't find an enabled control called “{args['name']}” in the active window.")
        if len(buttons) > 1 and len({b.name for b in buttons}) > 1:
            return ToolResult.fail("There are several matching controls: " + ", ".join(sorted({b.name for b in buttons})[:6])
                                   + ". Which one?")
        try:
            ctx.screen.click(buttons[0])
        except ScreenUnavailable as exc:
            return ToolResult.fail(str(exc))
        return ToolResult.ok(f"Clicked “{buttons[0].name}”.", element=buttons[0].public())


class FindScreenElement(Tool):
    name = "find_screen_element"
    category = "screen"
    description = "Find the control in the active window that best matches a description (UI Automation names; no clicking)."
    parameters = obj({"description": s("What to look for", maxLength=300)}, ["description"])

    def run(self, args, ctx):
        from .web import rank_elements

        bad = _disabled(ctx)
        if bad:
            return bad
        try:
            a = ctx.screen.analyze(use_vision=False)
        except ScreenUnavailable as exc:
            return ToolResult.fail(str(exc))
        role_of = {"Button": "button", "Hyperlink": "link", "Edit": "field", "MenuItem": "button", "TabItem": "button"}
        els = [{"name": e.name, "role": role_of.get(e.control_type, e.control_type.lower()), "source": "screen"} for e in a.elements]
        ranked = rank_elements(els, args["description"], "button" if "button" in args["description"].lower() else "")
        if not ranked:
            return ToolResult.fail(f"I can't see anything like “{args['description']}” in “{a.window_title or 'the active window'}”.")
        best = ranked[0][1]
        res = ToolResult.ok(f"Found “{best['name']}” in {a.window_title or 'the active window'}. Say “click it” to press it.",
                            element=best)
        res.untrusted_content = True
        return res


TOOLS = [AnalyzeScreen(), ReadScreenText(), ListScreenElements(), ClickScreenElement(), FindScreenElement()]
