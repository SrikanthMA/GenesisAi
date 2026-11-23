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
        "thread_id": s("Gmail thread id when this is a reply", maxLength=200),
        "in_reply_to": s("Message-ID header of the e-mail being replied to", maxLength=1000),
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
            info = ctx.gmail.create_draft(to, args["subject"], args["body"], cc=cc, attachments=attachments,
                                          thread_id=args.get("thread_id"), in_reply_to=args.get("in_reply_to"))
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
        kind = "reply" if args.get("thread_id") else "draft"
        return ToolResult.ok(f"Created a Gmail {kind} to {who}{extra} — subject “{args['subject']}”. It's open for you to review; "
                             "say “send it” when you're ready.", draft_id=info["draft_id"], message_id=info.get("message_id"),
                             url=info["url"], to=to, subject=args["subject"], body=args["body"],
                             thread_id=args.get("thread_id"), in_reply_to=args.get("in_reply_to"))

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
            res = ToolResult.fail(f"Gmail search failed: {exc}")
            res.retryable = bool(getattr(exc, "transient", False))
            return res
        if not msgs:
            return ToolResult.ok("No matching e-mails.", messages=[])
        lines = [f"{i + 1}. {m['subject']} — from {m['from'].split('<')[0].strip()}" + (" (unread)" if m["unread"] else "")
                 for i, m in enumerate(msgs)]
        words = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}
        kind = "unread e-mail" if "is:unread" in (args.get("query") or "") else "matching e-mail"
        head = f"Found {words.get(len(msgs), len(msgs))} {kind}{'s' if len(msgs) != 1 else ''}"
        res = ToolResult.ok(head + ":\n" + "\n".join(lines) + "\nSay “open the first one” (or the latest) to read one.",
                            messages=msgs, first_id=msgs[0]["id"],
                            context=wrap_untrusted("\n".join(f"{m['subject']}: {m['snippet']}" for m in msgs), "gmail"))
        res.untrusted_content = True
        return res


class GmailOpenMessage(Tool):
    name = "gmail_open_message"
    category = "email"
    description = "Open one e-mail (by Gmail message id) in the browser and read its contents (untrusted content)."
    parameters = obj({"message_id": s("Gmail message id", maxLength=200),
                      "open_browser": {"type": "boolean", "default": True}}, ["message_id"])
    remote = True

    def run(self, args, ctx):
        try:
            msg = ctx.gmail.get_message(args["message_id"])
        except GoogleAuthError as exc:
            return ToolResult.fail(f"I can't open that e-mail because {_lower_first(str(exc))}",
                                   hint="Connect your Google account in Genesis → Services.")
        except GmailError as exc:
            res = ToolResult.fail(f"I couldn't open that e-mail: {exc}")
            res.retryable = bool(getattr(exc, "transient", False))
            return res
        if args.get("open_browser", True):
            ctx.backend.open_url(msg["url"])
        preview = " ".join((msg.get("body") or msg.get("snippet") or "").split())[:280]
        att = msg.get("attachments") or []
        att_line = ("\nAttachments: " + ", ".join(a["filename"] for a in att[:6])) if att else ""
        res = ToolResult.ok(f"Opened “{msg['subject']}” from {msg['from_name']}." + (f"\n{preview}" if preview else "") + att_line, **msg)
        res.untrusted_content = True
        return res

    def verify(self, args, result, ctx):
        return bool(result.success and result.data.get("id") == args["message_id"])


class GmailUpdateDraft(Tool):
    name = "gmail_update_draft"
    category = "email"
    description = "Replace the contents of an existing Gmail draft (after a revision). Does not send."
    parameters = obj({
        "draft_id": s("Draft id", maxLength=200),
        "to": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
        "subject": s("Subject", maxLength=300),
        "body": s("Body", maxLength=50_000),
        "thread_id": s("Thread id", maxLength=200),
        "in_reply_to": s("In-Reply-To header", maxLength=1000),
        "open_after": {"type": "boolean", "default": False},
    }, ["draft_id", "subject", "body"])
    remote = True

    def run(self, args, ctx):
        try:
            to = _validate_recipients(args.get("to"))
            info = ctx.gmail.update_draft(args["draft_id"], to, args["subject"], args["body"], thread_id=args.get("thread_id"),
                                          in_reply_to=args.get("in_reply_to"))
        except ValidationError as exc:
            return ToolResult.fail(str(exc))
        except GoogleAuthError as exc:
            return ToolResult.fail(f"I couldn't update the draft because {_lower_first(str(exc))}")
        except GmailError as exc:
            return ToolResult.fail(f"Gmail didn't accept the change: {exc}")
        if args.get("open_after"):
            ctx.backend.open_url(info["url"])
        return ToolResult.ok("Updated the draft. Say “send it” when it looks right.", draft_id=info["draft_id"], url=info["url"],
                             to=to, subject=args["subject"], body=args["body"], thread_id=args.get("thread_id"),
                             in_reply_to=args.get("in_reply_to"))

    def verify(self, args, result, ctx):
        if not result.success:
            return False
        try:
            return ctx.gmail.get_draft(result.data["draft_id"]).get("subject", "") == args["subject"]
        except Exception:
            return None


