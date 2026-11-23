"""Code Reviewer: deterministic static checks over the generated project (no model needed).

Looks for: hard-coded secrets, eval/exec, shell=True, SQL built with string formatting, pickle loads, debug servers left
on, wildcard CORS, duplicated function definitions across files, empty/stub bodies, and very long functions.
"""
from __future__ import annotations

import ast
import re
from collections import defaultdict

SEC = [
    (r"""(?i)(api[_-]?key|secret|password|token)\s*=\s*['"][A-Za-z0-9_\-]{12,}['"]""", "high", "possible hard-coded secret"),
    (r"\beval\(|\bexec\(", "high", "eval/exec on dynamic input"),
    (r"shell\s*=\s*True", "high", "subprocess with shell=True"),
    (r"""(?i)(execute|executemany)\(\s*f?['"].*(\{|%s|\+\s*\w+)""", "high", "SQL built with string formatting (use parameters)"),
    (r"pickle\.loads?\(", "medium", "pickle on possibly untrusted data"),
    (r"debug\s*=\s*True", "medium", "debug mode enabled"),
    (r"""allow_origins\s*=\s*\[\s*['"]\*['"]""", "medium", "CORS allows every origin"),
    (r"verify\s*=\s*False", "medium", "TLS verification disabled"),
    (r"dangerouslySetInnerHTML|innerHTML\s*=", "medium", "raw HTML injection (XSS risk)"),
]


def review(files: dict[str, str]) -> list[dict]:
    issues: list[dict] = []
    defs: dict[str, list[str]] = defaultdict(list)
    for path, text in files.items():
        if path.endswith(".md") or "/tests/" in "/" + path or path.startswith("test"):
            pass
        else:
            for rx, level, msg in SEC:
                for m in re.finditer(rx, text):
                    line = text.count("\n", 0, m.start()) + 1
                    issues.append({"file": path, "line": line, "level": level, "issue": msg})
        if path.endswith(".py"):
            try:
                tree = ast.parse(text)
            except SyntaxError as exc:
                issues.append({"file": path, "line": exc.lineno or 0, "level": "high", "issue": f"syntax error: {exc.msg}"})
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not node.name.startswith("_") and not path.startswith("tests"):
                        defs[node.name].append(path)
                    body = [b for b in node.body if not (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant))]
                    if (not body or all(isinstance(b, (ast.Pass, ast.Raise)) for b in body)) and not node.name.startswith("__"):
                        stub = any(isinstance(b, ast.Raise) for b in node.body) and any(
                            "NotImplemented" in ast.dump(b) for b in node.body)
                        if stub or not body:
                            issues.append({"file": path, "line": node.lineno, "level": "medium",
                                           "issue": f"{node.name}() is an empty stub"})
                    length = (getattr(node, "end_lineno", node.lineno) or node.lineno) - node.lineno
                    if length > 80:
                        issues.append({"file": path, "line": node.lineno, "level": "low",
                                       "issue": f"{node.name}() is {length} lines long: consider splitting it"})
    for name, where in defs.items():
        where = sorted(set(where))
        if len(where) > 1 and name not in ("main", "get", "post", "put", "delete", "run", "setup", "create", "list", "read"):
            issues.append({"file": where[0], "line": 0, "level": "low", "issue": f"{name}() is defined in {len(where)} files: "
                           + ", ".join(where)})
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(issues, key=lambda i: (order[i["level"]], i["file"], i["line"]))
