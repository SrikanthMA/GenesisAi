"""Genesis's speaking style: calm, warm, concise — "Done, Boss." instead of "Command executed successfully."

Applied once to the final reply of a turn (text and speech). The form of address is a setting
(``persona.address``, default "Boss"; empty = no address), so it works for every user.
"""
from __future__ import annotations

import re
from typing import Any

DEFAULT_ADDRESS = "Boss"


def address(settings: Any) -> str:
    try:
        value = settings.get("persona.address", DEFAULT_ADDRESS)
    except Exception:
        value = DEFAULT_ADDRESS
    return str(value or "").strip()[:40]


def styled(text: str, settings: Any) -> str:
    """Add the form of address to acknowledgements and to AI-connection problems; everything else is left as written."""
    a = address(settings)
    if not a or not text:
        return text
    if text.startswith("Done — "):
        return f"Done, {a} — " + text[len("Done — "):]
    if re.fullmatch(r"(Done|Okay|OK)\.", text):
        return f"{text[:-1]}, {a}."
    if text.startswith("I'm having trouble connecting to my AI model"):
        return f"{a}, I'm" + text[len("I'm"):]
    return text
