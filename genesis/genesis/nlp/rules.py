"""High-precision rule-based NLU (intent patterns + slot extractors).

This layer handles the most common, safety-relevant commands deterministically
(and offline). Anything it doesn't recognise falls through to the statistical
classifier and then to the LLM (see :mod:`genesis.nlp.pipeline`).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

from ..services.app_catalog import lookup as app_lookup
from .entities import (
    EMAIL_RE,
    extract_entities,
    find_apps,
    find_filenames,
    find_folder,
    find_paths,
    find_urls,
    parse_datetime,
    remove_spans,
)
from .intents import Entity, Intent, NLUResult

I = re.I
WAKE_RE = re.compile(r"^\s*(?:(?:hey|hi|hello|okay|ok|yo|oi)[\s,]+)?(?:genesis|jenesis|genisis|genesys)\b[\s,.:!\-]*", I)
POLITE_RE = re.compile(r"^(?:(?:please|kindly|can you|could you|would you|will you|i want you to|i need you to|i'd like you to|"
                       r"go ahead and|just)\s+)+", I)

ACTION_VERBS = (
    r"(?:open|launch|start|run|close|quit|exit|kill|search|google|look up|find|locate|draft|write|compose|send|email|"
    r"create|make|remind|set|rename|move|copy|delete|remove|trash|attach|shut|restart|reboot|lock|play|go to|navigate|"
    r"visit|read|check|show|list|explain|summari[sz]e|mute|unmute|turn|put|add|index|remember|forget)"
)
_PROTECT_RE = re.compile(
    r"\b(saying|that says|to say|telling (?:him|her|them)|to tell|and tell (?:him|her|them)|letting|remind me(?: to| about| that)?|"
    r"reminder (?:to|about)|with the message|message:)\b",
    I,
)
_SPLIT_RE = re.compile(
    rf"\s*(?:,\s*(?:and\s+)?(?:then\s+)?|;\s*|\s+(?:and then|then|after that|and also|and)\s+)(?=(?:please\s+)?(?:also\s+)?{ACTION_VERBS}\b)",
    I,
)


def normalize(text: str) -> str:
    t = (text or "").strip()
    t = WAKE_RE.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = t.rstrip(" .!")
    return t


def strip_polite(text: str) -> str:
    return POLITE_RE.sub("", text).strip()


def split_requests(text: str) -> list[str]:
    """Split compound commands: 'open Gmail and draft an email to X saying ...'."""
    text = normalize(text)
    m = _PROTECT_RE.search(text)
    head, tail = (text[:m.start()], text[m.start():]) if m else (text, "")
    parts = [p.strip(" ,") for p in _SPLIT_RE.split(head) if p and p.strip(" ,")]
    if not parts:
        return [text] if text else []
    parts[-1] = (parts[-1] + " " + tail).strip()
    return parts


# ============================================================ slot extractors
_EMAIL_STOP = (r"(?=\s+(?:saying|that says|that|telling|to say|to tell|and say|and tell|and let|letting|about|regarding|"
               r"re|with (?:the )?subject|asking|informing|and ask|to ask)\b|[,:;]|$)")
_EMAIL_RECIP_RE = re.compile(
    r"\b(?:e-?mail|mail|message|note|letter|reply|write|draft|compose|send)\b"
    r"(?:\s+(?:an?|the|a quick|a short|a brief|quick|short|formal))?(?:\s+(?:e-?mail|mail|message|note|reply))?\s+to\s+(?P<r>.+?)" + _EMAIL_STOP,
    I,
)
_BARE_RECIP_RE = re.compile(r"^(?:e-?mail|mail|message|write to)\s+(?P<r>(?!to\b|an?\b|the\b).+?)" + _EMAIL_STOP, I)
_MSG_RE = re.compile(
    r"\b(?P<marker>saying(?: that)?|that says|to say(?: that)?|and say(?: that)?|say(?: that)?|"
    r"telling (?:him|her|them)(?: that)?|to tell (?:him|her|them)(?: that)?|and tell (?:him|her|them)(?: that)?|tell (?:him|her|them)(?: that)?|"
    r"letting (?:him|her|them) know(?: that)?|and let (?:him|her|them) know(?: that)?|let (?:him|her|them) know(?: that)?|"
    r"informing (?:him|her|them)(?: that)?|asking (?:him|her|them)?\s*(?:to|if|whether)?|and ask (?:him|her|them)?\s*(?:to|if|whether)?|"
    r"with the message|message:)\s*(?P<m>.+)$",
    I,
)
_SUBJECT_RE = re.compile(
    r"\b(?:with (?:the )?subject(?: line)?|subject(?: line)?(?: is)?)\s*[:\-]?\s*[\"“']?(?P<s>.+?)[\"”']?(?=\s+(?:saying|and (?:say|tell)|body|message)\b|$)",
    I,
)
_ABOUT_RE = re.compile(r"\b(?:about|regarding|re)\s+(?P<a>.+?)(?=\s+(?:saying|and say|and tell)\b|$)", I)


def _clean_recipient(r: str) -> str:
    r = re.sub(r"\b(please|now|asap|quickly|for me)\b", "", r, flags=I)
    r = re.sub(r"\s+(on|via|through|using)\s+(gmail|email|mail)\b.*$", "", r, flags=I)
    return re.sub(r"\s{2,}", " ", r).strip(" ,.")


def email_slots(text: str) -> dict[str, Any]:
    slots: dict[str, Any] = {}
    m = _EMAIL_RECIP_RE.search(text) or _BARE_RECIP_RE.search(text)
    rest_after_recipient = ""
    if m:
        recip = _clean_recipient(m.group("r"))
        if recip and recip.lower() not in {"me", "myself"}:
            em = EMAIL_RE.search(recip)
            if em:
                slots["email"] = em.group(0)
                name_part = recip.replace(em.group(0), "").strip(" <>()")
                slots["recipient"] = name_part or em.group(0)
            else:
                slots["recipient"] = recip
        elif recip.lower() in {"me", "myself"}:
            slots["recipient"] = "me"
        rest_after_recipient = text[m.end():].strip()
    else:
        em = EMAIL_RE.search(text)
        if em:
            slots["email"] = em.group(0)
            slots["recipient"] = em.group(0)

    mm = _MSG_RE.search(text)
    if mm:
        msg = mm.group("m").strip().strip("\"“”'")
        if msg.endswith("?") and not re.match(r"^(can|could|would|will|are|is|do|did|does|what|when|where|why|how|should|shall)\b", msg, I):
            msg = msg[:-1].rstrip()
        marker = mm.group("marker").lower()
        if marker.startswith(("asking", "and ask")):
            msg = re.sub(r"^and\s+", "", marker) + " " + msg
        slots["message"] = msg
    elif rest_after_recipient.lower().startswith("that "):
        slots["message"] = rest_after_recipient[5:].strip()
    sm = _SUBJECT_RE.search(text)
    if sm:
        slots["subject"] = sm.group("s").strip()
    am = _ABOUT_RE.search(rest_after_recipient or text)
    if am and "message" not in slots:
        slots["topic"] = am.group("a").strip()
        slots["message"] = am.group("a").strip()
    if re.search(r"\b(it|the (?:file|document|pdf))\b.*\battach|\battach(?:ing)?\b", text, I):
        slots["attach_last_file"] = True
    return slots


def reminder_slots(text: str, now: Optional[datetime] = None) -> dict[str, Any]:
    slots: dict[str, Any] = {}
    dt = parse_datetime(text, now)
    body = text
    if dt:
        slots["datetime"] = dt.value
        body = remove_spans(text, dt.spans)
    m = re.search(r"\b(?:remind me|set (?:a |an |me a )?(?:reminder|alarm)|create (?:a )?reminder|add (?:a )?reminder|"
                  r"reminder)\b\s*(?P<rest>.*)$", body, I)
    rest = (m.group("rest") if m else body).strip(" ,.")
    rest = re.sub(r"^(?:for|at|on)\s+(?=(?:to|that|about)\b)", "", rest, flags=I)
    rest = re.sub(r"^(?:to|that|about|of|for|saying)\s+", "", rest, flags=I)
    rest = re.sub(r"\s+(?:to|that|about|at|on|for)$", "", rest, flags=I).strip(" ,.")
    if rest:
        slots["reminder_text"] = rest
    return slots


_SEARCH_RE = re.compile(
    r"^(?:search|google|look up|lookup|bing|research|find out)"
    r"(?:\s+(?:on\s+|in\s+)?(?:the\s+)?(?:web|internet|online|google|net))?(?:\s+(?:for|about|on))?\s+(?P<q>.+)$",
    I,
)


def search_slots(text: str) -> dict[str, Any]:
    t = strip_polite(text)
    m = _SEARCH_RE.match(t)
    q = m.group("q") if m else t
    q = re.sub(r"\s+(on|in) (the )?(web|internet|google)$", "", q, flags=I).strip(" ?.")
    return {"query": q} if q else {}


_EXT_WORDS = {
    r"\bpdfs?\b": ".pdf", r"\b(word documents?|docx?|word files?)\b": ".docx", r"\b(excel|spreadsheets?|xlsx)\b": ".xlsx",
    r"\b(powerpoints?|presentations?|slides?|pptx?)\b": ".pptx", r"\b(images?|photos?|pictures?|screenshots?|pngs?|jpe?gs?)\b": "image",
    r"\bpython (files?|scripts?)\b": ".py", r"\b(text files?|txt)\b": ".txt", r"\b(videos?|mp4)\b": "video",
    r"\b(zip files?|zips?|archives?)\b": ".zip", r"\b(csv files?|csvs?)\b": ".csv",
}
_FILE_STOP = {
    "find", "search", "for", "locate", "look", "where", "is", "my", "the", "a", "an", "latest", "newest", "most", "recent",
    "recently", "last", "file", "files", "document", "documents", "in", "on", "folder", "from", "all", "show", "me", "of", "that",
    "which", "i", "downloaded", "saved", "modified", "named", "called", "with", "and", "pdf", "pdfs", "docx", "doc", "please",
    "list", "any", "some", "desktop", "downloads", "pictures", "music", "videos", "home", "computer", "pc", "at",
}


def file_search_slots(text: str) -> dict[str, Any]:
    low = text.lower()
    slots: dict[str, Any] = {"file_op": "search"}
    for pattern, ext in _EXT_WORDS.items():
        if re.search(pattern, low):
            slots["extension"] = ext
            low = re.sub(pattern, " ", low)
            break
    fn = find_filenames(text)
    if fn:
        slots["query"] = fn[0].value
    if re.search(r"\b(latest|newest|most recent|recent|last|recently)\b", low):
        slots["sort"] = "newest"
    folder = find_folder(text)
    if folder:
        slots["folder"] = folder.value
    if "query" not in slots:
        words = [w for w in re.findall(r"[\w\-]+", low) if w not in _FILE_STOP and not w.isdigit()]
        if words:
            slots["query"] = " ".join(words)
    return slots


def _name_after(text: str, words: str = r"called|named|titled") -> Optional[str]:
    m = re.search(rf"\b(?:{words})\s+[\"“']?(?P<n>[^\"”']+?)[\"”']?(?=\s+(?:in|on|inside|under|at|to)\s|$)", text, I)
    return m.group("n").strip(" .") if m else None


def _location_after(text: str) -> Optional[str]:
    paths = find_paths(text)
    m = re.search(r"\b(?:in|on|inside|under|into|to)\s+(?:the\s+|my\s+)?(desktop|documents|downloads|pictures|music|videos|home)\b", text, I)
    if m:
        return m.group(1).lower()
    for p in paths:
        pre = text[max(0, p.start - 10):p.start].lower()
        if re.search(r"\b(in|inside|under|into|to|at)\s*$", pre):
            return p.value
    return None


def file_op_slots(text: str, op: str) -> dict[str, Any]:
    slots: dict[str, Any] = {"file_op": op}
    paths = find_paths(text)
    files = find_filenames(text)
    if op in {"create_file", "create_folder"}:
        name = _name_after(text)
        if not name and files:
            name = files[0].value
        if name:
            slots["name"] = name
        loc = _location_after(text)
        if loc:
            slots["location"] = loc
        m = re.search(r"\b(?:with|containing)\s+(?:the\s+)?(?:content|text)\s*[:\-]?\s*[\"“']?(?P<c>.+?)[\"”']?$", text, I)
        if m:
            slots["content"] = m.group("c")
        return slots
    if op in {"move", "copy"}:
        m = re.search(r"\b(?:move|copy)\s+(?P<src>.+?)\s+(?:to|into|in)\s+(?P<dst>.+)$", text, I)
        if m:
            slots["source"] = _strip_article(m.group("src"))
            dst = m.group("dst").strip(" .")
            folder = find_folder(dst)
            slots["destination"] = folder.value if folder and len(dst.split()) <= 3 else dst
        return slots
    if op == "rename":
        m = re.search(r"\brename\s+(?P<src>.+?)\s+(?:to|as|into)\s+[\"“']?(?P<new>.+?)[\"”']?$", text, I)
        if m:
            slots["source"] = _strip_article(m.group("src"))
            slots["new_name"] = m.group("new").strip(" .")
        else:
            m = re.search(r"\brename\s+(?P<src>.+)$", text, I)
            if m:
                slots["source"] = _strip_article(m.group("src"))
        return slots
    # read / open / delete
    if paths:
        slots["source"] = paths[0].value
    elif files:
        slots["source"] = files[0].value
    else:
        m = re.search(r"\b(?:read|open|delete|remove|trash|summari[sz]e|show)\s+(?P<src>.+)$", text, I)
        if m:
            slots["source"] = _strip_article(m.group("src"))
    return slots


def _strip_article(s: str) -> str:
    s = s.strip(" .\"'“”")
    s = re.sub(r"^(?:the|my|a|an|this|that)\s+", "", s, flags=I)
    s = re.sub(r"\s+(?:file|document|folder)$", "", s, flags=I)
    return s.strip()


def app_slot(text: str) -> Optional[str]:
    apps = find_apps(text)
    if apps:
        return apps[0].value
    m = re.search(r"\b(?:open|launch|start|run|fire up|bring up|close|quit|exit|kill|terminate)\s+(?:up\s+)?(?:the\s+|my\s+)?(?P<a>.+?)"
                  r"(?:\s+(?:app|application|program|window))?$", strip_polite(text), I)
    if m:
        name = m.group("a").strip(" .")
        name = re.sub(r"\s+(?:app|application|program|window)$", "", name, flags=I)
        return name.lower() if name else None
    return None


def project_slot(text: str) -> Optional[str]:
    m = (re.search(r"\bproject\s+(?:called|named)\s+[\"“']?(?P<p>[\w .\-]+?)[\"”']?(?:\s+in\s+.*)?$", text, I)
         or re.search(r"\b(?:my|the|our)\s+(?P<p>[\w.\-]+(?:\s+[\w.\-]+)?)\s+project\b", text, I)
         or re.search(r"\b(?P<p>[\w.\-]+)\s+project\b", text, I))
    if m:
        p = m.group("p").strip().lower()
        if p not in {"my", "the", "a", "this", "that", "current", "last"}:
            return p
    return None


# ================================================================ main rules
def _r(text: str, intent: Intent, slots: Optional[dict] = None, conf: float = 0.95) -> NLUResult:
    return NLUResult(text=text, intent=intent, confidence=conf, slots=slots or {}, entities=extract_entities(text), source="rules")


def parse(text: str, now: Optional[datetime] = None) -> Optional[NLUResult]:
    """Return an NLUResult if a high-precision rule matches, else ``None``."""
    raw = normalize(text)
    t = strip_polite(raw)
    low = t.lower()
    if not low:
        return None

    # ---- cancel / UI ----------------------------------------------------
    if re.search(r"\b(cancel|abort|stop)\s+(the\s+)?(shutdown|shut down|restart|reboot)\b", low):
        return _r(raw, Intent.SYSTEM_CONTROL, {"system_action": "cancel_shutdown"})
    if re.fullmatch(r"(cancel|never ?mind|forget it|stop|abort)( that| it| this)?( please)?", low):
        return _r(raw, Intent.CANCEL)
    if re.search(r"\b(open|show|expand|bring up)\s+(?:the\s+|your\s+)?(chat|conversation|full (window|mode|screen)|main window|chat window)\b", low):
        return _r(raw, Intent.UI_CONTROL, {"ui_action": "open_chat"})
    if re.fullmatch(r"(hide|minimi[sz]e|go away|dismiss|close)( yourself| genesis| the chat| chat| window)?", low):
        return _r(raw, Intent.UI_CONTROL, {"ui_action": "hide"})
    if re.search(r"\b(new|start a new|start new|fresh)\s+(conversation|chat)\b", low):
        return _r(raw, Intent.MEMORY, {"memory_op": "new_conversation"})
    if re.search(r"\bclear\s+(?:the\s+|this\s+|our\s+)?(conversation|chat|history)\b", low):
        return _r(raw, Intent.MEMORY, {"memory_op": "clear_conversation"})

    # ---- settings ---------------------------------------------------------
    setting_rules = [
        (r"\b(stop|pause|disable|turn off)\s+(listening|the microphone|the mic|wake word)\b|\bmute (the )?mic", "listening", False),
        (r"\b(start|resume|enable|turn on)\s+(listening|the microphone|the mic|wake word)\b|\bunmute (the )?mic", "listening", True),
        (r"\b(mute|disable|turn off)\s+(your\s+)?(voice|speech|tts)\b|\bstop talking\b|\bbe quiet\b|\bdon'?t speak\b", "tts", False),
        (r"\b(unmute|enable|turn on)\s+(your\s+)?(voice|speech|tts)\b|\bstart talking\b|\bspeak (to me )?again\b", "tts", True),
        (r"\b(disable|turn off|stop using)\s+(your\s+)?memory\b|\bdon'?t remember (anything|things)\b", "memory", False),
        (r"\b(enable|turn on|start using)\s+(your\s+)?memory\b", "memory", True),
        (r"\b(don'?t|do not|stop)\s+start(ing)?\s+(genesis\s+|yourself\s+)?(with|on)\s+windows\b|\bdisable (auto ?start|startup)\b", "start_with_windows", False),
        (r"\bstart\s+(genesis\s+|yourself\s+)?(with|on|at)\s+(windows|startup|boot|login)\b|\benable (auto ?start|startup)\b", "start_with_windows", True),
    ]
    for pattern, setting, value in setting_rules:
        if re.search(pattern, low):
            return _r(raw, Intent.SETTINGS, {"setting": setting, "value": value})
    if re.search(r"\b(open|show)\s+(the\s+)?settings\b", low) and "windows settings" not in low:
        return _r(raw, Intent.UI_CONTROL, {"ui_action": "open_settings"})

    # ---- reminders (before power actions: 'remind me to shut down...') -----
    if re.search(r"\b(show|list|what are|what'?s|any|read)\b.*\breminders?\b", low):
        return _r(raw, Intent.INFORMATION_REQUEST, {"topic": "reminders"})
    if re.search(r"\b(remind me|set (a |an |me a )?(reminder|alarm)|create (a )?reminder|add (a )?reminder|reminder (to|for|about))\b", low):
        return _r(raw, Intent.CREATE_REMINDER, reminder_slots(raw, now))

    # ---- memory -------------------------------------------------------------
    if re.search(r"\bwhat do you (know|remember)\b|\b(show|list|view)\s+(me\s+)?(your|my)?\s*memor(y|ies)\b|\bwhat have you remembered\b", low):
        return _r(raw, Intent.MEMORY, {"memory_op": "list"})
    m = re.match(r"^(?:forget|delete|remove)\s+(?:that\s+|about\s+|the memory\s+(?:about\s+)?)?(?P<x>.+)$", t, I)
    if m and re.match(r"^forget\b", low):
        return _r(raw, Intent.MEMORY, {"memory_op": "forget", "memory_text": m.group("x").strip()})
    m = re.match(r"^(?:remember|note|keep in mind|don'?t forget)\s+(?:that\s+)?(?P<x>.+)$", t, I)
    if m:
        return _r(raw, Intent.MEMORY, {"memory_op": "add", "memory_text": m.group("x").strip(), **_contact_from_text(m.group("x"))})
    m = re.match(r"^(?:(?P<who>[\w .]+?)'s|(?:his|her|their))\s+e-?mail(?: address)? is\s+(?P<e>\S+@\S+)$", t, I)
    if m:
        slots = {"memory_op": "add_contact", "email": m.group("e").strip(".,")}
        if m.group("who"):
            slots["contact_name"] = m.group("who").strip()
        return _r(raw, Intent.MEMORY, slots)

    # ---- power -------------------------------------------------------------
    device = r"(?:the |my |this )?(?:computer|pc|laptop|system|machine|windows|device|desktop)"
    if re.search(rf"\b(shut\s*down|power\s*off|switch off|turn off)\s+{device}\b", low) or re.fullmatch(r"(shut\s*down|power off|shutdown)( now| please)?", low):
        return _r(raw, Intent.SHUTDOWN, {"system_action": "shutdown"})
    if re.search(rf"\b(restart|reboot)\s+{device}\b", low) or re.fullmatch(r"(restart|reboot)( now| please)?", low):
        return _r(raw, Intent.RESTART, {"system_action": "restart"})
    if re.search(rf"\block\s+{device}\b|\block (the )?screen\b|\block (it|up)\b|^lock$|\block my (computer|pc|laptop)", low):
        return _r(raw, Intent.SYSTEM_CONTROL, {"system_action": "lock"})
    if re.search(rf"\b(put|send)\s+{device}\s+to sleep\b|\b(sleep|hibernate|suspend)\s+{device}\b|^(go to )?sleep( now)?$|\bsleep mode\b", low):
        return _r(raw, Intent.SYSTEM_CONTROL, {"system_action": "sleep"})
    if re.search(r"\b(volume up|turn (it |the volume )?up|increase (the )?volume|louder|raise (the )?volume)\b", low):
        return _r(raw, Intent.SYSTEM_CONTROL, {"system_action": "volume_up"})
    if re.search(r"\b(volume down|turn (it |the volume )?down|decrease (the )?volume|quieter|lower (the )?volume)\b", low):
        return _r(raw, Intent.SYSTEM_CONTROL, {"system_action": "volume_down"})
    if re.search(r"\b(mute|unmute)\s+(the\s+)?(sound|volume|audio|computer|speakers?)\b|^(mute|unmute)$", low):
        return _r(raw, Intent.SYSTEM_CONTROL, {"system_action": "mute"})
    if re.search(r"\b(battery|cpu usage|ram usage|memory usage|disk space|storage left|system (info|information|status)|uptime)\b", low):
        return _r(raw, Intent.SYSTEM_CONTROL, {"system_action": "info"})

    # ---- information (local) -------------------------------------------
    if re.search(r"\bwhat('?s| is)? (the )?(current )?(time|date)\b|\bwhat day is (it|today)\b|\btoday'?s date\b|\bwhat time is it\b", low):
        return _r(raw, Intent.INFORMATION_REQUEST, {"topic": "datetime"})

    # ---- e-mail ------------------------------------------------------------
    if re.fullmatch(r"(send|send it|send the (draft|e-?mail|mail)|send that( e-?mail| draft)?|send it now|go ahead and send it)", low):
        return _r(raw, Intent.SEND_EMAIL, {"use_last_draft": True})
    is_mail = re.search(r"\b(e-?mails?|mails?|inbox|messages?)\b", low)
    if is_mail and re.search(r"\b(check|read|show|any|find|search|list|open)\b", low) and not re.search(r"\b(draft|write|compose|send|reply)\b", low) \
            and not re.fullmatch(r"open (my |the )?(e-?mail|mail|inbox|gmail)( app)?", low):
        slots = {}
        m = re.search(r"\bfrom\s+(?P<f>[\w@.\- ]+?)(?:\s+(?:about|today|this week)|$)", t, I)
        if m:
            slots["from"] = m.group("f").strip()
        m = re.search(r"\babout\s+(?P<q>.+)$", t, I)
        if m:
            slots["query"] = m.group("q").strip()
        if "unread" in low:
            slots["unread"] = True
        return _r(raw, Intent.READ_EMAIL, slots)
    if re.search(r"\b(draft|write|compose|prepare|create|type up)\b.*\b(e-?mail|mail|message|reply|note)\b", low):
        return _r(raw, Intent.DRAFT_EMAIL, email_slots(t))
    if re.search(r"\b(send|shoot|fire off)\b.*\b(e-?mail|mail|message|note)\b", low) or re.match(r"^(e-?mail|mail)\s+(?!to\b)\S", low) \
            or re.match(r"^write to\s", low):
        return _r(raw, Intent.SEND_EMAIL, email_slots(t))

    # ---- knowledge base -----------------------------------------------------
    if re.search(r"\b(add|index|ingest|import|learn|load)\b.*\b(to|into)\s+(your |my |the )?(knowledge( base)?|brain|documents)\b", low) \
            or re.match(r"^(index|ingest|learn from)\s+", low):
        paths = find_paths(t) or find_filenames(t)
        return _r(raw, Intent.KNOWLEDGE, {"knowledge_op": "ingest", "path": paths[0].value if paths else None})
    m = re.search(r"\b(?:according to|based on|in|from|search|ask|check)\s+my\s+(?:documents|notes|files|knowledge base|knowledge)\b[,:]?\s*(?P<q>.*)$", t, I)
    if m:
        q = m.group("q").strip() or re.sub(r"\b(according to|based on|in|from|search|ask|check)\s+my\s+(documents|notes|files|knowledge base|knowledge)\b[,:]?", "", t, flags=I).strip()
        return _r(raw, Intent.KNOWLEDGE, {"knowledge_op": "query", "query": q})

    # ---- code / VS Code ------------------------------------------------------
    if re.search(r"\bproject\b", low) and re.search(r"\b(open|launch|load|start|show)\b", low):
        return _r(raw, Intent.CODE_ACTION, {"code_action": "open_project", "project": project_slot(t)})
    if re.search(r"\b(find|check|fix|look for|detect|spot|debug|any)\b.*\b(errors?|bugs?|issues?|problems?|mistakes?)\b", low) \
            and re.search(r"\b(file|code|script|this|it|\.py|\.js)\b", low):
        paths = find_paths(t) or find_filenames(t)
        return _r(raw, Intent.CODE_ACTION, {"code_action": "check_file", "path": paths[0].value if paths else None})
    m = re.match(r"^explain\s+(?:this|the|my)?\s*(?P<what>error|exception|traceback|code|file|function)\b\s*[:\-]?\s*(?P<x>.*)$", t, I | re.S)
    if m:
        paths = find_paths(t) or find_filenames(t)
        return _r(raw, Intent.CODE_ACTION, {"code_action": "explain", "error_text": m.group("x").strip() or None,
                                            "target": m.group("what").lower(), "path": paths[0].value if paths else None})
    if re.search(r"\bin (vs ?code|visual studio code|the editor|code editor)\b", low) and re.search(r"\b(open|show|edit)\b", low):
        paths = find_paths(t) or find_filenames(t)
        if paths:
            return _r(raw, Intent.CODE_ACTION, {"code_action": "open_file", "path": paths[0].value})

    # ---- browser actions ------------------------------------------------------
    if re.search(r"\b(read|summari[sz]e|what does)\b.*\b(this|the|current|that)\s+(page|website|site|article|web ?page|tab)\b", low):
        urls = find_urls(t)
        return _r(raw, Intent.BROWSER_ACTION, {"browser_op": "read", "url": urls[0].value if urls else None})
    m = re.match(r"^click\s+(?:on\s+)?(?:the\s+)?[\"“']?(?P<x>.+?)[\"”']?(?:\s+(?:button|link))?$", t, I)
    if m:
        return _r(raw, Intent.BROWSER_ACTION, {"browser_op": "click", "target": m.group("x")})
    m = re.match(r"^(?:type|fill(?: in)?|enter)\s+[\"“']?(?P<v>.+?)[\"”']?\s+(?:in|into)\s+(?:the\s+)?(?P<f>.+?)(?:\s+(?:field|box|input))?$", t, I)
    if m:
        return _r(raw, Intent.BROWSER_ACTION, {"browser_op": "fill", "value": m.group("v"), "target": m.group("f")})

    # ---- files -----------------------------------------------------------------
    if re.search(r"\b(create|make|new|add)\b.*\b(folder|directory)\b", low):
        return _r(raw, Intent.FILE_OPERATION, file_op_slots(t, "create_folder"))
    if re.search(r"\b(create|make|new)\b.*\b(file|document)\b", low) or re.match(r"^(create|make|new)\s+[\w\-]+\.\w{1,6}\b", low):
        return _r(raw, Intent.FILE_OPERATION, file_op_slots(t, "create_file"))
    if re.match(r"^rename\b", low):
        return _r(raw, Intent.FILE_OPERATION, file_op_slots(t, "rename"))
    if re.match(r"^(move|copy)\b", low):
        return _r(raw, Intent.FILE_OPERATION, file_op_slots(t, low.split()[0]))
    if re.match(r"^(delete|remove|trash|erase)\b", low) and not re.search(r"\b(reminder|memory|contact)\b", low):
        return _r(raw, Intent.FILE_OPERATION, file_op_slots(t, "delete"))
    if re.search(r"\b(find|search for|locate|look for|where is|where are|show me)\b", low) and (
            re.search(r"\b(files?|pdfs?|documents?|docx?|images?|photos?|pictures?|spreadsheets?|presentations?|screenshots?|videos?|zips?|downloads?)\b", low)
            or find_filenames(t)):
        return _r(raw, Intent.FILE_OPERATION, file_search_slots(t))
    if re.match(r"^(read|summari[sz]e|what'?s in|show me the contents of)\b", low) and (find_paths(t) or find_filenames(t) or re.search(r"\b(file|it|this)\b", low)):
        return _r(raw, Intent.FILE_OPERATION, file_op_slots(t, "read"))

    # ---- close app ---------------------------------------------------------------
    if re.match(r"^(close|quit|exit|kill|terminate|end)\s+\S", low):
        return _r(raw, Intent.CLOSE_APPLICATION, {"app": app_slot(t)})

    # ---- open things --------------------------------------------------------------
    open_m = re.match(r"^(open|launch|start|run|fire up|bring up|show|go to|visit|navigate to|browse to|load|take me to)\b\s*(?P<rest>.*)$", t, I)
    if open_m:
        rest = open_m.group("rest")
        paths = find_paths(rest)
        urls = find_urls(rest)
        folder = find_folder(rest)
        files = find_filenames(rest)
        if folder and (re.search(r"\bfolder\b", rest, I) or len(rest.split()) <= 3) and not find_apps(rest):
            return _r(raw, Intent.OPEN_FOLDER, {"folder": folder.value})
        code_exts = (".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cpp", ".h", ".cs", ".go", ".rs", ".rb", ".php",
                     ".html", ".css", ".json", ".yaml", ".yml", ".toml", ".md", ".ipynb", ".sql", ".sh")
        if paths:
            if paths[0].value.lower().endswith(code_exts):
                return _r(raw, Intent.CODE_ACTION, {"code_action": "open_file", "path": paths[0].value})
            return _r(raw, Intent.FILE_OPERATION, {"file_op": "open", "source": paths[0].value})
        if urls:
            return _r(raw, Intent.OPEN_URL, {"url": urls[0].value})
        if files:
            if files[0].value.lower().endswith(code_exts):
                return _r(raw, Intent.CODE_ACTION, {"code_action": "open_file", "path": files[0].value})
            return _r(raw, Intent.FILE_OPERATION, {"file_op": "open", "source": files[0].value})
        if re.search(r"\b(folder|directory)\b", rest, I):
            name = re.sub(r"\b(the|my|folder|directory)\b", "", rest, flags=I).strip()
            return _r(raw, Intent.OPEN_FOLDER, {"folder": name or None})
        app = app_slot(t)
        if app:
            return _r(raw, Intent.OPEN_APPLICATION, {"app": app})

    # ---- web search ------------------------------------------------------------------
    if _SEARCH_RE.match(t):
        return _r(raw, Intent.SEARCH_WEB, search_slots(t))
    if re.search(r"\b(latest|news|headlines|today'?s|current|price of|stock price|weather|score|trending|announce(d|ment))\b", low) and \
            re.match(r"^(what|who|when|where|how|is|are|did|tell me|give me|show me)\b", low):
        return _r(raw, Intent.SEARCH_WEB, {"query": t.rstrip("?")}, conf=0.85)
    return None


def _contact_from_text(text: str) -> dict:
    """'my professor is Professor Rao, rao@uni.edu' -> contact slots."""
    em = EMAIL_RE.search(text)
    m = re.match(r"^(?:my\s+)?(?P<alias>[\w ]+?)(?:'s name)?\s+is\s+(?P<name>[^,@]+?)(?:[,;]?\s*(?:and\s+)?(?:(?:his|her|their)\s+)?"
                 r"(?:e-?mail(?: address)?(?: is)?|at|:)?\s*(?P<email>\S+@\S+))?$", text.strip().rstrip("."), I)
    slots: dict[str, Any] = {}
    if m and (em or re.match(r"^my\s", text.strip(), I)):
        slots["memory_op"] = "add_contact"
        slots["contact_name"] = m.group("name").strip()
        slots["contact_alias"] = m.group("alias").strip().lower()
        if m.group("email"):
            slots["email"] = m.group("email").strip(".,")
    elif em:
        name = text[:em.start()].strip(" ,:-")
        name = re.sub(r"(?i)\b(e-?mail( address)?|is|'s|at)\b", "", name).strip(" ,:-")
        if name:
            slots["memory_op"] = "add_contact"
            slots["contact_name"] = name
            slots["email"] = em.group(0).strip(".,")
    return slots


def extract_slots_for(intent: Intent, text: str, now: Optional[datetime] = None) -> dict[str, Any]:
    """Slot extraction for an intent predicted by another component (classifier/LLM)."""
    t = strip_polite(normalize(text))
    if intent in (Intent.DRAFT_EMAIL, Intent.SEND_EMAIL):
        return email_slots(t)
    if intent == Intent.CREATE_REMINDER:
        return reminder_slots(t, now)
    if intent == Intent.SEARCH_WEB:
        return search_slots(t)
    if intent in (Intent.OPEN_APPLICATION, Intent.CLOSE_APPLICATION):
        return {"app": app_slot(t)}
    if intent == Intent.OPEN_FOLDER:
        f = find_folder(t)
        return {"folder": f.value if f else None}
    if intent == Intent.OPEN_URL:
        u = find_urls(t)
        return {"url": u[0].value if u else None}
    if intent == Intent.FILE_OPERATION:
        return file_search_slots(t)
    return {}


def entity_values(entities: list[Entity], etype: str) -> list[Any]:
    return [e.value for e in entities if e.type == etype]


__all__ = ["parse", "normalize", "split_requests", "extract_slots_for", "email_slots", "reminder_slots", "app_lookup"]
