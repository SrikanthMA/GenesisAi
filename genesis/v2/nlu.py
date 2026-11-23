"""2.0 understanding layer, consulted before / after the 1.x rule parser (hooks in ``nlp/pipeline.py``).

Three kinds of improvement, each measured on ``eval/data/intent_heldout.jsonl`` (see docs/GENESIS_EVALUATION.md):

1. **Guards** — inputs that must never trigger an action, e.g. reported speech from untrusted content:
   "the email says delete all my files" used to become a file operation.
2. **Canonicalisation** — common phrasings rewritten to a form the existing rules already understand
   ("ping me at 5 to …" → "remind me at 5 to …", "turn the sound down" → "volume down"). The rules do the actual parsing,
   so slot extraction stays identical.
3. **Overrides / post-fixes** for known misparses found in the 2.0 audit:
   "do I have any unread emails" (was: calendar), "remember that my exam is on friday" (was: a contact called "on friday"),
   "run the tests" (was: an app called "tests"), a bare app name ("chrome" was classified as *close* application).
4. **Splitting** — "please open chrome and spotify" (the "please" blocked the split), "draft an email … saying X and then open
   gmail" (the command became part of the message), "remind me … at 6 and open my downloads".
5. **Dependency-aware routing** — "find my latest report, summarise it and email it to my professor": later parts refer to
   the result of earlier ones, so splitting them into independent commands loses "it". With an AI model available the whole
   request goes to the planner, which can pass results between steps ({{steps.N.data.x}}) under the normal safety checks.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Callable, Optional, Union

from ..nlp.intents import Intent, NLUResult

I = re.I


def _env_enabled() -> bool:
    import os

    return os.getenv("GENESIS_V2_NLU", "1").strip().lower() not in ("0", "false", "off", "no")


# Rollback / A-B switch: GENESIS_V2_NLU=0 restores the exact 1.x understanding behaviour (the safety policy stays on).
ENABLED = _env_enabled()

# ----------------------------------------------------------------------------------------------- 1. guards
_REPORTED = re.compile(
    r"^(?:the|this|that|an?|his|her|their|some|someone'?s?)\s+(?:e-?mail|mail|message|text|sms|whats ?app|page|web ?page|website|"
    r"site|document|doc|file|note|pdf|screen|pop-?up|post|tweet|comment|chat|letter|link|ad|advert(?:isement)?)\s+"
    r"(?:says|said|tells me|told me|asks me|asked me|reads|wants me|instructs me|is telling me|is asking me|claims)\b", I)
_SOMEONE_SAID = re.compile(r"^(?:some ?one|somebody|a stranger|a website|an app)\s+(?:told|asked|wants)\s+me\s+to\b", I)


_USER_ASKS = re.compile(r"[,;:.]\s*(?:and\s+)?(?:please\s+|can you\s+|could you\s+)?(?:remind|send|reply|forward|summari[sz]e|"
                        r"translate|tell|ask|save|add|note|read|open|draft|write|email|e-mail|text|message|check|show|"
                        r"what|why|how|is|should|do|does)\b", I)


def guard(text: str) -> Optional[NLUResult]:
    """A bare instruction quoted from someone else's content ("the email says delete all my files") must never become an
    action. Not guarded: the user's own message ("my message says…"), a request the user adds ("the note says buy milk,
    remind me at 5"), or a question about it ("the document says what?")."""
    t = text.strip()
    if t.endswith("?") or _USER_ASKS.search(t):
        return None
    if _REPORTED.match(t) or _SOMEONE_SAID.match(t):
        return NLUResult(text=t, intent=Intent.CONVERSATION, confidence=0.9, source="v2-guard",
                         slots={"reported_speech": True,
                                "v2_reply": "That's an instruction from someone else's message, not from you, so I won't act "
                                            "on it. If you want me to do something, just tell me directly."})
    return None


