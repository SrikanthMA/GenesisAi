"""High-precision rules for the 1.2 agent capabilities: YouTube, media, windows, calendar, coding, history, preferences
and web answers. Called from :func:`genesis.nlp.rules.parse`; anything not matched here falls through to the
existing rules, then the classifier / LLM NLU.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from .entities import date_time_parts, find_filenames, find_paths
from .intents import Intent, NLUResult

I = re.I

LANG_WORDS = [
    (r"\b(type ?script|\.ts\b|\bts\b)", "typescript"),
    (r"\b(java\b(?!\s*script))", "java"),
    (r"\b(javascript|node(?:\.?js)?|express|\bjs\b)", "javascript"),
    (r"\b(html|css|web ?page|website|landing page)\b", "html"),
    (r"\b(python|flask|django|fastapi|pandas|\.py\b)", "python"),
]
PROGRAM_NOUNS = (r"program|script|app|application|api|website|web ?page|page|game|tool|cli|bot|server|"
                 r"project|code|calculator|scraper|converter|generator|parser|backend|endpoint")


def language_in(text: str) -> Optional[str]:
    for rx, lang in LANG_WORDS:
        if re.search(rx, text, I):
            return lang
    return None


def _when(low: str) -> str:
    if "yesterday" in low:
        return "yesterday"
    if "today" in low or "this morning" in low or "earlier today" in low:
        return "today"
    if "last week" in low:
        return "last_week"
    if "this week" in low or "few days" in low:
        return "this_week"
    return "any"


def _date_slots(text: str, now: Optional[datetime]) -> dict:
    day, tm = date_time_parts(text, now)
    out: dict = {}
    if day:
        out["date"] = day.isoformat()
    if tm:
        out["time"] = f"{tm[0]:02d}:{tm[1]:02d}"
    return out


EVENT_NOUNS = r"meeting|call|event|appointment|interview|lunch|dinner|session|standup|stand-up|sync|catch ?up|class|lecture|demo|review"


def calendar_slots(t: str, now: Optional[datetime] = None) -> Optional[dict]:
    low = t.lower()
    has_event = re.search(rf"\b({EVENT_NOUNS})s?\b", low)
    cal = re.search(r"\b(calendar|schedule|agenda)\b", low)
    # ---- read
    if re.search(r"\b(what('?s| is)? on my (calendar|schedule|agenda)|what do i have|what have i got|do i have any(thing)?|"
                 r"am i (free|busy)|my (schedule|agenda|calendar)|show (me )?my (calendar|schedule|agenda|meetings|events)|"
                 r"(list|any) (my )?(meetings|events|appointments))\b", low) and not re.search(r"\b(add|put|create|book|move|cancel)\b", low):
        slots = {"calendar_op": "list", **_date_slots(t, now)}
        if "this week" in low or "next week" in low:
            slots["range_days"] = 7
            slots["label"] = "this week" if "this week" in low else "next week"
        m = re.search(r"\b(?:with|about)\s+(?P<q>[\w .'-]+?)(?:\s+(?:today|tomorrow|this week|next week|on\b).*)?$", t, I)
        if m:
            slots["query"] = m.group("q").strip()
        return slots
    if not (has_event or cal):
        return None
    # ---- move
    m = re.match(r"^(?:please\s+)?(?:move|reschedule|push|shift|postpone|change|bring forward)\s+(?P<what>.+?)\s+(?:to|until|till)\s+(?P<to>.+)$", t, I)
    if m and has_event:
        what, to = m.group("what"), m.group("to")
        find = _date_slots(what, now)
        new = _date_slots(to, now)
        if not new:
            return None
        q = re.sub(rf"\b(my|the|a|an|{EVENT_NOUNS}|today|tomorrow|at|on|\d{{1,2}}(:\d{{2}})?\s*(am|pm)?|[ap]\.?m\.?)\b", " ", what, flags=I)
        return {"calendar_op": "move", "find_date": find.get("date"), "find_time": find.get("time"), "query": " ".join(q.split()),
                "new_date": new.get("date"), "new_time": new.get("time")}
    # ---- cancel
    if re.match(r"^(?:please\s+)?(?:cancel|delete|remove|clear|drop|call off)\b", low) and has_event:
        find = _date_slots(t, now)
        q = re.sub(rf"^(?:please\s+)?(cancel|delete|remove|clear|drop|call off)\b|\b(my|the|a|an|{EVENT_NOUNS}|today|tomorrow|on|at|"
                   r"from (my|the) calendar|\d{1,2}(:\d{2})?\s*(am|pm)?)\b", " ", t, flags=I)
        return {"calendar_op": "delete", "find_date": find.get("date"), "find_time": find.get("time"), "query": " ".join(q.split())}
    # ---- create
    if re.search(r"\b(put|add|schedule|set up|setup|book|create|arrange|plan|organi[sz]e|make)\b", low) and (has_event or cal) \
            and not re.search(r"\bremind\b", low):
        tm = re.search(rf"\b(?P<title>(?:a |an |the )?(?:{EVENT_NOUNS})(?:\s+(?:with|for|about)\s+(?:the\s+)?[\w .'&-]+?)?)"
                       r"(?=\s+(?:today|tonight|tomorrow|on|at|next|this|for|from|in)\b|\s*$|,)", t, I)
        title = tm.group("title").strip() if tm else "Meeting"
        title = re.sub(r"^(a|an|the)\s+", "", title, flags=I)
        title = title[:1].upper() + title[1:]
        slots = {"calendar_op": "create", "title": title, **_date_slots(t, now)}
        d = re.search(r"\bfor\s+(?P<n>\d+|an?|one|two|half an?)\s*(?P<u>hours?|hrs?|minutes?|mins?)\b", low)
        if d:
            n = {"a": 1, "an": 1, "one": 1, "two": 2, "half a": 0.5, "half an": 0.5}.get(d.group("n"), None)
            n = float(d.group("n")) if n is None else n
            slots["duration"] = int(n * 60) if d.group("u").startswith(("h", "hr")) else int(n)
        return slots
    return None


_NAME = r"(?P<n>[A-Za-z][\w'.-]*(?:\s+[A-Za-z][\w'.-]*){0,4})"


def identity_slots(t: str) -> Optional[dict]:
    low = t.lower().strip(" .!?")
    if re.fullmatch(r"(what'?s|what is|do you (know|remember)|tell me|say|can you say) my (full |real )?name|who am i", low):
        return {"identity_op": "whoami"}
    if re.fullmatch(r"(that'?s|that is|it'?s|it is) not my name|you got my name wrong|wrong name", low):
        return {"identity_op": "not_my_name"}
    if re.fullmatch(r"(please )?(forget|delete|remove) my name", low):
        return {"identity_op": "forget"}
    m = re.match(rf"^(?:please\s+)?(?:remember\s+(?:that\s+)?)?my\s+(?:full\s+|real\s+|actual\s+)?name\s+is\s+{_NAME}\s*[.!]?$", t, I)
    if m:
        return {"identity_op": "set_name", "name": m.group("n")}
    m = re.match(rf"^(?:please\s+|you can\s+|just\s+|from now on,?\s+)?call me\s+{_NAME}(?:\s+from now on)?\s*[.!]?$", t, I)
    if m:
        return {"identity_op": "set_preferred", "name": m.group("n")}
    m = re.match(rf"^(?:i go by|my nick ?name is|people call me|friends call me)\s+{_NAME}\s*[.!]?$", t, I)
    if m:
        return {"identity_op": "add_nickname", "name": m.group("n")}
    m = re.match(rf"^(?:it'?s|its|my name is)\s+(?:spelled|spelt|written)\s+{_NAME}\s*[.!]?$", t, I)
    if m:
        return {"identity_op": "set_name", "name": m.group("n"), "spelling": True}
    m = re.match(rf"^(?:it'?s|its)\s+pronounced\s+{_NAME}\s*[.!]?$", t, I)
    if m:
        return {"identity_op": "learn_variant", "name": m.group("n")}
    # self-introduction: "hi genesis, this is Srikanth" / "it's Shri Kant here" / "I'm Srikanth" (capitalised name only)
    m = re.match(rf"^(?:(?:hi|hey|hello|yo)\b[\s,]*)?(?:genesis\b[\s,]*)?(?P<how>this is|it'?s|i'?m|i am)\s+{_NAME}"
                 r"(?:\s+(?:here|speaking))?\s*[.!]?$", t, I)
    if m:
        name = re.sub(r"\s+(?:here|speaking)$", "", m.group("n"), flags=I)
        words = name.split()
        common = {"here", "back", "home", "done", "busy", "tired", "fine", "good", "ok", "okay", "ready", "sorry", "going",
                  "not", "just", "so", "very", "really", "the", "a", "an", "in", "at", "on", "working", "trying", "sure"}
        if words and words[0].lower() not in common and (m.group("how").lower().startswith("this") or all(w[:1].isupper() for w in words)):
            return {"identity_op": "introduce", "name": name}
    return None


# value → preference category (so "I prefer Gmail" / "my default browser is Firefox" become typed preferences)
PREF_VALUES = {
    "email": {"gmail": "gmail", "outlook": "outlook"},
    "browser": {"chrome": "chrome", "google chrome": "chrome", "edge": "edge", "microsoft edge": "edge", "firefox": "firefox",
                "brave": "brave"},
    "search": {"google": "google", "bing": "bing", "duckduckgo": "duckduckgo", "duck duck go": "duckduckgo"},
    "music": {"spotify": "spotify", "youtube": "youtube", "youtube music": "youtube"},
    "editor": {"vs code": "vscode", "vscode": "vscode", "visual studio code": "vscode", "pycharm": "pycharm"},
}
PREF_CATEGORY_WORDS = {"email": "email", "e-mail": "email", "mail": "email", "email client": "email", "mail app": "email",
                       "browser": "browser", "web browser": "browser", "search engine": "search", "search": "search",
                       "music": "music", "music app": "music", "editor": "editor", "code editor": "editor", "ide": "editor"}


def preference_slots(t: str) -> Optional[dict]:
    m = re.match(r"^(?:remember(?: that)?\s+)?(?:i\s+(?:prefer|like|use|always use|usually use|mostly use)(?:\s+using)?|"
                 r"my\s+(?:preferred|default|favou?rite|main|usual|go-to)\s+(?P<cat>[\w -]+?)\s+is|use)\s+(?P<v>[\w .+#-]+?)"
                 r"(?:\s+(?:for|as)\s+(?:my\s+)?(?P<cat2>[\w -]+?))?(?:\s+(?:from now on|by default|please))?\s*[.!]?$", t, I)
    if not m:
        return None
    value = m.group("v").strip().lower()
    cat_word = (m.group("cat") or m.group("cat2") or "").strip().lower()
    cat = PREF_CATEGORY_WORDS.get(cat_word)
    if cat is None:
        cat = next((c for c, vals in PREF_VALUES.items() if value in vals), None)
    if cat is None or value not in PREF_VALUES.get(cat, {}):
        return None
    return {"memory_op": "preference", "key": f"pref.{cat}", "value": PREF_VALUES[cat][value], "memory_text": t}


def parse_agent(raw: str, t: str, low: str, now: Optional[datetime], r) -> Optional[NLUResult]:
    """``r`` is rules._r (passed in to avoid an import cycle)."""
    low = re.sub(r"[\s?.!]+$", "", low)  # "what's on my to-do list?" — trailing punctuation never changes the meaning
    if re.search(r"\b(check|test|verify|diagnose|fix)\b.*\b(ai|llm|model|language model|brain|api key)\b|\bare you (connected|online)\b|"
                 r"\breconnect\b.*\b(ai|llm|model)\b|\bis (the|your) (ai|llm|model|brain) (working|connected|online)\b|"
                 r"\b(ai|llm|model) (status|health)\b", low):
        return r(raw, Intent.INFORMATION_REQUEST, {"topic": "ai_status"})
    idn = identity_slots(t)
    if idn is not None:
        return r(raw, Intent.IDENTITY, idn)
    pref = preference_slots(t)
    if pref is not None:
        return r(raw, Intent.MEMORY, pref)
    from .desktop_rules import parse_desktop

    hit = parse_desktop(raw, t, low, now, r)
    if hit is not None:
        return hit
    # standing instructions: "from now on, always …" / "never …"
    m = re.match(r"^(?:(?:from now on|going forward|in (?:the )?future)[,]?\s+)?(?:please\s+)?(?P<x>(?:always|never|don'?t ever|do not ever)\s+.+)$",
                 t, I)
    if m and (re.match(r"^(from now on|going forward|in (the )?future)", low) or re.match(r"^(always|never)\b", low)):
        return r(raw, Intent.MEMORY, {"memory_op": "instruction", "memory_text": m.group("x").strip().rstrip(".")})
    if re.match(r"^why did you (do that|do this|(?:open|close|play|search|pick|choose|send|create|use)\b.*)\??$", low) or \
            low in ("why", "why that", "why?"):
        return r(raw, Intent.MEMORY, {"memory_op": "explain"})
    # ---------------------------------------------------------------- preferences
    m = re.match(r"^(?:remember(?: that)?\s+)?my\s+(?:preferred|favou?rite|default|main|usual|go-to)\s+(?:coding\s+|programming\s+)?"
                 r"language\s+is\s+(?P<v>[\w+#.]+)", t, I) or \
        re.match(r"^i\s+(?:prefer|like|usually|mostly|always)\s+(?:to\s+)?(?:code|coding|write code|program|programming|use|using)\s+"
                 r"(?:in\s+|with\s+)?(?P<v>python|javascript|typescript|java|js|ts|html)\b", t, I) or \
        re.match(r"^i\s+prefer\s+(?P<v>python|javascript|typescript|java)\b", t, I)
    if m:
        from ..services.coderunner import language_for

        lang = language_for(m.group("v"))
        return r(raw, Intent.MEMORY, {"memory_op": "preference", "key": "coding.language",
                                      "value": lang.key if lang else m.group("v").lower(), "memory_text": t})

    # ---------------------------------------------------------------- history ("that X we talked about yesterday")
    if re.search(r"\b(we|i|you)\s+(were\s+)?(talk(ed|ing)|discuss(ed|ing)|look(ed|ing) at|watch(ed|ing)|mention(ed)?|search(ed)? for|"
                 r"found|opened|played)\b", low) and re.search(r"\b(find|open|show|play|get|what was|which was|pull up|bring up)\b", low) \
            and not re.fullmatch(r"(?:(?:use|open|take|show)\s+)?(?:the\s+)?(?:one|file|email|folder|link)\s+\w+\s+\w+(?:\s+\w+)?", low):
        q = re.sub(r"\b(find|open|show|play|get|pull up|bring up|what was|which was|that|the|we|i|you|were|was|talk\w*|discuss\w*|"
                   r"look\w*|watch\w*|mention\w*|search\w*|found|opened|played|about|at|for|yesterday|today|earlier|last week|this week)\b",
                   " ", t, flags=I)
        return r(raw, Intent.HISTORY, {"query": " ".join(q.split()) or t, "when": _when(low),
                                       "action": "play" if re.match(r"^play\b", low) else "open" if re.match(r"^open\b", low) else "find"})
    if re.match(r"^what (did|have) (we|i) (talk|talked|discuss|discussed|do|did|work|worked) (about|on)\b", low):
        return r(raw, Intent.HISTORY, {"query": "", "when": _when(low), "action": "find"})

    # ---------------------------------------------------------------- media control
    if re.fullmatch(r"(pause|stop)( it| this| that| the (video|music|song|track|playback|youtube)| playback| the youtube video)?"
                    r"( please)?", low) and low not in ("stop", "stop it", "stop that", "stop this"):
        return r(raw, Intent.MEDIA, {"action": "pause"})
    if re.fullmatch(r"pause", low):
        return r(raw, Intent.MEDIA, {"action": "pause"})
    if re.fullmatch(r"(resume|unpause|continue( playing)?|keep playing|play( it)? again|play|start playing)( it| the (video|music|song))?"
                    r"( please)?", low):
        return r(raw, Intent.MEDIA, {"action": "resume"})
    if re.fullmatch(r"(skip|next)( it| this( one)?| (the )?(song|video|track))?|(play )?(the )?next (song|video|track|one)|skip to the next "
                    r"(one|song|video|track)", low):
        return r(raw, Intent.MEDIA, {"action": "next"})
    if re.fullmatch(r"(play )?(the )?previous (song|video|track|one)|go back to the previous (song|video|track)", low):
        return r(raw, Intent.MEDIA, {"action": "previous"})

    # ---------------------------------------------------------------- windows & screenshots
    if re.search(r"\b(take|grab|capture|save|get)\s+(a\s+|me\s+a\s+)?(screen ?shot|screen ?grab|snapshot of (the|my) screen)\b|"
                 r"^screen ?shot( (it|this|the screen|my screen))?$|\bcapture (the|my) screen\b", low):
        return r(raw, Intent.WINDOW, {"window_op": "screenshot", "open_after": bool(re.search(r"\b(show|open)\b", low))})
    if re.search(r"\b(minimi[sz]e (everything|all( windows)?)|show (me )?(the )?desktop|hide (all|every) windows?|clear the screen)\b", low):
        return r(raw, Intent.WINDOW, {"window_op": "show_desktop"})
    if re.search(r"\b(what|which) windows (are|do i have) open\b|\b(list|show) (me )?(my |the |all )?(open )?windows\b", low):
        return r(raw, Intent.WINDOW, {"window_op": "list"})
    m = re.match(r"^(?:please\s+)?(?:switch|change|flip|jump|alt[- ]tab)\s+(?:back\s+)?to\s+(?P<w>.+?)(?:\s+window)?$", t, I) or \
        re.match(r"^(?:bring|put)\s+(?P<w>.+?)\s+(?:to the front|in front|forward)$", t, I) or \
        re.match(r"^focus\s+(?:on\s+)?(?:the\s+)?(?P<w>.+?)\s+window$", t, I)
    if m and not re.search(r"\b(dark|light) mode\b|\bmode\b", low):
        return r(raw, Intent.WINDOW, {"window_op": "focus", "window": m.group("w").strip()})
    m = re.match(r"^(?P<op>minimi[sz]e|maximi[sz]e|restore|full ?screen)\s+(?:the\s+)?(?P<w>.+?)(?:\s+window)?$", t, I)
    if m and not re.fullmatch(r"(yourself|genesis|the chat|chat|window|this|it)", m.group("w").lower()):
        op = m.group("op").lower()
        op = "minimize" if op.startswith("minim") else "maximize" if op.startswith(("maxim", "full")) else "restore"
        return r(raw, Intent.WINDOW, {"window_op": op, "window": m.group("w").strip()})
    m = re.match(r"^(?P<op>minimi[sz]e|maximi[sz]e)\s+(?:it|this|this window|the window)$", t, I)
    if m:
        return r(raw, Intent.WINDOW, {"window_op": "minimize" if m.group("op").lower().startswith("minim") else "maximize",
                                      "window": ""})

    # ---------------------------------------------------------------- YouTube
    yt = re.search(r"\b(youtube|you tube|yt)\b", low)
    m = re.match(r"^(?:please\s+)?(?:search|look up|find|look for)\s+(?:on\s+)?(?:youtube|you tube|yt)\s+(?:for\s+)?(?P<q>.+)$", t, I) or \
        re.match(r"^(?:please\s+)?(?:search|look up|find|look for)\s+(?P<q>.+?)\s+(?:on|in)\s+(?:youtube|you tube|yt)$", t, I) or \
        re.match(r"^(?:please\s+)?youtube\s+(?:search\s+)?(?:for\s+)?(?P<q>.+)$", t, I)
    if m:
        q = m.group("q").strip(" ?.")
        return r(raw, Intent.YOUTUBE, {"youtube_op": "search", "query": re.sub(r"^(?:videos?|some videos?)\s+(?:about|on|of)\s+", "", q, flags=I),
                                       "latest": bool(re.search(r"\b(latest|newest|recent)\b", q, I))})
    m = re.match(r"^(?:please\s+)?(?:play|put on|watch|start|stream)\s+(?:me\s+)?(?P<q>.+?)(?:\s+(?:on|from|in)\s+(?:youtube|you tube|yt))?$", t, I)
    if m and not re.fullmatch(r"(it|this|that|the (first|second|third|fourth|fifth|last|other|next|previous) (one|video|result|song))",
                              m.group("q").lower()):
        q = m.group("q").strip(" ?.")
        if re.search(r"\bon spotify\b", low):
            return None  # "play X on spotify" → handled as opening Spotify by the normal rules
        latest = bool(re.search(r"\b(latest|newest|most recent|new)\b", q, I))
        q2 = re.sub(r"^(?:the\s+)?(?:latest|newest|most recent|new)\s+", "", q, flags=I)
        q2 = re.sub(r"^(?:some\s+|a\s+|the\s+)", "", q2, flags=I)
        q2 = re.sub(r"\s+(?:video|song|music video)$", "", q2, flags=I) if latest else q2
        if yt or re.search(r"\b(video|song|music|tutorial|trailer|podcast|lecture|album|playlist|by)\b", low) or latest or \
                len(q.split()) <= 4:
            return r(raw, Intent.YOUTUBE, {"youtube_op": "play", "query": q2 or q, "latest": latest})

    # ---------------------------------------------------------------- calendar
    cs = calendar_slots(t, now)
    if cs is not None:
        return r(raw, Intent.CALENDAR, cs)

    # ---------------------------------------------------------------- coding agent
    m = re.match(rf"^(?:please\s+|can you\s+|could you\s+)?(?:create|write|make|build|generate|code(?: up)?|develop|program|set up|scaffold)"
                 rf"(?:\s+and\s+(?:run|test|execute|try)(?:\s+it)?)?\s+(?:me\s+)?(?:a|an|some|the|one)?\s*(?:new\s+)?(?:small|simple|quick|basic|little|tiny|short)?\s*"
                 rf"(?:[\w.+#/]+\s+){{0,2}}?(?:{PROGRAM_NOUNS})s?\b(?P<rest>.*)$", t, I)
    if m and not re.search(r"\b(e-?mail|reminder|routine|folder|directory|calendar|meeting|playlist)\b", low) \
            and not re.search(r"\b(file|document)\s+(called|named)\b", low):
        run = not re.search(r"\b(don'?t|do not|without) run", low)
        return r(raw, Intent.CODE_ACTION, {"code_action": "create_program", "task": t, "language": language_in(t), "run": run,
                                           "open_vscode": bool(re.search(r"\b(vs ?code|editor|visual studio)\b", low)),
                                           "uses_context_file": bool(re.search(r"\b(this|that|the)\s+(csv|file|json|text file|excel|"
                                                                               r"spreadsheet|data|log)\b", low))})
    m = re.fullmatch(r"(?:please\s+)?(?:run|execute|test|try)\s+(?P<what>it|this|that|the (?:program|script|code|project|app)|"
                     r"my (?:program|script|code)|(?:it|that|the program|the script) again|[\w\-./\\: ]+\.(?:py|js|mjs|ts|java|html))"
                     r"(?:\s+again)?(?:\s+please)?", low)
    if m:
        paths = find_paths(t) or find_filenames(t)
        return r(raw, Intent.CODE_ACTION, {"code_action": "run", "path": paths[0].value if paths else None})

    # ---------------------------------------------------------------- to-do list
    m = re.match(r"^(?:please\s+)?(?:add|put)\s+(?P<x>.+?)\s+(?:to|on)\s+(?:my\s+|the\s+)?(?:to[- ]?do|todo|task)s?(?:\s+list)?\s*[.!]?$", t, I) or \
        re.match(r"^(?:please\s+)?(?:add|create|new)\s+(?:a\s+)?(?:task|to-?do)(?:\s+item)?[:,]?\s+(?:to\s+)?(?P<x>.+)$", t, I)
    if m:
        x = m.group("x").strip()
        day, tm = date_time_parts(x, now)
        slots = {"tasks_op": "add", "text": re.sub(r"\s+(?:by|due|on|before)\s+.+$", "", x, flags=I) if (day or tm) else x}
        if day or tm:
            d = day or datetime.now().date()
            hh, mm = tm or (17, 0)
            slots["due"] = datetime.combine(d, datetime.min.time()).replace(hour=hh, minute=mm).isoformat()
        return r(raw, Intent.TASKS, slots)
    if re.fullmatch(r"(what'?s|what is|show( me)?|read( out)?|list|what are)( on)? (my |the )?(to[- ]?do|todo|task)s?( list)?|what do i (still )?have to do|"
                    r"my (to[- ]?do|todo|task)s?( list)?", low):
        return r(raw, Intent.TASKS, {"tasks_op": "list"})
    m = re.match(r"^(?:mark|tick off|check off|cross off)\s+(?P<x>.+?)\s*(?:as\s+)?(?:done|complete|completed|finished)?\s*[.!]?$", t, I) or \
        re.match(r"^i(?:'ve| have)?\s+(?:finished|completed|done)\s+(?P<x>.+?)(?:\s+task)?\s*[.!]?$", t, I)
    if m and (re.search(r"\b(done|complete|completed|finished|tick|check off|cross off)\b", low)):
        return r(raw, Intent.TASKS, {"tasks_op": "complete", "ref": re.sub(r"^(?:the\s+)?", "", m.group("x"), flags=I).removesuffix(" task")})
    m = re.match(r"^(?:please\s+)?(?:remove|delete)\s+(?P<x>.+?)\s+from\s+(?:my\s+|the\s+)?(?:to[- ]?do|todo|task)s?(?:\s+list)?$", t, I)
    if m:
        return r(raw, Intent.TASKS, {"tasks_op": "delete", "ref": m.group("x")})

    # ---------------------------------------------------------------- notes
    m = re.match(r"^(?:please\s+)?(?:take|make|write|jot)\s+(?:down\s+)?(?:a\s+)?note(?:\s+(?:that|to))?[:,]?\s+(?P<x>.+)$", t, I) or \
        re.match(r"^(?:please\s+)?note\s+(?:that|down)[:,]?\s+(?P<x>.+)$", t, I) or \
        re.match(r"^(?:please\s+)?write\s+(?:this\s+)?down[:,]?\s+(?P<x>.+)$", t, I)
    if m:
        return r(raw, Intent.NOTES, {"notes_op": "add", "content": m.group("x").strip()})
    if re.fullmatch(r"(show|list|read|open)( me)? (all )?(my |the )?notes|what notes do i have", low):
        return r(raw, Intent.NOTES, {"notes_op": "find", "query": ""})
    m = re.match(r"^(?:what did i (?:note|write down)|(?:find|show|read|open)(?: me)? (?:my |the )?notes?)\s+(?:about|on|for)\s+(?P<x>.+?)\??$", t, I)
    if m:
        return r(raw, Intent.NOTES, {"notes_op": "find", "query": m.group("x")})
    m = re.match(r"^(?:please\s+)?(?:delete|remove)\s+(?:my\s+|the\s+)?note\s+(?:about|on|called)\s+(?P<x>.+)$", t, I)
    if m:
        return r(raw, Intent.NOTES, {"notes_op": "delete", "ref": m.group("x")})

    # ---------------------------------------------------------------- change an existing project / run developer commands
    m = re.match(r"^(?:please\s+|can you\s+|could you\s+)?(?P<verb>add|implement|change|update|refactor|rename|remove|make|fix|improve|"
                 r"convert|translate|optimi[sz]e|write tests for|add tests to|clean up|document)\s+(?P<x>.+?)\s+(?:to|in|for|on|of|across)\s+"
                 r"(?:this|the|my|our)\s+(?:(?P<p>[\w.-]+)\s+)?(?:project|app|code ?base|code|website|site|repo|page)\b\s*[.!]?$", t, I)
    if m:
        return r(raw, Intent.CODE_ACTION, {"code_action": "modify", "request": t, "project": m.group("p")})
    m = re.match(r"^(?:please\s+)?(?:run|execute)\s+(?P<cmd>(?:npm|npx|pip|python|py|node|git|pytest|mvn|gradle|dotnet|cargo|go|javac|java)\b.+?)"
                 r"(?:\s+(?:in|for)\s+(?:this|the|my)\s+(?:(?P<p>[\w.-]+)\s+)?project)?\s*$", t, I)
    if m and not re.search(r"\.(py|js|mjs|ts|java|html)$", m.group("cmd").strip(), I):
        return r(raw, Intent.CODE_ACTION, {"code_action": "run_command", "command": m.group("cmd").strip(), "project": m.group("p")})

    # ---------------------------------------------------------------- page / window interaction (semantic, never coordinates)
    ORD = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5}
    m = re.fullmatch(r"(?:please\s+)?(?:click|press|tap|hit)\s+(?:on\s+)?the\s+(?P<o>first|second|third|fourth|fifth|sixth|1st|2nd|3rd|4th|5th)"
                     r"\s+(?P<k>button|link|field|option|tab)", low)
    if m:
        return r(raw, Intent.BROWSER_ACTION, {"browser_op": "click_nth", "nth": ORD[m.group("o")],
                                              "kind": {"option": "button", "tab": "button"}.get(m.group("k"), m.group("k"))})
    if re.fullmatch(r"(?:what'?s|what is|what do you see) on (?:this|the|my) (?:page|website|site|web ?page|tab)|what can i click( here)?|"
                    r"(?:list|show me|read out) (?:the |all )?(?P<k>buttons|links|fields)(?: on (?:this|the) page| here)?", low):
        k = re.search(r"\b(buttons|links|fields)\b", low)
        return r(raw, Intent.BROWSER_ACTION, {"browser_op": "elements", "kind": k.group(1) if k else "all"})
    m = re.fullmatch(r"(?:please\s+)?scroll(?: the page)?(?: (?P<d>down|up))?(?: a bit| a little| more| some)?|(?:go|scroll|jump) to the "
                     r"(?P<e>top|bottom)(?: of the page)?|page (?P<p>down|up)", low)
    if m:
        return r(raw, Intent.BROWSER_ACTION, {"browser_op": "scroll", "direction": m.group("d") or m.group("e") or m.group("p") or "down"})
    m = re.fullmatch(r"(?:please\s+)?(?:press|hit) (?P<k>enter|return|tab|escape|esc|page down|page up)", low)
    if m:
        key = {"enter": "Enter", "return": "Enter", "tab": "Tab", "escape": "Escape", "esc": "Escape", "page down": "PageDown",
               "page up": "PageUp"}[m.group("k")]
        return r(raw, Intent.BROWSER_ACTION, {"browser_op": "press", "key": key})
    m = re.match(r"^(?:please\s+)?(?:find|locate|where is|where'?s|show me|look for)\s+(?:the\s+|an?\s+)?(?P<x>.+?\b(?:button|link|field|"
                 r"box|tab|option|menu|checkbox|icon))\b(?:\s+(?:on|in)\s+(?:this|the)\s+(?:page|window|site|screen))?\s*[?.]?$", t, I)
    if m:
        return r(raw, Intent.BROWSER_ACTION, {"browser_op": "find", "description": m.group("x").strip()})

    # ---------------------------------------------------------------- goals ("prepare my computer for coding")
    m = re.match(r"^(?:please\s+)?(?:prepare|get|set up|setup|ready|prep)\s+(?:my\s+|the\s+)?(?:computer|pc|laptop|machine|desktop|"
                 r"workspace|everything|things|me)(?:\s+up)?(?:\s+ready)?\s+(?:for|to)\s+(?P<g>.+)$", t, I) or \
        re.match(r"^(?:get me ready|let'?s get ready|time to start|let'?s start|i want to start|i'?m about to start)\s+(?:for\s+|on\s+)?"
                 r"(?P<g>.+)$", t, I)
    if m:
        return r(raw, Intent.GOAL, {"goal": m.group("g").strip(" .!?"), "request": t})

    # ---------------------------------------------------------------- research lists ("find five AI internships I can apply for")
    m = re.match(r"^(?:please\s+)?(?:find|get|look for|search for|show me|list|give me|suggest|recommend)\s+(?:me\s+)?"
                 r"(?P<n>\d+|two|three|four|five|six|seven|eight|nine|ten|a few|some|several|a couple of)\s+"
                 r"(?:good\s+|top\s+|best\s+|great\s+|decent\s+|relevant\s+)?(?P<topic>.+?)\s*[.?!]?$", t, I)
    if m and not re.search(r"\b(files?|e-?mails?|mails?|messages?|photos?|pictures?|documents?|pdfs?|folders?|videos? on youtube)\b",
                           m.group("topic"), I):
        words = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
                 "a few": 3, "some": 5, "several": 5, "a couple of": 2}
        n = m.group("n").lower()
        return r(raw, Intent.RESEARCH, {"query": m.group("topic").strip(), "count": str(words.get(n, n)), "operation": "list",
                                        "question": t})

    # ---------------------------------------------------------------- web answers / research
    m = re.match(r"^(?:please\s+)?(?:search|look (?:it )?up|google|check|browse)\s+(?:on\s+)?(?:the\s+)?(?:internet|web|online|google)?\s*"
                 r"(?:for\s+(?P<topic>.+?)\s+)?(?:and|to)\s+(?:tell me|find out|explain|figure out|see)\s+(?P<q>.+)$", t, I)
    if m:
        q = m.group("q").strip(" ?.")
        return r(raw, Intent.RESEARCH, {"query": (m.group("topic") + " " + q).strip() if m.group("topic") else q, "operation": "answer",
                                        "question": q, "about_error": bool(re.search(r"\b(this|that|the)\s+(error|exception|problem|bug|crash)\b", q, I))})
    m = re.match(r"^(?:please\s+)?(?:find|get|tell me|give me|show me|what'?s|what is)\s+(?:me\s+)?(?:the\s+)?(?:latest|recent|newest|current)\s+"
                 r"(?:news|information|info|updates?|developments?)\s+(?:about|on|regarding|for)\s+(?P<q>.+)$", t, I)
    if m:
        return r(raw, Intent.RESEARCH, {"query": f"latest news {m.group('q').strip(' ?.')}", "operation": "news",
                                        "question": f"What is the latest news about {m.group('q').strip(' ?.')}?"})
    m = re.match(r"^(?:please\s+)?(?:why|how come)\s+(?:is|am|does|do|did)\s+(?:this|that|my|the)\s+(?:error|exception|program|code|script)\b.*$", t, I)
    if m:
        return r(raw, Intent.RESEARCH, {"query": t.rstrip("?"), "operation": "answer", "question": t, "about_error": True})
    return None
