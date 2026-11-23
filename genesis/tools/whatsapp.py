"""WhatsApp tools. Drafting and sending are separate; sending is HIGH risk (always confirmed, never trusted away)."""
from __future__ import annotations

from ..security.permissions import RiskLevel
from ..services.whatsapp import WhatsAppError
from .base import Tool, ToolResult, obj, s


def _svc(ctx):
    svc = getattr(ctx, "whatsapp", None)
    if svc is None:
        raise WhatsAppError("WhatsApp isn't available in this session.", "unavailable")
    return svc


def _fail(exc: WhatsAppError) -> ToolResult:
    hint = {"login": "After linking, just ask again.",
            "unavailable": "Say “open WhatsApp” to use the app yourself."}.get(getattr(exc, "kind", ""), None)
    res = ToolResult.fail(str(exc), hint=hint, kind=getattr(exc, "kind", "error"))
    return res


def _fmt(m: dict, chat: str) -> str:
    who = "You" if not m.get("incoming") else (m.get("sender") or chat)
    when = f" ({m['time']})" if m.get("time") else ""
    return f"{who}{when}: {m.get('text') or '[media]'}"


class WhatsAppOpen(Tool):
    name = "whatsapp_open"
    category = "whatsapp"
    description = "Open WhatsApp (WhatsApp Web in Genesis's browser, or the desktop app) so chats can be read and messages drafted."
    parameters = obj({})
    remote = True

    def run(self, args, ctx):
        try:
            info = _svc(ctx).open()
        except WhatsAppError as exc:
            res = _fail(exc)
            if exc.kind in ("unavailable", "error"):  # automation failed → open WhatsApp the normal way
                res.recovery = {"tool": "open_application", "args": {"app": "whatsapp"}, "complete": True,
                                "reason": "I couldn't use my automated browser, so I opened WhatsApp normally."}
            return res
        if not info.get("logged_in"):
            return ToolResult.ok("WhatsApp is open. It isn't linked yet: on your phone open WhatsApp → Linked devices → "
                                 "Link a device, and scan the QR code on screen. I'll remember it after that.",
                                 logged_in=False, mode=info.get("mode"))
        return ToolResult.ok("Done — WhatsApp is open.", logged_in=True, mode=info.get("mode"))


class WhatsAppRead(Tool):
    name = "whatsapp_read"
    category = "whatsapp"
    description = "Read the latest messages in a WhatsApp chat (by contact/group name), newest last."
    parameters = obj({"chat": s("Contact or group name, e.g. 'Mom'", maxLength=120),
                      "count": {"type": "integer", "minimum": 1, "maximum": 30, "default": 5},
                      "incoming_only": {"type": "boolean", "default": False}})
    output = {"chat": "str", "messages": "list[{incoming, text, time, sender}]"}
    remote = True

    def run(self, args, ctx):
        try:
            info = _svc(ctx).read(args.get("chat"), int(args.get("count", 5)) * (3 if args.get("incoming_only") else 1))
        except WhatsAppError as exc:
            return _fail(exc)
        msgs = info["messages"]
        if args.get("incoming_only"):
            msgs = [m for m in msgs if m.get("incoming")][-int(args.get("count", 5)):]
        if not msgs:
            return ToolResult.ok(f"There are no {'messages from ' + info['chat'] if args.get('incoming_only') else 'messages'} "
                                 f"in your chat with {info['chat']} yet.", chat=info["chat"], messages=[])
        if len(msgs) == 1:
            text = f"The latest message from {info['chat']}:\n{_fmt(msgs[0], info['chat'])}"
        else:
            text = f"Latest messages with {info['chat']}:\n" + "\n".join(_fmt(m, info["chat"]) for m in msgs)
        res = ToolResult.ok(text, chat=info["chat"], messages=msgs, last=msgs[-1])
        res.untrusted_content = True  # message text is someone else's content
        return res


class WhatsAppUnread(Tool):
    name = "whatsapp_unread"
    category = "whatsapp"
    description = "List WhatsApp chats with unread messages."
    parameters = obj({})
    remote = True

    def run(self, args, ctx):
        try:
            items = _svc(ctx).unread()
        except WhatsAppError as exc:
            return _fail(exc)
        if not items:
            return ToolResult.ok("No unread WhatsApp messages.", chats=[])
        lines = "\n".join(f"{i + 1}. {c['chat']} ({c['unread']})" for i, c in enumerate(items[:10]))
        res = ToolResult.ok(f"Unread WhatsApp chats:\n{lines}", chats=items)
        res.untrusted_content = True
        return res


class WhatsAppDraft(Tool):
    name = "whatsapp_draft"
    category = "whatsapp"
    description = "Open a WhatsApp chat and type a message into the box WITHOUT sending it (sending is whatsapp_send)."
    parameters = obj({"chat": s("Contact or group name", maxLength=120), "text": s("The message, as it should be sent", maxLength=4000)},
                     ["chat", "text"])
    output = {"chat": "str", "text": "str"}
    remote = True

    def run(self, args, ctx):
        try:
            info = _svc(ctx).draft(args["chat"], args["text"])
        except WhatsAppError as exc:
            return _fail(exc)
        return ToolResult.ok(f"I've drafted the message to {info['chat']}: “{info['text']}”", **info)

    def verify(self, args, result, ctx):
        if not result.success:
            return False
        try:
            d = _svc(ctx).driver()
            return d.current_chat().lower() == result.data["chat"].lower() and (d.draft_text() or "").strip() == args["text"].strip()
        except Exception:
            return None


class WhatsAppSend(Tool):
    name = "whatsapp_send"
    category = "whatsapp"
    description = "Send a WhatsApp message to a chat (re-checks the recipient and exact text first). Always asks for confirmation."
    parameters = obj({"chat": s("Contact or group name", maxLength=120), "text": s("The exact message", maxLength=4000)},
                     ["chat", "text"])
    output = {"sent": "bool", "status": "str"}
    risk = RiskLevel.HIGH
    always_confirm = True
    remote = True

    def confirmation_prompt(self, args):
        return f"I've drafted the message to {args['chat']}: “{args['text']}” Send it?"

    def run(self, args, ctx):
        try:
            info = _svc(ctx).send(args["chat"], args["text"])
        except WhatsAppError as exc:
            return _fail(exc)
        if not info["sent"]:
            if info.get("composer_empty"):
                return ToolResult.ok(f"I pressed Send for {info['chat']}, but I couldn't confirm the message went out — please "
                                     "check WhatsApp.", **info)
            return ToolResult.fail(f"I pressed Send, but the message to {info['chat']} doesn't show as sent. Please check WhatsApp.",
                                   **info)
        return ToolResult.ok(f"Sent to {info['chat']}.", **info)

    def verify(self, args, result, ctx):
        return True if result.success and result.data.get("sent") else (None if result.success else False)


TOOLS = [WhatsAppOpen(), WhatsAppRead(), WhatsAppUnread(), WhatsAppDraft(), WhatsAppSend()]
