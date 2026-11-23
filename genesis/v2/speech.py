"""What Genesis says out loud (2.2.2).

The reply on screen can hold code, tables, file names, settings and technical asides; read aloud word for word that sounds
robotic ("Setup underscore Images dot bat", "open bracket no image service is set up close bracket", a whole program
spelled out). ``natural`` turns screen text into something a person would say:

* code blocks and tables are not read - "it's on your screen" instead;
* (asides in brackets) are dropped - they're details for reading, not listening;
* file names become words ("Setup_Images.bat" -> "the Setup Images file"), settings like KEY=value are skipped;
* "Credit: …" lines and similar footnotes are skipped;
* at most the first two or three sentences are spoken.

The full text always stays on screen.
"""
from __future__ import annotations

import re

_FENCE = re.compile(r"```.*?(?:```|$)", re.S)
_TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$", re.M)
_PAREN = re.compile(r"\s*\([^()]{0,240}\)")
_SETTING = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+(?:=\S*)?")
_FILE = re.compile(r"\b(?P<stem>[\w.-]*[A-Za-z][\w-]*)\.(?P<ext>bat|py|exe|env|txt|docx|xlsx|pptx|pdf|csv|json|md|ps1|ipynb|log|zip|jpg|png|webp)\b",
                   re.I)
_TECH = re.compile(r"(?:^|\s)\.env\b|\bpip install\b|\bplaywright install\b|\bpython -m\b|\b--[a-z][\w-]+|"
                   r"\b[A-Za-z]:\\\\|\blogs[\\/]", re.I)
_FOOTNOTES = re.compile(r"(?:^|(?<=[.!?]\s))(?:Credit|Source|Sources|Note|Tip|Heads-up|Hint)\s*:[^.!?\n]*[.!?]?", re.I)
_EXT_WORD = {"bat": "file", "ps1": "file", "exe": "program", "py": "script", "ipynb": "notebook", "docx": "document",
             "xlsx": "spreadsheet", "pptx": "presentation", "pdf": "PDF", "csv": "file", "json": "file", "txt": "file",
             "md": "file", "env": "settings file", "log": "log", "zip": "zip file", "jpg": "picture", "png": "picture",
             "webp": "picture"}
MAX_SENTENCES = 3
MAX_CHARS = 320


def _file_words(m: re.Match) -> str:
    stem = re.sub(r"[_\-.]+", " ", m.group("stem")).strip()
    if not stem:
        return "the settings file"
    return f"the {stem} {_EXT_WORD.get(m.group('ext').lower(), 'file')}"


def natural(text: str) -> str:
    """Screen text -> what to say. Returns a short, natural sentence or two (may be "" for an empty reply)."""
    t = text or ""
    had_code = bool(_FENCE.search(t))
    had_table = bool(_TABLE_LINE.search(t))
    t = _FENCE.sub(" ", t)
    t = _TABLE_LINE.sub(" ", t)
    t = re.sub(r"https?://\S+", "", t)
    t = _FOOTNOTES.sub(" ", t)
    t = _PAREN.sub("", t)
    # a sentence about settings or install commands is for reading, not listening: skip it whole
    parts = re.split(r"(?<=[.!?])\s+|\n+", t)
    t = "\n".join(p for p in parts if not (_SETTING.search(p) or _TECH.search(p)))
    t = _FILE.sub(_file_words, t)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"[“”\"]", "", t)
    t = re.sub(r"\s*\n+\s*", ". ", t)
    t = re.sub(r"\s{2,}", " ", t)
    t = re.sub(r"\s+([.,!?;:])", r"\1", t)
    t = re.sub(r"([.!?])(?:\s*[.;:,])+", r"\1", t).strip(" .;:,-")
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", t) if len(s.strip(" .")) > 1]
    spoken = " ".join(sentences[:MAX_SENTENCES])
    if len(spoken) > MAX_CHARS:
        spoken = " ".join(sentences[:2]) if len(" ".join(sentences[:2])) <= MAX_CHARS else sentences[0][:MAX_CHARS]
    if spoken and spoken[-1] not in ".!?":
        spoken += "."
    trimmed = len(sentences) > MAX_SENTENCES
    if had_code and "screen" not in spoken.lower():
        spoken += " The code is on your screen."
    elif (had_table or trimmed) and "screen" not in spoken.lower():
        spoken += " The details are on your screen."
    return spoken.strip()
