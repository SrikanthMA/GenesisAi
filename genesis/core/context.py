"""Context engine: a compact, structured view of "what's going on" for the reasoning layer.

Instead of sending the whole conversation to the LLM every time, the planner/NLU/chat prompts get a short
snapshot built from the dialogue state:

    current app / website · current task (+ status) · previous command · entities in focus ·
    results currently visible (the last list) · latest correction · conversation state ·
    relevant preferences and standing instructions

Everything here is **temporary** (in-memory, per conversation). Persistent memory lives in
``memory/manager.py`` and ``memory/identity.py`` and is only added when it's relevant.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

MAX_ITEMS = 5


@dataclass
class ContextSnapshot:
    user: str = ""
    conversation_state: str = ""
    current_app: str = ""
    current_site: str = ""
    current_task: str = ""
    previous_command: str = ""
    focus: list[str] = field(default_factory=list)
    visible_results: list[str] = field(default_factory=list)
    latest_correction: str = ""
    preferences: list[str] = field(default_factory=list)
    instructions: list[str] = field(default_factory=list)
    recent_turns: list[str] = field(default_factory=list)

    def to_prompt(self) -> str:
        lines = []
        for label, value in (("User", self.user), ("Conversation", self.conversation_state), ("Current app", self.current_app),
                             ("Current website", self.current_site), ("Current task", self.current_task),
                             ("Previous command", self.previous_command), ("Latest correction", self.latest_correction)):
            if value:
                lines.append(f"{label}: {value}")
        if self.focus:
            lines.append("Things in focus (most recent first): " + "; ".join(self.focus))
        if self.visible_results:
            lines.append("Results on screen: " + "; ".join(f"{i + 1}) {r}" for i, r in enumerate(self.visible_results)))
        if self.preferences:
            lines.append("Preferences: " + "; ".join(self.preferences))
        if self.instructions:
            lines.append("Standing instructions from the user: " + "; ".join(self.instructions))
        if self.recent_turns:
            lines.append("Last turns:\n" + "\n".join(self.recent_turns))
        return "\n".join(lines) or "(no context yet)"


class ContextEngine:
    def __init__(self, services: Any, session: Any = None) -> None:
        self.svc = services
        self.session = session

    def snapshot(self, st, *, turns: int = 4) -> ContextSnapshot:
        snap = ContextSnapshot()
        idm = getattr(self.svc, "identity", None)
        if idm is not None:
            ident = idm.get()
            if ident.canonical:
                snap.user = f"{ident.canonical} (call them {ident.call_name})"
        if self.session is not None:
            snap.conversation_state = self.session.phase.value.lower().replace("_", " ")
        for f in st.focus[:8]:
            if f.kind == "app" and not snap.current_app:
                snap.current_app = str(f.value.get("key") or f.label)
            if f.kind in ("url", "media") and not snap.current_site:
                snap.current_site = str(f.value.get("url") or f.label)[:120]
        snap.focus = [f"{f.kind}: {_short(f.label or f.value.get('path') or f.value.get('url') or '')}" for f in st.focus[:MAX_ITEMS]]
        if st.last_list is not None:
            snap.visible_results = [f"{st.last_list.kind}: {_short(str(it.get('label') or it.get('title') or ''))}"
                                    for it in st.last_list.items[:MAX_ITEMS]]
        if st.current_task:
            snap.current_task = f"{st.current_task.get('request', '')} [{st.current_task.get('status', '')}]"
        if st.last_request is not None:
            snap.previous_command = st.last_request.text[:120]
        snap.latest_correction = (st.last_correction or "")[:120]
        mem = getattr(self.svc, "memory", None)
        if mem is not None and mem.enabled:
            prefs = self.svc.repos.preferences.all()
            snap.preferences = [f"{k} = {v}" for k, v in prefs.items() if k.startswith(("pref.", "coding."))][:8]
            snap.instructions = [f["content"] for f in self.svc.repos.memory.list(kind="instruction")][:8]
        snap.recent_turns = [f"{m['role']}: {_short(m['content'], 160)}" for m in list(st.history)[-turns:]]
        return snap


def _short(text: Optional[str], n: int = 70) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"
