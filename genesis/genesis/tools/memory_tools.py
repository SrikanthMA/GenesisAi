"""Memory tools (explicit, user-requested only)."""
from __future__ import annotations

from ..security.validators import ValidationError, validate_email
from .base import Tool, ToolResult, obj, s


class Remember(Tool):
    name = "remember"
    category = "memory"
    description = "Store a fact the user explicitly asked Genesis to remember."
    parameters = obj({"text": s("The fact", maxLength=1000)}, ["text"])

    def run(self, args, ctx):
        if not ctx.memory.enabled:
            return ToolResult.fail("Memory is turned off, so I didn't save that. You can enable it in Settings.")
        mid = ctx.memory.remember(args["text"])
        return ToolResult.ok(f"Got it — I'll remember that {args['text'].rstrip('.')}.", memory_id=mid)


class Forget(Tool):
    name = "forget"
    category = "memory"
    description = "Delete remembered facts that match the text."
    parameters = obj({"text": s("Text to match", maxLength=500)}, ["text"])

    def run(self, args, ctx):
        n = ctx.memory.forget(args["text"])
        if not n:
            return ToolResult.ok("I didn't have anything like that stored.", deleted=0)
        return ToolResult.ok(f"Forgotten ({n} item{'s' if n != 1 else ''} deleted).", deleted=n)


class AddContact(Tool):
    name = "add_contact"
    category = "memory"
    description = "Save a contact (name, optional alias like 'professor', optional e-mail)."
    parameters = obj({"name": s("Name", maxLength=200), "alias": s("Relation/alias", maxLength=100),
                      "email": s("E-mail", maxLength=320)}, ["name"])

    def run(self, args, ctx):
        if not ctx.memory.enabled:
            return ToolResult.fail("Memory is turned off, so I can't save contacts.")
        email = None
        if args.get("email"):
            try:
                email = validate_email(args["email"])
            except ValidationError as exc:
                return ToolResult.fail(str(exc))
        ctx.memory.save_contact(args["name"], email=email, alias=args.get("alias"))
        bits = [args["name"]]
        if args.get("alias"):
            bits.append(f"(your {args['alias']})")
        if email:
            bits.append(f"<{email}>")
        return ToolResult.ok("Saved contact " + " ".join(bits) + ".", name=args["name"], email=email)


class ListMemory(Tool):
    name = "list_memory"
    category = "memory"
    description = "Show what Genesis remembers."
    parameters = obj({})

    def run(self, args, ctx):
        snap = ctx.memory.snapshot()
        lines = [f"• {f['content']}" for f in snap["facts"][:20]]
        lines += [f"• Contact: {c['name']}" + (f" ({c['alias']})" if c.get("alias") else "") + (f" <{c['email']}>" if c.get("email") else "")
                  for c in snap["contacts"][:20]]
        lines += [f"• Project: {p['name']} → {p['path']}" for p in snap["projects"][:10]]
        if not lines:
            state = "" if snap["enabled"] else " (memory is currently turned off)"
            return ToolResult.ok(f"I don't have anything stored about you{state}.", **snap)
        return ToolResult.ok("Here's what I remember (you can delete any of it on the Memory page):\n" + "\n".join(lines), **snap)


TOOLS = [Remember(), Forget(), AddContact(), ListMemory()]