# ----------------------------------------------------------------------------------------------- 2. canonical phrasings
_REWRITES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^(?:please\s+)?(?:ping|nudge|buzz|alert)\s+me\b", I), "remind me"),
    (re.compile(r"^(?:please\s+)?(?:power down|power off|switch off|turn off)\s+(?:the|my|this)\s+"
                r"(?:machine|pc|computer|laptop|system|device)$", I), "shut down the computer"),
    (re.compile(r"^(?:please\s+)?turn (?:the )?(?:sound|volume|audio) (?P<d>up|down)$", I), r"volume \g<d>"),
    (re.compile(r"^(?:please\s+)?get (?P<a>[\w .+-]{2,40}?) running$", I), r"open \g<a>"),
    (re.compile(r"^(?:please\s+)?get rid of (?:the )?(?P<a>[\w .+-]{2,40}?) window$", I), r"close \g<a>"),
    (re.compile(r"^(?:what'?s|what is)\s+the\s+tiem$|^(?:the\s+)?time(?: please| now)?$|^current time$", I), "what time is it"),
    (re.compile(r"^(?:please\s+)?(?:search|look through|check) (?:my|the) (?:computer|pc|laptop|files|drive|disk) for (?P<q>.+)$", I),
     r"find \g<q>"),
    (re.compile(r"^(?:please\s+)?(?:start|begin|open) a new (?:e-?mail|mail) (?:to|for) (?P<r>.+)$", I), r"draft an email to \g<r>"),
    (re.compile(r"^what (?:have you|did you) (?:stored?|saved?|got|kept|learned|learnt) about me\??$", I), "what do you remember about me"),
    (re.compile(r"^(?:please\s+)?(?:don'?t|do not|stop) (?:talk(?:ing)?|speak(?:ing)?) (?:out loud|aloud)$", I), "mute your voice"),
]


# 2.1: words speech recognition confuses. "show me an image of a dragon" is very often heard as "...an EMAIL of a
# dragon", which 1.x/2.0 then treated as an e-mail request. Nobody asks for "an email of a <thing>", so it is repaired.
_NOT_A_THING = r"(?!(?:my|your|his|her|our|their|the|this|that|these|those|me|him|them|us)\b)"
_MISHEARD: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^(?P<pre>(?:please\s+)?(?:(?:can|could|would) you\s+)?(?:(?:show|give|get|send|find|search(?: for)?|look up|"
                r"generate|create|make|draw|display|bring up|pull up)(?:\s+me)?|i (?:want|need)(?: to see)?)\s+)?"
                r"(?:an?\s+|some\s+)?e-?mails?\s+of\s+" + _NOT_A_THING + r"(?P<rest>.+)$", I), "image"),
    (re.compile(r"\bimaged?s?\s+of\b", I), "fix-imaged"),
]
_WANT_IMAGE = re.compile(r"^(?:please\s+)?(?:i (?:want|need|would like)(?: to see)?\s+|can i (?:see|get|have)\s+)?"
                         r"(?:an?\s+|some\s+)?(?P<k>image|picture|photo|pic)s?\s+of\s+(?P<rest>.+)$", I)


def repair_mishearing(text: str) -> str:
    t = text.strip()
    m = _MISHEARD[0][0].match(t)
    if m:
        pre = (m.group("pre") or "").strip()
        verb = pre or "show me"
        t = f"{verb} an image of {m.group('rest')}".strip()
    t = re.sub(r"\bimaged\s+of\b", "image of", t, flags=I)
    m = _WANT_IMAGE.match(t)
    if m:  # "image of a dragon", "I want a picture of a cat" → the 1.x image rule's phrasing
        t = f"show me a picture of {m.group('rest')}"
    return t


def canonicalize(text: str) -> str:
    t = repair_mishearing(text)
    for rx, rep in _REWRITES:
        new = rx.sub(rep, t, count=1)
        if new != t:
            return new
    return t


# ----------------------------------------------------------------------------------------------- 3. overrides / post-fixes
_UNREAD = re.compile(
    r"^(?:do i have|have i got|are there|is there|did i get|did i receive|any)\s+(?:any\s+)?(?P<u>new|unread|fresh)?\s*"
    r"(?:e-?mails?|mails?|messages? in (?:my )?(?:inbox|gmail|mail))(?:\s+(?:today|yet|now|right now))?\??$|"
    r"^did (?:any ?one|anybody|someone) (?:e-?mail|mail|write to|send me an? e-?mail|send me mail)(?: me)?(?:\s+today)?\??$", I)
