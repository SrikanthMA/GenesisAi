"""Offline language-detection evaluation (run: python -m eval.run_language_eval).

The key number is the **English false-foreign rate**: every English command wrongly flagged as foreign would cost a
translation call and could change the meaning. Foreign words inside English commands (song titles, names, dishes) are
deliberately included.
"""
from __future__ import annotations

from collections import defaultdict

from genesis.nlp import rules
from genesis.nlp.language import detect

DATA = [
    # English — including foreign names, titles and food inside English commands
    ("play despacito on youtube", "en"), ("open la la land on netflix", "en"), ("search for croissant recipes", "en"),
    ("play la vie en rose", "en"), ("email pierre about the meeting", "en"), ("remind me to buy paneer at 6", "en"),
    ("open spotify and play arijit singh", "en"), ("what is the capital of germany", "en"), ("set volume to 30", "en"),
    ("find my resume.pdf", "en"), ("search the web for dosa near me", "en"), ("type del /s /q c:\\", "en"),
    ("mark buy milk as done", "en"), ("tell me about the eiffel tower", "en"), ("open my documents folder", "en"),
    ("how do you say thank you in japanese", "en"), ("send rahul a message saying hola", "en"),
    ("play bella ciao", "en"), ("who wrote don quixote", "en"), ("open google docs", "en"),
    # French
    ("ouvre youtube", "fr"), ("quelle heure est-il", "fr"), ("rappelle-moi d'appeler maman", "fr"), ("éteins l'ordinateur", "fr"),
    ("cherche des tutoriels python", "fr"), ("joue la deuxième", "fr"), ("envoie un message à rahul", "fr"),
    # Spanish / German / Italian / Portuguese / Dutch
    ("abre youtube por favor", "es"), ("¿qué hora es?", "es"), ("busca recetas de pasta", "es"),
    ("öffne bitte youtube", "de"), ("wie spät ist es", "de"), ("apri il calendario", "it"), ("che ore sono", "it"),
    ("abra o youtube", "pt"), ("que horas são", "pt"), ("open alsjeblieft youtube", "nl"),
    # Hindi (Devanagari and Latin) and code-switching
    ("यूट्यूब खोलो", "hi"), ("मुझे पांच बजे याद दिलाना", "hi"), ("youtube kholo", "hi-Latn"), ("gaana bajao", "hi-Latn"),
    ("mujhe kal yaad dilao", "hi-Latn"), ("youtube pe python tutorial search karo", "hi-Latn"),
    ("chrome band karo", "hi-Latn"), ("kya time hai", "hi-Latn"),
    # other Indian and world scripts
    ("ಯೂಟ್ಯೂಬ್ ತೆರೆಯಿರಿ", "kn"), ("யூடியூப் திற", "ta"), ("యూట్యూబ్ తెరువు", "te"), ("യൂട്യൂബ് തുറക്കുക", "ml"),
    ("ইউটিউব খোলো", "bn"), ("યુટ્યુબ ખોલો", "gu"), ("ਯੂਟਿਊਬ ਖੋਲ੍ਹੋ", "pa"), ("یوٹیوب کھولیں", "ur"), ("افتح يوتيوب", "ar"),
    ("открой ютуб", "ru"), ("YouTubeを開いて", "ja"), ("打开油管", "zh"), ("유튜브 열어줘", "ko"), ("άνοιξε το youtube", "el"),
]


def main() -> dict:
    parser = lambda t: rules.parse(rules.normalize(t)) is not None  # noqa: E731 - same guard GenesisCore uses
    per = defaultdict(list)
    wrong = []
    for text, lang in DATA:
        got = detect(text, english_parser=parser).lang
        good = got == lang or (lang == "en" and got == "und")
        per[lang if lang in ("en",) else ("indic/world script" if ord(max(text)) > 0x2FF else "latin foreign")].append(good)
        if not good:
            wrong.append((text, lang, got))
    en = per["en"]
    res = {"n": len(DATA), "accuracy": sum(g for v in per.values() for g in v) / len(DATA),
           "english_false_foreign_rate": 1 - sum(en) / len(en), "groups": {k: f"{sum(v)}/{len(v)}" for k, v in per.items()},
           "errors": wrong}
    return res


if __name__ == "__main__":
    r = main()
    print(f"language detection: {r['n']} utterances, accuracy {r['accuracy']:.3f}, "
          f"English wrongly flagged as foreign: {r['english_false_foreign_rate']:.1%}")
    print("groups:", r["groups"])
    for e in r["errors"]:
        print("  wrong:", e)
