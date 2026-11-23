"""Memory manager — transparent, user-controlled memory.

Layers:
    short-term   – the dialogue window kept by DialogueState (current conversation)
    session      – tasks performed since Genesis started (in memory, not persisted)
    conversation – persisted message history per conversation (SQLite)
    long-term    – facts, preferences and contacts the user EXPLICITLY asked to keep

Nothing is added to long-term memory silently: facts are stored only on an
explicit "remember ..." and contact e-mails only when the user supplies them
(and the auto-save setting is on, with a visible notice). Everything is
viewable and deletable from the Memory page; memory can be disabled.
"""
from __future__ import annotations

from typing import Optional

from ..core.settings import Settings
from ..database.repositories import Repositories


class MemoryManager:
    def __init__(self, repos: Repositories, settings: Settings) -> None:
        self.repos = repos
        self.settings = settings
        self.session_tasks: list[dict] = []

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("memory.enabled", True))

    # ------------------------------------------------------------- facts
    def remember(self, text: str, kind: str = "fact") -> Optional[int]:
        if not self.enabled:
            return None
        return self.repos.memory.add(text.strip(), kind=kind, source="user")

    def forget(self, text: str) -> int:
        return self.repos.memory.delete_matching(text.strip())

    def facts(self) -> list[dict]:
        return self.repos.memory.list()

    # ---------------------------------------------------------- contacts
    def save_contact(self, name: str, email: Optional[str] = None, alias: Optional[str] = None) -> Optional[int]:
        if not self.enabled:
            return None
        return self.repos.contacts.upsert(name, email=email, alias=alias)

    def find_contact(self, text: str) -> Optional[dict]:
        return self.repos.contacts.find(text)

    # ------------------------------------------------------- preferences
    def set_preference(self, key: str, value) -> None:
        if self.enabled:
            self.repos.preferences.set(key, value)

    def get_preference(self, key: str, default=None):
        return self.repos.preferences.get(key, default)

    # --------------------------------------------------------- context
    def context_for_prompt(self, limit: int = 15) -> str:
        if not self.enabled:
            return ""
        lines = [f"- {m['content']}" for m in self.repos.memory.list()[:limit]]
        for c in self.repos.contacts.list()[:limit]:
            alias = f" (their {c['alias']})" if c.get("alias") else ""
            lines.append(f"- Contact: {c['name']}{alias}" + (f" <{c['email']}>" if c.get("email") else ""))
        return "\n".join(lines)

    def record_task(self, request: str, status: str, summary: str) -> None:
        self.session_tasks.append({"request": request, "status": status, "summary": summary})
        self.session_tasks = self.session_tasks[-100:]

    def snapshot(self) -> dict:
        return {
            "enabled": self.enabled,
            "facts": self.repos.memory.list(),
            "contacts": self.repos.contacts.list(),
            "preferences": self.repos.preferences.all(),
            "projects": self.repos.projects.list(),
            "session_tasks": list(self.session_tasks),
        }

    def wipe_long_term(self) -> None:
        self.repos.memory.clear()
        self.repos.contacts.clear()
        self.repos.preferences.clear()
