"""Risk levels and the permission manager.

Policy summary (enforced in code and covered by ``tests/test_security.py``):

* LOW    – runs directly (unless the user set the tool's policy to ``ask``).
* MEDIUM – asks for confirmation unless the user explicitly trusted the tool.
* HIGH   – ALWAYS asks for confirmation. Cannot be trusted away.
* ``deny`` policy – the tool is blocked entirely.
* Any action planned while untrusted external content (web pages, e-mails,
  documents) is in the context is escalated so it requires confirmation.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING, Optional

from ..core.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from ..core.settings import Settings
    from ..database.repositories import PermissionRepo, SecurityEventRepo

log = get_logger("security")


class RiskLevel(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3

    @property
    def label(self) -> str:
        return self.name.lower()


@dataclass
class PermissionDecision:
    allowed: bool
    needs_confirmation: bool
    risk: RiskLevel
    reason: str = ""


class PermissionManager:
    def __init__(self, repo: "PermissionRepo", settings: "Settings",
                 security_events: Optional["SecurityEventRepo"] = None) -> None:
        self.repo = repo
        self.settings = settings
        self.security_events = security_events

    def _log(self, event: str, **detail) -> None:
        log.info(event, extra={"data": detail})
        if self.security_events is not None:
            try:
                self.security_events.add(event, detail)
            except Exception:
                pass

    def trusted_tools(self) -> set[str]:
        return set(self.settings.get("security.trusted_tools", []) or [])

    def check(self, tool_name: str, risk: RiskLevel, *, always_confirm: bool = False,
              untrusted_context: bool = False) -> PermissionDecision:
        policy = self.repo.get(tool_name)
        if policy == "deny":
            self._log("permission_denied", tool=tool_name, reason="policy=deny")
            return PermissionDecision(False, False, risk, f"The tool '{tool_name}' is disabled in your permissions.")

        if risk >= RiskLevel.HIGH:
            decision = PermissionDecision(True, True, risk, "high-risk action")
        elif always_confirm:
            decision = PermissionDecision(True, True, risk, "this action always requires confirmation")
        elif risk == RiskLevel.MEDIUM:
            trusted = policy == "allow" or tool_name in self.trusted_tools()
            decision = PermissionDecision(True, not trusted, risk, "trusted by user" if trusted else "medium-risk action")
        else:
            decision = PermissionDecision(True, policy == "ask", risk, "low-risk action")

        if untrusted_context and not decision.needs_confirmation:
            decision = PermissionDecision(True, True, risk, "planned while reading untrusted external content")
            self._log("permission_escalated", tool=tool_name, reason="untrusted_context")
        return decision

    def set_policy(self, tool_name: str, policy: Optional[str], risk: RiskLevel) -> None:
        if policy == "allow" and risk >= RiskLevel.HIGH:
            raise PermissionError("High-risk tools can never be set to run without confirmation.")
        if policy is None:
            self.repo.clear(tool_name)
        else:
            if policy not in {"ask", "allow", "deny"}:
                raise ValueError("policy must be ask/allow/deny")
            self.repo.set(tool_name, policy)
        self._log("permission_policy_changed", tool=tool_name, policy=policy)
