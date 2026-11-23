"""Tool registry — the only way the agent can reach the outside world."""
from __future__ import annotations

from typing import Iterable, Optional

from .base import Tool


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for t in tools:
            self.register(t)

    def register(self, tool: Tool) -> None:
        if not tool.name or not tool.name.replace("_", "").isalnum():
            raise ValueError(f"invalid tool name: {tool.name!r}")
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def names(self) -> list[str]:
        return sorted(self._tools)

    def catalog_for_llm(self, exclude: Iterable[str] = ()) -> str:
        import json

        lines = []
        for t in sorted(self._tools.values(), key=lambda x: x.name):
            if t.name in exclude:
                continue
            props = t.parameters.get("properties", {})
            args = {k: v.get("type", "any") + ("*" if k in t.parameters.get("required", []) else "") for k, v in props.items()}
            lines.append(f"- {t.name}({json.dumps(args)}): {t.description} [risk={t.risk.label}]")
        return "\n".join(lines)


def build_default_registry() -> ToolRegistry:
    from . import apps, code, email_tools, files, knowledge_tools, memory_tools, reminders, settings_tools, system, web

    reg = ToolRegistry()
    for module in (apps, files, web, system, email_tools, reminders, code, memory_tools, knowledge_tools, settings_tools):
        for tool in module.TOOLS:
            reg.register(tool)
    return reg
