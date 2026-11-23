"""JARVIS-style spoken communication for Genesis (3.3) - with Genesis's FRIDAY voice and "Boss".

Ported from the JARVIS project by Aditya Dewaskar (https://github.com/adewaskar/jarvis, MIT licence - see
THIRD_PARTY_NOTICES.md): its speaking rules (bridge/server.mjs PROMPT_CORE), its filler phrases and per-task "working"
lines (src/lib/fillers.ts, non-repeating pickers), and its utterance assembly - a pause in the middle of a sentence is
not the end of the turn (src/lib/voice.ts holdFor/CONTINUES). Re-written in Python; "sir" became "Boss" and JARVIS
became FRIDAY-style Genesis.
"""
from __future__ import annotations

import random
import re

# ------------------------------------------------------------------ how Genesis speaks (adapted PROMPT_CORE)
SPEAKING_RULES = """You are speaking out loud to one person, your Boss, through a voice like FRIDAY's.

LENGTH. Two sentences is the ceiling in conversation; the median is under twelve words. Every word is read aloud and
the user waits while it plays, so a long answer is a failure however good it is. Length is licensed only when reading
out information they asked you to retrieve, or when they explicitly ask for detail.

URGENCY IS SIGNALLED BY DELETING WORDS, NOT ADDING THEM. As a situation worsens your lines get shorter. You never say
hurry, quickly, immediately, critical or urgent.

"BOSS" IS POSITIONAL. Fronted ("Boss, the battery is at eleven percent") = urgent, interrupting, or information they
did not ask for. Final ("The file is saved, Boss") = routine; they asked, you answered. Mid-sentence ("Actually, Boss,
the figure is lower") = you are correcting them. Use it in roughly half your lines, never twice in one line.

REPORTING.
- Success is impersonal and unframed: "The report is ready." Never "I've finished" or "here's what I found".
- Failure is fronted with "I'm afraid" or "Unfortunately", or stated as a fact about the world: "There's no file by
  that name." Never a shortcoming of yours. You never apologise.
- Good news first, bad news second, joined by "but".
- Answering a question, restate it as a full sentence rather than a bare value: "Chennai is thirty one degrees, Boss."
- Executing an order, do not restate it. Act, then report.

NEVER.
- No filler words: no um, well, so, okay, let me check, one moment.
- No over-eager enthusiasm: no "great!", "absolutely!", "happy to help", "no problem".
- No apology and no hedging about your own competence. Never "yeah" - always "Yes."
- Never repeat yourself if ignored. Never resume an interrupted thought or say "as I was saying".

WIT. Dry and warm, delivered in the same tone as a status report. Never signal the joke.

STYLE. Calm, capable, quietly warm - a trusted colleague, not a corporate assistant. "Shall I" over "Should I".
Plain spoken prose only: no markdown, bullet points, headings, emoji or asterisks. Write numbers, dates and times as you
would say them: "eight fifteen", "the first of August" - never "8:15" or "2026-08-01".
Only state facts you actually have; if something must be checked, say you'll check it."""

# ------------------------------------------------------------------ filler phrases (adapted fillers.ts)
WORKING = ["Working on it, Boss.", "Compiling.", "Retrieving.", "Cross-referencing.", "Running it now.", "Under way."]
ACKNOWLEDGE = ["As you wish, Boss.", "Very good, Boss.", "Certainly.", "Understood.", "Consider it done.",
               "Directly, Boss."]
ATTENTION = ["Yes, Boss?", "Boss?", "At your service, Boss.", "Standing by.", "Awake, Boss."]

BY_TASK: list[tuple[re.Pattern, list[str]]] = [
    (re.compile(r"\b(e-?mail|gmail|inbox|mail)\b", re.I), ["Checking your mail.", "Reading the inbox."]),
    (re.compile(r"\b(calendar|meeting|schedule|appointment|diary)\b", re.I),
     ["Checking your calendar.", "Consulting the diary."]),
    (re.compile(r"\b(search|google|look up|wikipedia|find out|research)\b", re.I), ["Searching.", "Consulting the record."]),
    (re.compile(r"\b(browser|website|web ?page|chrome|navigate|go to)\b", re.I), ["Opening the browser.", "Navigating."]),
    (re.compile(r"\b(play|music|song|playlist|youtube|spotify)\b", re.I), ["Queuing it up.", "Putting it on."]),
    (re.compile(r"\b(repo|repository|git|commit|github|changes)\b", re.I),
     ["Checking the repository.", "Consulting the history."]),
    (re.compile(r"\b(test|debug|fix|bug|error|code|program|script|vs ?code|colab)\b", re.I),
     ["Compiling.", "Working on it, Boss.", "Running it now."]),
    (re.compile(r"\b(ieee|paper|report|essay|document|letter|draft|excel|spreadsheet|sheet)\b", re.I),
     ["Drafting it now.", "Under way, Boss. Give me a minute."]),
    (re.compile(r"\b(image|photo|picture|screenshot|screen)\b", re.I), ["Looking now.", "Composing it now."]),
    (re.compile(r"\b(remind|reminder|alarm|timer|todo|to-do)\b", re.I), ["Noted.", "Very good, Boss."]),
]