_RUN_TESTS = re.compile(
    r"^(?:please\s+)?(?:run|execute)\s+(?:the\s+|my\s+|our\s+|all\s+(?:the\s+)?)?(?:unit\s+tests|tests|test suite)"
    r"(?:\s+(?:in|for|of)\s+(?:the\s+|my\s+|this\s+)?(?:(?P<p>[\w.-]+)\s+)?project)?\s*$", I)
_NOT_A_NAME = re.compile(r"^(?:on|at|in|by|for|due|next|this|last|tomorrow|today|tonight|every|before|after|around|from|until|\d)", I)
_WHEN_WORD = re.compile(r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|today|tonight|week|month|"
                        r"january|february|march|april|may|june|july|august|september|october|november|december|\d)\b", I)


_RESUME = re.compile(r"^(?:please\s+)?(?:continue|resume|carry on|pick up)(?:\s+(?:from\s+)?(?:where|what)\s+(?:you|we|i)\s+"
                     r"(?:stopped|left off|were doing|were)|\s+(?:the|my)\s+(?:last|previous|unfinished|interrupted)\s+task|"
                     r"\s+(?:with\s+)?that task|\s+(?:with\s+)?(?:the|my)\s+task)$", I)  # bare "resume"/"continue" = media/yes
_UNFINISHED = re.compile(r"^(?:what (?:was i|were we|were you) doing|where did (?:we|you|i) (?:stop|leave off)|"
                         r"(?:show|list|what are)(?: me)? (?:my |the )?(?:unfinished|incomplete|interrupted|pending) tasks)$", I)
_CLEAR_TASKS = re.compile(r"^(?:please\s+)?(?:clear|delete|erase|wipe) (?:my |the |all )?task (?:history|progress)$", I)


def task_command(t: str) -> Optional[NLUResult]:
    """Resume / list / clear the persistent task journal."""
    if _RESUME.match(t):  # resolved by the planner (no side effects while parsing)
        return NLUResult(text=t, intent=Intent.FOLLOW_UP, confidence=0.95, source="v2-tasks", slots={"v2_resume": True})
    if _UNFINISHED.match(t):
        return NLUResult(text=t, intent=Intent.FOLLOW_UP, confidence=0.95, source="v2-tasks",
                         slots={"tool": "list_unfinished_tasks", "args": {}, "description": "List unfinished tasks"})
    if _CLEAR_TASKS.match(t):
        return NLUResult(text=t, intent=Intent.FOLLOW_UP, confidence=0.95, source="v2-tasks",
                         slots={"tool": "clear_task_history", "args": {}, "description": "Clear task history"})
    return None


# ----------------------------------------------------------------------------------------------- 2.1 commands
_MEET_WORD = r"(?:google meet|g ?meet|meet\.google\.com|google meeting)"
_CHAT_ON = re.compile(
    r"^(?:please\s+)?(?:let'?s|lets|can we|i want to) (?:have a |just )?(?:chat|talk|have a conversation|converse)"
    r"(?: for a (?:bit|while))?$|"
    r"^(?:start|begin|enable|turn on|switch on|open|activate)(?: the| a)? (?:voice chat|voice conversation|conversation mode|"
    r"chat mode|talk mode|voice mode)(?: mode)?$|^(?:talk|chat) (?:to|with) me$|^voice chat(?: mode)?(?: on)?$", I)
_CHAT_OFF = re.compile(
    r"^(?:please\s+)?(?:stop|end|exit|quit|close|turn off|switch off|disable|leave)(?: the)? (?:voice chat|voice conversation|"
    r"conversation mode|chat mode|talk mode|voice mode|chatting|talking)(?: mode)?$|^voice chat off$", I)
_MEET_HOME = re.compile(r"^(?:please\s+)?(?:open|launch|go to|show|bring up)(?: up)? " + _MEET_WORD + r"$", I)
_MEET_NEW = re.compile(
    r"^(?:please\s+)?(?:start|create|begin|set up|make|host|open a new|open a)(?: me)? (?:a |an )?(?:new |instant |quick )?"
    r"(?:" + _MEET_WORD + r"|video call|video meeting)(?: meeting| call)?(?: (?:on|in|with) " + _MEET_WORD + r")?$|"
    r"^(?:please\s+)?(?:start|create|begin|host) (?:a |an )?(?:new |instant |quick )?(?:meeting|call) (?:on|in|with) "
    + _MEET_WORD + r"$|^(?:please\s+)?(?:start|begin|host) (?:a |an )?(?:new |instant |quick )(?:meeting|video call)$", I)
