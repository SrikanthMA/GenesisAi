"""E-mail tools: compose (NLG), Gmail draft/send/search, and a web-compose fallback."""
from __future__ import annotations

import re
from pathlib import Path

from ..ai import prompts
from ..ai.llm.base import LLMError
from ..core.logging_setup import get_logger
from ..security.injection import wrap_untrusted
from ..security.permissions import RiskLevel
from ..security.validators import ValidationError, validate_email, validate_path
from ..services.gmail import GmailError, compose_url
from ..services.google_auth import GoogleAuthError
from .base import Tool, ToolResult, obj, s

log = get_logger("tools.email")

FORMAL_HINTS = re.compile(r"\b(professor|prof|dr|doctor|sir|madam|ma'am|manager|boss|principal|dean|hr|mr|mrs|ms|teacher|"
                          r"supervisor|advisor|mentor|client|landlord|team lead|director|hod)\b", re.I)


def _sentence(text: str) -> str:
    t = text.strip().strip("\"“”").rstrip()
    if not t:
        return t
    t = t[0].upper() + t[1:]
    t = re.sub(r"\bi\b", "I", t)
    t = re.sub(r"\bi'(ll|m|ve|d)\b", lambda m: "I'" + m.group(1), t)
    if t[-1] not in ".!?":
        t += "."
    return t


def template_email(recipient_name: str, message: str, sender: str = "", subject: str = "") -> dict:
    """Deterministic natural-language generation used when no LLM is available."""
    formal = bool(FORMAL_HINTS.search(recipient_name or ""))
    name = (recipient_name or "").strip()
    if name.lower().startswith("my "):
        name = name[3:].capitalize()
    greeting = f"Dear {name}," if formal else (f"Hi {name.split()[0]}," if name and "@" not in name else "Hello,")
    first_person = re.match(r"^(i|i'm|i'll|i've|i'd|we|we're|we'll|my|our|please|could|can|would|thank)\b", message.strip(), re.I)
    if first_person:
        body_line = _sentence(message)
    elif formal:
        body_line = "I wanted to let you know that " + _sentence(message)[0].lower() + _sentence(message)[1:]
    else:
        body_line = "Just a quick note: " + _sentence(message)[0].lower() + _sentence(message)[1:]
    closing = "Thank you for your understanding.\n\nBest regards," if formal else "Thanks,"
    body = f"{greeting}\n\n{body_line}\n\n{closing}\n{sender}".rstrip() + "\n"
    if not subject:
        core = re.sub(r"^(i|we)\s+(will|'ll|am going to|shall)\s+", "", message.strip(), flags=re.I)
        core = re.sub(r"[.?!]+$", "", core)
        words = core.split()
        subject = " ".join(words[:8]) + ("…" if len(words) > 8 else "")
        subject = subject[:1].upper() + subject[1:]
    return {"subject": subject, "body": body, "method": "template"}


class ComposeEmail(Tool):
    name = "compose_email"
    category = "email"
    description = "Write an e-mail subject and body from what the user wants to say (no side effects)."
    parameters = obj({
        "recipient_name": s("How to address the recipient", maxLength=200),
        "message": s("What the user wants to say", maxLength=5000),
        "subject": s("Subject if the user gave one", maxLength=300),
    }, ["message"])

    def run(self, args, ctx):
        sender = ctx.settings.get("user.name", "") or ""
        recipient = args.get("recipient_name") or ""
        msg = args["message"]
        llm = ctx.llm
        if getattr(llm, "is_generative", False):
            try:
                hint = f"Use this subject: {args['subject']}" if args.get("subject") else ""
                data = llm.chat_json([{"role": "user", "content": prompts.EMAIL_COMPOSE_PROMPT.format(
                    sender=sender or "(unknown)", recipient=recipient or "(unknown)", message=msg, subject_hint=hint)}],
                    max_tokens=500, temperature=0.4)
                if isinstance(data, dict) and data.get("body"):
                    subject = args.get("subject") or str(data.get("subject") or "").strip() or template_email(recipient, msg)["subject"]
                    return ToolResult.ok("Wrote the e-mail.", subject=subject[:300], body=str(data["body"]).strip() + "\n",
                                         method="llm")
            except (LLMError, ValueError) as exc:
                log.info("LLM compose failed, using template: %s", exc)
        out = template_email(recipient, msg, sender, args.get("subject") or "")
        return ToolResult.ok("Wrote the e-mail.", **out)


