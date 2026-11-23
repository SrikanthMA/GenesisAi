"""Multilingual layer: detect the user's language, translate it to English for the pipeline, and translate replies back.

Design (cheapest reliable method first, like the rest of the NLU):

1. **Detect offline** (``detect``) — no model call:
   * non-Latin scripts by Unicode block (Devanagari → Hindi, Kannada, Tamil, Arabic, Cyrillic, CJK, …);
   * Latin-script languages by function-word overlap with small profiles (French, Spanish, German, Italian, Portuguese,
     Dutch, Romanized Hindi/"Hinglish") plus diacritic hints (ç, ñ, ß, ã …);
   * a *rule-first guard*: if the English rule parser already understands the sentence, it is English
     ("open la la land" stays English even though "la" is French).
   * for voice, Whisper's own language identification is used as a hint.
2. **Translate in** (``LanguageService.understand``) — only for non-English input: one LLM call returns
   ``{"language": …, "english": …}``. Names, app names, URLs, file names and quoted text are kept unchanged.
3. **The normal English pipeline runs** (rules → classifier → LLM, planner, executor) — nothing downstream changes.
4. **Translate out** (``LanguageService.localize``) — the English reply is translated into the user's language
   (one LLM call); conversational answers are written directly in that language by the chat model instead.

English input costs nothing extra: detection is a few microseconds and no model is called.
"""
from __future__ import annotations

import re
import threading
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional

from ..core.logging_setup import get_logger

log = get_logger("nlp.language")

# ISO 639-1 code → (English name, native name). "hi-Latn" = Hindi written in Latin letters ("Hinglish").
LANGUAGES: dict[str, tuple[str, str]] = {
    "en": ("English", "English"), "fr": ("French", "Français"), "es": ("Spanish", "Español"), "de": ("German", "Deutsch"),
    "it": ("Italian", "Italiano"), "pt": ("Portuguese", "Português"), "nl": ("Dutch", "Nederlands"),
    "hi": ("Hindi", "हिन्दी"), "hi-Latn": ("Hindi (Latin script)", "Hinglish"), "kn": ("Kannada", "ಕನ್ನಡ"),
    "ta": ("Tamil", "தமிழ்"), "te": ("Telugu", "తెలుగు"), "ml": ("Malayalam", "മലയാളം"), "bn": ("Bengali", "বাংলা"),
    "gu": ("Gujarati", "ગુજરાતી"), "pa": ("Punjabi", "ਪੰਜਾਬੀ"), "or": ("Odia", "ଓଡ଼ିଆ"), "mr": ("Marathi", "मराठी"),
    "ur": ("Urdu", "اردو"), "ar": ("Arabic", "العربية"), "fa": ("Persian", "فارسی"), "he": ("Hebrew", "עברית"),
    "ru": ("Russian", "Русский"), "uk": ("Ukrainian", "Українська"), "el": ("Greek", "Ελληνικά"), "th": ("Thai", "ไทย"),
    "zh": ("Chinese", "中文"), "ja": ("Japanese", "日本語"), "ko": ("Korean", "한국어"), "tr": ("Turkish", "Türkçe"),
    "pl": ("Polish", "Polski"), "sv": ("Swedish", "Svenska"), "id": ("Indonesian", "Bahasa Indonesia"),
    "vi": ("Vietnamese", "Tiếng Việt"), "ka": ("Georgian", "ქართული"), "hy": ("Armenian", "Հայերեն"),
}


def language_name(code: Optional[str]) -> str:
    return LANGUAGES.get(code or "", (code or "unknown", ""))[0]


def base_code(code: Optional[str]) -> str:
    """'hi-Latn' → 'hi', 'pt-BR' → 'pt', 'FR' → 'fr'."""
    return (code or "").split("-")[0].split("_")[0].lower()


