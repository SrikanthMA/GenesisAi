"""Regenerate docs/TOOLS.md from the live tool registry:  python scripts/gen_tool_docs.py"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from genesis.tools.registry import build_default_registry  # noqa: E402

reg = build_default_registry()
lines = [
    "# Genesis tool reference",
    "",
    "_Generated from the code by `python scripts/gen_tool_docs.py` — do not edit by hand._",
    "",
    "Every action Genesis performs is one of these tools. The planner can only choose from this list, every call is",
    "validated against the JSON schema, and the permission manager applies the risk level before anything runs.",
    "",
    "| Risk | Meaning |",
    "|---|---|",
    "| **low** | runs directly (you can set it to *always ask*) |",
    "| **medium** | asks for confirmation unless you allow the tool on the Tools page |",
    "| **high** | **always** asks; can never be set to run silently |",
    "",
    "Tools marked *always confirm* ask every time regardless of trust settings. Tools marked *remote* send data off the PC.",
    "",
]
by_cat: dict[str, list] = {}
for t in reg.all():
    by_cat.setdefault(t.category, []).append(t)
for cat in sorted(by_cat):
    lines += [f"## {cat.capitalize()}", ""]
    for t in sorted(by_cat[cat], key=lambda x: x.name):
        flags = [t.risk.label]
        if t.always_confirm:
            flags.append("always confirm")
        if t.remote:
            flags.append("remote")
        lines += [f"### `{t.name}` — {', '.join(flags)}", "", t.description, ""]
        props = t.parameters.get("properties", {})
        if props:
            lines += ["| Argument | Type | Required | Notes |", "|---|---|---|---|"]
            for name, spec in props.items():
                req = "yes" if name in t.parameters.get("required", []) else ""
                notes = spec.get("description", "")
                if "enum" in spec:
                    notes += f" one of: {', '.join(map(str, spec['enum']))}"
                if "default" in spec:
                    notes += f" (default {spec['default']})"
                lines.append(f"| `{name}` | {spec.get('type', 'any')} | {req} | {notes.strip()} |")
            lines.append("")
        else:
            lines += ["_No arguments._", ""]
(ROOT / "docs" / "TOOLS.md").write_text("\n".join(lines), encoding="utf-8")
print(f"wrote docs/TOOLS.md ({len(reg.all())} tools)")