def _validate_recipients(values) -> list[str]:
    out = []
    for v in values or []:
        if v:
            out.append(validate_email(v))
    return out


class GmailCreateDraft(Tool):
    name = "gmail_create_draft"
    category = "email"
    description = "Create a draft in the user's Gmail (does NOT send). Opens it for review by default."
    parameters = obj({
        "to": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
        "cc": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
        "subject": s("Subject", maxLength=300),
        "body": s("Plain-text body", maxLength=50_000),
        "attachments": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
        "open_after": {"type": "boolean", "default": True},
    }, ["subject", "body"])
    risk = RiskLevel.LOW  # a draft is private and reversible
    remote = True

    def run(self, args, ctx):
        try:
            to = _validate_recipients(args.get("to"))
            cc = _validate_recipients(args.get("cc"))
            attachments = [validate_path(p, allowed_roots=ctx.allowed_roots(), must_exist=True) for p in args.get("attachments") or []]
        except ValidationError as exc:
            return ToolResult.fail(f"I can't create that draft: {exc}")
        for a in attachments:
            if a.is_dir():
                return ToolResult.fail(f"{a.name} is a folder; Gmail can only attach files.")
        fallback = {"tool": "gmail_compose_web", "args": {"to": to, "subject": args["subject"], "body": args["body"]}}
        try:
            info = ctx.gmail.create_draft(to, args["subject"], args["body"], cc=cc, attachments=attachments)
        except GoogleAuthError as exc:
            res = ToolResult.fail(f"I couldn't create the Gmail draft because {_lower_first(str(exc))}",
                                  hint="Open Genesis → Services and connect your Google account.")
            res.recovery = {**fallback, "reason": "Gmail isn't connected, so I opened a pre-filled Gmail compose window instead."
                            + (" You'll need to attach the file yourself." if attachments else "")}
            return res
        except GmailError as exc:
            res = ToolResult.fail(f"I couldn't create the Gmail draft: {exc}")
            res.recovery = {**fallback, "reason": "I opened a pre-filled Gmail compose window instead."}
            return res
        if args.get("open_after", True):
            ctx.backend.open_url(info["url"])
        who = ", ".join(to) if to else "no recipient yet"
        extra = f" with {len(attachments)} attachment(s)" if attachments else ""
        return ToolResult.ok(f"Created a Gmail draft to {who}{extra} — subject “{args['subject']}”. It's open for you to review; "
                             "say “send it” when you're ready.", draft_id=info["draft_id"], message_id=info.get("message_id"),
                             url=info["url"], to=to, subject=args["subject"], body=args["body"])

    def verify(self, args, result, ctx):
        if not result.success:
            return False
        try:
            d = ctx.gmail.get_draft(result.data["draft_id"])
            return d.get("draft_id") == result.data["draft_id"]
        except Exception:
            return None


def _lower_first(t: str) -> str:
    return t[:1].lower() + t[1:] if t else t