# ----------------------------------------------------------------------------------------------- script detection
# (first code point, last code point, language) — checked per letter
_SCRIPTS: list[tuple[int, int, str]] = [
    (0x0900, 0x097F, "hi"), (0x0980, 0x09FF, "bn"), (0x0A00, 0x0A7F, "pa"), (0x0A80, 0x0AFF, "gu"), (0x0B00, 0x0B7F, "or"),
    (0x0B80, 0x0BFF, "ta"), (0x0C00, 0x0C7F, "te"), (0x0C80, 0x0CFF, "kn"), (0x0D00, 0x0D7F, "ml"), (0x0E00, 0x0E7F, "th"),
    (0x0600, 0x06FF, "ar"), (0x0750, 0x077F, "ar"), (0x0590, 0x05FF, "he"), (0x0400, 0x04FF, "ru"), (0x0370, 0x03FF, "el"),
    (0x10A0, 0x10FF, "ka"), (0x0530, 0x058F, "hy"), (0x3040, 0x30FF, "ja"), (0xAC00, 0xD7AF, "ko"), (0x1100, 0x11FF, "ko"),
    (0x4E00, 0x9FFF, "zh"), (0x3400, 0x4DBF, "zh"),
]
_URDU_ONLY = set("ٹڈڑںےۓھ")
_PERSIAN_ONLY = set("پچژگ")  # also used in Urdu; only decisive without Urdu letters
_UKRAINIAN_ONLY = set("іїєґІЇЄҐ")


def _script_language(text: str) -> tuple[Optional[str], float]:
    counts: dict[str, int] = {}
    letters = 0
    for ch in text:
        if not ch.isalpha():
            continue
        letters += 1
        cp = ord(ch)
        if cp < 0x0370:
            continue
        for lo, hi, lang in _SCRIPTS:
            if lo <= cp <= hi:
                counts[lang] = counts.get(lang, 0) + 1
                break
    if not counts or not letters:
        return None, 0.0
    lang, _n = max(counts.items(), key=lambda kv: kv[1])
    if counts.get("ja") and lang in ("zh", "ja"):  # kanji + kana → Japanese
        lang = "ja"
    share = sum(counts.values()) / letters  # "YouTubeを開いて": the brand name is Latin, the sentence is not
    if share < 0.2 or sum(counts.values()) < 2:
        return None, 0.0
    chars = set(text)
    if lang == "ar":
        if chars & _URDU_ONLY:
            lang = "ur"
        elif chars & _PERSIAN_ONLY:
            lang = "fa"
    elif lang == "ru" and chars & _UKRAINIAN_ONLY:
        lang = "uk"
    return lang, min(1.0, 0.6 + share * 0.4)


# ----------------------------------------------------------------------------------------------- Latin-script profiles
# Function words and common command verbs. Words shared by several languages count for each of them.
_PROFILES: dict[str, set[str]] = {
    "en": set("the a an and is are am to of in on my me you your please open close what what's how why who where when it "
              "for with this that can could do does play search show set send remind i at from about tell create make find "
              "turn start stop go take write read check give let's want need yes no thanks hello hey as by add mark done "
              "type buy call list file folder new all be will have just now today tomorrow not into out up down off then "
              "next last first second some any be there here".split()),
    "fr": set("le la les un une des et est de du à au aux je tu il elle nous vous mon ma mes ton ta tes son sa ses "
              "s'il plaît ouvre ouvrir ferme fermer cherche chercher joue jouer mets envoie envoyer quel quelle quoi comment "
              "pourquoi c'est sur pour avec dans oui non merci bonjour salut moi peux veux est-ce qu'est-ce rappelle "
              "rappelle-moi écris crée montre-moi montre lance peux-tu pouvez-vous voudrais j'ai ça éteins éteindre allume "
              "redémarre baisse monte augmente".split()),
    "es": set("el la los las un una y es de del en que qué por para con mi mis tu tus abre abrir cierra cerrar busca "
              "buscar pon reproduce envía enviar cómo cuál dónde sí gracias hola favor puedes quiero recuérdame escribe "
              "crea muéstrame está estoy necesito también".split()),
    "de": set("der die das und ist ein eine einen ich du mein meine bitte öffne öffnen schließe schließen suche suchen "
              "spiele spielen zeig zeige mir wie was warum nicht mit für auf ja nein danke hallo kannst erinnere "
              "schreib schreibe erstelle den dem des sie wir".split()),
    "it": set("il lo la gli le un una e è di del della che per con mio mia miei apri aprire chiudi chiudere cerca "
              "cercare riproduci metti invia inviare come cosa perché sì grazie ciao puoi voglio ricordami scrivi crea "
              "mostrami sono".split()),
    "pt": set("o a os as um uma e é de do da dos das que por para com meu minha abra abrir feche fechar procure "
              "pesquise toque coloque envie enviar como qual sim não obrigado obrigada olá você pode quero lembre "
              "escreva crie mostre estou".split()),
    "nl": set("de het een en is ik mijn alsjeblieft open sluit zoek speel toon hoe wat waarom niet met voor op ja "
              "nee dank bedankt hallo kun kunt wil herinner schrijf maak".split()),
    "hi-Latn": set("hai hain kya kaise karo kar kardo kholo khol band bajao chalao batao dikhao mujhe mera meri mere "
                   "tum aap nahi nahin haan aur ko se ka ki ke mein yeh woh kuch abhi gaana gana bhejo likho yaad "
                   "dilao chahiye kijiye karna karde dikha suna sunao pe wala wali".split()),
}
# Unmistakably foreign command words ("open", "play", "show" … in other languages). When one is present the English
# rule parser's opinion is ignored — otherwise "youtube kholo" would be read as an app called "youtube kholo".
_STRONG: set[str] = set("ouvre ouvrir ferme cherche joue mets envoie rappelle-moi écris montre-moi éteins allume abre abrir cierra busca "
                        "reproduce envía muéstrame recuérdame öffne öffnen schließe suche spiele zeige erinnere apri chiudi "
                        "cerca riproduci metti invia ricordami mostrami abra feche procure pesquise toque envie lembre "
                        "kholo khol bajao chalao batao dikhao bhejo likho sunao dikha suna karo kardo kijiye".split())
