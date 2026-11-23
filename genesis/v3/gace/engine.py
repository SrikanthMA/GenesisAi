"""The GACE engine: UNDERSTAND -> PLAN -> BUILD -> SETUP -> TEST -> DIAGNOSE -> REPAIR -> VERIFY -> REPORT.

Roles (each has one job and a measurable output; the orchestrator is this class, agents never talk to each other freely):
  Architect  - request -> validated project plan (files, dependencies, commands)
  Coder      - one file at a time, with only the interfaces of the files it needs (selective context)
  Terminal   - validated commands only (runner.py)
  Debugger   - error analysis (errors.py) + one targeted fix per attempt
  QA         - runs the project's tests; "verified" only if tests really ran and passed
  Reviewer   - static review (review.py)
The loop is bounded: a global repair limit, a per-error limit, a time limit, and a fix that doesn't improve the test
result is undone and counted as an unnecessary retry.
"""
from __future__ import annotations

import ast
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import errors, review, runner
from .memory import ProjectMemory
from .workspace import Workspace, WorkspaceError

MAX_FILES = 40
CODE_EXTS = {".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".cpp", ".cc", ".c", ".h", ".hpp", ".html", ".css", ".sql", ".json",
             ".md", ".txt", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".go", ".rs", ".cs", ".xml", ".env.example", ".gitignore"}

ARCHITECT_SYSTEM = (
    "You are a senior software architect. Return only JSON (a JSON object, nothing else) with keys: "
    "name (short slug), summary, stack (list), requirements (list of short strings), "
    "files (list of {path, purpose} in build order: config first, then core code, then tests last), "
    "pip (list of pip package names the project needs, [] if none), npm (list of npm packages, [] if none), "
    "test_command (argv list such as [\"python\",\"-m\",\"pytest\",\"-q\"]), "
    "run_command (argv list or []), questions (list of {question, blocking}) only for information that is truly missing. "
    "Use relative paths, at most 30 files, include real automated tests, never include secrets or .env files."
)
CODER_SYSTEM = ("You are a careful software engineer. Write the complete content of exactly one file. Output only the file "
                "inside a single fenced code block. No explanations. The code must run as written: no placeholders, no TODO "
                "stubs, correct imports, consistent with the interfaces you are given.")
FIXER_SYSTEM = ("You are a debugging engineer. You get a failing test or error output and one file. Fix the root cause with "
                "the smallest change. Output the complete corrected file inside one fenced code block, nothing else. Do not "
                "weaken or delete tests to make them pass, and do not add shell commands, network access or file deletion.")


@dataclass
class Limits:
    max_repairs: int = 8
    per_error: int = 2
    max_seconds: int = 1500
    max_files: int = MAX_FILES
    cmd_timeout: int = 300


@dataclass
class Result:
    status: str = "new"          # verified | partial | not_verified | needs_info | cancelled | failed
    summary: str = ""
    workspace: str = ""
    spec: dict = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    failed_files: list[str] = field(default_factory=list)
    commands: list[dict] = field(default_factory=list)
    repairs: list[dict] = field(default_factory=list)
    review: list[dict] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    report: str = ""
    tests_collected: int = 0

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def slug(text: str, default: str = "project") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40]
    return s or default


def code_from(answer: str) -> str:
    """The code in a model answer: the longest fenced block, else the whole answer if it isn't prose."""
    blocks = re.findall(r"```[\w+\-#.]*\n(.*?)```", answer or "", re.S)
    if blocks:
        return max(blocks, key=len).rstrip() + "\n"
    t = (answer or "").strip()
    return (t + "\n") if t and not t.lower().startswith(("sure", "here", "i ", "certainly")) else ""


def interface_of(rel: str, text: str) -> str:
    """A short summary of a file for other files' prompts (signatures only, not the whole code)."""
    if rel.endswith(".py"):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return f"# {rel}"
        out = [f"# {rel}"]
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append(f"def {n.name}({', '.join(a.arg for a in n.args.args)})")
            elif isinstance(n, ast.ClassDef):
                out.append(f"class {n.name}: " + ", ".join(m.name for m in n.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))))
            elif isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id.isupper():
                out.append(f"{n.targets[0].id} = ...")
        return "\n".join(out[:40])
    if rel.endswith((".js", ".jsx", ".ts", ".tsx")):
        lines = [ln for ln in text.splitlines() if re.match(r"\s*(export|import|function|const \w+ = \(|class)\b", ln)]
        return f"// {rel}\n" + "\n".join(lines[:25])
    return f"# {rel}: " + " ".join(text.split())[:160]