class GmailComposeWeb(Tool):
    name = "gmail_compose_web"
    category = "email"
    description = "Open Gmail's compose window in the browser pre-filled with recipient/subject/body (no API needed)."
    parameters = obj({
        "to": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
        "subject": s("Subject", maxLength=300),
        "body": s("Body", maxLength=8000),
    }, ["subject", "body"])

    def run(self, args, ctx):
        try:
            to = _validate_recipients(args.get("to"))
        except ValidationError as exc:
            return ToolResult.fail(str(exc))
        url = compose_url(to, args["subject"], args["body"])
        if not ctx.backend.open_url(url):
            return ToolResult.fail("The browser didn't open.")
        return ToolResult.ok("Opened a pre-filled Gmail compose window in your browser. Gmail saves it to Drafts automatically "
                             "once it's open; review it and press Send yourself when ready.", url=url, to=to,
                             subject=args["subject"], compose_web=True)


class GmailSendDraft(Tool):
    name = "gmail_send_draft"
    category = "email"
    description = "Send an existing Gmail draft. Always requires explicit confirmation."
    parameters = obj({"draft_id": s("Draft id", maxLength=200), "to": {"type": "array", "items": {"type": "string"}},
                      "subject": s("Subject (for the confirmation)", maxLength=300)}, ["draft_id"])
    risk = RiskLevel.MEDIUM
    always_confirm = True
    remote = True

    def confirmation_prompt(self, args):
        to = ", ".join(args.get("to") or []) or "the recipients in the draft"
        return f"Send the e-mail “{args.get('subject') or '(draft)'}” to {to} now?"

    def run(self, args, ctx):
        try:
            info = ctx.gmail.send_draft(args["draft_id"])
        except GoogleAuthError as exc:
            return ToolResult.fail(f"I couldn't send it because {_lower_first(str(exc))}",
                                   hint="Reconnect your Google account in Genesis → Services.")
        except GmailError as exc:
            return ToolResult.fail(f"Gmail didn't send the e-mail: {exc}")
        sent = "SENT" in (info.get("labels") or []) or bool(info.get("message_id"))
        if not sent:
            return ToolResult.fail("Gmail accepted the request but didn't confirm the e-mail was sent.")
        to = ", ".join(args.get("to") or []) or "its recipients"
        return ToolResult.ok(f"Sent the e-mail “{args.get('subject') or '(no subject)'}” to {to}.", **info)

    def verify(self, args, result, ctx):
        return bool(result.success and result.data.get("message_id"))


class GmailSearch(Tool):
    name = "gmail_search"
    category = "email"
    description = "Search/read the user's Gmail inbox (Gmail search syntax, e.g. 'from:amazon is:unread')."
    parameters = obj({"query": s("Gmail search query", maxLength=300), "max_results": {"type": "integer", "minimum": 1,
                                                                                       "maximum": 20, "default": 5}})
    remote = True

    def run(self, args, ctx):
        try:
            msgs = ctx.gmail.search(args.get("query") or "", args.get("max_results", 5))
        except GoogleAuthError as exc:
            res = ToolResult.fail(f"I can't read your e-mail because {_lower_first(str(exc))}",
                                  hint="Connect your Google account in Genesis → Services.")
            res.recovery = {"tool": "open_url", "args": {"url": "https://mail.google.com/mail/u/0/#inbox"},
                            "reason": "I opened your Gmail inbox in the browser instead."}
            return res
        except GmailError as exc:
            return ToolResult.fail(f"Gmail search failed: {exc}")
        if not msgs:
            return ToolResult.ok("No matching e-mails.", messages=[])
        lines = [f"{i + 1}. {m['subject']} — from {m['from'].split('<')[0].strip()}" + (" (unread)" if m["unread"] else "")
                 for i, m in enumerate(msgs)]
        res = ToolResult.ok(f"Here are your latest {len(msgs)} matching e-mails:\n" + "\n".join(lines), messages=msgs,
                            context=wrap_untrusted("\n".join(f"{m['subject']}: {m['snippet']}" for m in msgs), "gmail"))
        res.untrusted_content = True
        return res


def attachments_exist(paths: list[str]) -> bool:
    return all(Path(p).is_file() for p in paths)


TOOLS = [ComposeEmail(), GmailCreateDraft(), GmailComposeWeb(), GmailSendDraft(), GmailSearch()]
