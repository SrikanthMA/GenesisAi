"""Audit every registered tool against the tool contract (run: python -m eval.tool_audit)."""
from __future__ import annotations

import inspect
import json
import re
import sys
from collections import Counter

from genesis.tools.base import Tool
from genesis.tools.registry import build_default_registry

# Side effects that must never be repeated automatically (sending, creating, deleting, submitting, purchasing …)
CONSEQUENTIAL = re.compile(r"(send|submit|delete|create|write|apply|move|rename|copy|restore|shutdown|restart|sleep|"
                           r"run_program|run_project|run_tests|install|post|reply|update_calendar|routine|forget)", re.I)


def audit() -> dict:
    reg = build_default_registry()
    rows = []
    for t in reg.all():
        src = inspect.getsource(type(t))
        rows.append({
            "name": t.name, "category": t.category, "risk": t.risk.label, "always_confirm": t.always_confirm,
            "dynamic_risk": type(t).risk_for is not Tool.risk_for,
            "verifies": type(t).verify is not Tool.verify,
            "custom_prompt": type(t).confirmation_prompt is not Tool.confirmation_prompt,
            "remote": t.remote,
            "has_schema": bool(t.parameters.get("properties")) or t.parameters.get("type") == "object",
            "closed_schema": t.parameters.get("additionalProperties", False) is False,
            "described": len(t.description) >= 15,
            "may_mark_retryable": "retryable=True" in src or "retryable = True" in src,
            "consequential": bool(CONSEQUENTIAL.search(t.name)),
        })
    names = [r["name"] for r in rows]
    return {
        "tools": len(rows),
        "unique_names": len(set(names)) == len(names),
        "by_risk": dict(Counter(r["risk"] for r in rows)),
        "without_verify": [r["name"] for r in rows if not r["verifies"]],
        "consequential_low_risk": [r["name"] for r in rows if r["consequential"] and r["risk"] == "low" and not r["always_confirm"]],
        "consequential_retryable": [r["name"] for r in rows if r["consequential"] and r["may_mark_retryable"]],
        "open_schema": [r["name"] for r in rows if not r["closed_schema"]],
        "short_description": [r["name"] for r in rows if not r["described"]],
        "rows": rows,
    }


if __name__ == "__main__":
    out = audit()
    if "--json" in sys.argv:
        print(json.dumps(out, indent=1))
    else:
        for k, v in out.items():
            if k != "rows":
                print(f"{k}: {v if not isinstance(v, list) else f'{len(v)} → {v}'}")