def validate_content(rel: str, text: str) -> Optional[str]:
    """None if the file looks valid, else the reason."""
    if not text.strip():
        return "the file is empty"
    if rel.endswith(".py"):
        try:
            ast.parse(text)
        except SyntaxError as exc:
            return f"SyntaxError line {exc.lineno}: {exc.msg}"
    if rel.endswith(".json"):
        try:
            json.loads(text)
        except ValueError as exc:
            return f"invalid JSON: {exc}"
    if re.search(r"(?m)^\s*(\.\.\.|# ?TODO|pass\s*# ?implement)", text) and rel.endswith(".py") and len(text) < 200:
        return "it is only a placeholder"
    return None


def validate_spec(spec, ws: Workspace, limits: Limits) -> tuple[dict, list[str]]:
    """Check the architect's plan. Returns (clean spec, problems). Problems make the plan unusable."""
    probs: list[str] = []
    if not isinstance(spec, dict):
        return {}, ["the plan wasn't a JSON object"]
    out = {"name": slug(str(spec.get("name") or "")), "summary": str(spec.get("summary") or "")[:500],
           "stack": [str(x)[:40] for x in (spec.get("stack") or [])][:10],
           "requirements": [str(x)[:200] for x in (spec.get("requirements") or [])][:30], "files": [], "pip": [], "npm": [],
           "test_command": [], "run_command": [], "questions": []}
    seen = set()
    for f in spec.get("files") or []:
        path = (f.get("path") if isinstance(f, dict) else str(f)) or ""
        path = path.replace("\\", "/").strip()
        while path.startswith("./"):
            path = path[2:]
        try:
            ws.resolve(path)
        except WorkspaceError as exc:
            probs.append(str(exc))
            continue
        ext = ("." + path.rsplit(".", 1)[-1].lower()) if "." in path.rsplit("/", 1)[-1] else ""
        if path.endswith(".env.example"):
            ext = ".env.example"
        if ext not in CODE_EXTS and path.rsplit("/", 1)[-1] not in (".gitignore", "Dockerfile", "Makefile"):
            probs.append(f"{path}: file type not allowed")
            continue
        if path in seen or path in ("requirements.txt", "README.md"):
            continue  # those two are written by Genesis itself from the plan
        seen.add(path)
        out["files"].append({"path": path, "purpose": str((f.get("purpose") if isinstance(f, dict) else "") or "")[:200]})
    if len(out["files"]) > limits.max_files:
        probs.append(f"the plan has {len(out['files'])} files; the limit is {limits.max_files}")
    if not out["files"]:
        probs.append("the plan lists no files")
    for p in spec.get("pip") or []:
        p = str(p).strip()
        if runner.PKG.match(p):
            out["pip"].append(p)
        else:
            probs.append(f"pip package “{p}” rejected (not a plain package name)")
    for p in spec.get("npm") or []:
        p = str(p).strip()
        if runner.NPM_PKG.match(p):
            out["npm"].append(p)
        else:
            probs.append(f"npm package “{p}” rejected")
    for key in ("test_command", "run_command"):
        cmd = spec.get(key) or []
        if isinstance(cmd, str):
            cmd = cmd.split()
        cmd = [str(c) for c in cmd]
        if cmd:
            try:
                runner.validate(cmd, ws.root)
                out[key] = cmd
            except runner.CommandRefused as exc:
                probs.append(f"{key} refused: {exc}")
    for q in spec.get("questions") or []:
        if isinstance(q, dict) and q.get("blocking") and q.get("question"):
            out["questions"].append(str(q["question"])[:300])
    return out, probs