_CODE_SEP = r"[a-z]{3}[\s-][a-z]{4}[\s-][a-z]{3}|https://meet\.google\.com/\S+"
_MEET_JOIN = re.compile(  # "join abc-defg-hij" / "open meeting abcdefghij" (a bare 10-letter word needs "meet"/"meeting")
    r"^(?:please\s+)?(?:join|enter|connect to)(?: the)?(?: " + _MEET_WORD + r")?(?: meeting| call)?(?: code)?\s+"
    r"(?P<code>" + _CODE_SEP + r")$|"
    r"^(?:please\s+)?(?:join|open|enter|go to|connect to)(?: the)? (?:" + _MEET_WORD + r"|meeting|call)(?: meeting| call)?"
    r"(?: code)?\s+(?P<code2>[a-z]{3}[\s-]?[a-z]{4}[\s-]?[a-z]{3}|https://meet\.google\.com/\S+)$", I)
_NOTES_START = re.compile(
    r"^(?:please\s+)?(?:(?:start|begin|go ahead and)\s+)?(?:take|taking|write|writing|record|recording|make|making|capture|"
    r"capturing|keep|keeping|prepare|note)(?: down)?(?: the| a)? (?:meeting )?(?:notes|minutes|mom|m\.?o\.?m\.?|"
    r"minutes of (?:the |this )?meeting)(?: (?:of|for|in|during|from) (?:this|the|my) (?:meeting|call|meet))?$|"
    r"^(?:please\s+)?(?:start|begin)(?: the)? (?:meeting notes|meeting minutes|minutes|mom|meeting recording)$|"
    r"^(?:please\s+)?record (?:this|the|my) (?:meeting|call|meet)$", I)
_NOTES_STOP = re.compile(
    r"^(?:please\s+)?(?:stop|end|finish|wrap up|complete)(?: taking| recording| the| writing)?(?: the)? (?:meeting )?"
    r"(?:notes|minutes|mom|m\.?o\.?m\.?|recording|meeting recording)(?: now)?$|"
    r"^(?:please\s+)?(?:stop recording|stop taking notes)(?: the| this)?(?: meeting| call)?$|"
    r"^(?:please\s+)?(?:the )?meeting (?:is over|has ended|ended)(?:,? write the (?:minutes|mom))?$|"
    r"^(?:please\s+)?(?:generate|create|make|write|give me) the (?:minutes|mom|m\.?o\.?m\.?)(?: now)?$", I)
_NOTES_STATUS = re.compile(r"^(?:are you (?:still )?(?:taking notes|recording(?: the meeting)?)|meeting notes status|"
                           r"how long have you been recording)\??$", I)


def _tool(t: str, tool: str, args: dict, description: str) -> NLUResult:
    return NLUResult(text=t, intent=Intent.FOLLOW_UP, confidence=0.95, source="v2-21",
                     slots={"tool": tool, "args": args, "description": description})


def commands_21(t: str) -> Optional[NLUResult]:
    if _CHAT_ON.match(t):
        return _tool(t, "voice_chat", {"on": True}, "Start voice chat")
    if _CHAT_OFF.match(t):
        return _tool(t, "voice_chat", {"on": False}, "Stop voice chat")
    if _NOTES_STOP.match(t):
        ambiguous = re.match(r"^(?:please\s+)?(?:generate|create|make|write|give me) the ", t, I) is not None
        return _tool(t, "meeting_notes_stop", {"start_if_idle": True} if ambiguous else {},
                     "Stop meeting notes and write the minutes")
    if _NOTES_START.match(t):
        return _tool(t, "meeting_notes_start", {}, "Take meeting notes")
    if _NOTES_STATUS.match(t):
        return _tool(t, "meeting_notes_status", {}, "Meeting notes status")
    m = _MEET_JOIN.match(t)
    if m:
        return _tool(t, "meet_open", {"op": "join", "code": m.group("code") or m.group("code2")},
                     "Join the Google Meet meeting")
    if _MEET_HOME.match(t):
        return _tool(t, "meet_open", {"op": "home"}, "Open Google Meet")
    if _MEET_NEW.match(t):
        return _tool(t, "meet_open", {"op": "new"}, "Start a new Google Meet meeting")
    return None


