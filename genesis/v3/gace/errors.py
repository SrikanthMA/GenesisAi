"""Error analysis: classify a failure, find the files that matter, and say what to try - without a model.

Pure functions, so the repair loop's first decisions are deterministic and testable; the model only writes the fix.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# import name -> pip package (only where they differ)
PIP_NAMES = {"yaml": "pyyaml", "cv2": "opencv-python", "PIL": "pillow", "sklearn": "scikit-learn", "bs4": "beautifulsoup4",
             "dotenv": "python-dotenv", "jose": "python-jose", "jwt": "pyjwt", "multipart": "python-multipart",
             "dateutil": "python-dateutil", "serial": "pyserial"}

RULES: list[tuple[str, str, str]] = [
    # (category, regex, advice)
    ("frontend", r"Module build failed|Failed to compile|SyntaxError: Unexpected token|Unexpected token|"
                 r"Objects are not valid as a React child|Invalid hook call|is not defined\s+no-undef|ReferenceError",
     "fix the JSX/JavaScript error at the reported file"),
    ("syntax", r"SyntaxError|IndentationError|TabError", "fix the syntax at the reported line"),
    ("dependency", r"ModuleNotFoundError: No module named ['\"]([\w\.\-]+)|Cannot find module ['\"]([^'\"]+)|"
                   r"ERESOLVE|No matching distribution|Could not find a version", "install or pin the missing package"),
    ("import", r"ImportError|cannot import name", "fix the import name or the module it comes from"),
    ("name", r"NameError", "define or import the missing name"),
    ("attribute", r"AttributeError", "the object has no such attribute: check the type and the spelling"),
    ("type", r"TypeError", "wrong argument types or count"),
    ("database", r"sqlite3\.\w*Error|OperationalError|IntegrityError|no such table|UNIQUE constraint|SQLAlchemyError",
     "check the schema, table creation and queries"),
    ("port", r"address already in use|EADDRINUSE|WinError 10048|Errno 98", "the port is taken: use another port"),
    ("api", r"HTTPException|ConnectionError|HTTP(?:Error)? 4\d\d|HTTP(?:Error)? 5\d\d|status code (?:4|5)\d\d|"
            r"AssertionError: assert [45]\d\d", "an endpoint returned an error: check the route and its handler"),
    ("config", r"KeyError: ['\"][A-Z_]{3,}|environment variable|\.env|ValidationError.*(?:settings|config)", "a setting is missing"),
    ("assertion", r"AssertionError|assert ", "a test expectation failed: compare expected and actual behaviour"),
    ("runtime", r"RuntimeError|ValueError|KeyError|IndexError|ZeroDivisionError|FileNotFoundError|Exception", "runtime failure"),
]
PY_FRAME = re.compile(r'File "([^"]+)", line (\d+), in ([^\n]+)')
JS_FRAME = re.compile(r"(?:at .*?\(|\bin\s+|^|\s)((?:\./|\.\./)?[\w@/\.\-\\:]+\.(?:js|jsx|ts|tsx|mjs|java|cpp|cc|c|h)):(\d+)(?::\d+)?", re.M)
JAVA_ERR = re.compile(r"([\w/\\\.\-]+\.java):(\d+): error: ([^\n]+)")


@dataclass
class Diagnosis:
    category: str = "unknown"
    summary: str = ""
    advice: str = ""
    files: list[dict] = field(default_factory=list)   # [{"path", "line"}] most relevant first
    missing_package: Optional[str] = None
    signature: str = ""                               # same error -> same signature (for loop prevention)
    tail: str = ""

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("category", "summary", "advice", "files", "missing_package", "signature")}


def _norm(path: str, root: str) -> Optional[str]:
    p = path.replace("\\", "/")
    r = root.replace("\\", "/").rstrip("/") + "/"
    if p.startswith(r):
        p = p[len(r):]
    if re.match(r"^([A-Za-z]:)?/", p) or "site-packages" in p or "node_modules" in p or "/lib/python" in p.lower():
        return None
    while p.startswith("./"):
        p = p[2:]
    return p


def diagnose(output: str, root: str = "") -> Diagnosis:
    out = output or ""
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    d = Diagnosis(tail="\n".join(lines[-25:]))
    for cat, rx, advice in RULES:
        m = re.search(rx, out)
        if m:
            d.category, d.advice = cat, advice
            if cat == "dependency":
                name = next((g for g in m.groups() if g), None) if m.groups() else None
                if name and not name.startswith("."):
                    top = name.split(".")[0]
                    d.missing_package = PIP_NAMES.get(top, top)
            break
    # summary = the last line that names an error
    for pattern in (r"\w+(?:Error|Exception)\b.*:|^\s*E\s+\w|error:", r"Error|Exception|failed|FAILED|Cannot|not found"):
        hit = next((ln for ln in reversed(lines) if re.search(pattern, ln)), None)
        if hit:
            d.summary = hit.strip()[:240]
            break
    d.summary = d.summary or (lines[-1][:240] if lines else "no output")
    seen: set[str] = set()
    frames: list[dict] = []
    for m in PY_FRAME.finditer(out):
        rel = _norm(m.group(1), root)
        if rel and (rel, m.group(2)) not in seen:
            seen.add((rel, m.group(2)))
            frames.append({"path": rel, "line": int(m.group(2))})
    for m in JAVA_ERR.finditer(out):
        rel = _norm(m.group(1), root)
        if rel:
            frames.append({"path": rel, "line": int(m.group(2))})
    for m in JS_FRAME.finditer(out):
        rel = _norm(m.group(1), root)
        if rel and not any(f["path"] == rel for f in frames):
            frames.append({"path": rel, "line": int(m.group(2))})
    # the innermost project frame is usually the fault, but a failing test frame points at test code: prefer non-test
    non_test = [f for f in frames if not re.search(r"(^|/)(tests?/|test_|.*\.test\.)", f["path"])]
    d.files = list(reversed(non_test)) + [f for f in reversed(frames) if f not in non_test] if frames else []
    sig_src = f"{d.category}|{d.summary[:120]}|{d.files[0]['path'] if d.files else ''}"
    d.signature = re.sub(r"\d+", "N", sig_src)
    return d


def tests_collected(output: str) -> int:
    """How many tests actually ran (so '0 tests' is never reported as a pass)."""
    best = 0
    for rx in (r"(\d+) passed", r"Ran (\d+) tests?", r"Tests:\s+(?:\d+ failed, )?(\d+) passed", r"(\d+) tests? (?:completed|passed)"):
        for m in re.finditer(rx, output or ""):
            best = max(best, int(m.group(1)))
    return best