_DIACRITICS: dict[str, str] = {"fr": "çéèêëàâùûôîïœ", "es": "ñ¿¡áíóú", "de": "äöüß", "pt": "ãõçáâêô", "it": "àèéìòù"}
_WORD = re.compile(r"[a-zà-öø-ÿœß']+(?:-[a-zà-öø-ÿ]+)?", re.I)


@dataclass
class Detection:
    lang: str  # ISO code ("en", "fr", "hi-Latn" …) or "und" (undetermined)
    confidence: float  # 0..1
    method: str  # script | words | rules | hint | default

    @property
    def is_english(self) -> bool:
        return self.lang in ("en", "und")


_NOT_WORDS = re.compile(r"\S+@\S+|https?://\S+|www\.\S+|\S+\.(?:com|org|net|in|io|ai|edu|gov)\b\S*|[a-z]:\\\S*|/\S+", re.I)


def _latin_scores(text: str) -> dict[str, float]:
    text = _NOT_WORDS.sub(" ", text)  # e-mail addresses, URLs and paths say nothing about the language
    words = [w.lower() for w in _WORD.findall(text)]
    scores = {lang: 0.0 for lang in _PROFILES}
    for w in words:
        for lang, vocab in _PROFILES.items():
            if w in vocab:
                scores[lang] += 1.0
        if re.match(r"^(?:l|d|j|qu|n|c|s|m|t)'[a-zà-ÿ]", w):  # elision: l'ordinateur, d'appeler, qu'il
            scores["fr"] += 1.0
            scores["it"] += 0.5
    low = text.lower()
    for lang, chars in _DIACRITICS.items():
        hits = sum(low.count(c) for c in chars)
        if hits:
            scores[lang] += min(2.0, 0.75 * hits)
    return scores


def detect(text: str, *, hint: Optional[str] = None, hint_confidence: float = 0.0, english_parser=None) -> Detection:
    """Offline language identification for one utterance.

    ``hint`` — language reported by speech recognition (Whisper); ``english_parser`` — callable returning truthy when the
    English rule layer already understands the text (the rule-first guard)."""
    t = (text or "").strip()
    if not t:
        return Detection("und", 0.0, "default")
    lang, conf = _script_language(t)
    if lang:
        return Detection(lang, conf, "script")
    if hint and base_code(hint) != "en" and hint_confidence >= 0.75:
        return Detection(base_code(hint), hint_confidence, "hint")
    scores = _latin_scores(t)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (best, top), (_, second) = ranked[0], ranked[1]
    en = scores["en"]
    # rule-first guard: the English rules understand it and no unmistakably foreign command word is present
    strong = any(w.lower() in _STRONG for w in _WORD.findall(t))
    if english_parser is not None and not strong:
        try:
            if english_parser(t):
                return Detection("en", 0.95, "rules")
        except Exception:
            pass
    if top <= 0:
        return Detection("und", 0.0, "default")
    if strong:  # v2 hook — code-switching ("youtube pe python tutorial search karo"): a foreign command verb wins ties
        lang_f, score_f = max(((k, v) for k, v in scores.items() if k != "en"), key=lambda kv: kv[1])
        if score_f > 0 and score_f >= en:
            return Detection(lang_f, 0.7, "code-switch")
    if best == "en" or en >= top:
        return Detection("en", min(1.0, 0.5 + 0.15 * en), "words")
    if top - max(en, 0) < 1.0:  # not enough evidence against English
        return Detection("und", 0.3, "words")
    margin = top - second
    return Detection(best, min(0.95, 0.45 + 0.15 * top + 0.1 * margin), "words")


