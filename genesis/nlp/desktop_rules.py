"""High-precision rules for the 1.4 desktop-agent capabilities: WhatsApp, Google Colab, images, forms, generic computer
control (type / hotkeys / copy / paste / right- and double-click / drag) and connection diagnosis.

These cover the common phrasings quickly and offline. Anything else still reaches the classifier / LLM NLU and the LLM
planner, which can choose the same tools from the registry — so Genesis isn't limited to these sentences.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from .intents import Intent, NLUResult

I = re.I
OTHER_APPS = r"\b(slack|teams|telegram|discord|signal|sms|imessage|instagram|linkedin|messenger|skype)\b"
MAIL_WORDS = r"\b(e-?mail|mail|gmail|inbox|outlook)\b"
_WHO = r"(?P<who>[A-Za-z][\w'.-]*(?:\s+[A-Za-z][\w'.-]*){0,3}?)"
IMAGE_NOUNS = (r"(?:images|image|pictures|picture|pics|pic|photographs|photograph|photos|photo|wallpapers|wallpaper|illustrations|"
               r"illustration|drawings|drawing|paintings|painting)\b")
NOT_IMAGE_SUBJECTS = re.compile(
    r"^(?:my|your|the (?:latest|last|next|previous|first|second|third|results?|list|page|window|screen|code|file|files|email|emails|"
    r"message|messages|options?|settings|output|error|answer|difference|way)|how|what|why|where|when|which|who|around|more|less|"
    r"again|it|that|this|them|everything|all|some (?:examples?|ideas?)|an? (?:example|summary|overview|list|plan|joke|demo|"
    r"tutorial|video|song|file|email|message|reminder|note|task|event|meeting|way|option|idea|recipe|poem|story))\b", I)
HOTKEY = re.compile(r"^(?:please\s+)?(?:press|hit|push|use)\s+(?:the\s+)?(?P<k>(?:(?:ctrl|control|alt|shift|win|windows|cmd)\s*\+?\s*)+"
                    r"(?:[a-z0-9]|f\d{1,2}|tab|enter|esc|escape|delete|del|left|right|up|down|home|end|space|backspace|page ?up|page ?down)"
                    r"|f\d{1,2}|backspace|delete|home|end|space(?:bar)?|arrow (?:up|down|left|right)|(?:up|down|left|right) arrow)"
                    r"(?:\s+key)?(?:\s+(?P<n>\d+|twice|three)\s*times?)?$", I)


FORMAL = re.compile(r"\b(professor|prof|dr|doctor|sir|madam|ma'am|manager|boss|principal|dean|hr|recruiter|teacher|supervisor|"
                    r"advisor|mentor|client|landlord|team|director|hod|committee|department)\b", I)


def whatsapp_slots(t: str, low: str, in_whatsapp: bool = False) -> Optional[dict]:
    """``in_whatsapp``: the conversation is already about WhatsApp (so "show me the latest message from Mom" means WhatsApp)."""
    if re.search(MAIL_WORDS, low) or re.search(OTHER_APPS, low):
        return None
    wa = in_whatsapp or bool(re.search(r"\bwhats ?app\b", low))
    if re.fullmatch(r"(?:please\s+)?(?:open|launch|start|go to|show|bring up|check)\s+(?:my\s+|the\s+)?whats ?app(?: web)?(?: for me)?", low):
        return {"wa_op": "open"}
    if wa and re.search(r"\b(unread|new|any)\b.*\bmessages?\b|\bmessages?\b.*\b(unread|new)\b", low) and not re.search(r"\bfrom\b", low):
        return {"wa_op": "unread"}
    m = re.match(r"^(?:(?:please\s+)?(?:open|check)\s+(?:my\s+)?whats ?app\s+and\s+)?(?:please\s+)?(?:show|read|get|tell|give|what(?:'s| is| was| were)|"
                 r"find)(?:\s+me)?\s+(?:the\s+|my\s+)?(?P<latest>latest|last|newest|most recent|recent|new)?\s*(?:whats ?app\s+)?"
                 r"(?P<n>messages?|texts?|chats?|msgs?)\s+(?:from|with|by|of)\s+" + _WHO + r"(?:\s+on\s+whats ?app)?\s*\??$", t, I)
    if m and (wa or m.group("n").lower().startswith("text")):
        plural = m.group("n").lower().endswith("s") and not m.group("latest") or (m.group("n").lower().endswith("s") and "recent" in low)
        return {"wa_op": "read", "chat": m.group("who").strip(), "count": 5 if plural else 1, "incoming_only": not plural}
    m = re.match(r"^what did\s+" + _WHO + r"\s+(?:say|send|write|text)(?:\s+(?:me|on whats ?app))*\s*\??$", t, I)
    if m and wa and m.group("who").lower() not in ("he", "she", "they", "you", "i", "we", "it"):
        return {"wa_op": "read", "chat": m.group("who").strip(), "count": 1, "incoming_only": True}
    # ---- drafting / sending
    draft_only = bool(re.match(r"^(?:please\s+)?(?:draft|write|compose)\b", low))
    pats = [  # (pattern, explicitly a chat message?)
        (r"^(?:please\s+)?(?:send|write|draft|compose)\s+(?:a\s+|an\s+)?(?:quick\s+|short\s+)?(?P<noun>whats ?app|message|text|msg)"
         r"(?:\s+message)?\s+(?:to|for)\s+" + _WHO + r"(?:\s+on\s+whats ?app)?(?:\s*(?:saying|that says|that|to say|telling (?:him|her|them)"
         r"(?: that)?|:|,)\s*(?P<msg>.+))?$", None),
        (r"^(?:please\s+)?(?P<noun>message|text|whats ?app|ping|msg)\s+" + _WHO + r"(?:\s+on\s+whats ?app)?\s*(?:saying|that says|that|"
         r"to say|:|,)\s*(?P<msg>.+)$", True),  # "message Rahul saying …" — the chat-app verb
        (r"^(?:please\s+)?(?:tell|let)\s+" + _WHO + r"\s+(?:know\s+)?on\s+whats ?app\s+(?:that\s+)?(?P<msg>.+)$", True),
        (r"^(?:please\s+)?(?:send|write)\s+" + _WHO + r"\s+a\s+(?P<noun>whats ?app|message|text)(?:\s+message)?\s*(?:saying|that says|"
         r"that|:|,)\s*(?P<msg>.+)$", None),
    ]
    for p, explicit in pats:
        m = re.match(p, t, I)
        if not m:
            continue
        who = m.group("who").strip()
        if who.lower() in ("me", "myself", "him", "her", "them", "it"):
            return None
        noun = (m.groupdict().get("noun") or "").lower()
        explicit = bool(explicit or wa or noun.startswith(("whats", "text", "ping", "msg")))
        if not explicit and FORMAL.search(who):
            return None  # "draft a message to my professor" → an e-mail
        msg = (m.groupdict().get("msg") or "").strip().strip("\"“”")
        return {"wa_op": "draft" if draft_only else "send", "chat": who, "text": msg or None, "explicit": explicit}
    return None


def colab_slots(t: str, low: str) -> Optional[dict]:
    if re.fullmatch(r"(?:please\s+)?(?:open|launch|start|go to|bring up)\s+(?:google\s+)?colab(?:oratory)?(?: notebooks?)?(?: for me)?", low):
        return {"colab_op": "open"}
    if re.fullmatch(r"(?:please\s+)?(?:create|make|start|open|new)\s+(?:me\s+)?(?:a\s+|an\s+)?(?:new\s+|blank\s+|empty\s+)?"
                    r"(?:python\s+|colab\s+|jupyter\s+|google colab\s+)?notebook(?:\s+(?:in|on)\s+(?:google\s+)?colab)?(?: for me)?", low):
        return {"colab_op": "new"}
    m = re.match(r"^(?:please\s+)?(?:write|add|put|generate)\s+(?:some\s+)?(?:python\s+)?code\s+(?:in|into|to|on)\s+(?:the\s+|a\s+|my\s+)?"
                 r"(?:google\s+)?(?:colab|notebook|cell)(?:\s+(?:notebook|cell))?\s+(?:that|to|which|for)\s+(?P<task>.+)$", t, I) or \
        re.match(r"^(?:please\s+)?(?:in|on)\s+(?:google\s+)?colab,?\s+(?:write|add|put|generate)\s+(?:some\s+)?(?:python\s+)?code\s+"
                 r"(?:that|to|which|for)\s+(?P<task>.+)$", t, I)
    if m:
        task = m.group("task")
        run = re.search(r"\s+(?:and|then)\s+(?:run|execute)\s+it(?:\s+(?:too|as well|after|afterwards))?$", task, I)
        if run:
            return {"colab_op": "write", "task": task[:run.start()].strip(), "run_after": True}
        return {"colab_op": "write", "task": task}
    if re.fullmatch(r"(?:please\s+)?run\s+(?:the\s+|this\s+|my\s+)?(?:colab\s+)?(?:cell|notebook cell)(?: in colab)?", low):
        return {"colab_op": "run"}
    return None


_PERSONAL = re.compile(r"^(?:my|our|your)\b|\b(?:on|in|from) (?:my|our) (?:computer|pc|laptop|drive|files?|folders?|phone|downloads|"
                       r"desktop|documents|gallery|pictures)\b|\b(?:last|this|next) (?:week|month|year|weekend)\b|\b(?:yesterday|today|"
                       r"tomorrow)\b|\b(?:calendar|schedule|inbox|week|day|agenda)\b", I)


def _personal(x: str) -> bool:
    return bool(_PERSONAL.search((x or "").strip()))


def image_slots(t: str, low: str) -> Optional[dict]:
    m = re.match(r"^(?:please\s+)?(?:can you\s+)?(?:generate|create|make|draw|paint|render|design|imagine|sketch)\s+(?:me\s+)?(?:a\s+|an\s+|some\s+)?"
                 r"(?:(?:new|cool|nice|beautiful|realistic|futuristic|cartoon|simple)\s+)*(?P<noun>(?:ai\s+)?" + IMAGE_NOUNS +
                 r"|art(?:work)?\b|logo\b|poster\b)\s+(?P<prep>of|showing|with|for|about|depicting|that shows)\s+(?P<x>.+)$", t, I)
    if m:
        subject = m.group("x").strip()
        whole = re.sub(r"^(?:please\s+)?(?:can you\s+)?(?:generate|create|make|draw|paint|render|design|imagine|sketch)\s+(?:me\s+)?", "", t, flags=I)
        if not subject:
            return None
        style = re.match(r"^(?:a\s+|an\s+|some\s+)?(?P<adj>(?:(?:new|cool|nice|beautiful|realistic|futuristic|cartoon|simple)\s+)*)", whole, I)
        adj = (style.group("adj") or "").strip() if style else ""
        subject = re.sub(r"^(?:a|an|the|some)\s+", "", subject, flags=I) if adj else subject
        noun = m.group("noun").lower()
        if re.match(r"(logo|poster|art|artwork|wallpaper|illustration|drawing|painting)", noun):
            return {"image_op": "generate", "prompt": f"{adj} {noun} {m.group('prep')} {subject}".strip()}
        return {"image_op": "generate", "prompt": (f"{adj} {subject}" if adj else subject).strip()}
    m = re.match(r"^(?:please\s+)?(?:can you\s+)?draw\s+(?:me\s+)?(?P<x>(?:a|an)\s+.+)$", t, I)
    if m and not re.match(r"^an?\s+(?:line|conclusion|bath|breath|blank|card|salary|comparison|distinction|parallel|diagram of my|"
                          r"number|lot|lottery|straw|check|cheque)\b", m.group("x"), I):
        return {"image_op": "generate", "prompt": m.group("x").strip()}
    m = re.match(r"^(?:please\s+)?(?:can you\s+)?(?:show|find|get|give|search(?: for)?|look up|pull up|display|send)\s+(?:me\s+)?(?:for\s+)?"
                 r"(?:a\s+|an\s+|some\s+|the\s+)?(?:(?P<n>\d|two|three|four)\s+)?(?:good\s+|nice\s+)?" + IMAGE_NOUNS +
                 r"\s+(?:of|showing)\s+(?P<x>.+?)(?:\s+(?:on|from) the (?:web|internet))?\s*[.?!]?$", t, I)
    if m and _personal(m.group("x")):
        return None  # "pictures of my kids", "photos of cats on my computer" → the user's own files, not the web
    if m:
        n = {"two": 2, "three": 3, "four": 4}.get((m.group("n") or "").lower(), int(m.group("n")) if (m.group("n") or "").isdigit() else 1)
        many = re.search(r"\b(images|pictures|pics|photos|photographs)\b", low) is not None
        q = re.sub(r"^(?:a|an|the|some)\s+", "", m.group("x").strip(), flags=I)
        return {"image_op": "search", "query": q, "count": min(4, n if m.group("n") else (3 if many else 1))}
    m = re.match(r"^(?:please\s+)?(?:can you\s+)?(?:show|find|get|display|pull up)\s+(?:me\s+)?(?:a\s+|an\s+|some\s+|the\s+)?"
                 r"(?P<x>[a-z][\w' -]{1,40}?)\s+" + IMAGE_NOUNS + r"(?:\s+(?:from|on) the (?:web|internet))?\s*[.?!]?$", t, I) or \
        re.match(r"^(?:i (?:want|would like|'d like) to see|let me see|can i see)\s+(?:a\s+|an\s+|some\s+)?" + IMAGE_NOUNS +
                 r"\s+(?:of|showing)\s+(?P<x>.+?)\s*[.?!]?$", t, I) or \
        re.match(r"^(?:search|scour|look)\s+(?:the\s+)?(?:internet|web|google)\s+for\s+(?:a\s+|an\s+|some\s+)?" + IMAGE_NOUNS +
                 r"\s+(?:of\s+)?(?P<x>.+?)\s*[.?!]?$", t, I)
    if m and not _personal(m.group("x")) and not NOT_IMAGE_SUBJECTS.match(m.group("x")) and \
            not re.match(r"^(?:my|our|screen|desktop|profile|product|google|web|search|latest|recent|new|newest|last|old|oldest|all|"
                         r"these|those|downloaded|saved|camera|phone)\b", m.group("x"), I):
        many = re.search(r"\b(images|pictures|pics|photos|photographs)\b", low) is not None
        return {"image_op": "search", "query": re.sub(r"^(?:a|an|the|some)\s+", "", m.group("x").strip(), flags=I),
                "count": 3 if many else 1}
    m = re.match(r"^what (?:does|do) (?:a |an |the )?(?P<x>[\w' -]{2,40}?) look like\s*\??$", t, I)
    if m and not _personal(m.group("x")):
        return {"image_op": "search", "query": m.group("x").strip(), "count": 1}
    m = re.match(r"^(?:please\s+)?show\s+me\s+(?P<x>(?:a|an)\s+[a-z][a-z' -]{1,40}?)\s*[.!]?$", t, I)
    if m and not NOT_IMAGE_SUBJECTS.match(m.group("x")) and not re.search(
            r"\b(of my|on my|in my|from my|for my|screen|window|files?|folders?|emails?|messages?|calendar|reminders?|notes?|tasks?|"
            r"buttons?|links?|fields?|tabs?|videos?|songs?|playlists?|photos?|pictures?|images?|screenshots?|documents?|downloads?|"
            r"yesterday|today|last|week|month|year|how|way|list|summary|example|demo)\b", m.group("x"), I):
        return {"image_op": "search", "query": re.sub(r"^(?:a|an|the|some)\s+", "", m.group("x").strip(), flags=I), "count": 1}
    return None


def computer_slots(t: str, low: str) -> Optional[dict]:
    m = HOTKEY.match(t)
    if m and re.search(r"[+]|\b(ctrl|control|alt|shift|win|windows|cmd|f\d{1,2}|backspace|delete|home|end|space|arrow)\b", m.group("k"), I):
        n = {"twice": 2, "three": 3}.get((m.group("n") or "").lower()) or (int(m.group("n")) if (m.group("n") or "").isdigit() else 1)
        return {"computer_op": "press", "keys": m.group("k").strip(), "times": n}
    m = re.match(r"^(?:please\s+)?type\s+(?:out\s+)?[\"“'](?P<q>.+?)[\"”'](?:\s+(?:in|into)\s+(?:the\s+)?(?P<f>.+?)(?:\s+(?:field|box))?)?\s*$",
                 t, I)
    if m:
        return {"computer_op": "type", "text": m.group("q"), "field": m.group("f")}
    m = re.match(r"^(?:please\s+)?type\s+(?:in(?:to)?\s+(?:the\s+)?(?P<f2>[\w ]+?)\s+(?:field|box|bar)\s+)(?P<q>.+)$", t, I)
    if m:
        return {"computer_op": "type", "text": m.group("q"), "field": m.group("f2")}
    m = re.match(r"^(?:please\s+)?type\s+(?:out\s+)?(?P<q>.+)$", t, I)
    if m and re.search(r"\bmy\s+(?:password|passcode|pin|otp|card)\b", low):
        return None
    if m and not low.startswith("type script") and not re.search(r"\s(?:in|into)\s+(?:the\s+)?\S.*\b(?:field|box|input)$", low):
        return {"computer_op": "type", "text": m.group("q"), "field": None}
    if re.fullmatch(r"(?:please\s+)?copy(?: (?:this|that|it|the selection|the selected text|selected text|what'?s selected))?(?: to (?:the )?clipboard)?", low):
        return {"computer_op": "copy"}
    m = re.match(r"^(?:please\s+)?copy\s+[\"“'](?P<x>.+)[\"”']\s+to\s+(?:the\s+)?clipboard$", t, I)
    if m:
        return {"computer_op": "copy", "text": m.group("x")}
    if re.fullmatch(r"(?:please\s+)?paste(?: (?:it|that|this|the text))?(?: here| in here| there)?", low):
        return {"computer_op": "paste"}
    if re.fullmatch(r"(?:please\s+)?select (?:all|everything)(?: the text| of it| text)?(?: here| in this window)?", low):
        return {"computer_op": "select_all"}
    m = re.match(r"^(?:please\s+)?(?P<how>right[- ]?click|double[- ]?click)\s+(?:on\s+)?(?:the\s+)?[\"“']?(?P<x>.+?)[\"”']?$", t, I)
    if m:
        how = m.group("how").lower()
        return {"computer_op": "click", "target": m.group("x"), "button": "right" if how.startswith("right") else "left",
                "double": how.startswith("double")}
    m = re.match(r"^(?:please\s+)?drag\s+(?:the\s+)?[\"“']?(?P<a>.+?)[\"”']?\s+(?:to|onto|into|over to)\s+(?:the\s+)?[\"“']?(?P<b>.+?)[\"”']?$", t, I)
    if m:
        return {"computer_op": "drag", "source": m.group("a"), "destination": m.group("b")}
    m = re.match(r"^(?:please\s+)?(?:in\s+(?:this|the)\s+(?:app|window|program)[,:]?\s+)(?P<g>.+)$|^(?:please\s+)?(?P<g2>.+?)\s+in\s+(?:this|the "
                 r"current|the open)\s+(?:app|window|program|application)$", t, I)
    if m:
        return {"computer_op": "task", "goal": (m.group("g") or m.group("g2")).strip()}
    if re.fullmatch(r"what(?:'s| is) in (?:this|the (?:active|current)) window|what (?:buttons|controls|options) (?:are|do i have|can i "
                    r"click)(?: here| there| in this window| on screen)?|(?:list|show me) (?:the )?(?:buttons|controls)(?: in this window| here)?", low):
        return {"computer_op": "observe"}
    return None


def form_slots(t: str, low: str) -> Optional[dict]:
    m = re.match(r"^(?:please\s+)?(?:fill|complete|fill out|fill in)(?:\s+(?:out|in))?\s+(?:this|the|my)\s+(?:job\s+)?(?:application(?:\s+form)?|form)"
                 r"(?:\s+for me)?(?:\s+(?:using|with|from|based on)\s+(?:my\s+)?(?P<src>.+?))?\s*[.!]?$", t, I) or \
        re.match(r"^(?:please\s+)?(?:fill|complete)\s+(?:it|this|that)\s+(?:in\s+|out\s+)?(?:using|with|from|based on)\s+(?:my\s+)?(?P<src>.+?)\s*[.!]?$", t, I)
    if m:
        src = (m.group("src") or "resume").strip().lower()
        src = "resume" if re.fullmatch(r"(resume|résumé|cv|curriculum vitae)", src) else "profile" if re.fullmatch(
            r"(profile|details|info|information|data)", src) else (m.group("src") or "resume").strip()
        return {"form_op": "fill", "source": src}
    if re.fullmatch(r"(?:ok(?:ay)?,?\s+|now\s+)?(?:please\s+)?submit (?:the |this |my )?(?:form|application)(?: now)?", low):
        return {"form_op": "submit"}
    return None


def connection_slots(t: str, low: str) -> Optional[dict]:
    if re.fullmatch(r"(?:is|check) (?:my |the )?(?:internet|connection|network|wi-?fi)(?: connection)?(?: working| ok| up| down)?\??|"
                    r"am i (?:online|connected(?: to the internet)?)\??|(?:check|test) my (?:internet|connection|network|wi-?fi)", low):
        return {"topic": "connection"}
    m = re.match(r"^why (?:isn'?t|is not|won'?t|doesn'?t|does not|can'?t|cannot) (?:the\s+)?(?P<x>[\w.-]+(?:\.[a-z]{2,})?)(?: website| site| page)? "
                 r"(?:load|loading|open|opening|work|working)\s*\??$", t, I)
    if m:
        x = m.group("x").lower()
        if x in ("internet", "wifi", "wi-fi", "network", "connection"):
            return {"topic": "connection"}
        from ..services.app_catalog import lookup

        found = lookup(x)
        url = found[1].get("url") if found and found[1].get("url") else (f"https://{x}" if "." in x else None)
        if url or x in ("page", "it", "this", "that"):
            return {"topic": "connection", "url": url}
    return None


# ------------------------------------------------------------------ GitHub (1.5)
_REPO = r"(?:github\s+)?repo(?:sitory|sitories|s)?"
_RNAME = r"[“\"']?(?P<name>[A-Za-z0-9][\w.\- ]{0,99}?)[”\"']?"


def github_slots(t: str, low: str) -> Optional[dict]:
    low = low.rstrip(" .!?")
    if re.fullmatch(r"(?:please\s+)?(?:open|go to|launch|show me|bring up|visit)\s+(?:my\s+)?git ?hub(?:\s+(?:home|home ?page|page|"
                    r"website|site|dashboard))?(?: for me)?", low):
        return {"github_op": "open", "target": "home"}
    if re.fullmatch(r"(?:please\s+)?(?:open|go to|show me)\s+(?:my\s+)?git ?hub\s+(?:profile|repos|repositories)|"
                    r"(?:please\s+)?(?:open|go to)\s+my\s+(?:git ?hub\s+)?repositories(?: on git ?hub)?", low):
        return {"github_op": "open", "target": "repos"}
    if re.fullmatch(r"(?:please\s+)?(?:show|list|what are|which are|tell me)\s+(?:me\s+)?(?:all\s+)?(?:of\s+)?my\s+(?:git ?hub\s+)?"
                    r"(?:repos|repositories|projects on git ?hub)(?: on git ?hub)?", low):
        return {"github_op": "list"}
    m = re.match(rf"^(?:please\s+)?(?:create|make|start|set up|add)\s+(?:me\s+)?(?:a\s+)?(?:new\s+)?(?P<vis>private\s+|public\s+)?"
                 rf"(?:git ?hub\s+)?repo(?:sitory)?(?:\s+on\s+git ?hub)?\s+(?:called|named|with the name)?\s*{_RNAME}"
                 rf"(?:\s+on\s+git ?hub)?(?:\s+(?P<vis2>private|public))?$", t, I)
    if m and m.group("name").strip().lower() not in ("on github", "github"):
        vis = (m.group("vis") or m.group("vis2") or "private").strip().lower()
        return {"github_op": "create", "repo": m.group("name").strip(), "private": vis != "public"}
    m = re.match(rf"^(?:please\s+)?(?:delete|remove|destroy|erase)\s+(?:my\s+|the\s+)?(?:git ?hub\s+)?repo(?:sitory)?\s+"
                 rf"(?:called|named)?\s*{_RNAME}(?:\s+(?:from|on)\s+git ?hub)?$", t, I) or \
        re.match(rf"^(?:please\s+)?(?:delete|remove|destroy|erase)\s+(?:my\s+|the\s+)?{_RNAME}\s+repo(?:sitory)?"
                 rf"(?:\s+(?:from|on)\s+git ?hub)?$", t, I) or \
        re.match(rf"^(?:please\s+)?(?:delete|remove|destroy|erase)\s+(?:my\s+|the\s+)?{_RNAME}\s+(?:from|on)\s+git ?hub$", t, I)
    if m and m.group("name").strip().lower() not in ("it", "this", "that", "the", "my"):
        return {"github_op": "delete", "repo": m.group("name").strip()}
    m = re.match(rf"^(?:please\s+)?(?:open|show|go to)\s+(?:my\s+|the\s+)?(?:git ?hub\s+)?repo(?:sitory)?\s+(?:called|named)?\s*{_RNAME}"
                 rf"(?:\s+on\s+git ?hub)?$", t, I) or \
        re.match(rf"^(?:please\s+)?(?:open|show|go to)\s+(?:my\s+|the\s+)?{_RNAME}\s+(?:repo(?:sitory)?\s+)?on\s+git ?hub$", t, I)
    if m:
        return {"github_op": "open", "target": m.group("name").strip()}
    return None


# ------------------------------------------------------------------ Word / Excel / PowerPoint (1.5)
_WORD = r"(?:ms\s+|microsoft\s+)?word(?:\s+(?:document|doc|file))?"
_EXCEL = r"(?:ms\s+|microsoft\s+)?(?:excel(?:\s+(?:sheet|spreadsheet|file|workbook))?|spreadsheet|workbook)"
_PPT = r"(?:ms\s+|microsoft\s+)?(?:power ?point(?:\s+(?:presentation|deck|file))?|ppt|presentation|slide ?deck|slides)"
_GENERATED = re.compile(r"^(?:an?\s+|some\s+|the\s+|my\s+)?(?:short\s+|long\s+|brief\s+|detailed\s+|\d+[- ](?:word|page|paragraph)s?\s+)?"
                        r"(?:essay|paragraph|paragraphs|report|letter|article|summary|poem|story|note|notes|introduction|conclusion|"
                        r"bullet points|points|list|table|outline|plan|speech|description|explanation|review|abstract|agenda|"
                        r"minutes|cover letter|resume|cv|budget|schedule|timetable|tracker|slide|slides|content|text)\b", I)


def _office_kind(s: str) -> Optional[str]:
    s = s.lower()
    if re.search(r"\b(power ?point|ppt|presentation|slide ?deck|slides?)\b", s):
        return "powerpoint"
    if re.search(r"\b(excel|spreadsheet|workbook|sheet)\b", s):
        return "excel"
    if re.search(r"\b(word|docx|document|doc)\b", s):
        return "word"
    return None


def office_slots(t: str, low: str) -> Optional[dict]:
    low = low.rstrip(" .!?")
    t = t.rstrip(" .!?")
    if re.search(r"\b(e-?mail|gmail|whats ?app|colab|notebook|python|script|program|code)\b", low) and \
            not re.search(r"\b(word|excel|power ?point|ppt|spreadsheet)\b", low):
        return None
    slides = None
    ms = re.search(r"\b(?:with|of|in|having)\s+(\d{1,2}|three|four|five|six|seven|eight|nine|ten)\s+slides?\b", low)
    if ms:
        w = ms.group(1)
        slides = int(w) if w.isdigit() else ["three", "four", "five", "six", "seven", "eight", "nine", "ten"].index(w) + 3
        t = re.sub(r"\s*\b(?:with|of|in|having)\s+\S+\s+slides?\b", "", t, flags=I)
    # "create / make / prepare a word document about X", "make a powerpoint on X with 5 slides", "create an excel sheet for my budget"
    m = re.match(rf"^(?:please\s+)?(?:can you\s+)?(?:create|make|prepare|build|generate|design|draft|write|start)\s+(?:me\s+)?"
                 rf"(?:a\s+|an\s+)?(?:new\s+|short\s+|simple\s+|nice\s+|professional\s+)?(?P<kind>{_WORD}|{_EXCEL}|{_PPT})"
                 rf"(?:\s+(?:about|on|for|of|explaining|covering|to track|that (?:shows|lists|explains)|with|called|named|titled)\s+(?P<x>.+))?$",
                 t, I)
    if m:
        kind = _office_kind(m.group("kind"))
        x = (m.group("x") or "").strip()
        return {"office_op": "create", "app": kind, "request": x or None, "slides": slides}
    # "make a budget spreadsheet", "create a machine learning presentation"
    m = re.match(rf"^(?:please\s+)?(?:can you\s+)?(?:create|make|prepare|build|generate|design|draft|start)\s+(?:me\s+)?(?:a\s+|an\s+)?"
                 rf"(?P<x>[a-z0-9][\w' -]{{1,50}}?)\s+(?P<kind>{_WORD}|{_EXCEL}|{_PPT})(?:\s+(?:about|on|for|with)\s+(?P<y>.+))?$", t, I)
    if m and not re.match(r"^(?:new|short|simple|nice|professional)$", m.group("x"), I):
        kind = _office_kind(m.group("kind"))
        req = m.group("x").strip() + (f" {m.group('y').strip()}" if m.group("y") else "")
        return {"office_op": "create", "app": kind, "request": req, "slides": slides}
    # "write an essay on X in word", "put this in excel: …", "add a slide about X to my presentation", "type hello in word"
    m = re.match(rf"^(?:please\s+)?(?:can you\s+)?(?P<v>write|type|put|add|insert|create|make)\s+(?P<x>.+?)\s+(?:in|into|to|on)\s+"
                 rf"(?:my\s+|the\s+|this\s+|a\s+|an\s+)?(?:open\s+|current\s+|new\s+)?(?P<kind>{_WORD}|{_EXCEL}|{_PPT})$", t, I)
    if m:
        kind = _office_kind(m.group("kind"))
        x = m.group("x").strip()
        quoted = re.fullmatch(r"[“\"'](.+)[”\"']", x)
        new_doc = bool(re.search(r"\b(?:a|an)\s+new\b|\bnew\s+(?:word|excel|power|ppt|doc|presentation|spreadsheet)", low))
        if kind == "powerpoint" and re.match(r"^(?:an?\s+|\d+\s+|some\s+)?(?:new\s+)?slides?\b", x, I):
            x = re.sub(r"^(?:an?\s+|some\s+)?(?:new\s+)?slides?\s*(?:about|on|for|explaining)?\s*", "", x, flags=I) or x
            return {"office_op": "create" if new_doc else "write", "app": kind, "request": x, "slides": slides or 1}
        if quoted or not _GENERATED.match(x):
            return {"office_op": "create" if new_doc else "write", "app": kind, "text": quoted.group(1) if quoted else x}
        return {"office_op": "create" if new_doc else "write", "app": kind, "request": x, "slides": slides}
    # "in word, write …"
    m = re.match(rf"^(?:in|on)\s+(?:my\s+|the\s+)?(?P<kind>{_WORD}|{_EXCEL}|{_PPT}),?\s+(?:write|type|add|put)\s+(?P<x>.+)$", t, I)
    if m:
        kind = _office_kind(m.group("kind"))
        x = m.group("x").strip()
        return {"office_op": "write", "app": kind, **({"request": x} if _GENERATED.match(x) else {"text": x.strip("“”\"'")})}
    return None


def parse_desktop(raw: str, t: str, low: str, now: Optional[datetime], r) -> Optional[NLUResult]:
    """``r`` is rules._r (passed in to avoid an import cycle)."""
    for fn, intent in ((connection_slots, Intent.INFORMATION_REQUEST), (github_slots, Intent.GITHUB), (office_slots, Intent.OFFICE),
                       (whatsapp_slots, Intent.WHATSAPP), (colab_slots, Intent.COLAB),
                       (form_slots, Intent.FORM), (image_slots, Intent.IMAGE), (computer_slots, Intent.COMPUTER)):
        slots = fn(t, low)
        if slots is not None:
            return r(raw, intent, slots)
    return None
