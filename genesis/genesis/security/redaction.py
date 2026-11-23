"""Redaction of secrets from logs, audit records and UI output."""
from __future__ import annotations

import logging
import re
from typing import Any

_SENSITIVE_KEYS = re.compile(
    r"(pass(word)?|secret|token|api[_-]?key|authorization|access[_-]?key|refresh|client_secret|cookie|credential)",
    re.I,
)

_PATTERNS = [
    re.compile(r"hf_[A-Za-z0-9]{20,}"),  # Hugging Face tokens
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),  # OpenAI-style keys
    re.compile(r"ya29\.[A-Za-z0-9_\-\.]+"),  # Google OAuth access tokens
    re.compile(r"1//[A-Za-z0-9_\-]{20,}"),  # Google refresh tokens
    re.compile(r"GOCSPX-[A-Za-z0-9_\-]+"),  # Google client secrets
    re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.=]+"),
    re.compile(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key)\s*[=:]\s*\S+"),
]

MASK = "[REDACTED]"


def redact_text(text: str) -> str:
    if not text:
        return text
    out = str(text)
    for pat in _PATTERNS:
        out = pat.sub(MASK, out)
    return out


def redact(obj: Any, _depth: int = 0) -> Any:
    """Recursively redact dicts/lists/strings."""
    if _depth > 8:
        return obj
    if isinstance(obj, dict):
        clean = {}
        for k, v in obj.items():
            if isinstance(k, str) and _SENSITIVE_KEYS.search(k):
                clean[k] = MASK
            else:
                clean[k] = redact(v, _depth + 1)
        return clean
    if isinstance(obj, (list, tuple)):
        return type(obj)(redact(v, _depth + 1) for v in obj)
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


class RedactingFilter(logging.Filter):
    """Logging filter that scrubs secrets from every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact_text(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = redact(record.args)
                else:
                    record.args = tuple(redact(a) if isinstance(a, (str, dict, list)) else a for a in record.args)
            extra = getattr(record, "data", None)
            if extra is not None:
                record.data = redact(extra)
        except Exception:  # never break logging
            pass
        return True
