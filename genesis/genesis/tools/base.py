"""Tool abstraction.

Every real-world action Genesis can take is a :class:`Tool` with:

* ``name`` / ``description``         – shown to the planner LLM and in the UI
* ``parameters``                      – JSON schema (validated before execution)
* ``risk``                            – LOW / MEDIUM / HIGH (see security.permissions)
* ``always_confirm``                  – forces confirmation regardless of trust
* ``run(args, ctx)``                  – performs the action, returns ToolResult
* ``verify(args, result, ctx)``       – independent post-condition check
* ``confirmation_prompt(args)``       – human-readable description for the dialog
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from ..security.permissions import RiskLevel
from ..security.validators import ValidationError

if TYPE_CHECKING:  # pragma: no cover
    from ..core.container import ServiceContainer


@dataclass
class ToolResult:
    success: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    verified: Optional[bool] = None  # None = could not be independently verified
    untrusted_content: bool = False  # result contains external content (web, e-mail, files)
    recovery: Optional[dict] = None  # {"tool": ..., "args": ..., "reason": ...}
    hint: Optional[str] = None  # what the user can do to fix a failure

    @classmethod
    def ok(cls, message: str, **data: Any) -> "ToolResult":
        return cls(True, message, data)

    @classmethod
    def fail(cls, message: str, error: Optional[str] = None, hint: Optional[str] = None, **data: Any) -> "ToolResult":
        return cls(False, message, data, error=error or message, hint=hint)

    def to_dict(self) -> dict:
        return {
            "success": self.success, "message": self.message, "data": _jsonable(self.data), "error": self.error,
            "verified": self.verified, "untrusted_content": self.untrusted_content, "hint": self.hint,
        }


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


class Tool(ABC):
    name: str = ""
    description: str = ""
    category: str = "general"
    parameters: dict = {"type": "object", "properties": {}, "required": []}
    risk: RiskLevel = RiskLevel.LOW
    always_confirm: bool = False
    remote: bool = False  # sends data off the machine (privacy page)

    def risk_for(self, args: dict, ctx: "Optional[ServiceContainer]" = None) -> RiskLevel:
        return self.risk

    def confirmation_prompt(self, args: dict) -> str:
        return f"Genesis wants to run '{self.name}'. Do you want me to continue?"

    def validate(self, args: dict) -> dict:
        return validate_schema(args or {}, self.parameters, self.name)

    @abstractmethod
    def run(self, args: dict, ctx: "ServiceContainer") -> ToolResult:
        ...

    def verify(self, args: dict, result: ToolResult, ctx: "ServiceContainer") -> Optional[bool]:
        return None

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters,
                "risk": self.risk.label, "always_confirm": self.always_confirm, "category": self.category}


# ---------------------------------------------------------------- validation
_TYPES = {
    "string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict,
}


def validate_schema(args: dict, schema: dict, tool_name: str = "tool") -> dict:
    """Minimal JSON-schema validator (type, required, enum, maxLength, items, additionalProperties)."""
    if not isinstance(args, dict):
        raise ValidationError(f"{tool_name}: arguments must be an object")
    props: dict = schema.get("properties", {})
    out: dict = {}
    for req in schema.get("required", []):
        if args.get(req) in (None, ""):
            raise ValidationError(f"{tool_name}: missing required argument '{req}'")
    if schema.get("additionalProperties", False) is False:
        extra = set(args) - set(props)
        if extra:
            raise ValidationError(f"{tool_name}: unexpected argument(s) {sorted(extra)}")
    for key, spec in props.items():
        if key not in args or args[key] is None:
            if "default" in spec:
                out[key] = spec["default"]
            continue
        val = args[key]
        typ = spec.get("type")
        if typ == "integer" and isinstance(val, str) and val.strip().lstrip("-").isdigit():
            val = int(val)
        if typ == "number" and isinstance(val, str):
            try:
                val = float(val)
            except ValueError:
                pass
        if typ == "boolean" and isinstance(val, str):
            val = val.strip().lower() in {"true", "1", "yes"}
        if typ == "array" and isinstance(val, str):
            val = [val]
        if typ and not isinstance(val, _TYPES[typ]) or (typ in {"integer", "number"} and isinstance(val, bool)):
            raise ValidationError(f"{tool_name}: '{key}' must be of type {typ}")
        if "enum" in spec and val not in spec["enum"]:
            raise ValidationError(f"{tool_name}: '{key}' must be one of {spec['enum']}")
        if typ == "string" and len(val) > spec.get("maxLength", 20_000):
            raise ValidationError(f"{tool_name}: '{key}' is too long")
        if typ == "integer" or typ == "number":
            if "minimum" in spec and val < spec["minimum"]:
                raise ValidationError(f"{tool_name}: '{key}' must be >= {spec['minimum']}")
            if "maximum" in spec and val > spec["maximum"]:
                raise ValidationError(f"{tool_name}: '{key}' must be <= {spec['maximum']}")
        if typ == "array" and "items" in spec:
            item_t = spec["items"].get("type")
            if item_t and not all(isinstance(v, _TYPES[item_t]) for v in val):
                raise ValidationError(f"{tool_name}: '{key}' items must be {item_t}")
            if len(val) > spec.get("maxItems", 100):
                raise ValidationError(f"{tool_name}: '{key}' has too many items")
        out[key] = val
    return out


def obj(properties: dict, required: Optional[list[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


def s(desc: str, **kw: Any) -> dict:
    return {"type": "string", "description": desc, **kw}
