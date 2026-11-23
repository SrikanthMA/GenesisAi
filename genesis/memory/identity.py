"""User identity: canonical name, preferred name, nicknames and speech-recognition variants.

Speech recognisers spell names in many ways ("Srikanth" → "Srikant", "Shri Kant", "Shreekanth"). Instead of hard-coding
one spelling, Genesis keeps an identity record and matches what it hears against it with a phonetic key + fuzzy
similarity:

* score ≥ ACCEPT  → it's the user's name (no questions asked)
* score ≥ CONFIRM → ask **once**: "Did you mean Srikanth?" — a "yes" stores the heard form as a variant, a "no" stores it
  as rejected, so the same question is never asked twice
* below           → not the user's name

The record is generic (any user can set their own name) and is stored locally in the preferences table. The canonical
and preferred names also feed the speech recogniser's vocabulary hint so it spells them correctly in the first place.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import asdict, dataclass, field
from typing import Optional

ACCEPT = 0.9
CONFIRM = 0.72
PREF_KEY = "identity"

_PHONETIC_RULES = [
    (r"sch", "s"), (r"sh", "s"), (r"th", "t"), (r"ph", "f"), (r"ck", "k"), (r"kh", "k"), (r"gh", "g"), (r"dh", "d"), (r"bh", "b"),
    (r"jh", "j"), (r"ch", "c"), (r"q", "k"), (r"x", "ks"), (r"w", "v"), (r"z", "s"), (r"c(?=[eiy])", "s"), (r"c", "k"),
    (r"ee|ea|ie|ey|y", "i"), (r"oo|ou|u", "u"), (r"aa", "a"), (r"(?<=[^aeiou])h", ""), (r"^h", ""),
]


def phonetic_key(text: str) -> str:
    """A spelling-insensitive key: 'Shri Kant', 'Srikant', 'Shreekanth' → 'srikant'."""
    t = re.sub(r"[^a-z]", "", (text or "").lower())
    for rx, rep in _PHONETIC_RULES:
        t = re.sub(rx, rep, t)
    return re.sub(r"(.)\1+", r"\1", t)  # collapse doubled letters


def similarity(a: str, b: str) -> float:
    ka, kb = phonetic_key(a), phonetic_key(b)
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 0.97
    return difflib.SequenceMatcher(None, ka, kb).ratio()


@dataclass
class Identity:
    canonical: str = ""  # full name, e.g. "Srikanth Mandyam Manadhare" (used to sign e-mails)
    preferred: str = ""  # what Genesis calls the user, e.g. "Srikanth"
    nicknames: list[str] = field(default_factory=list)
    variants: list[str] = field(default_factory=list)  # confirmed recogniser spellings
    rejected: list[str] = field(default_factory=list)  # forms the user said are NOT their name

    @property
    def call_name(self) -> str:
        return self.preferred or (self.canonical.split()[0] if self.canonical else "")

    def known_forms(self) -> list[str]:
        forms = [self.canonical, self.preferred, *self.nicknames, *self.variants]
        parts = self.canonical.split()
        if len(parts) >= 2:
            forms += [parts[0], " ".join(parts[:2])]
        return [f for f in dict.fromkeys(x.strip() for x in forms if x and x.strip())]


@dataclass
class NameMatch:
    heard: str
    score: float
    form: str

    @property
    def certain(self) -> bool:
        return self.score >= ACCEPT

    @property
    def uncertain(self) -> bool:
        return CONFIRM <= self.score < ACCEPT


class IdentityManager:
    def __init__(self, repos, settings) -> None:
        self.repos = repos
        self.settings = settings

    # ------------------------------------------------------------------ storage
    def get(self) -> Identity:
        raw = self.repos.preferences.get(PREF_KEY, None)
        if isinstance(raw, dict):
            known = {k: raw.get(k) for k in Identity.__dataclass_fields__ if k in raw}
            ident = Identity(**known)
        else:
            ident = Identity()
        legacy = (self.settings.get("user.name", "") or "").strip()  # 1.0–1.2 stored just a name in settings
        if legacy and not ident.canonical:
            ident.canonical = legacy
        return ident

    def save(self, ident: Identity) -> None:
        ident.nicknames = list(dict.fromkeys(n for n in ident.nicknames if n))[:20]
        ident.variants = list(dict.fromkeys(v for v in ident.variants if v))[:50]
        ident.rejected = list(dict.fromkeys(v for v in ident.rejected if v))[:50]
        self.repos.preferences.set(PREF_KEY, asdict(ident))
        self.settings.set("user.name", ident.canonical)  # keeps e-mail signatures and older code working
        self.settings.set("voice.stt_hint", self.stt_hint(ident))

    # ------------------------------------------------------------------ updates
    def set_name(self, name: str, *, preferred: Optional[str] = None) -> Identity:
        ident = self.get()
        name = _clean(name)
        if ident.canonical and similarity(name, ident.canonical) < CONFIRM:
            ident.variants, ident.rejected = [], []  # a genuinely different name: start fresh
        ident.canonical = name
        if preferred is not None:
            ident.preferred = _clean(preferred)
        elif not ident.preferred or ident.preferred.lower() not in name.lower():
            ident.preferred = name.split()[0] if name else ""
        self.save(ident)
        return ident

    def fix_spelling(self, name: str) -> Identity:
        """'It's spelled Shrikanth' → replace just the matching part of the full name (and the preferred name)."""
        ident = self.get()
        name = _clean(name)
        words, n = ident.canonical.split(), len(name.split())
        if not words or n >= len(words):
            return self.set_name(name, preferred=name.split()[0] if name else None)
        best_i, best = 0, 0.0
        for i in range(0, len(words) - n + 1):
            sc = similarity(" ".join(words[i:i + n]), name)
            if sc > best:
                best_i, best = i, sc
        old_part = " ".join(words[best_i:best_i + n])
        words[best_i:best_i + n] = name.split()
        ident.canonical = " ".join(words)
        if ident.preferred.lower() == old_part.lower() or similarity(ident.preferred, old_part) >= ACCEPT:
            ident.preferred = name
        if old_part.lower() != name.lower():
            ident.variants.append(old_part)
        self.save(ident)
        return ident

    def set_preferred(self, name: str) -> Identity:
        ident = self.get()
        name = _clean(name)
        if name and name.lower() not in (n.lower() for n in ident.nicknames) and ident.canonical and \
                name.lower() not in ident.canonical.lower():
            ident.nicknames.append(name)
        ident.preferred = name
        if not ident.canonical:
            ident.canonical = name
        self.save(ident)
        return ident

    def learn_variant(self, heard: str) -> None:
        ident = self.get()
        heard = _clean(heard)
        if heard and heard.lower() not in (v.lower() for v in ident.known_forms()):
            ident.variants.append(heard)
        ident.rejected = [r for r in ident.rejected if r.lower() != heard.lower()]
        self.save(ident)

    def reject_variant(self, heard: str) -> None:
        ident = self.get()
        ident.rejected.append(_clean(heard))
        self.save(ident)

    def forget(self) -> None:
        self.repos.preferences.delete(PREF_KEY)
        self.settings.set("user.name", "")
        self.settings.set("voice.stt_hint", "")

    # ------------------------------------------------------------------ matching
    def match(self, heard: str) -> Optional[NameMatch]:
        """How well does *heard* (a name as the recogniser wrote it) match the user's identity?"""
        ident = self.get()
        heard = _clean(heard)
        if not heard or not ident.canonical:
            return None
        if heard.lower() in (r.lower() for r in ident.rejected):
            return NameMatch(heard, 0.0, "")
        best = NameMatch(heard, 0.0, "")
        for form in ident.known_forms():
            if heard.lower() == form.lower():
                return NameMatch(heard, 1.0, form)
            sc = similarity(heard, form)
            if sc > best.score:
                best = NameMatch(heard, sc, form)
        return best

    def find_in_text(self, text: str) -> Optional[NameMatch]:
        """Look for the user's name in free text (1–4 word windows)."""
        words = re.findall(r"[A-Za-z][A-Za-z'.-]*", text or "")
        best: Optional[NameMatch] = None
        for n in (4, 3, 2, 1):
            for i in range(0, max(0, len(words) - n + 1)):
                chunk = " ".join(words[i:i + n])
                if len(chunk) < 3:
                    continue
                m = self.match(chunk)
                if m and (best is None or m.score > best.score):
                    best = m
        return best if best and best.score >= CONFIRM else None

    # ------------------------------------------------------------------ speech
    @staticmethod
    def stt_hint(ident: Identity) -> str:
        """Vocabulary prompt for the recogniser so it spells the user's name correctly."""
        names = [n for n in (ident.canonical, ident.preferred, *ident.nicknames[:3]) if n]
        return ("My name is " + ", ".join(dict.fromkeys(names)) + ".") if names else ""

    def describe(self) -> str:
        ident = self.get()
        if not ident.canonical:
            return "I don't know your name yet. Tell me “my name is …”."
        parts = [f"Your name is {ident.canonical}"]
        if ident.preferred and ident.preferred != ident.canonical:
            parts.append(f"and I call you {ident.preferred}")
        text = " ".join(parts) + "."
        if ident.nicknames:
            text += " Nicknames: " + ", ".join(ident.nicknames) + "."
        if ident.variants:
            text += " I also recognise: " + ", ".join(ident.variants[:6]) + "."
        return text


def _clean(name: str) -> str:
    name = re.sub(r"[^\w .'-]", " ", name or "", flags=re.UNICODE)
    name = " ".join(name.split()).strip(" .'-")
    return " ".join(w[:1].upper() + w[1:] if w.islower() else w for w in name.split())[:120]


__all__ = ["IdentityManager", "Identity", "NameMatch", "phonetic_key", "similarity", "ACCEPT", "CONFIRM"]
