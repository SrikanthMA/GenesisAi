"""Entity extraction: e-mails, URLs, paths, file names, folders, applications,
people, dates/times and numbers.

The date/time parser is a small deterministic grammar covering the forms people
actually use with assistants ("tomorrow at 9 am", "in 10 minutes", "next
Monday evening", "on 5 October at 14:30"). ``dateparser`` is used as a fallback
for anything else when it is installed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from ..core.paths import user_known_folders
from ..services.app_catalog import alias_index
from .intents import Entity

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-']+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
URL_RE = re.compile(
    r"\b((?:https?://)[^\s<>\"']+|(?:www\.)[^\s<>\"']+|[a-z0-9\-]+(?:\.[a-z0-9\-]+)*\.(?:com|org|net|io|dev|ai|edu|gov|in|co|uk|app|me|info|tv|gg|so)(?:/[^\s<>\"']*)?)",
    re.I,
)
WIN_PATH_RE = re.compile(r"(?:\"([A-Za-z]:\\[^\"]+)\"|'([A-Za-z]:\\[^']+)'|\b([A-Za-z]:\\[^\s\"']*))")
REL_PATH_RE = re.compile(r"(?<![\w@])((?:~|\.{1,2})?[\\/]?(?:[\w.\-]+[\\/])+[\w.\-]+\.[A-Za-z0-9]{1,8})\b")
FILE_RE = re.compile(r"(?<![\w@/\\.\-])([\w\-][\w\-.]{0,80}?\.(?:py|js|ts|tsx|jsx|java|c|cpp|h|cs|go|rs|rb|php|html|css|json|yaml|yml|toml|md|txt|csv|xlsx?|docx?|pptx?|pdf|png|jpe?g|gif|zip|ini|cfg|log|ipynb|sql|sh|bat|cmd|ps1|exe|msi|vbs|lnk|jar|mp3|mp4|wav|mkv|mov))\b", re.I)
UNIX_PATH_RE = re.compile(r"(?:(?<=\s)|^)((?:~|/)[\w.\-]*(?:/[\w.\-]+)+/?)")
QUOTED_RE = re.compile(r"[\"“']([^\"”']{1,200})[\"”']")

HONORIFICS = r"(?:professor|prof\.?|dr\.?|doctor|mr\.?|mrs\.?|ms\.?|miss|sir|madam|ma'am)"
RELATION_WORDS = {
    "professor", "teacher", "boss", "manager", "mom", "mum", "mother", "dad", "father", "brother", "sister",
    "friend", "wife", "husband", "girlfriend", "boyfriend", "team", "colleague", "mentor", "advisor", "hr",
    "landlord", "doctor", "tutor", "guide", "supervisor", "classmate", "roommate", "partner", "client",
}

_NUM_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty five": 45, "forty-five": 45,
    "sixty": 60, "half an": 0.5, "half a": 0.5, "a couple of": 2, "couple of": 2, "few": 3, "a few": 3,
}
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})
_MONTHS["sept"] = 9

_REL_RE = re.compile(
    r"\bin\s+(\d+(?:\.\d+)?|half an?|an?|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty|"
    r"forty[ -]five|sixty|a couple of|couple of|a few|few)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?)\b",
    re.I,
)
_DAY_RE = re.compile(r"\b(the day after tomorrow|day after tomorrow|today|tonight|tomorrow|tmrw|tmr)\b", re.I)
_WEEKDAY_RE = re.compile(r"\b(?:(next|this|on|coming)\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)
_MONTH_NAMES = "january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec"
_DATE_DM_RE = re.compile(rf"\b(?:on\s+)?(?:the\s+)?(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_NAMES})\b\.?(?:\s+(\d{{4}}))?", re.I)
_DATE_MD_RE = re.compile(rf"\b(?:on\s+)?({_MONTH_NAMES})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?", re.I)
_DATE_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_TIME_RE = re.compile(
    r"\b(?:at|by|around|@)?\s*(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?m\.?|p\.?m\.?|am|pm|o'?clock)\b|\b(?:at|by|around)\s+(\d{1,2})(?:[:.](\d{2}))?\b(?!\s*(?:%|minutes|mins|hours|days|files|items))",
    re.I,
)
_PERIOD_RE = re.compile(r"\b(?:(?:in|this|tomorrow|the)\s+)?(morning|afternoon|evening|night|noon|midnight|midday)\b", re.I)
_PERIOD_TIMES = {"morning": (9, 0), "afternoon": (14, 0), "evening": (18, 0), "night": (21, 0), "noon": (12, 0),
                 "midday": (12, 0), "midnight": (0, 0)}


@dataclass
class ParsedDateTime:
    value: datetime
    spans: list[tuple[int, int]]

    @property
    def text_spans(self) -> list[tuple[int, int]]:
        return self.spans


def _num(token: str) -> float:
    token = token.lower().strip()
    if token in _NUM_WORDS:
        return float(_NUM_WORDS[token])
    return float(token)


def parse_datetime(text: str, now: Optional[datetime] = None) -> Optional[ParsedDateTime]:
    """Parse the first date/time expression in *text* into a local naive datetime in the future."""
    now = now or datetime.now()
    low = text.lower()
    spans: list[tuple[int, int]] = []

    m = _REL_RE.search(low)
    if m:
        n = _num(m.group(1))
        unit = m.group(2).lower()
        if unit.startswith("sec"):
            delta = timedelta(seconds=n)
        elif unit.startswith("min"):
            delta = timedelta(minutes=n)
        elif unit.startswith(("hour", "hr")):
            delta = timedelta(hours=n)
        elif unit.startswith("day"):
            delta = timedelta(days=n)
        else:
            delta = timedelta(weeks=n)
        return ParsedDateTime((now + delta).replace(microsecond=0), [m.span()])

    day: Optional[date] = None
    day_word = None
    explicit_day = False

    m = _DAY_RE.search(low)
    if m:
        word = m.group(1)
        spans.append(m.span())
        explicit_day = True
        day_word = word
        if "after" in word:
            day = now.date() + timedelta(days=2)
        elif word in {"tomorrow", "tmrw", "tmr"}:
            day = now.date() + timedelta(days=1)
        else:
            day = now.date()
    if day is None:
        m = _WEEKDAY_RE.search(low)
        if m:
            spans.append(m.span())
            explicit_day = True
            target = _WEEKDAYS.index(m.group(2).lower())
            ahead = (target - now.weekday()) % 7
            if ahead == 0 and (m.group(1) or "").lower() != "this":
                ahead = 7
            day = now.date() + timedelta(days=ahead)
    if day is None:
        for rx, order in ((_DATE_ISO_RE, "iso"), (_DATE_DM_RE, "dm"), (_DATE_MD_RE, "md")):
            m = rx.search(low)
            if not m:
                continue
            try:
                if order == "iso":
                    day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                elif order == "dm":
                    year = int(m.group(3)) if m.group(3) else now.year
                    day = date(year, _MONTHS[m.group(2).lower().rstrip(".")], int(m.group(1)))
                else:
                    year = int(m.group(3)) if m.group(3) else now.year
                    day = date(year, _MONTHS[m.group(1).lower().rstrip(".")], int(m.group(2)))
                if order != "iso" and day < now.date() and not m.group(3):
                    day = day.replace(year=day.year + 1)
            except (ValueError, KeyError):
                day = None
                continue
            spans.append(m.span())
            explicit_day = True
            break

    hour = minute = None
    m = _TIME_RE.search(low)
    if m:
        if m.group(1):
            hour, minute, mer = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower().replace(".", "")
        else:
            hour, minute, mer = int(m.group(4)), int(m.group(5) or 0), ""
        if hour > 23 or minute > 59:
            hour = minute = None
        else:
            spans.append(m.span())
            if mer.startswith("p") and hour < 12:
                hour += 12
            elif mer.startswith("a") and hour == 12:
                hour = 0
            elif not mer.startswith(("a", "p")) and 1 <= hour <= 7:
                hour += 12  # "at 5" almost always means 5 pm
            if day_word == "tonight" and hour < 12:
                hour += 12
    period = None
    if hour is None:
        m = _PERIOD_RE.search(low)
        if m:
            period = m.group(1).lower()
            hour, minute = _PERIOD_TIMES[period]
            spans.append(m.span())
        elif day_word == "tonight":
            hour, minute = 20, 0
    elif _PERIOD_RE.search(low):
        pm = _PERIOD_RE.search(low)
        if pm.group(1).lower() in {"evening", "night", "afternoon"} and hour < 12:
            hour += 12
        spans.append(pm.span())

    if day is None and hour is None:
        try:  # optional broader fallback
            import dateparser.search  # type: ignore

            found = dateparser.search.search_dates(
                text, settings={"PREFER_DATES_FROM": "future", "RELATIVE_BASE": now, "RETURN_AS_TIMEZONE_AWARE": False}
            )
            if found:
                frag, dt = found[0]
                if len(frag.strip()) > 3 and not frag.strip().isdigit():
                    idx = low.find(frag.lower())
                    return ParsedDateTime(dt.replace(microsecond=0), [(idx, idx + len(frag))] if idx >= 0 else [])
        except Exception:
            pass
        return None

    if day is None:
        day = now.date()
    if hour is None:
        hour, minute = 9, 0
    result = datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute or 0)
    if result <= now and not explicit_day:
        result += timedelta(days=1)
    return ParsedDateTime(result, spans)


def remove_spans(text: str, spans: list[tuple[int, int]]) -> str:
    out = text
    for s, e in sorted(spans, key=lambda x: -x[0]):
        out = out[:s] + " " + out[e:]
    return re.sub(r"\s{2,}", " ", out).strip(" ,.")


# ------------------------------------------------------------------ others
def find_emails(text: str) -> list[Entity]:
    return [Entity("email", m.group(0), m.group(0), *m.span()) for m in EMAIL_RE.finditer(text)]


def find_urls(text: str) -> list[Entity]:
    out = []
    email_spans = [m.span() for m in EMAIL_RE.finditer(text)]
    for m in URL_RE.finditer(text):
        val = m.group(1).rstrip(".,;:!?)")
        if any(s <= m.start() < e or s < m.end() <= e for s, e in email_spans):
            continue
        if FILE_RE.fullmatch(val) and not val.lower().startswith(("http", "www")):
            continue  # "report.pdf" is a file, not a site
        out.append(Entity("url", val, m.group(0), *m.span()))
    return out


def find_paths(text: str) -> list[Entity]:
    out = []
    for m in WIN_PATH_RE.finditer(text):
        val = next(g for g in m.groups() if g)
        out.append(Entity("path", val.rstrip(".,;"), m.group(0), *m.span()))
    for m in UNIX_PATH_RE.finditer(text):
        out.append(Entity("path", m.group(1).rstrip(".,;"), m.group(0), *m.span(1)))
    taken = [(e.start, e.end) for e in out]
    for m in REL_PATH_RE.finditer(text):
        if any(s <= m.start() < e for s, e in taken) or "://" in text[max(0, m.start() - 8):m.start() + 3]:
            continue
        out.append(Entity("path", m.group(1), m.group(0), *m.span()))
    return out


def find_filenames(text: str) -> list[Entity]:
    out = []
    for m in FILE_RE.finditer(text):
        val = m.group(1).strip()
        # keep only the last "word run" before the extension ("open report.pdf" -> "report.pdf")
        val = re.split(r"\s+(?:called|named|file|open|the|a|an|in|to|into|from)\s+", val)[-1]
        out.append(Entity("file_name", val, m.group(0), *m.span()))
    return out


def find_folder(text: str) -> Optional[Entity]:
    low = text.lower()
    for name in user_known_folders():
        for pattern in (rf"\b(?:my\s+)?{name}(?:\s+folder)?\b",):
            m = re.search(pattern, low)
            if m and name != "home" or (m and "home folder" in low):
                return Entity("folder", name, m.group(0), *m.span())
    if re.search(r"\b(downloads?)\b", low):
        m = re.search(r"\bdownloads?\b", low)
        return Entity("folder", "downloads", m.group(0), *m.span())
    return None


def find_apps(text: str) -> list[Entity]:
    low = text.lower()
    out: list[Entity] = []
    used: list[tuple[int, int]] = []
    for alias, key in alias_index():
        for m in re.finditer(rf"(?<![\w.]){re.escape(alias)}(?![\w])", low):
            if any(s < m.end() and m.start() < e for s, e in used):
                continue
            used.append(m.span())
            out.append(Entity("app", key, m.group(0), *m.span()))
    out.sort(key=lambda e: e.start)
    return out


def find_people(text: str) -> list[Entity]:
    """Names with honorifics, relation phrases ('my professor') and capitalised names after 'to'."""
    out = []
    for m in re.finditer(rf"\b({HONORIFICS}\s+[A-Z][a-zA-Z\-']+(?:\s+[A-Z][a-zA-Z\-']+)?)", text, re.I):
        out.append(Entity("person", _clean_person(m.group(1)), m.group(1), *m.span(1)))
    for m in re.finditer(r"\bmy\s+(" + "|".join(sorted(RELATION_WORDS, key=len, reverse=True)) + r")\b", text, re.I):
        out.append(Entity("person", "my " + m.group(1).lower(), m.group(0), *m.span()))
    for m in re.finditer(r"\b(?:to|with|for|email|mail|message|tell|ask|call)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b", text):
        name = m.group(1)
        if name.lower() in {"i", "gmail", "genesis", "monday", "tuesday", "wednesday", "thursday", "friday",
                             "saturday", "sunday", "google", "chrome"}:
            continue
        if not any(e.start <= m.start(1) < e.end for e in out):
            out.append(Entity("person", name, name, *m.span(1), confidence=0.7))
    return out


def _clean_person(name: str) -> str:
    parts = name.split()
    first = parts[0].rstrip(".").lower()
    mapping = {"prof": "Professor", "professor": "Professor", "dr": "Dr.", "doctor": "Dr.", "mr": "Mr.", "mrs": "Mrs.", "ms": "Ms."}
    head = mapping.get(first, parts[0].capitalize())
    return " ".join([head] + [p[:1].upper() + p[1:] for p in parts[1:]])


def extract_entities(text: str, now: Optional[datetime] = None) -> list[Entity]:
    ents: list[Entity] = []
    ents += find_emails(text)
    ents += find_urls(text)
    ents += find_paths(text)
    ents += find_filenames(text)
    folder = find_folder(text)
    if folder:
        ents.append(folder)
    ents += find_apps(text)
    ents += find_people(text)
    dt = parse_datetime(text, now)
    if dt:
        s, e = dt.spans[0] if dt.spans else (-1, -1)
        ents.append(Entity("datetime", dt.value, text[s:e] if s >= 0 else "", s, e))
    for m in re.finditer(r"\b\d+(?:\.\d+)?\b", text):
        ents.append(Entity("number", float(m.group(0)), m.group(0), *m.span(), confidence=0.5))
    ents.sort(key=lambda e: (e.start, e.type))
    return ents