def raw_command(item: str) -> Optional[NLUResult]:
    """2.1 commands checked on the text *before* 1.x strips leading "stop"/"wait" as interjections."""
    if not ENABLED:
        return None
    return commands_21(str(item).strip().rstrip("?.! "))


def override(text: str) -> Optional[NLUResult]:
    """High-precision readings checked *before* the 1.x rules."""
    g = guard(text.strip().rstrip(".! "))  # keep "?": a question about reported content is not guarded
    t = text.strip().rstrip("?.! ")
    if g is not None:
        return g
    tc = task_command(t) or commands_21(t)
    if tc is not None:
        return tc
    m = _UNREAD.match(t)
    if m:
        unread = bool(m.group("u")) or "unread" in t.lower() or re.match(r"^did\b", t, I) is not None
        return NLUResult(text=t, intent=Intent.READ_EMAIL, confidence=0.95, source="v2-rules",
                         slots={"unread": True} if unread else {})
    m = _RUN_TESTS.match(t)
    if m:
        args = {"project": m.group("p")} if m.group("p") else {}
        return NLUResult(text=t, intent=Intent.FOLLOW_UP, confidence=0.95, source="v2-rules",
                         slots={"tool": "run_tests", "args": args, "description": "Run the tests"})
    return None


def bare_app(text: str, predicted: Intent, app_lookup: Callable[[str], object]) -> Optional[NLUResult]:
    """A bare app name ("chrome") that the classifier read as *close* application means "open it"."""
    t = text.strip().rstrip("?.! ")
    if predicted != Intent.CLOSE_APPLICATION or not 1 <= len(t.split()) <= 3:
        return None
    if re.search(r"\b(?:close|quit|exit|stop|kill|shut|end)\b", t, I):
        return None
    try:
        app = app_lookup(t)
    except Exception:
        app = None
    if not app:
        return None
    return NLUResult(text=t, intent=Intent.OPEN_APPLICATION, confidence=0.9, source="v2-rules", slots={"app": t})


def fix(hit: Optional[NLUResult]) -> Optional[NLUResult]:
    """Correct known misreadings of the 1.x rules after they ran."""
    if hit is None:
        return hit
    s = hit.slots
    if hit.intent == Intent.MEMORY and s.get("memory_op") == "add_contact" and not s.get("email"):
        name = str(s.get("contact_name") or "")
        if _NOT_A_NAME.match(name) or _WHEN_WORD.search(name):  # "my exam is on friday" is a fact, not a contact
            hit.slots = {"memory_op": "add", "memory_text": s.get("memory_text") or hit.text}
            hit.source = f"{hit.source}+v2"
    return hit


# ----------------------------------------------------------------------------------------------- 4./5. splitting & routing
_THEN = re.compile(r"\s*(?:,\s*)?\b(?P<c>and then|then|and after that|after that)\s+(?P<rest>.+)$", I)
_BODY_MARKER = re.compile(r"\b(?:saying|that says|to say|telling (?:him|her|them)|with the message|message:)\b|:", I)
_REMINDER = re.compile(r"^(?:please\s+)?(?:remind me|set (?:a|an) (?:reminder|alarm))\b", I)
_HARMLESS = {Intent.OPEN_APPLICATION, Intent.OPEN_FOLDER, Intent.OPEN_URL, Intent.SEARCH_WEB, Intent.YOUTUBE,
             Intent.MEDIA, Intent.UI_CONTROL, Intent.INFORMATION_REQUEST}
_AND_AFTER_TIME = re.compile(r"(?:\b(?:at|by|in)\s+\d[\d:.]*\s*(?:a\.?m\.?|p\.?m\.?|minutes?|hours?|mins?)?|\btomorrow|\btonight|"
                             r"\bmorning|\bevening)\s+and\s+(?P<rest>.+)$", I)