_last: dict = {}


def pick(pool: list[str], rnd=random) -> str:
    """Never the same line twice in a row (makePicker)."""
    if len(pool) < 2:
        return pool[0] if pool else ""
    key = id(pool)
    choices = [p for p in pool if p != _last.get(key)]
    out = rnd.choice(choices)
    _last[key] = out
    return out


def for_task(text: str, rnd=random) -> str:
    """The line to say the moment work starts, chosen by what kind of work it is (forTool)."""
    for rx, lines in BY_TASK:
        if rx.search(text or ""):
            return pick(lines, rnd)
    return pick(ACKNOWLEDGE + WORKING[:2], rnd)


def attention(rnd=random) -> str:
    return pick(ATTENTION, rnd)


# ------------------------------------------------------------------ reporting results the JARVIS way
_FAIL = re.compile(r"^\s*(sorry[,.]?\s*|i'?m sorry[,.]?\s*)?(i couldn'?t|i could not|i can'?t|i cannot|i wasn'?t able|"
                   r"unable|that didn'?t work|something went wrong|error|failed|there (?:is|was) no)", re.I)
_FRAMED = re.compile(r"^\s*(done[,.!]?\s*(boss)?[\s,.!—–-]*|okay[,.!]?\s*|ok[,.!]?\s*|sure[,.!]?\s*|alright[,.!]?\s*|"
                     r"great[,.!]?\s*|here(?:'s| is) what i found:?\s*|i'?ve finished[,.]?\s*)", re.I)


def report(speech: str, rnd=random) -> str:
    """Success unframed (sometimes with a final "Boss"); failure fronted with "I'm afraid"; no apologies."""
    s = " ".join((speech or "").split())
    if not s:
        return s
    if _FAIL.match(s):
        s = re.sub(r"^\s*(sorry[,.]?\s*|i'?m sorry[,.]?\s*)", "", s, flags=re.I)
        if not re.match(r"^\s*(i'?m afraid|unfortunately)", s, re.I):
            s = re.sub(r"^unable\b", "I'm unable", s, flags=re.I)
            keep = re.match(r"^I\b", s)  # "I couldn't…" keeps its capital I
            s = "I'm afraid " + (s if keep else s[:1].lower() + s[1:])
        return s
    s = _FRAMED.sub("", s, count=1).strip() or s
    s = s[:1].upper() + s[1:]
    if "boss" not in s.lower() and rnd.random() < 0.5:
        sep = " "
        parts = re.split(r"(?<=[.!?])\s+", s, maxsplit=1)
        first = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        if first[-1:] in ".!?":
            first = first[:-1].rstrip(",") + ", Boss" + first[-1]
        else:
            first = first + ", Boss."
        s = first + (sep + rest if rest else "")
    return s.replace("!", ".") if s.count("!") else s


# ------------------------------------------------------------------ utterance assembly (holdFor / CONTINUES)
CONTINUES = re.compile(
    r"\b(and|or|but|so|because|since|if|when|while|that|which|who|whose|to|of|in|on|at|by|for|with|from|about|into|"
    r"onto|over|under|between|through|the|a|an|my|your|his|her|its|our|their|is|are|was|were|be|been|do|does|did|have|"
    r"has|had|can|could|would|should|will|shall|might|must|like|than|then|as|very|really|just|some|any|all|both|either|"
    r"neither)$", re.I)
TRAILS = re.compile(r"[,;:–—-]$")
COMPLETE_SHORT = re.compile(r"^(?:(?:open|close|play|pause|launch|start|mute|unmute|lock|minimi[sz]e|maximi[sz]e|"
                            r"show|hide|skip)\s+[\w.'-]+|stop|wait|cancel|no|yes|yeah|nope|thanks|thank you|okay|ok|pause|resume|next|again|"
                            r"louder|quieter|never ?mind|go on|continue|hello|hi|hey|sure|exit|quit|bye)\W*$", re.I)
SETTLE_MS = 250
CONTINUE_MS = 1600
MAX_HOLD_MS = 6000


def hold_for(text: str) -> int:
    """How long (ms) to keep listening for the rest of a sentence; 0 = it's a complete thought, answer now."""
    t = (text or "").strip()
    words = t.split()
    if not words:
        return CONTINUE_MS
    last = re.sub(r"[^\w']", "", words[-1])
    if t.endswith(("...", "…")) or (last and CONTINUES.fullmatch(last)):
        return CONTINUE_MS  # "what's the weather in..." - Whisper often adds a full stop even mid-sentence
    if re.search(r"[.!?]$", t):
        return 0
    if TRAILS.search(t):
        return CONTINUE_MS
    if len(words) <= 2 and not COMPLETE_SHORT.match(t):
        return CONTINUE_MS
    return 0