# ----------------------------------------------------------------------------------------------- yes / no everywhere
# Short confirmation answers in many languages — understood offline, so a "oui" never needs a model call.
YES_WORDS = {"oui", "ouais", "d'accord", "sí", "si", "claro", "vale", "ja", "jawohl", "genau", "sì", "certo", "sim",
             "haan", "haa", "haanji", "ji", "theek hai", "thik hai", "हाँ", "हां", "जी", "ಹೌದು", "ஆம்", "అవును", "അതെ",
             "да", "так", "نعم", "بله", "ہاں", "はい", "ええ", "是", "是的", "好", "好的", "네", "예", "evet", "tak", "ja tack", "ya", "vâng"}
NO_WORDS = {"non", "no", "nein", "não", "nao", "nahi", "nahin", "na", "mat", "नहीं", "ना", "ಇಲ್ಲ", "இல்லை", "కాదు", "వద్దు",
            "ഇല്ല", "нет", "ні", "لا", "نه", "نہیں", "いいえ", "いや", "不", "不是", "不要", "아니요", "아니", "hayır", "nie", "nej",
            "tidak", "không", "annuler", "cancelar", "abbrechen", "annulla"}


def multilingual_yes_no(text: str) -> Optional[bool]:
    t = unicodedata.normalize("NFC", (text or "").strip().lower())
    t = re.sub(r"\s+", " ", re.sub(r"[,，、!?¡¿.。！]", " ", t)).strip()
    t = re.sub(r"^(?:hey |ok |okay )?genesis[, ]*", "", t).strip()
    t = re.sub(r"\s+(?:please|s'il te plaît|s'il vous plaît|por favor|bitte|per favore|merci|gracias|danke)$", "", t)
    if t in NO_WORDS:
        return False
    if t in YES_WORDS:
        return True
    return None


# ----------------------------------------------------------------------------------------------- LLM translation
TRANSLATE_IN_PROMPT = """You translate a user's spoken or typed request for a desktop assistant into English.
Detected language (may be wrong): {lang}.
Rules:
- Translate the meaning faithfully and naturally; keep it a request or question exactly as the user meant it.
- Keep unchanged: names of people, apps and websites, file and folder names, URLs, e-mail addresses, numbers, times,
  and anything inside quotation marks. "Genesis" is the assistant's name ("Hey Genesis" is its wake phrase).
- If the text is already English, return it unchanged.
Return JSON only: {{"language": "<ISO 639-1 code of the original text>", "english": "<English translation>"}}
Text: {text}"""

TRANSLATE_OUT_PROMPT = """Translate this reply from a desktop assistant from English into {lang}.
Rules: natural, friendly and concise; keep unchanged names, video/file titles in quotes, file names, URLs, numbers, times
and the line breaks / list numbering. If the reply is already in {lang}, return it unchanged.
Return JSON only: {{"text": "<translation>"}}
Reply:
{text}"""


@dataclass
class Incoming:
    original: str
    english: str
    lang: str  # detected language of the original
    translated: bool = False
    error: Optional[str] = None  # set when the text is not English but could not be translated