_COMMAND_START = re.compile(r"^(?:please\s+)?(?:open|launch|start|close|quit|search|play|show|set|turn|lock|create|send|draft|"
                            r"check|read|find|go to|take|mute|pause|remind)\b", I)
_BACKREF = re.compile(r"\b(?:it|its|that|them|this|those|these|the (?:result|results|summary|file|report|document|pdf|draft|"
                      r"answer|list|output))\b", I)
_PRONOUN_COMMAND = re.compile(r"^(?:and\s+|then\s+)?(?:please\s+)?\w+(?:\s+\w+)?\s+(?:it|them|that|this)\b", I)
_DATAFLOW = re.compile(r"\b(?:summari[sz]e|analy[sz]e|translate|attach|e-?mail|mail|send|share|compare|extract|forward|upload|"
                       r"rewrite|shorten|proofread|explain)\b", I)


def _detach_command(part: str, parse: Callable[[str], Optional[NLUResult]]) -> list[str]:
    """'… saying I'll be late and then open gmail' → ['… saying I'll be late', 'open gmail'] (only for a real command)."""
    for rx in (_THEN, _AND_AFTER_TIME):
        m = rx.search(part)
        if not m:
            continue
        rest = m.group("rest").strip(" ,.")
        left = part[:m.start("rest")].rstrip()
        left = re.sub(r"\s*(?:,\s*)?\b(?:and then|then|and after that|after that|and also|and)$", "", left, flags=I).strip(" ,")
        if not (left and _COMMAND_START.match(rest)):
            continue
        hit = parse(rest)
        if hit is None:
            continue
        if _BODY_MARKER.search(left) and hit.intent not in _HARMLESS:
            continue  # "text mom saying I'll clean up and then delete the photos" — could be part of the message
        if rx is _AND_AFTER_TIME and (_BODY_MARKER.search(left) or not _REMINDER.match(left)):
            continue  # a plain "and" only ends a *reminder* that already has its time
        return [left] + _detach_command(rest, parse)
    return [part]


def split(text: str, base_split: Callable[[str], list[str]], parse: Callable[[str], Optional[NLUResult]],
          app_list_split: Callable[[str], list[str]], *, llm_ready: bool = False) -> list[Union[str, NLUResult]]:
    parts = [x for part in base_split(text) for x in _split_21(part)]
    out: list[str] = []
    for p in parts:
        bare = re.sub(r"^please\s+", "", p, flags=I)
        pieces = app_list_split(bare) if bare != p else [p]  # "please open chrome and spotify"
        for piece in (pieces if len(pieces) > 1 else [p]):
            out.extend(_detach_command(piece, parse))
    if llm_ready and len(out) >= 2 and dependent_chain(out, parse):
        whole = text.strip()
        return [NLUResult(text=whole, intent=Intent.GOAL, confidence=0.9, source="v2-chain",
                          slots={"goal": whole, "request": whole})]
    return out  # without a model: exactly the 1.x step-by-step behaviour


def _split_21(text: str) -> list[str]:
    """2.1: "open google meet and take notes" → two commands, when both halves are 2.1 commands."""
    t = text.strip().rstrip(".!")
    for m in re.finditer(r"\s*,?\s+(?:and then|and also|and|then)\s+", t, I):
        left, right = t[:m.start()].strip(), t[m.end():].strip()
        if left and right and commands_21(left) is not None and commands_21(right) is not None:
            return [left, right]
    return [text]


def dependent_chain(parts: list[str], parse: Callable[[str], Optional[NLUResult]]) -> bool:
    """Later parts use the *result* of earlier ones and at least one part isn't a known command ("find my report,
    summarise it and email it"). Plain sequences ("open chrome and maximise it", "search YouTube and play the second
    one") keep working step by step as before."""
    later_refers = any(_BACKREF.search(p) and _DATAFLOW.search(p) for p in parts[1:])
    if not later_refers:
        return False
    # "<verb> it" parts ("rename it", "attach it") are resolved step by step by the 1.x follow-up resolver, which already
    # works; only a part that nothing understands ("find my latest project report") needs the planner
    unknown = [p for p in parts if parse(p) is None and not _PRONOUN_COMMAND.match(p)]
    return bool(unknown)


def now() -> datetime:  # pragma: no cover - tiny indirection for tests
    return datetime.now()