class Engine:
    def __init__(self, ws: Workspace, ask: Callable[..., str], confirm: Callable[[str], bool],
                 on_event: Optional[Callable[[dict], None]] = None, limits: Optional[Limits] = None,
                 cancelled: Optional[Callable[[], bool]] = None) -> None:
        self.ws, self._ask, self.confirm = ws, ask, confirm
        self.on_event = on_event
        self.limits = limits or Limits()
        self.cancelled = cancelled or (lambda: False)
        self.mem = ProjectMemory(ws)
        self.res = Result(workspace=str(ws.root))
        self.t0 = time.time()
        self.model_calls = 0
        self.installed_ok = False
        self.tried: dict[str, list[str]] = {}

    # ------------------------------------------------------------------ plumbing
    def ev(self, stage: str, text: str, kind: str = "info", **data) -> None:
        e = {"t": round(time.time() - self.t0, 1), "stage": stage, "kind": kind, "text": text, **data}
        self.res.events.append(e)
        if self.on_event:
            try:
                self.on_event(e)
            except Exception:
                pass

    def model(self, prompt: str, system: str, tokens: int, purpose: str) -> str:
        self.model_calls += 1
        return self._ask(prompt, system=system, max_tokens=tokens, purpose=purpose)

    def out_of_time(self) -> bool:
        return self.cancelled() or time.time() - self.t0 > self.limits.max_seconds

    def exec(self, argv: list[str], stage: str, timeout: Optional[int] = None) -> runner.Result:
        py = runner.venv_python(self.ws.root)
        if py and Path(argv[0]).name.lower().removesuffix(".exe") in ("python", "py", "python3", "pytest", "pip"):
            argv = ([str(py)] + argv[1:]) if argv[0] != "pytest" else [str(py), "-m", "pytest"] + argv[1:]
            argv = [a for a in argv]
        self.ev(stage, "$ " + " ".join(Path(a).name if i == 0 else a for i, a in enumerate(argv)), "command")
        r = runner.run(argv, self.ws.root, timeout or self.limits.cmd_timeout)
        self.res.commands.append({"cmd": " ".join(argv[1:4]) if len(argv) > 1 else argv[0], "exe": Path(argv[0]).name,
                                  "code": r.code, "seconds": round(r.seconds, 1), "timed_out": r.timed_out})
        return r

    # ------------------------------------------------------------------ PLAN
    def plan(self, request: str) -> bool:
        self.ev("UNDERSTAND", "reading the request")
        mem = self.mem.data
        hint = ""
        if mem.get("known_errors"):
            hint = "\nKnown problems from earlier work on this folder: " + "; ".join(e["summary"] for e in mem["known_errors"][-3:])
        for attempt in range(2):
            try:
                ans = self.model(f"Project request:\n{request}\n{hint}", ARCHITECT_SYSTEM, 1800, "gace-plan")
            except Exception as exc:
                self.res.status, self.res.summary = "failed", f"No AI model answered the planning step ({exc})."
                return False
            raw = _json(ans)
            spec, probs = validate_spec(raw, self.ws, self.limits)
            if spec.get("files") and not any("limit" in p for p in probs):
                break
            self.ev("PLAN", "plan rejected: " + "; ".join(probs[:3]), "error")
            request += "\n\nYour previous plan was rejected: " + "; ".join(probs[:4]) + ". Return a corrected JSON plan."
        else:
            self.res.status, self.res.summary = "failed", "I couldn't get a usable project plan from the model."
            return False
        if spec["questions"]:
            self.res.status, self.res.questions, self.res.spec = "needs_info", spec["questions"], spec
            self.res.summary = "I need a little more information before I start."
            return False
        self._ensure_tests_and_commands(spec)
        self.res.spec = self.mem.data["spec"] = spec
        self.mem.decide(f"stack: {', '.join(spec['stack'])}")
        self.ev("PLAN", f"{len(spec['files'])} files planned: " + ", ".join(f['path'] for f in spec["files"][:8]), "plan")
        return True

    def _ensure_tests_and_commands(self, spec: dict) -> None:
        paths = [f["path"] for f in spec["files"]]
        py = any(p.endswith(".py") for p in paths)
        if py and not spec["test_command"]:
            spec["test_command"] = ["python", "-m", "pytest", "-q"] if "pytest" in " ".join(spec["pip"]).lower() else \
                ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]
        if py and "pytest" in " ".join(spec["test_command"]) and not any(p.lower().startswith("pytest") for p in spec["pip"]):
            spec["pip"].append("pytest")
        if py and not any(re.search(r"(^|/)(test_[^/]*|[^/]*_test)\.py$", p) for p in paths):
            spec["files"].append({"path": "tests/test_app.py", "purpose": "automated tests for the main behaviour"})

    # ------------------------------------------------------------------ BUILD
    def build_files(self, only: Optional[set[str]] = None) -> None:
        spec = self.res.spec
        done: dict[str, str] = {}
        for f in spec["files"]:
            if (self.ws.root / f["path"]).exists():
                done[f["path"]] = self.ws.read(f["path"])
        for f in spec["files"]:
            rel = f["path"]
            if only is not None and rel not in only:
                continue
            if self.out_of_time():
                self.res.failed_files.append(rel)
                continue
            ctx_parts = [interface_of(p, t) for p, t in done.items() if p != rel][-12:]
            prompt = (f"Project: {spec['summary']}\nStack: {', '.join(spec['stack'])}\nRequirements:\n- " +
                      "\n- ".join(spec["requirements"][:15]) + f"\n\nAll files: {', '.join(x['path'] for x in spec['files'])}\n"
                      f"Interfaces of files already written:\n" + ("\n".join(ctx_parts) or "(none yet)") +
                      f"\n\nWrite the complete file `{rel}`. Purpose: {f['purpose']}")
            text, why = "", ""
            for attempt in range(2):
                try:
                    text = code_from(self.model(prompt + (f"\n\nYour last attempt was rejected: {why}" if why else ""),
                                                CODER_SYSTEM, 3200, "gace-code"))
                except Exception as exc:
                    why = f"the model failed ({exc})"
                    break
                why = validate_content(rel, text) or ""
                if not why:
                    break
            if why or not text:
                self.res.failed_files.append(rel)
                self.ev("BUILD", f"{rel}: not written ({why or 'no code returned'})", "error", path=rel)
                continue
            info = self.ws.write(rel, text, who="coder")
            self.mem.record_file(rel)
            done[rel] = text
            self.res.files.append(rel)
            self.ev("BUILD", f"{'created' if info['op'] == 'create' else 'updated'} {rel}", "file", path=rel)
        self._write_fixed_files()
        self.mem.save()

    def _write_fixed_files(self) -> None:
        spec = self.res.spec
        if spec["pip"]:
            self.ws.write("requirements.txt", "\n".join(sorted(set(spec["pip"]), key=str.lower)) + "\n", who="genesis")
            self.mem.record_file("requirements.txt")
        if not (self.ws.root / "README.md").exists():
            cmds = [" ".join(spec["run_command"])] if spec["run_command"] else []
            self.ws.write("README.md", f"# {spec['name']}\n\n{spec['summary']}\n\n## Requirements\n" +
                          "\n".join(f"- {r}" for r in spec["requirements"]) + "\n\n## Run\n" + ("\n".join(f"    {c}" for c in cmds) or
                          "See the files above.") + "\n\n## Test\n    " + " ".join(spec["test_command"] or ["(no tests)"]) +
                          "\n\n_Generated by Genesis (GACE). See GENESIS_BUILD_REPORT.md for what was and wasn't verified._\n",
                          who="genesis")
            self.mem.record_file("README.md")

    # ------------------------------------------------------------------ SETUP
    def setup(self) -> None:
        spec = self.res.spec
        need_py = bool(spec["pip"]) and (self.ws.root / "requirements.txt").exists()
        need_npm = (self.ws.root / "package.json").exists()
        if not (need_py or need_npm):
            return
        what = []
        if need_py:
            what.append("create a project-only Python environment and install: " + ", ".join(spec["pip"][:12]))
        if need_npm:
            what.append("run npm install" + (" (" + ", ".join(spec["npm"][:8]) + ")" if spec["npm"] else ""))
        if not self.confirm("I'll " + " and ".join(what) + ". This downloads packages from the internet. Go ahead?"):
            self.ev("SETUP", "installation skipped by you; tests may fail without the packages", "error")
            self.res.commands.append({"cmd": "install", "exe": "-", "code": None, "seconds": 0, "timed_out": False, "skipped": True})
            return
        if need_py:
            if not runner.venv_python(self.ws.root):
                r = self.exec(["python", "-m", "venv", ".venv"], "SETUP")
                if not r.ok:
                    self.ev("SETUP", "could not create the environment: " + r.output[-200:], "error")
                    return
            r = self.exec(["python", "-m", "pip", "install", "-r", "requirements.txt"], "SETUP", 900)
            self.installed_ok = r.ok
            if not r.ok:
                self.ev("SETUP", "pip install failed", "error", detail=r.output[-600:])
        if need_npm:
            r = self.exec(["npm", "install"], "SETUP", 900)
            if not r.ok:
                self.ev("SETUP", "npm install failed", "error", detail=r.output[-600:])

    # ------------------------------------------------------------------ TEST / DIAGNOSE / REPAIR
    def verify_commands(self) -> list[list[str]]:
        cmds = []
        if self.res.spec["test_command"]:
            cmds.append(self.res.spec["test_command"])
        pj = self.ws.root / "package.json"
        if pj.exists():
            try:
                scripts = (json.loads(pj.read_text(encoding="utf-8")).get("scripts") or {})
            except ValueError:
                scripts = {}
            if "build" in scripts:
                cmds.append(["npm", "run", "build"])
            if "test" in scripts and not any(c[:1] == ["npm"] for c in cmds):
                cmds.append(["npm", "test"])
        return cmds

    def run_checks(self) -> tuple[bool, str, int]:
        """(all passed, combined output, tests collected). 'Passed' needs exit 0 AND at least one test running."""
        outs, ok, collected = [], True, 0
        for rel in self.ws.files():
            if rel.endswith(".py"):
                try:
                    ast.parse(self.ws.read(rel))
                except SyntaxError as exc:
                    return False, f'  File "{rel}", line {exc.lineno}\nSyntaxError: {exc.msg}', 0
        cmds = self.verify_commands()
        if not cmds:
            return False, "No test command is defined for this project, so nothing can be verified.", 0
        for c in cmds:
            r = self.exec(c, "TEST", self.limits.cmd_timeout)
            outs.append(r.output)
            if not r.ok:
                ok = False
            elif c[:1] != ["npm"] or "test" in c:
                collected += errors.tests_collected(r.output)
        is_test = any("pytest" in " ".join(c) or "unittest" in " ".join(c) or "test" in c for c in cmds)
        if ok and is_test and collected == 0:
            ok = False
            outs.append("The test command succeeded but no tests were collected, so nothing was verified.")
        out = "\n".join(outs)
        self.ev("TEST", "all checks passed" if ok else "checks failed", "test", passed=ok, tests=collected)
        return ok, out, collected

    def repair_loop(self) -> tuple[bool, str, int]:
        from ..debugger import failure_count

        ok, out, collected = self.run_checks()
        attempts: dict[str, int] = {}
        n = 0
        while not ok and n < self.limits.max_repairs and not self.out_of_time():
            diag = errors.diagnose(out, str(self.ws.root))
            self.mem.note_error(diag.signature, diag.summary)
            self.ev("DIAGNOSE", f"{diag.category}: {diag.summary}", "error", files=diag.files[:3])
            attempts[diag.signature] = attempts.get(diag.signature, 0) + 1
            if attempts[diag.signature] > self.limits.per_error:
                self.ev("DIAGNOSE", "the same error keeps coming back; stopping so a person can look", "error")
                break
            n += 1
            before = failure_count(out, 1)
            if diag.category == "dependency" and diag.missing_package:
                if self._add_package(diag.missing_package):
                    new_ok, new_out, new_c = self.run_checks()
                    self.res.repairs.append({"n": n, "kind": "package", "file": "requirements.txt",
                                             "summary": f"added {diag.missing_package}", "result": "kept" if new_ok or failure_count(new_out, 1) <= before else "no change"})
                    ok, out, collected = new_ok, new_out, new_c
                    continue
            tried = self.tried.setdefault(diag.signature, [])
            target = next((f for f in self._targets(diag) if f["path"] not in tried), None)
            if target is None:
                self.ev("DIAGNOSE", "I couldn't tell which file is at fault (or I've tried them all)", "error")
                break
            tried.append(target["path"])
            rel = target["path"]
            old = self.ws.read(rel)
            cid = self.ws.checkpoint(f"pre-repair-{n}")
            prompt = (f"Error output:\n{diag.tail[-2500:]}\n\nCategory: {diag.category}. Hint: {diag.advice}.\n"
                      f"Fault reported at {rel}:{target['line']}.\n\nCurrent `{rel}`:\n```\n{old[:9000]}\n```\n"
                      + self._related(rel, diag) + "\nReturn the complete corrected file.")
            try:
                new = code_from(self.model(prompt, FIXER_SYSTEM, 3200, "gace-fix"))
            except Exception as exc:
                self.ev("REPAIR", f"the model failed ({exc})", "error")
                break
            bad = validate_content(rel, new) or self._unsafe(rel, old, new)
            if bad:
                self.res.repairs.append({"n": n, "kind": "fix", "file": rel, "summary": diag.summary[:100],
                                         "result": f"rejected: {bad}"})
                self.ev("REPAIR", f"{rel}: fix rejected ({bad})", "error")
                continue
            info = self.ws.write(rel, new, who="debugger")
            self.ev("REPAIR", f"patched {rel}", "fix", path=rel, diff=info["diff"][:3000])
            new_ok, new_out, new_c = self.run_checks()
            after = failure_count(new_out, 0 if new_ok else 1)
            if new_ok or after < before:
                self.res.repairs.append({"n": n, "kind": "fix", "file": rel, "summary": diag.summary[:100], "result": "kept",
                                         "diff": info["diff"][:3000], "checkpoint": cid})
                self.mem.record_file(rel)
                self.mem.note_solution(diag.signature, rel, diag.summary)
                ok, out, collected = new_ok, new_out, new_c
            else:
                self.ws.write(rel, old, who="debugger-undo")
                self.res.repairs.append({"n": n, "kind": "fix", "file": rel, "summary": diag.summary[:100],
                                         "result": "undone (tests did not improve)"})
                self.ev("REPAIR", f"{rel}: change undone, tests did not improve", "error")
        return ok, out, collected

    def _targets(self, diag) -> list[dict]:
        """Files to try, best first. A failing assertion usually points at a test, but the bug is in the code under test:
        so the modules the test imports are tried first, and the test file itself last."""
        out: list[dict] = []
        for f in diag.files:
            if not (self.ws.root / f["path"]).is_file():
                continue
            if re.search(r"(^|/)(tests?/|test_[^/]*$|[^/]*_test\.py$|[^/]*\.test\.)", f["path"]):
                if f["path"].endswith(".py"):
                    try:
                        tree = ast.parse(self.ws.read(f["path"]))
                    except SyntaxError:
                        tree = None
                    mods = []
                    for n in ast.walk(tree) if tree else []:
                        if isinstance(n, ast.ImportFrom) and n.module:
                            mods.append(n.module)
                        elif isinstance(n, ast.Import):
                            mods += [a.name for a in n.names]
                    for m in mods:
                        cand = m.replace(".", "/") + ".py"
                        if (self.ws.root / cand).is_file() and not any(x["path"] == cand for x in out):
                            out.append({"path": cand, "line": 0})
                out.append(f)
            elif not any(x["path"] == f["path"] for x in out):
                out.insert(len([x for x in out if not re.search(r"test", x["path"])]), f)
        # code files first, test files after
        out.sort(key=lambda x: bool(re.search(r"(^|/)(tests?/|test_)", x["path"])))
        return out

    def _related(self, rel: str, diag) -> str:
        """A little context: the failing test when the fault is in code, the code under test when the fault is a test."""
        parts = []
        for f in (diag.files[:3] + [{"path": p, "line": 0} for p in self._failing_tests(diag)]):
            if f["path"] != rel and (self.ws.root / f["path"]).is_file():
                parts.append(f"Related `{f['path']}` (line {f['line']}):\n```\n{self.ws.read(f['path'])[:3500]}\n```\n")
        return "\n".join(parts[:2])

    def _failing_tests(self, diag) -> list[str]:
        return [f["path"] for f in diag.files if re.search(r"(^|/)(tests?/|test_)", f["path"])][:1]

    def _unsafe(self, rel: str, old: str, new: str) -> str:
        if len(new) < 0.35 * len(old) and len(old) > 400:
            return "it deletes most of the file"
        if rel.startswith("tests") or "/test" in rel:
            if len(re.findall(r"\bdef test_|\bit\(|\btest\(", new)) < len(re.findall(r"\bdef test_|\bit\(|\btest\(", old)):
                return "it removes tests"
        before = {(i["issue"]) for i in review.review({rel: old}) if i["level"] == "high"}
        after = {(i["issue"]) for i in review.review({rel: new}) if i["level"] == "high"}
        if after - before:
            return "it adds a risky construct (" + "; ".join(sorted(after - before)) + ")"
        return ""

    def _add_package(self, pkg: str) -> bool:
        if not runner.PKG.match(pkg):
            return False
        planned = {p.lower().split("==")[0].split(">=")[0] for p in self.res.spec["pip"]}
        if pkg.lower() not in planned:
            if not self.confirm(f"The project needs the package “{pkg}”, which wasn't in the plan. Install it? "
                                "(Check the name is right: a wrong name could be a different, unwanted package.)"):
                return False
            self.res.spec["pip"].append(pkg)
        if not runner.venv_python(self.ws.root):
            return False
        self._write_fixed_files()
        req = self.ws.root / "requirements.txt"
        lines = set(req.read_text(encoding="utf-8").split()) if req.exists() else set()
        lines.add(pkg)
        self.ws.write("requirements.txt", "\n".join(sorted(lines, key=str.lower)) + "\n", who="debugger")
        r = self.exec(["python", "-m", "pip", "install", pkg], "REPAIR", 600)
        return r.ok

    # ------------------------------------------------------------------ whole run
    def build(self, request: str) -> Result:
        try:
            if not self.plan(request):
                return self._finish(request, planned=False)
            self.ws.checkpoint("before-build")
            self.ev("BUILD", "starting to write files")
            self.build_files()
            self.setup()
            ok, out, collected = self.repair_loop()
            self.res.tests_collected = collected
            self._conclude(ok, out)
        except WorkspaceError as exc:
            self.res.status, self.res.summary = "failed", str(exc)
        except runner.CommandRefused as exc:
            self.res.status, self.res.summary = "failed", f"A command was refused for safety: {exc}"
        return self._finish(request)

    def resume(self, request: str = "") -> Result:
        """Continue an unfinished project: write only the planned files that are missing, then test again.
        Memory is compared with the real files first; changed files are never overwritten."""
        spec = self.mem.data.get("spec")
        if not spec:
            return self.build(request or "Continue this project")
        audit = self.mem.audit()
        self.res.spec = spec
        self.ev("INSPECT", f"{len(audit['ok'])} files as recorded, {len(audit['changed'])} changed outside Genesis, "
                f"{len(audit['missing'])} missing", "info")
        missing = {f["path"] for f in spec["files"] if not (self.ws.root / f["path"]).exists()}
        try:
            self.ws.checkpoint("before-resume")
            if missing:
                self.build_files(only=missing)
            else:
                self._write_fixed_files()
            self.setup()
            ok, out, collected = self.repair_loop()
            self.res.tests_collected = collected
            self._conclude(ok, out)
        except (WorkspaceError, runner.CommandRefused) as exc:
            self.res.status, self.res.summary = "failed", str(exc)
        return self._finish(request or spec.get("summary", ""))

    def _conclude(self, ok: bool, out: str) -> None:
        files = {f: self.ws.read(f) for f in self.ws.files() if f.rsplit(".", 1)[-1] in ("py", "js", "jsx", "ts", "tsx", "html", "java")
                 and (self.ws.root / f).stat().st_size < 200_000}
        self.res.review = review.review(files)[:40]
        high = [i for i in self.res.review if i["level"] == "high"]
        if self.cancelled():
            self.res.status = "cancelled"
            self.res.summary = "Stopped on request. Files written so far are kept."
        elif ok and not self.res.failed_files:
            self.res.status = "verified"
            self.res.summary = f"Verified: the checks pass ({self.res.tests_collected} tests ran)."
        elif ok:
            self.res.status = "partial"
            self.res.summary = "The checks pass, but some planned files could not be written: " + ", ".join(self.res.failed_files[:5])
        else:
            self.res.status = "not_verified"
            why = errors.diagnose(out).summary if out else ""
            self.res.summary = ("Not verified: " + (why + ". " if why else "the tests still fail or couldn't run. ")
                                + "Details are in the build report.")
        if high:
            self.res.summary += f" {len(high)} security finding(s) need your attention."

    def _finish(self, request: str, planned: bool = True) -> Result:
        r = self.res
        kept = [x for x in r.repairs if x["result"] == "kept"]
        undone = [x for x in r.repairs if str(x["result"]).startswith(("undone", "rejected", "no change"))]
        r.metrics = {"seconds": round(time.time() - self.t0, 1), "model_calls": self.model_calls, "files_written": len(r.files),
                     "files_failed": len(r.failed_files), "repairs_tried": len(r.repairs), "repairs_kept": len(kept),
                     "unnecessary_retries": len(undone), "tests_collected": r.tests_collected, "verified": r.status == "verified",
                     "error_recovery": (len(kept) / len(r.repairs)) if r.repairs else None}
        if planned and r.spec:
            self.mem.data["status"] = r.status
            self.mem.data["history"].append({"ts": time.time(), "request": request[:300], "status": r.status, "metrics": r.metrics})
            del self.mem.data["history"][:-20]
            self.mem.save()
            r.report = self._report(request)
            try:
                self.ws.write("GENESIS_BUILD_REPORT.md", r.report, who="genesis")
            except WorkspaceError:
                pass
        return r

    def _report(self, request: str) -> str:
        r, m = self.res, self.res.metrics
        label = {"verified": "VERIFIED", "partial": "PARTIAL", "not_verified": "NOT VERIFIED", "cancelled": "STOPPED",
                 "failed": "FAILED"}.get(r.status, r.status.upper())
        L = [f"# Build report: {r.spec.get('name', '')}", "", f"**Status: {label}**  - {r.summary}", "", "## Request", request.strip()[:1500], ""]
        L += ["## What \"verified\" means here", "Verified = the project's own test command exited 0 **and** at least one test ran. "
              "Nothing else is claimed. The requirements below come from the plan; they are not each proven.", ""]
        L += ["## Requirements (from the plan)"] + [f"- {x}" for x in r.spec.get("requirements", [])] + [""]
        L += ["## Files", *(f"- {f}" for f in r.files)]
        if r.failed_files:
            L += ["", "Not written: " + ", ".join(r.failed_files)]
        L += ["", "## Commands run"] + [f"- `{c['exe']} {c['cmd']}` -> " + ("skipped" if c.get("skipped") else f"exit {c['code']} in {c['seconds']}s")
                                      for c in r.commands]
        L += ["", "## Repairs"] + ([f"- #{x['n']} {x['file']}: {x['summary']} -> {x['result']}" for x in r.repairs] or ["- none needed"])
        L += ["", "## Code review (static, automatic)"] + ([f"- [{i['level']}] {i['file']}:{i['line']} {i['issue']}" for i in r.review[:25]]
                                                            or ["- no findings"])
        L += ["", "## Metrics", f"- time {m['seconds']}s, model calls {m['model_calls']}, repairs kept {m['repairs_kept']}/{m['repairs_tried']}, "
              f"unnecessary retries {m['unnecessary_retries']}, tests run {m['tests_collected']}", "",
              "Rollback: Genesis keeps checkpoints in `.genesis/checkpoints`; say \"roll back the project\" to restore one."]
        return "\n".join(L) + "\n"


def _json(text: str):
    t = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j > i:
        try:
            return json.loads(t[i:j + 1])
        except ValueError:
            return None
    return None
