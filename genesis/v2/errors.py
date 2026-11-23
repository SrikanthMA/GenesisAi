"""Structured error categories for failed steps (stored in ``ToolResult.data["error_category"]``)."""
from __future__ import annotations

import re
from enum import Enum
from typing import Optional

from ..security.permissions import RiskLevel


class ErrorCategory(str, Enum):
    MISSING_DEPENDENCY = "missing_dependency"
    AUTHENTICATION = "authentication"
    NETWORK = "network"
    APP_UNAVAILABLE = "app_unavailable"
    INVALID_INPUT = "invalid_input"
    PERMISSION_DENIED = "permission_denied"
    AMBIGUOUS_TARGET = "ambiguous_target"
    TOOL_FAILURE = "tool_failure"
    VERIFICATION_FAILED = "verification_failed"
    UNKNOWN_OUTCOME = "unknown_outcome"
    CANCELLED = "cancelled"


_PATTERNS: list[tuple[ErrorCategory, re.Pattern]] = [
    (ErrorCategory.MISSING_DEPENDENCY, re.compile(r"not installed|no module named|pip install|isn'?t available in this session|"
                                                  r"importerror|dll load failed|needs? (?:node|a jdk|playwright|office)", re.I)),
    (ErrorCategory.AUTHENTICATION, re.compile(r"\b(?:401|403)\b|token|sign(?:ed)? in|log ?in|not connected|authori[sz]|"
                                              r"credential|oauth|api key|expired", re.I)),
    (ErrorCategory.NETWORK, re.compile(r"timed? ?out|timeout|connection|unreachable|offline|network|dns|http 5\d\d|rate.?limit|"
                                       r"\b429\b|couldn'?t reach", re.I)),
    (ErrorCategory.APP_UNAVAILABLE, re.compile(r"couldn'?t find (?:an )?(?:app|application)|not running|isn'?t open|"
                                               r"no (?:\w+ )?window|not found on this pc|isn'?t installed", re.I)),
    (ErrorCategory.PERMISSION_DENIED, re.compile(r"outside the folders|not allowed|blocked|permission|access (?:is )?denied|"
                                                 r"protected", re.I)),
    (ErrorCategory.AMBIGUOUS_TARGET, re.compile(r"which one|several|more than one|ambiguous|multiple (?:matches|files|chats)", re.I)),
    (ErrorCategory.INVALID_INPUT, re.compile(r"missing required|unexpected argument|must be|invalid|doesn'?t look like|"
                                             r"too long|not a valid", re.I)),
]


def categorize(message: str, error: str = "", *, crashed: bool = False, verified: Optional[bool] = None,
               risk: RiskLevel = RiskLevel.LOW, validation: bool = False) -> ErrorCategory:
    if validation:
        return ErrorCategory.INVALID_INPUT
    if crashed and risk >= RiskLevel.MEDIUM:
        return ErrorCategory.UNKNOWN_OUTCOME  # it may have half-happened: never retry blindly
    if verified is False:
        return ErrorCategory.VERIFICATION_FAILED
    text = f"{message} {error}"
    for cat, rx in _PATTERNS:
        if rx.search(text):
            return cat
    return ErrorCategory.TOOL_FAILURE


ADVICE = {
    ErrorCategory.UNKNOWN_OUTCOME: "I can't tell whether it went through, so I won't try again automatically — please check first.",
    ErrorCategory.VERIFICATION_FAILED: "Please check it yourself before I try anything else.",
}


def annotate(result, *, crashed: bool = False, risk: RiskLevel = RiskLevel.LOW) -> None:
    """Add ``error_category`` to a failed ToolResult (and advice for uncertain outcomes)."""
    if result is None or result.success:
        return
    cat = categorize(result.message or "", result.error or "", crashed=crashed, verified=result.verified, risk=risk)
    result.data["error_category"] = cat.value
    if cat in ADVICE and ADVICE[cat] not in (result.message or ""):
        result.hint = (result.hint + " " if result.hint else "") + ADVICE[cat]