class LanguageService:
    """Detection + translation, with a small LRU cache so repeated phrases cost nothing."""

    def __init__(self, llm, settings, english_parser=None, cache_size: int = 256) -> None:
        self.llm = llm
        self.settings = settings
        self.english_parser = english_parser
        self._cache: "OrderedDict[tuple, str]" = OrderedDict()
        self._lock = threading.Lock()
        self.cache_size = cache_size

    # settings
    @property
    def enabled(self) -> bool:
        return str(self.settings.get("language.mode", "auto") or "auto") != "english"

    @property
    def reply_in_user_language(self) -> bool:
        return str(self.settings.get("language.reply", "same") or "same") == "same"

    def reply_language(self, user_lang: Optional[str]) -> str:
        """Language to answer in: the user's (setting 'same'), English, or a fixed language the user chose."""
        pref = str(self.settings.get("language.reply", "same") or "same")
        if pref == "english":
            return "en"
        if pref == "same":
            return user_lang or "en"
        return pref if pref in LANGUAGES else (user_lang or "en")

    def _llm_ready(self) -> bool:
        return bool(self.llm is not None and getattr(self.llm, "is_generative", False))

    def _cached(self, key: tuple) -> Optional[str]:
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        return None

    def _store(self, key: tuple, value: str) -> None:
        with self._lock:
            self._cache[key] = value
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)

    def detect(self, text: str, hint: Optional[str] = None, hint_confidence: float = 0.0) -> Detection:
        return detect(text, hint=hint, hint_confidence=hint_confidence, english_parser=self.english_parser)

    def understand(self, text: str, *, hint: Optional[str] = None, hint_confidence: float = 0.0,
                   previous: Optional[str] = None) -> Incoming:
        """Return the English version of ``text`` (unchanged when it is English)."""
        if not self.enabled or not (text or "").strip():
            return Incoming(text, text, "en")
        det = self.detect(text, hint, hint_confidence)
        if det.lang == "und":
            # no evidence either way: a short answer (a name, a number, "YouTube") keeps the conversation's language;
            # a longer sentence of unknown words is treated as English
            short = len(text.split()) <= 2
            return Incoming(text, text, (previous or "en") if short else "en")
        if det.is_english:
            return Incoming(text, text, "en")
        key = ("in", text.strip())
        hit = self._cached(key)
        if hit is not None:
            lang, _, english = hit.partition("\x1f")
            return Incoming(text, english, lang or det.lang, translated=True)
        if not self._llm_ready():
            return Incoming(text, text, det.lang, error="no_model")
        try:
            data: Any = self.llm.chat_json([{"role": "user", "content": TRANSLATE_IN_PROMPT.format(
                lang=language_name(det.lang), text=text.strip()[:2000])}], max_tokens=400, temperature=0.0)
        except Exception as exc:  # LLMError and malformed output alike
            log.info("translation to English failed: %s", type(exc).__name__)
            return Incoming(text, text, det.lang, error="translate_failed")
        english = str((data or {}).get("english") or "").strip() if isinstance(data, dict) else ""
        if not english:
            return Incoming(text, text, det.lang, error="translate_failed")
        lang = str((data or {}).get("language") or "").strip()
        lang = lang if re.fullmatch(r"[a-zA-Z]{2,3}(?:-[A-Za-z]{2,4})?", lang or "") else det.lang
        if det.lang == "hi-Latn" and base_code(lang) == "hi":
            lang = "hi-Latn"  # keep replying in Latin script to someone who writes Hinglish
        if base_code(lang) == "en":  # the model says it was English after all
            return Incoming(text, english[:2000], "en")
        english = english[:2000]
        self._store(key, f"{lang}\x1f{english}")
        log.info("translated_in", extra={"data": {"lang": lang, "method": det.method, "chars": len(text)}})
        return Incoming(text, english, lang, translated=True)

    def localize(self, text: str, lang: str) -> str:
        """Translate an English reply into ``lang``. Falls back to the English text if that isn't possible."""
        if not text or base_code(lang) in ("en", "und", "") or not self._llm_ready():
            return text
        key = ("out", lang, text)
        hit = self._cached(key)
        if hit is not None:
            return hit
        name = "Hindi written in Latin letters (Hinglish), not Devanagari" if lang == "hi-Latn" else language_name(lang)
        try:
            data: Any = self.llm.chat_json([{"role": "user", "content": TRANSLATE_OUT_PROMPT.format(lang=name, text=text[:3000])}],
                                           max_tokens=900, temperature=0.2)
        except Exception as exc:
            log.info("reply translation failed: %s", type(exc).__name__)
            return text
        out = str(data.get("text") or "").strip() if isinstance(data, dict) else ""
        if not out:
            return text
        self._store(key, out)
        return out
