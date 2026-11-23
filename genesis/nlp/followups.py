"""Context-aware follow-up resolution.

Turns utterances that only make sense in context into concrete requests,
using the dialogue state's focus stack, the last enumerated list and the last
executed request:

    "open it" / "close that"            → act on the current focus
    "open the second one" / "the latest"→ pick from the last list (files, e-mails, search results, issues)
    "no, the other one"                 → switch the selection and repeat the last list action
    "do the same thing again" / "try again"
    "do that for Rahul too"             → repeat the last request with a different person
    "reply saying I'll submit it Friday"→ reply to the e-mail being discussed
    "actually change tomorrow to Friday" / "make it shorter" / "add that …" → revise the current draft
    "go back"                           → previous folder / browser page
    "the one we discussed earlier"      → the previous focus item of the same kind

If a reference is genuinely ambiguous (nothing to refer to), a short
clarifying question is returned instead of guessing.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .dialogue import DialogueState, Focus
from .intents import Intent, NLUResult

I = re.I
INTERJECTIONS = re.compile(r"^(?:stop|wait|hold on|no|nope|actually|sorry|ok(?:ay)?|hmm+|um+|uh+|oh)[\s,.!;:-]+(?=\S)", I)

ORDINALS = {
    "first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2, "fourth": 3, "4th": 3, "fifth": 4, "5th": 4,
    "one": 0, "two": 1, "three": 2, "four": 3, "five": 4, "1": 0, "2": 1, "3": 2, "4": 3, "5": 4,
}
LIST_VERBS = r"open|show|read|use|pick|choose|select|take|play|watch|fix|check|view|go with|reply to|delete|remove|switch to|focus|click|press|tap|hit|complete|finish|tick off"
NOUNS = r"one|email|e-mail|mail|message|file|result|link|issue|error|problem|item|reminder|page|pdf|document|video|song|window|event|meeting|button|field|tab|option|element|control|task|note"
_ORD_RE = re.compile(
    rf"^(?:(?P<verb>{LIST_VERBS})\s+)?(?:the\s+|number\s+|no\.?\s*)?"
    rf"(?P<ord>first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th|last|latest|newest|most recent|oldest|other|next|previous|[1-5])"
    rf"(?:\s+(?P<noun>{NOUNS}))?(?:\s+(?:instead|please|then|one))?$",
    I,
)
_PRONOUN_RE = re.compile(
    r"^(?P<verb>open|reopen|close|show|read|launch|start|play|quit|exit|summari[sz]e|view|click|press|tap|select)\s+"
    r"(?:it|that|this|them|that one|this one|the same)(?:\s+(?:again|now|please|up|back up))?$",
    I,
)
_REPEAT_RE = re.compile(
    r"^(?:(?:please\s+)?(?:do|run)\s+(?:that|it|this|the same(?: thing)?)\s+again|again|repeat(?: that| it)?|"
    r"one more time|same again|(?:try|retry)(?: that| it)?(?: again)?|redo(?: that| it)?)$",
    I,
)
_FOR_OTHER_RE = re.compile(
    r"^(?:(?:do|send|write|draft)\s+(?:that|the same|it|the same thing|one)(?:\s+thing)?|same|the same)\s+"
    r"(?:for|to)\s+(?P<who>.+?)(?:\s+(?:too|as well|also))?$|^(?:and\s+)?(?:for|to)\s+(?P<who2>[a-z][\w .'-]*?)\s+(?:too|as well)$",
    I,
)
_REPLY_RE = re.compile(
    r"^(?:reply|respond|answer|write back|(?:draft|write|compose|prepare|send|type up)\s+(?:a|an|the)\s+(?:quick\s+|short\s+)?"
    r"(?:reply|response|answer))(?:\s+to\s+(?:it|that|him|her|them|this|the email|this email|that email|the mail))?"
    r"(?:\s*(?:saying|with|that|and say|telling (?:him|her|them)(?: that)?|:)\s*(?P<msg>.+))?$",
    I,
)
_REPLACE_RE = re.compile(r"^(?:change|replace|swap)\s+(?:the\s+word\s+)?[\"“']?(?P<a>.+?)[\"”']?\s+(?:to|with|for|into)\s+[\"“']?(?P<b>.+?)[\"”']?$", I)
_STYLE_RE = re.compile(
    r"^make\s+(?:it|the (?:email|mail|draft|reply|message))\s+(?P<style>shorter|longer|more formal|less formal|more casual|"
    r"friendlier|more friendly|more polite|politer|more professional|simpler|clearer|more concise|warmer)$",
    I,
)
_ADD_RE = re.compile(r"^(?:also\s+)?(?:add|mention|include|say|write)\s+(?:that|:)\s*(?P<x>.+)$|^also say\s+(?P<y>.+)$", I)
_SUBJECT_RE = re.compile(r"^(?:make|change|set)\s+the\s+subject\s+(?:to|as)?\s*[\"“']?(?P<s>.+?)[\"”']?$", I)
_BACK_RE = re.compile(r"^(?:go\s+)?back(?:\s+(?:to the previous (?:one|folder|page)|please))?$", I)
_EARLIER_RE = re.compile(
    r"^(?:(?P<verb>use|open|take|show)\s+)?(?:the\s+)?(?:one|file|email|folder|link)\s+(?:we|i|you)\s+"
    r"(?:discussed|talked about|mentioned|opened|used|looked at|saw)(?:\s+(?:earlier|before|previously|a moment ago))?$|"
    r"^(?:(?P<verb2>use|open|take)\s+)?the previous one$",
    I,
)
_FROM_RE = re.compile(r"^(?:open|read|show)\s+(?:the\s+)?(?:one|email|mail|message)?\s*from\s+(?P<who>.+)$", I)


_DEFERRED_RE = re.compile(
    r"^(?:ok(?:ay)?|alright|all right|fine|great|good|now|yes|yeah)?[\s,]*(?:please\s+)?(?:go ahead and\s+|you can\s+)?"
    r"(?P<verb>send|submit|post|click)\s+(?:it|that|this|the (?:message|form|application|reply|draft|button))(?:\s+now)?(?:\s+please)?$",
    I)
DEFERRED_TTL = 1800  # seconds


_COLAB_RUN = re.compile(r"^(?:please\s+)?(?:run|execute|rerun|re-run)(?:\s+(?:it|this|that|the code|the cell|my code|the notebook))?"
                        r"(?:\s+(?:again|now|please))*$", I)
_COLAB_WHY = re.compile(r"^(?:why (?:did|does|is) (?:it|this|that|the (?:cell|code)) (?:fail(?:ing)?|crash(?:ing)?|not work(?:ing)?)|"
                        r"what went wrong|what(?:'s| is) (?:the|this) error|what does (?:this|the|that) error mean|explain (?:the|this) error|"
                        r"why (?:did it|it) (?:fail|crash)(?:ed)?)\??$", I)
_COLAB_FIX = re.compile(r"^(?:please\s+)?(?:fix|correct|repair|debug)\s+(?:it|this|that|the error|the code|the bug|the cell)"
                        r"(?P<rerun>\s+and\s+(?:run|try)\s+it(?:\s+again)?)?$", I)
_COLAB_WRITE = re.compile(r"^(?:please\s+)?(?:write|add|put|generate)\s+(?:some\s+|the\s+)?(?:python\s+)?code\s+(?:that|to|which)\s+(?P<task>.+)$", I)
_WA_REPLY = re.compile(r"^(?:reply|respond|answer|write back)(?:\s+to\s+(?:that|it|this|him|her|them|that message|the message|"
                       r"her message|his message|their message))?(?:\s*(?:saying|with|that|and say|:|,)\s*(?P<msg>.+))?$", I)
_DONT_SEND = re.compile(r"^(?:no,?\s+)?(?:don'?t|do not)\s+(?P<v>send|submit)\s+(?:it|that|this|the (?:message|form|application))"
                        r"(?:\s+(?:yet|now|just yet))?$|^not yet$", I)
_SEND_IT = re.compile(r"^(?:ok(?:ay)?|alright|now|yes)?[\s,]*(?:please\s+)?(?:go ahead and\s+)?(?P<v>send|submit)\s+(?:it|that|this|the "
                      r"(?:message|form|application))(?:\s+now)?$", I)
_FORM_CHANGE = re.compile(r"^(?:actually,?\s+|no,?\s+|wait,?\s+)*(?:change|set|make|update|put)\s+(?:my\s+|the\s+)?(?:(?:answer|response|value)\s+"
                          r"(?:for|to|of|in)\s+)?(?P<f>that(?:\s+question|\s+one|\s+field)?|it|.+?)\s+(?:to|as)\s+(?P<v>.+)$", I)


def _desktop_followup(raw: str, t: str, low: str, st: DialogueState) -> Optional[FollowUpResult]:
    """Context-dependent follow-ups for WhatsApp chats, Colab notebooks, forms and images (1.4)."""
    from ..services.whatsapp import second_person

    top = st.top_focus()
    kind = top.kind if top is not None else ""
    # ---- a GitHub repository in focus: "delete it" (still needs the typed name + confirmation), "open it"
    if kind == "repo":
        if re.fullmatch(r"(?:please\s+)?(?:delete|remove|destroy|erase)\s+(?:it|this|that|this repo(?:sitory)?|that repo(?:sitory)?|"
                        r"the repo(?:sitory)?)(?:\s+(?:from|on)\s+git ?hub)?", low):
            return FollowUpResult(NLUResult(text=raw, intent=Intent.GITHUB, confidence=0.95, source="context",
                                            slots={"github_op": "delete", "repo": top.value.get("repo")}))
        if re.fullmatch(r"(?:please\s+)?(?:open|show)\s+(?:it|this|that)(?:\s+on\s+git ?hub)?", low):
            return _fu(raw, "github_open", {"target": top.value.get("repo")}, "Open the repository")
    # ---- Word / Excel / PowerPoint in focus (just opened or just written): "write a paragraph about X", "add a slide on Y"
    office_app = None
    if kind == "document":
        office_app = top.value.get("app")
    elif kind == "app" and str(top.value.get("key", "")).lower() in ("word", "excel", "powerpoint"):
        office_app = str(top.value["key"]).lower()
    if office_app:
        m = re.match(r"^(?:please\s+)?(?:now\s+)?(?:write|type|add|put|insert)\s+(?P<x>.+)$", t, I)
        if m and not re.search(r"\b(?:e-?mail|whats ?app|reminder|note to|to-?do|task|word|excel|power ?point|ppt|presentation|"
                               r"spreadsheet|workbook|document|colab|notebook)\b", low):
            from .desktop_rules import _GENERATED

            x = m.group("x").strip()
            slots = {"office_op": "write", "app": office_app}
            if office_app == "powerpoint" and re.match(r"^(?:an?\s+|\d+\s+|some\s+)?(?:new\s+)?slides?\b", x, I):
                n = re.match(r"^(\d+)", x)
                slots.update(request=re.sub(r"^(?:an?\s+|\d+\s+|some\s+)?(?:new\s+)?slides?\s*(?:about|on|for|explaining)?\s*",
                                            "", x, flags=I) or x, slides=int(n.group(1)) if n else 1)
            elif re.fullmatch(r"[“\"'].+[”\"']", x) or not _GENERATED.match(x):
                slots["text"] = x.strip("“”\"'")
            else:
                slots["request"] = x
            return FollowUpResult(NLUResult(text=raw, intent=Intent.OFFICE, confidence=0.95, source="context", slots=slots))
    # ---- Colab notebook in focus
    if kind == "notebook":
        if _COLAB_RUN.match(t):
            return _fu(raw, "colab_run", {}, "Run the cell")
        if _COLAB_WHY.match(t):
            return _fu(raw, "colab_explain_error", {}, "Explain the error")
        m = _COLAB_FIX.match(t)
        if m:
            return _fu(raw, "colab_fix", {"rerun": bool(m.group("rerun"))}, "Fix the code")
        m = _COLAB_WRITE.match(t)
        if m:
            return _fu(raw, "colab_write_code", {"task": m.group("task")}, "Write the code into the notebook")
    # ---- WhatsApp in focus: "show me the latest message from Mom" / "message Rahul saying …" mean WhatsApp
    wa_ctx = kind in ("chat", "wa_draft") or (kind == "app" and top.value.get("key") == "whatsapp")
    if wa_ctx:
        from .desktop_rules import whatsapp_slots

        slots = whatsapp_slots(t, low, in_whatsapp=True)
        if slots is not None and slots.get("wa_op") in ("read", "send", "draft", "unread"):
            return FollowUpResult(NLUResult(text=raw, intent=Intent.WHATSAPP, confidence=0.95, slots=slots, source="context"))
    # ---- WhatsApp chat / message / draft in focus
    if kind in ("chat", "wa_draft"):
        chat = top.value.get("chat")
        m = _WA_REPLY.match(t)
        if m and chat:
            if not m.group("msg"):
                return FollowUpResult(question=f"What should I say to {chat}?")
            text = second_person(m.group("msg"))
            steps = [{"tool": "whatsapp_draft", "args": {"chat": chat, "text": text}, "description": f"Draft the reply to {chat}"},
                     {"tool": "whatsapp_send", "args": {"chat": chat, "text": text}, "description": f"Send the reply to {chat}"}]
            return FollowUpResult(NLUResult(text=raw, intent=Intent.FOLLOW_UP, confidence=0.95, source="context",
                                            slots={"steps": steps, "tool": "whatsapp_draft"}))
        if kind == "wa_draft":
            m = _SEND_IT.match(t)
            if m and m.group("v").lower() == "send":
                # the user saw the exact draft and now explicitly asks to send it (after an earlier "no", ask again)
                return _fu(raw, "whatsapp_send", {"chat": chat, "text": top.value.get("text", "")}, f"Send the message to {chat}",
                           pre_confirmed=not top.value.get("declined"))
            if _DONT_SEND.match(t):
                return FollowUpResult(reply=f"Okay — I won't send it. It's waiting in your chat with {chat}.")
    # ---- a form being filled
    if kind == "form":
        m = _DONT_SEND.match(t)
        if m:
            return FollowUpResult(reply="Okay — I won't submit it. Say “submit it” when you're ready.")
        m = _SEND_IT.match(t)
        if m and m.group("v").lower() == "submit":
            return _fu(raw, "form_submit", {}, "Submit the form")
        m = _FORM_CHANGE.match(t)
        if m and not re.search(r"\b(volume|brightness|timer|alarm|reminder|wallpaper|theme|mode|language|voice|speed|rate)\b",
                               m.group("f"), I):
            f = m.group("f")
            f = "that" if re.fullmatch(r"(it|that(?:\s+question|\s+one|\s+field)?)", f, I) else f
            labels = [str(x).lower() for x in (top.value.get("labels") or [])]
            fl = re.sub(r"^(?:my|the)\s+|\s+(?:field|box|answer)$", "", f.lower()).strip()
            if f != "that" and not any(fl and (fl in lab or lab in fl) for lab in labels if lab):
                return None  # "change the name of report.txt to …", "set my status to away" — not about this form
            return _fu(raw, "form_set_field", {"field": f, "value": m.group("v").strip().strip("\"“”")}, "Change that answer")
    return None


@dataclass
class FollowUpResult:
    nlu: Optional[NLUResult] = None
    question: Optional[str] = None
    reply: Optional[str] = None


def _site_word(url: str) -> str:
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").removeprefix("www.").removeprefix("m.")
    return host.split(".")[0] if host else "browser"


def strip_interjections(text: str) -> str:
    """'stop, open Spotify instead' → 'open Spotify' (corrections/interruptions)."""
    t = text.strip()
    if re.fullmatch(r"stop translat\w*(?: (?:things|languages|everything))?[.!]?", t, I):
        return t  # a command in its own right, not an interruption
    for _ in range(3):
        new = INTERJECTIONS.sub("", t)
        if new == t:
            break
        t = new
    return re.sub(r"[\s,]+instead[.!]?$", "", t, flags=I) or t


def _fu(text: str, tool: str, args: dict, description: str, **extra) -> FollowUpResult:
    slots = {"tool": tool, "args": args, "description": description, **extra}
    return FollowUpResult(NLUResult(text=text, intent=Intent.FOLLOW_UP, confidence=0.95, slots=slots, source="context"))


def _action_for(item_kind: str, item: dict, verb: str, text: str) -> Optional[FollowUpResult]:
    v = (verb or "open").lower()
    label = item.get("label") or item.get("subject") or Path(str(item.get("path", ""))).name or item.get("url", "")
    if item_kind == "repo":
        if v in ("delete", "remove"):
            return FollowUpResult(NLUResult(text=text, intent=Intent.GITHUB, confidence=0.95, source="context",
                                            slots={"github_op": "delete", "repo": item.get("full_name") or label}))
        return _fu(text, "github_open", {"target": item.get("full_name") or label}, f"Open {label}")
    if item_kind == "file":
        if v in ("delete", "remove"):
            return _fu(text, "delete_path", {"path": item["path"]}, f"Delete {label}")
        if v in ("read", "summarize", "summarise", "check"):
            return _fu(text, "read_file", {"path": item["path"]}, f"Read {label}")
        return _fu(text, "open_file", {"path": item["path"]}, f"Open {label}")
    if item_kind == "folder":
        return _fu(text, "open_folder", {"folder": item["path"]}, f"Open {label}")
    if item_kind == "email":
        if v == "reply to":
            return None
        return _fu(text, "gmail_open_message", {"message_id": item["id"]}, f"Open “{label}”")
    if item_kind in ("web", "url"):
        return _fu(text, "open_url", {"url": item["url"]}, f"Open {label}")
    if item_kind == "issue":
        if v == "fix":
            nlu = NLUResult(text=text, intent=Intent.CODE_ACTION, confidence=0.95, source="context",
                            slots={"code_action": "fix", "issue": item, "path": item.get("path")})
            return FollowUpResult(nlu)
        args = {"path": item["path"]}
        if item.get("line"):
            args["line"] = int(item["line"])
        return _fu(text, "vscode_open_file", args, f"Open {Path(item['path']).name} at the problem")
    if item_kind == "app":
        if v in ("close", "quit", "exit"):
            return _fu(text, "close_application", {"app": item["key"]}, f"Close {item['key']}")
        return _fu(text, "open_application", {"app": item["key"]}, f"Open {item['key']}")
    if item_kind == "draft":
        if item.get("url"):
            return _fu(text, "open_url", {"url": item["url"]}, "Open the draft")
        return FollowUpResult(reply="That draft was opened in a Gmail compose window — it's in your browser.")
    if item_kind == "project":
        return _fu(text, "vscode_open_project", {"path": item["path"]} if item.get("path") else {"project": item.get("key")},
                   f"Open the {item.get('key') or 'project'} project")
    if item_kind == "reminder":
        return FollowUpResult(reply=f"That reminder says: {item.get('text')}")
    if item_kind == "video":
        return _fu(text, "youtube_play", {"video_id": item["video_id"], "title": item.get("title", "")[:300]},
                   f"Play “{item.get('title') or 'the video'}”")
    if item_kind == "history":
        if item.get("url"):
            if item.get("kind") == "video":
                return _fu(text, "youtube_play", {"url": item["url"], "title": str(item.get("label", ""))[:300]},
                           f"Play {item.get('label') or 'the video'}")
            return _fu(text, "open_url", {"url": item["url"]}, f"Open {item.get('label') or item['url']}")
        if item.get("path"):
            return _fu(text, "open_file", {"path": item["path"]}, f"Open {Path(item['path']).name}")
    if item_kind == "window":
        return _fu(text, "manage_window", {"action": "focus", "window_id": int(item["id"])}, f"Switch to {item.get('title', '')}")
    if item_kind == "event":
        when = str(item.get("start") or "")[:16].replace("T", " at ")
        return FollowUpResult(reply=f"“{item.get('title')}” — {when}" + (f", {item['location']}" if item.get("location") else "") + ".")
    if item_kind == "media":
        return _fu(text, "media_control", {"action": "resume"}, "Resume")
    if item_kind == "task":
        if v in ("delete", "remove"):
            return _fu(text, "delete_task", {"ref": str(item["id"])}, f"Remove “{item.get('text', '')}”")
        return _fu(text, "complete_task", {"ref": str(item["id"])}, f"Mark “{item.get('text', '')}” done")
    if item_kind == "note":
        if v in ("delete", "remove"):
            return _fu(text, "delete_note", {"ref": str(item["id"])}, f"Delete “{item.get('title', '')}”")
        return FollowUpResult(reply=f"{item.get('title')}:\n{item.get('content', '')}")
    if item_kind == "element":
        if item.get("source") == "browser" and item.get("id"):
            return _fu(text, "browser_click", {"element_id": int(item["id"]), "target": str(item.get("name", ""))[:200]},
                       f"Click “{item.get('name', '')}”")
        if item.get("source") == "desktop" and item.get("id"):
            return _fu(text, "computer_click", {"element_id": int(item["id"]), "target": str(item.get("name", ""))[:200]},
                       f"Click “{item.get('name', '')}”")
        return _fu(text, "click_screen_element", {"name": str(item.get("name", ""))[:200]}, f"Click “{item.get('name', '')}”")
    if item_kind == "wa_chat":
        return _fu(text, "whatsapp_read", {"chat": item["chat"], "count": 5}, f"Open your chat with {item['chat']}")
    if item_kind == "image" and item.get("path"):
        return _fu(text, "open_file", {"path": item["path"]}, "Open the picture")
    return None


_CORR_RE = re.compile(
    r"^(?:(?:no|nope|nah|actually|sorry|wait|oops)[,.!]?\s+)*(?:i meant|i mean|i said|i wanted|what i meant was|i was talking about|"
    r"not that[,.]?|make (?:it|that)|change (?:it|that) to|rather)\s+(?P<x>.+)$", I)
_TIMEY = re.compile(r"\b(\d{1,2}(:\d{2})?\s*(am|pm)|today|tomorrow|tonight|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
                    r"morning|evening|afternoon|noon|next week)\b", I)
# the slot a correction replaces, per intent
_MAIN_SLOT = {Intent.SEARCH_WEB: "query", Intent.YOUTUBE: "query", Intent.RESEARCH: "query", Intent.HISTORY: "query",
              Intent.OPEN_APPLICATION: "app", Intent.CLOSE_APPLICATION: "app", Intent.OPEN_FOLDER: "folder",
              Intent.READ_EMAIL: "from", Intent.CREATE_REMINDER: "reminder_text", Intent.CALENDAR: "title",
              Intent.CODE_ACTION: "task", Intent.OPEN_URL: "url"}


def _correction(raw: str, t: str, st: DialogueState) -> Optional[FollowUpResult]:
    """'No, I meant AI internships' → modify the previous task instead of starting over."""
    from . import rules

    m = _CORR_RE.match(raw.strip())
    if m and re.match(r"^(louder|quieter|softer|bigger|smaller|shorter|longer|brighter|darker|faster|slower|more|less|"
                      r"formal|friendlier|simpler|clearer|full ?screen)\b", m.group("x"), I):
        return None  # "make it louder" is an adjustment, not a correction
    if m:
        x = m.group("x").strip(" .!?")
    elif re.match(r"^(?:no|nope|nah|actually)[,.!]?\s+\S", raw.strip(), I) and not re.match(r"^(?:no|nope)[,.!]?\s+(?:thanks|thank you)", raw, I):
        x = strip_interjections(raw).strip(" .!?")
        probe = rules.parse(x)
        if probe is not None and probe.intent not in (Intent.CONVERSATION, Intent.UNKNOWN):
            return None  # "no, open Spotify" is simply a new command
        if _ORD_RE.match(x) or _PRONOUN_RE.match(x):
            return None
    else:
        return None
    base = st.last_request or st.last_failed
    probe = rules.parse(x)
    if probe is not None and probe.intent not in (Intent.CONVERSATION, Intent.UNKNOWN) and len(x.split()) > 1 and (
            base is None or probe.intent != base.intent or _MAIN_SLOT.get(probe.intent) in probe.slots):
        st.last_correction = x
        return FollowUpResult(probe)  # the correction is a complete command of its own
    if base is None:
        return FollowUpResult(question="Sorry — what should I do?")
    if (base.intent == Intent.FOLLOW_UP or base.intent not in _MAIN_SLOT) and rules.app_lookup(x_clean_for_app(x)):
        st.last_correction = x
        return FollowUpResult(rules.parse("open " + x_clean_for_app(x)))
    if base.intent == Intent.FOLLOW_UP:  # e.g. a YouTube search that ran because YouTube was open
        args = dict(base.slots.get("args") or {})
        key = next((k for k in ("query", "app", "title", "folder") if isinstance(args.get(k), str)), None)
        if key is None:
            return FollowUpResult(question=f"What should I change about “{base.text}”?")
        args[key] = re.sub(r"^(?:the|a|an)\s+", "", x, flags=I)
        st.last_correction = x
        return _fu(raw, base.slots["tool"], args, f"{base.slots.get('description', 'Redo')} (corrected)")
    if base.intent not in _MAIN_SLOT and base.intent not in (Intent.DRAFT_EMAIL, Intent.SEND_EMAIL):
        return FollowUpResult(question=f"What should I change about “{base.text}”?")
    again = copy.deepcopy(base)
    again.text = raw
    again.source = "context"
    x_clean = re.sub(r"^(?:the|a|an)\s+", "", x, flags=I)
    last_undo = st.undo_stack[-1] if st.undo_stack else {}
    if _TIMEY.search(x) and base.intent == Intent.CALENDAR and last_undo.get("tool") == "delete_calendar_event":
        # the event already exists → move it rather than creating a duplicate
        from .entities import date_time_parts

        day, tm = date_time_parts(x)
        ev = last_undo["args"]
        upd = {"event_id": ev["event_id"], "title": ev.get("title", ""), "current_start": ev.get("start", "")}
        if tm:
            upd["new_time"] = f"{tm[0]:02d}:{tm[1]:02d}"
        if day:
            upd["new_date"] = day.isoformat()
        st.last_correction = x
        return _fu(raw, "update_calendar_event", upd, "Change the time")
    if _TIMEY.search(x) and base.intent == Intent.CREATE_REMINDER and last_undo.get("tool") == "cancel_reminder":
        from .entities import parse_datetime

        parsed = parse_datetime(x)
        if parsed is None:
            return FollowUpResult(question="What time should it be instead?")
        old = last_undo["args"]
        st.undo_stack.pop()
        st.last_correction = x
        return _steps(raw, [("cancel_reminder", {"reminder_id": old["reminder_id"], "text": old.get("text", "")}, "Cancel the old time"),
                            ("create_reminder", {"text": base.slots.get("reminder_text") or old.get("text", ""),
                                                 "due_at": parsed.value.isoformat()}, "Set it for the new time")])
    if _TIMEY.search(x) and base.intent in (Intent.CREATE_REMINDER, Intent.CALENDAR):
        from .entities import date_time_parts, parse_datetime

        if base.intent == Intent.CREATE_REMINDER:
            parsed = parse_datetime(x)
            if parsed:
                again.slots["datetime"] = parsed.value
        else:
            day, tm = date_time_parts(x)
            if day:
                again.slots["date"] = day.isoformat()
            if tm:
                again.slots["time"] = f"{tm[0]:02d}:{tm[1]:02d}"
    elif base.intent in (Intent.DRAFT_EMAIL, Intent.SEND_EMAIL):
        from .entities import EMAIL_RE

        if EMAIL_RE.search(x) or len(x.split()) <= 3:
            again.slots.pop("email", None)
            again.slots["recipient"] = x_clean
            em = EMAIL_RE.search(x)
            if em:
                again.slots["email"] = em.group(0)
        else:
            again.slots["message"] = x
    else:
        slot = _MAIN_SLOT[base.intent]
        value = x_clean
        if slot == "app":
            value = rules.app_slot("open " + x_clean) or x_clean
        again.slots[slot] = value
        if base.intent == Intent.RESEARCH:
            again.slots["question"] = value
    st.last_correction = x
    return FollowUpResult(again)


def x_clean_for_app(x: str) -> str:
    return re.sub(r"^(?:the|a|an|open)\s+", "", x.strip(), flags=I)


def _steps(text: str, steps: list[tuple[str, dict, str]]) -> FollowUpResult:
    slots = {"tool": steps[0][0], "args": steps[0][1], "description": steps[0][2],
             "steps": [{"tool": t, "args": a, "description": d} for t, a, d in steps]}
    return FollowUpResult(NLUResult(text=text, intent=Intent.FOLLOW_UP, confidence=0.95, slots=slots, source="context"))


def _current_site(st: DialogueState) -> Optional[str]:
    """Which site/app the user is currently working in (for 'search for X')."""
    for f in st.focus[:3]:
        key = str(f.value.get("key") or "").lower()
        url = str(f.value.get("url") or "").lower()
        if key == "youtube" or "youtube.com" in url or f.kind == "media":
            return "youtube"
        if key == "gmail" or "mail.google.com" in url or f.kind in ("email", "draft"):
            return "gmail"
        if f.kind in ("app", "url"):
            return None
    if st.last_list is not None and st.last_list.kind == "video":
        return "youtube"
    return None


def _pick_index(st: DialogueState, ord_word: str) -> Optional[int]:
    lst = st.last_list
    if lst is None or not lst.items:
        return None
    n = len(lst.items)
    w = ord_word.lower()
    if w in ORDINALS:
        i = ORDINALS[w]
    elif w in ("latest", "newest", "most recent"):
        i = 0 if lst.newest_first else n - 1
    elif w == "oldest":
        i = n - 1 if lst.newest_first else 0
    elif w == "last":
        i = 0 if lst.kind == "email" else n - 1  # "the last email" colloquially means the most recent one
    elif w == "other":
        cur = lst.selected if lst.selected is not None else 0
        i = (1 - cur) if n == 2 else (cur + 1) % n
    elif w == "next":
        i = ((lst.selected if lst.selected is not None else -1) + 1)
    elif w == "previous":
        i = ((lst.selected if lst.selected is not None else 1) - 1)
    else:
        return None
    return i if 0 <= i < n else None


def resolve(text: str, st: DialogueState) -> Optional[FollowUpResult]:
    raw = text.strip().rstrip(".!?")
    t = strip_interjections(raw)
    low = t.lower()
    if not low:
        return None

    # --- a send/submit the user postponed earlier ("don't submit it yet" … "okay, submit it") ------------
    d = st.deferred_action
    top0 = st.top_focus()
    if d is not None and top0 is not None and top0.kind in ("wa_draft", "chat") \
            and (top0.value.get("chat") or "").lower() != ((d.get("args") or {}).get("chat") or "").lower():
        st.deferred_action = d = None  # another chat is open / a newer draft to someone else — "send it" no longer means the old one
    if d is not None and _DEFERRED_RE.match(low):
        import time as _time

        if _time.time() - d.get("at", 0) <= DEFERRED_TTL:
            st.deferred_action = None
            # the user already saw exactly what will be sent/submitted and now explicitly asks for it → counts as the confirmation
            return _fu(raw, d["tool"], dict(d["args"]), d["description"], pre_confirmed=True)
        st.deferred_action = None

    hit = _desktop_followup(raw, t, low, st)
    if hit is not None:
        return hit

    # --- repeat / retry --------------------------------------------------
    if _REPEAT_RE.match(low):
        base = st.last_failed if low.startswith(("try", "retry")) and st.last_failed is not None else st.last_request
        if base is None:
            return FollowUpResult(question="What would you like me to do again?")
        again = copy.deepcopy(base)
        again.text = raw
        again.source = "context"
        again.slots.pop("draft_id", None) if again.intent != Intent.SEND_EMAIL else None
        return FollowUpResult(again)

    # --- same thing for someone else ------------------------------------
    m = _FOR_OTHER_RE.match(low)
    if m:
        who = (m.group("who") or m.group("who2") or "").strip()
        base = st.last_request
        if base is None or not who:
            return FollowUpResult(question=f"What should I do for {who or 'them'}?")
        if base.intent in (Intent.DRAFT_EMAIL, Intent.SEND_EMAIL, Intent.REPLY_EMAIL):
            again = copy.deepcopy(base)
            again.intent = Intent.DRAFT_EMAIL if base.intent == Intent.REPLY_EMAIL else base.intent
            for k in ("email", "draft_id", "use_last_draft", "message_id", "thread_id", "to_email"):
                again.slots.pop(k, None)
            em = re.fullmatch(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", who)
            again.slots["recipient"] = who
            if em:
                again.slots["email"] = who
            again.text = raw
            again.source = "context"
            return FollowUpResult(again)
        return FollowUpResult(question=f"What should I do for {who}? I can only repeat e-mails for another person.")

    # --- draft revisions -------------------------------------------------
    top = st.top_focus()
    draft = st.ref.draft
    draft_in_focus = draft is not None and top is not None and top.kind == "draft"
    if draft is not None and (draft_in_focus or top is None):
        rev = None
        m = _SUBJECT_RE.match(t)
        if m:
            rev = {"revision": "subject", "subject": m.group("s").strip()}
        elif _STYLE_RE.match(t):
            rev = {"revision": "style", "style": _STYLE_RE.match(t).group("style").lower()}
        elif _REPLACE_RE.match(t):
            m = _REPLACE_RE.match(t)
            rev = {"revision": "replace", "find": m.group("a").strip(), "replace": m.group("b").strip()}
        elif _ADD_RE.match(t):
            m = _ADD_RE.match(t)
            rev = {"revision": "add", "addition": (m.group("x") or m.group("y")).strip()}
        if rev:
            return FollowUpResult(NLUResult(text=raw, intent=Intent.REVISE_DRAFT, confidence=0.95, slots=rev, source="context"))

    # --- "summarise it" / "what does it say" ---------------------------------
    if re.fullmatch(r"(?:summari[sz]e|sum up|give me (?:a|the) summary of|tl;?dr|what does|what'?s|what is)\s*(?:it|that|this|the "
                    r"(?:e-?mail|mail|message|page|article|website|site|document|file|pdf))?\s*(?:say|about|say about)?", low) \
            and re.search(r"summar|sum up|tl;?dr|say|about", low):
        f = st.top_focus({"email", "url", "file"})
        if f is not None and f.kind == "email":
            body = (f.value.get("body") or f.value.get("snippet") or "")[:20000]
            return _steps(raw, [("llm_analyze", {"instruction": "Summarise this e-mail for the user in 2-4 short bullet points and "
                                                                "say what (if anything) they need to do.",
                                                 "content": f"From: {f.value.get('from', '')}\nSubject: {f.value.get('subject', '')}\n\n{body}"},
                                 "Summarise the e-mail")])
        if f is not None and f.kind == "url":
            return _steps(raw, [("browser_read_page", {"url": f.value["url"]}, "Read the page"),
                                ("llm_analyze", {"instruction": "Summarise this web page for the user in 3-5 sentences.",
                                                 "content": "{{steps.1.data.content}}"}, "Summarise it")])
        if f is not None and f.kind == "file":
            return _steps(raw, [("read_file", {"path": f.value["path"]}, f"Read {Path(f.value['path']).name}"),
                                ("llm_analyze", {"instruction": "Summarise this document for the user in 3-5 sentences.",
                                                 "content": "{{steps.1.data.content}}"}, "Summarise it")])
        if f is None:
            return FollowUpResult(question="What should I summarise — an e-mail, a web page or a file?")

    # --- "search for X" goes to the site you're using (YouTube / Gmail) ---------
    m = re.fullmatch(r"(?:now\s+|and\s+)?(?:search|look|find)\s+(?:for\s+)?(?P<q>.+?)(?:\s+there|\s+on it)?", t, I)
    if m and not re.search(r"\b(web|internet|google|online|youtube|gmail|files?|folders?|my computer|email|mail)\b", low) \
            and not re.search(r"\b(we|i|you)\s+(were\s+)?(talk|discuss|watch|look|mention|search|open|play)\w*\b", low):
        site = _current_site(st)
        if site == "youtube":
            q = m.group("q").strip()
            return _fu(raw, "youtube_search", {"query": q, "latest": bool(re.search(r"\b(latest|newest)\b", q, I))},
                       f"Search YouTube for “{q}”")
        if site == "gmail":
            return _fu(raw, "gmail_search", {"query": m.group("q").strip(), "max_results": 5}, "Search your e-mail")

    # --- reply to the e-mail being discussed -----------------------------
    m = _REPLY_RE.match(t)
    if m:
        email = st.top_focus({"email"})
        if email is None:
            if st.last_list and st.last_list.kind == "email" and st.last_list.selected is not None:
                it = st.last_list.items[st.last_list.selected]
                email = Focus("email", it, it.get("subject", ""))
        if email is None:
            return FollowUpResult(question="Which e-mail should I reply to? Say “check my email” and pick one.")
        v = email.value
        slots = {"message_id": v.get("id"), "thread_id": v.get("thread_id"), "to_email": v.get("from_email"),
                 "recipient": v.get("from_name") or v.get("from_email"), "subject": v.get("subject", ""),
                 "in_reply_to": v.get("message_id_header")}
        if m.group("msg"):
            slots["message"] = m.group("msg").strip()
        return FollowUpResult(NLUResult(text=raw, intent=Intent.REPLY_EMAIL, confidence=0.95, slots=slots, source="context"))

    # --- undo the last action ----------------------------------------------------
    if re.fullmatch(r"(?:please\s+)?(?:undo|revert|roll back|reverse|take back)(?: that| it| this| the (?:last )?(?:change|fix|action|thing)|"
                    r" what you (?:just )?did| your (?:last )?change| the last one)?(?: please)?", low):
        while st.undo_stack:
            entry = st.undo_stack.pop()
            if entry.get("irreversible"):
                return FollowUpResult(reply=entry["irreversible"])
            if entry.get("memory"):
                return FollowUpResult(NLUResult(text=raw, intent=Intent.MEMORY, confidence=0.95, slots={"memory_op": "forget_last"},
                                                source="context"))
            return _fu(raw, entry["tool"], dict(entry["args"]), entry["description"], undo=True)
        f = st.top_focus({"patch"})
        if f is not None:
            return _fu(raw, "restore_backup", {"path": f.value["path"], "backup": f.value["backup"]}, "Undo my change", undo=True)
        return FollowUpResult(question="Undo what? I haven't done anything in this conversation that I can reverse.")

    # --- corrections: "no, I meant AI internships" ---------------------------------
    corr = _correction(raw, t, st)
    if corr is not None:
        return corr
    if re.fullmatch(r"(?:fix|repair|solve) (?:it|that|this|them|that one|this one|the problem|the issue|the error)", low):
        if st.last_list is not None and st.last_list.kind == "issue" and st.last_list.items:
            idx = st.last_list.selected if st.last_list.selected is not None else 0
            st.last_list.selected = idx
            return _action_for("issue", st.last_list.items[idx], "fix", raw)
        return None

    # --- ordinals over the last list ------------------------------------
    m = _ORD_RE.match(t)
    if m:
        if st.last_list is None or not st.last_list.items:
            if (m.group("verb") or "").lower() in ("click", "press", "tap", "hit"):
                return None  # "click the second button" with no list yet → the planner looks at the page/window first
            if m.group("verb") or re.search(r"\b(one|email|file|result|issue)\b", low):
                return FollowUpResult(question="Which list do you mean? I haven't shown you any results yet in this conversation.")
            return None
        noun = (m.group("noun") or "").lower()
        if st.last_list.kind == "element" and noun in ("button", "link", "field"):
            # "click the second button" counts buttons only, not every item in the list
            role_items = [it for it in st.last_list.items if it.get("role") == noun or
                          (noun == "button" and it.get("control_type") == "Button") or (noun == "link" and it.get("control_type") == "Hyperlink")]
            n = ORDINALS.get(m.group("ord").lower())
            if n is not None and n < len(role_items):
                return _action_for("element", role_items[n], "click", raw)
            if n is not None:
                return FollowUpResult(question=f"I only see {len(role_items)} {noun}{'s' if len(role_items) != 1 else ''} there.")
        idx = _pick_index(st, m.group("ord"))
        if idx is None:
            return FollowUpResult(question=f"There {'is' if len(st.last_list.items) == 1 else 'are'} only "
                                           f"{len(st.last_list.items)} item(s) — which one do you mean?")
        verb = (m.group("verb") or (st.last_list.verb if m.group("ord").lower() in ("other", "next", "previous") else "open")).lower()
        if verb == "reply to" and st.last_list.kind == "email":
            st.last_list.selected = idx
            return None
        res = _action_for(st.last_list.kind, st.last_list.items[idx], verb, raw)
        if res is not None:
            st.last_list.selected = idx
            st.last_list.verb = verb
            if res.nlu is not None:
                res.nlu.slots["list_index"] = idx
        return res

    # --- "open the one from my professor" --------------------------------
    m = _FROM_RE.match(t)
    if m and st.last_list is not None and st.last_list.kind == "email":
        who = m.group("who").lower().removeprefix("my ").strip()
        for i, it in enumerate(st.last_list.items):
            if who and who in (it.get("from", "") + " " + it.get("from_name", "")).lower():
                st.last_list.selected = i
                return _action_for("email", it, "open", raw)

    # --- "open it" / "close that" ---------------------------------------
    m = _PRONOUN_RE.match(t)
    if m:
        verb = m.group("verb").lower()
        if verb in ("click", "press", "tap", "select"):
            f = st.top_focus({"element"})
            if f is None:
                return FollowUpResult(question="Click what? Tell me the button's name, or say “find the … button” first.")
            return _action_for("element", f.value, "click", raw)
        if verb in ("close", "quit", "exit"):
            f = st.top_focus({"app"})
            if f is None:
                return FollowUpResult(question="What should I close?")
            return _action_for("app", f.value, "close", raw)
        if verb == "play" and st.last_list is not None and st.last_list.kind == "video" and st.last_list.items:
            idx = st.last_list.selected or 0
            st.last_list.selected = idx
            return _action_for("video", st.last_list.items[idx], verb, raw)
        f = st.top_focus()
        if f is None:
            if st.last_list and st.last_list.items:
                return _action_for(st.last_list.kind, st.last_list.items[st.last_list.selected or 0], verb, raw)
            return FollowUpResult(question=f"What should I {verb}?")
        return _action_for(f.kind, f.value, verb, raw)

    # --- "the one we discussed earlier" ---------------------------------
    m = _EARLIER_RE.match(t)
    if m:
        cur = st.top_focus()
        if cur is None:
            return FollowUpResult(question="Which one do you mean? I don't have anything from earlier in this conversation.")
        prev = st.top_focus({cur.kind}, skip=1)
        if prev is None:
            others = [f.label for f in st.focus[1:4] if f.label]
            if not others:
                return FollowUpResult(question="Which one do you mean?")
            return FollowUpResult(question="Do you mean " + " or ".join(f"“{o}”" for o in others) + "?")
        return _action_for(prev.kind, prev.value, m.group("verb") or m.group("verb2") or "open", raw)

    # --- go back -----------------------------------------------------------
    if _BACK_RE.match(t):
        if st.top_focus({"url"}) is not None and st.top_focus().kind == "url":
            if st.top_focus().value.get("genesis_browser", True):
                return _fu(raw, "browser_back", {}, "Go back")
        top = st.top_focus()
        if top is not None and top.kind in ("url", "media") and not top.value.get("genesis_browser"):
            site = _site_word(top.value.get("url", ""))
            steps = [{"tool": "manage_window", "args": {"window": site, "action": "focus"}, "description": f"Switch to {site}"},
                     {"tool": "computer_press", "args": {"keys": "alt+left"}, "description": "Go back a page"}]
            return FollowUpResult(NLUResult(text=raw, intent=Intent.FOLLOW_UP, confidence=0.9, source="context",
                                            slots={"steps": steps, "tool": "computer_press", "back": True}))
        if len(st.folder_history) >= 1:
            prev = st.folder_history.pop()
            return _fu(raw, "open_folder", {"folder": prev}, f"Go back to {Path(prev).name}", back=True)
        f = st.top_focus({"folder", "file"})
        if f is not None:
            parent = str(Path(f.value["path"]).parent)
            return _fu(raw, "open_folder", {"folder": parent}, f"Open {Path(parent).name}")
        return FollowUpResult(question="Go back to what?")
    return None
