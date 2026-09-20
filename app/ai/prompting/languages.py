"""The set of languages Genesis explicitly supports in Phase 2.

This list is deliberately scoped to what the LLM detection/prompting layer
has been checked against, not what the underlying model "probably" handles.
STT and TTS language support (Phase 3) will each need their own capability
list - support for one layer must never be assumed from another (see
docs/ARCHITECTURE.md, "Multilingual Support").
"""

AUTO = "auto"

SUPPORTED_LANGUAGES: dict[str, str] = {
    "en": "English",
    "hi": "Hindi",
    "kn": "Kannada",
    "te": "Telugu",
    "ta": "Tamil",
    "ml": "Malayalam",
    "mr": "Marathi",
    "bn": "Bengali",
    "gu": "Gujarati",
    "pa": "Punjabi",
    "ur": "Urdu",
}


def is_valid_preference(code: str) -> bool:
    return code == AUTO or code in SUPPORTED_LANGUAGES


def language_name(code: str) -> str:
    return SUPPORTED_LANGUAGES.get(code, code)