_CLOSINGS = re.compile(r"^(best( regards)?|kind regards|regards|thanks|thank you|many thanks|sincerely|cheers|warm regards|"
                       r"yours( sincerely| truly)?)[,!.]?\s*$", re.I)


def _replace_ci(text: str, find: str, repl: str) -> tuple[str, int]:
    pattern = re.compile(r"(?<!\w)" + re.escape(find) + r"(?!\w)", re.I)

    def keep_case(m):
        return repl[:1].upper() + repl[1:] if m.group(0)[:1].isupper() else repl

    return pattern.subn(keep_case, text)


def _insert_before_closing(body: str, sentence: str) -> str:
    sentence = _sentence(sentence)
    lines = body.rstrip("\n").split("\n")
    for i in range(len(lines) - 1, -1, -1):
        if _CLOSINGS.match(lines[i].strip()):
            j = i
            while j > 0 and not lines[j - 1].strip():
                j -= 1
            return "\n".join(lines[:j] + ["", sentence] + lines[j:]) + "\n"
    return body.rstrip("\n") + "\n\n" + sentence + "\n"


class ReviseText(Tool):
    name = "revise_text"
    category = "email"
    description = "Revise an e-mail/draft text: replace words, add a sentence, change subject, or restyle (shorter, more formal…)."
    parameters = obj({
        "body": s("Current body", maxLength=50_000),
        "subject": s("Current subject", maxLength=300),
        "revision": {"type": "string", "enum": ["replace", "add", "style", "subject"]},
        "find": s("Text to replace", maxLength=500),
        "replace": s("Replacement", maxLength=500),
        "addition": s("Sentence to add", maxLength=2000),
        "style": s("Desired style", maxLength=60),
        "new_subject": s("New subject", maxLength=300),
    }, ["body", "revision"])

    def run(self, args, ctx):
        body, subject = args["body"], args.get("subject", "")
        kind = args["revision"]
        if kind == "replace":
            find, repl = args.get("find", ""), args.get("replace", "")
            new_body, n1 = _replace_ci(body, find, repl)
            new_subject, n2 = _replace_ci(subject, find, repl)
            if n1 + n2 == 0:
                return ToolResult.fail(f"I couldn't find “{find}” in the draft, so I didn't change anything.")
            return ToolResult.ok(f"Changed “{find}” to “{repl}”.", body=new_body, subject=new_subject)
        if kind == "add":
            return ToolResult.ok("Added that to the e-mail.", body=_insert_before_closing(body, args.get("addition", "")), subject=subject)
        if kind == "subject":
            return ToolResult.ok(f"Changed the subject to “{args.get('new_subject')}”.", body=body, subject=args.get("new_subject") or subject)
        style = (args.get("style") or "").lower()
        if getattr(ctx.llm, "is_generative", False):
            try:
                data = ctx.llm.chat_json([{"role": "user", "content": (
                    f"Rewrite this e-mail to be {style}. Keep every fact, the greeting and the sign-off name. "
                    "Do not add new facts. Return ONLY JSON {\"subject\": ..., \"body\": ...}.\n\n"
                    f"Subject: {subject}\n\n{body}")}], max_tokens=700, temperature=0.3)
                if isinstance(data, dict) and data.get("body"):
                    return ToolResult.ok(f"Made it {style}.", body=str(data["body"]).strip() + "\n",
                                         subject=str(data.get("subject") or subject))
            except (LLMError, ValueError):
                pass
        if style in ("shorter", "more concise", "simpler"):
            kept = [ln for ln in body.split("\n") if not re.match(r"^(thank you for your understanding|i hope (this|you)|"
                                                                  r"please let me know if)", ln.strip(), re.I)]
            short = re.sub(r"\n{3,}", "\n\n", "\n".join(kept))
            if short.strip() == body.strip():
                return ToolResult.fail("It's already about as short as I can make it without the language model.")
            return ToolResult.ok("Made it shorter.", body=short, subject=subject)
        return ToolResult.fail(f"Rewriting it to be {style} needs the language model, which is offline right now.",
                               hint="Set HF_TOKEN in .env (or use Ollama).")


def attachments_exist(paths: list[str]) -> bool:
    return all(Path(p).is_file() for p in paths)


TOOLS = [ComposeEmail(), GmailCreateDraft(), GmailComposeWeb(), GmailSendDraft(), GmailSearch(), GmailOpenMessage(),
         GmailUpdateDraft(), ReviseText()]
