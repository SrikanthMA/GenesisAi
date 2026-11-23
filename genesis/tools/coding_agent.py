"""Coding agent: write a program from a description, create the project, run it, read errors, fix, re-run.

Flow (planned by the planner for "create a Python script that …"):

    generate_code (LLM, no side effects)
      → write_code_files (MEDIUM — asks: "Create project X with main.py and run it?")
      → vscode_open_project (optional)
      → run_program (same confirmation group) — runs with a timeout, captures stdout/stderr; on failure the LLM
        proposes corrected files which are backed up + re-scanned + re-run, at most `max_attempts` times.

Boundaries:
* New projects are created only inside the coding workspace (default ~/GenesisProjects).
* Automatic fixes are applied only to projects Genesis created (marker file ``.genesis-project``); for the user's
  own code a failure is reported and "fix it" goes through propose_fix → apply_patch (which always asks).
* Every version of the code is safety-scanned before it runs; risky operations make running HIGH risk
  (always an explicit yes), and a fix that *introduces* a risky operation is rejected.
* Missing third-party packages are reported with the install command — Genesis never installs software itself.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Optional

from ..ai.llm.base import LLMError
from ..core.events import AssistantState, EventType
from ..security.injection import wrap_untrusted
from ..security.permissions import RiskLevel
from ..security.validators import ValidationError, validate_path
from ..services.coderunner import (LANGUAGES, language_for, looks_like_server, missing_module, parse_error, safety_scan)
from .base import Tool, ToolResult, obj, s

MARKER = ".genesis-project"
MAX_FILES = 12
MAX_FILE_BYTES = 120_000
TEXT_EXTS = {".py", ".js", ".mjs", ".cjs", ".ts", ".java", ".html", ".htm", ".css", ".json", ".md", ".txt", ".csv", ".toml",
             ".yaml", ".yml", ".ini", ".cfg", ".xml", ".sql", ".env.example"}


def workspace(ctx) -> Path:
    custom = ctx.settings.get("coding.workspace", "") or ""
    return Path(custom).expanduser() if custom else Path.home() / "GenesisProjects"


def slug(text: str) -> str:
    t = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (t[:40].rstrip("-") or "program")


def _safe_rel(path: str) -> str:
    p = str(path or "").replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    if not p or p.startswith("/") or re.match(r"^[a-zA-Z]:", p) or ".." in p.split("/") or p.split("/")[0] in (".git", MARKER):
        raise ValidationError(f"unsafe file path in generated project: {path!r}")
    if len(p.split("/")) > 4 or len(p) > 120:
        raise ValidationError(f"file path too deep/long: {path!r}")
    return p


def _clean_files(files, entry: str) -> tuple[list[dict], str]:
    if not isinstance(files, list) or not files:
        raise ValidationError("the model didn't return any files")
    out, seen = [], set()
    for f in files[:MAX_FILES]:
        if not isinstance(f, dict):
            raise ValidationError("bad file entry")
        rel = _safe_rel(f.get("path", ""))
        content = f.get("content", "")
        if not isinstance(content, str):
            raise ValidationError(f"{rel}: content must be text")
        if len(content.encode()) > MAX_FILE_BYTES:
            raise ValidationError(f"{rel} is too large")
        if rel in seen:
            continue
        seen.add(rel)
        out.append({"path": rel, "content": content})
    entry = _safe_rel(entry) if entry else out[0]["path"]
    if entry not in seen:
        raise ValidationError(f"the entry file {entry} isn't among the generated files")
    return out, entry


def _project_files(root: Path, limit: int = 20) -> dict[str, str]:
    files: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if len(files) >= limit:
            break
        if p.is_file() and (p.suffix.lower() in TEXT_EXTS) and "node_modules" not in p.parts and not p.name.startswith(".") \
                and p.stat().st_size <= MAX_FILE_BYTES:
            try:
                files[str(p.relative_to(root)).replace("\\", "/")] = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
    return files


def _marker(root: Path) -> Optional[dict]:
    m = root / MARKER
    if not m.is_file():
        return None
    try:
        return json.loads(m.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _find_root(p: Path) -> Path:
    for parent in [p] + list(p.parents):
        if (parent / MARKER).is_file():
            return parent
    return p if p.is_dir() else p.parent


def _entry_for(root: Path, meta: Optional[dict]) -> Optional[Path]:
    if meta and meta.get("entry") and (root / meta["entry"]).is_file():
        return root / meta["entry"]
    for name in ("main.py", "app.py", "index.js", "main.js", "index.ts", "main.ts", "Main.java", "index.html"):
        if (root / name).is_file():
            return root / name
    for p in sorted(root.iterdir()):
        if p.is_file() and language_for(p.name):
            return p
    return None


class GenerateCode(Tool):
    name = "generate_code"
    category = "coding"
    description = ("Write a small program for a task with the language model (returns files; writes nothing). "
                   "Languages: " + ", ".join(sorted(LANGUAGES)) + ".")
    parameters = obj({"task": s("What the program should do", maxLength=3000), "language": s("Programming language", maxLength=40),
                      "name": s("Short project name", maxLength=60), "context_file": s("A file the program should use", maxLength=1000),
                      "preferences": s("User coding preferences", maxLength=500)}, ["task"])
    remote = True

    def run(self, args, ctx):
        if not getattr(ctx.llm, "is_generative", False):
            return ToolResult.fail("Writing a program needs the language model, which is offline right now.",
                                   hint="Set HF_TOKEN in .env (or use Ollama), then say “try again”.")
        lang = language_for(args.get("language") or "python")
        if lang is None:
            return ToolResult.fail(f"I can't create {args.get('language')} projects yet — I support " +
                                   ", ".join(v.name for v in LANGUAGES.values()) + ".")
        context = ""
        if args.get("context_file"):
            try:
                cf = validate_path(args["context_file"], allowed_roots=ctx.allowed_roots(), must_exist=True)
                head = cf.read_text(encoding="utf-8", errors="replace")[:3000]
                context = (f"\nThe program must read this file (use its absolute path exactly): {cf}\n"
                           f"First part of the file:\n{wrap_untrusted(head, f'file:{cf.name}')}\n")
            except (ValidationError, OSError) as exc:
                return ToolResult.fail(f"I can't read {args['context_file']}: {exc}")
        runnable = "It will be opened in a web browser." if lang.runs_in_browser else (
            "It will be run non-interactively with no command-line arguments and no stdin, with a 30-second timeout, "
            "so do not wait for input(); print clear results to stdout.")
        java_note = " For Java put everything in ONE file whose public class matches the file name (e.g. Main.java)." \
            if lang.key == "java" else ""
        prompt = (
            f"You are writing a small, complete, working {lang.name} program for a Windows PC.\n"
            f"Task: {args['task']}\n{context}"
            f"User preferences: {args.get('preferences') or 'none'}\n"
            f"Rules: {runnable}{java_note} Prefer the standard library; if a third-party package is truly required, "
            "list it in \"requirements\". Never delete files, run shell commands, touch credentials, or access the network "
            "unless the task explicitly needs it. Keep it short and well-commented.\n"
            "Return ONLY JSON: {\"name\": \"short-project-name\", \"entry\": \"main file path\", \"summary\": \"one sentence\", "
            "\"requirements\": [], \"files\": [{\"path\": \"relative/path\", \"content\": \"full file text\"}]}")
        try:
            data = ctx.llm.chat_json([{"role": "user", "content": prompt}], max_tokens=2500, temperature=0.2)
            if not isinstance(data, dict):
                raise ValueError("the model's answer wasn't JSON")
            files, entry = _clean_files(data.get("files"), str(data.get("entry") or ""))
        except (LLMError, ValueError, ValidationError) as exc:
            return ToolResult.fail(f"I couldn't produce the program: {exc}", retryable=isinstance(exc, LLMError))
        actual = language_for(entry)
        if actual is None and not lang.runs_in_browser:
            return ToolResult.fail(f"The generated entry file {entry} isn't a {lang.name} file.")
        lang = actual or lang  # trust the file that was actually written
        name = slug(args.get("name") or str(data.get("name") or "") or args["task"][:40])
        lines = sum(f["content"].count("\n") + 1 for f in files)
        summary = str(data.get("summary") or "").strip().rstrip(".")
        req = [str(r)[:60] for r in (data.get("requirements") or []) if isinstance(r, str)][:10]
        return ToolResult.ok(f"I've written the {lang.name} code ({', '.join(f['path'] for f in files)}, {lines} lines)"
                             + (f": {summary}." if summary else "."),
                             name=name, language=lang.key, entry=entry, files=files, summary=summary, requirements=req)


class WriteCodeFiles(Tool):
    name = "write_code_files"
    category = "coding"
    description = "Create a NEW project folder in the coding workspace (~/GenesisProjects) with the given files."
    parameters = obj({"name": s("Project folder name", maxLength=60), "language": s("Language", maxLength=40),
                      "entry": s("Entry file", maxLength=200), "summary": s("What it does", maxLength=500),
                      "task": s("Original request", maxLength=3000),
                      "files": {"type": "array", "maxItems": MAX_FILES, "items": {"type": "object"}}}, ["name", "files"])
    risk = RiskLevel.MEDIUM

    def confirmation_prompt(self, args):
        files = args.get("files") or []
        names = ", ".join(str(f.get("path")) for f in files[:4]) + ("…" if len(files) > 4 else "")
        lang = language_for(args.get("language") or "") or language_for(args.get("entry") or "")
        return (f"I'll create a new {lang.name if lang else ''} project “{slug(args['name'])}” in GenesisProjects "
                f"({names}). Okay?").replace("  ", " ")

    def run(self, args, ctx):
        try:
            files, entry = _clean_files(args["files"], args.get("entry") or "")
            base = workspace(ctx)
            base.mkdir(parents=True, exist_ok=True)
            base = validate_path(str(base), allowed_roots=ctx.allowed_roots(), must_exist=True, for_write=True)
        except (ValidationError, OSError) as exc:
            return ToolResult.fail(f"I can't create the project: {exc}")
        root = base / slug(args["name"])
        n = 2
        while root.exists():
            root = base / f"{slug(args['name'])}-{n}"
            n += 1
        written = []
        try:
            for f in files:
                target = (root / f["path"]).resolve()
                target.relative_to(root.resolve())  # stays inside the project
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(f["content"], encoding="utf-8")
                written.append(str(target))
            (root / MARKER).write_text(json.dumps({"created": time.strftime("%Y-%m-%d %H:%M:%S"), "entry": entry,
                                                   "language": args.get("language") or "", "task": args.get("task", "")[:500],
                                                   "summary": args.get("summary", "")}, indent=2), encoding="utf-8")
        except (OSError, ValueError) as exc:
            shutil.rmtree(root, ignore_errors=True)  # only the folder we just created
            return ToolResult.fail(f"I couldn't write the files: {exc}")
        return ToolResult.ok(f"Created {root.name} with {len(written)} file{'s' if len(written) != 1 else ''}.",
                             project_path=str(root), path=str(root / entry), entry=entry, files=written, project=root.name)

    def verify(self, args, result, ctx):
        return all(Path(p).is_file() for p in result.data.get("files", [])) if result.success else False


class RunProgram(Tool):
    name = "run_program"
    category = "coding"
    description = ("Run a program file or project (Python, JavaScript, TypeScript, Java; HTML opens in the browser) with a "
                   "timeout and capture its output. For projects Genesis created, automatically fixes errors and re-runs "
                   "(max 3 attempts).")
    parameters = obj({"path": s("Program file or project folder", maxLength=1000), "language": s("Language", maxLength=40),
                      "stdin": s("Input to send", maxLength=5000), "timeout": {"type": "integer", "minimum": 5, "maximum": 120},
                      "auto_fix": {"type": "boolean", "default": True},
                      "max_attempts": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3},
                      "task": s("What the program is supposed to do (helps fixing)", maxLength=3000)}, ["path"])
    risk = RiskLevel.MEDIUM

    def _files(self, args) -> dict[str, str]:
        try:
            p = Path(args["path"]).expanduser()
            root = _find_root(p)
            return _project_files(root) if root.is_dir() else {}
        except OSError:
            return {}

    def risk_for(self, args, ctx=None):
        return RiskLevel.HIGH if safety_scan(self._files(args)) else RiskLevel.MEDIUM

    def confirmation_prompt(self, args):
        name = Path(args["path"]).name
        findings = safety_scan(self._files(args))
        if findings:
            return (f"⚠ {name} contains code that " + "; ".join(f.split(': ', 1)[1] for f in findings[:3]) +
                    ". Run it anyway?")
        return f"Run {name}?"

    def run(self, args, ctx):
        try:
            target = validate_path(args["path"], allowed_roots=ctx.allowed_roots(), must_exist=True)
        except ValidationError as exc:
            return ToolResult.fail(str(exc))
        root = _find_root(target)
        meta = _marker(root)
        entry = target if target.is_file() else _entry_for(root, meta)
        if entry is None:
            return ToolResult.fail(f"I couldn't find a program to run in {target.name}.")
        lang = language_for(args.get("language") or "") or language_for(entry.name)
        if lang is None:
            return ToolResult.fail(f"I don't know how to run {entry.name} yet.")
        if lang.runs_in_browser:
            ctx.backend.open_path(str(entry))
            return ToolResult.ok(f"Opened {entry.name} in your browser.", path=str(entry), project_path=str(root), passed=True,
                                 attempts=1)
        argv = lang.command(entry)
        if not argv:
            return ToolResult.fail(f"I can't run {lang.name} on this PC — the runtime isn't installed.", hint=lang.install_hint)
        timeout = float(args.get("timeout") or 30)
        can_fix = bool(meta is not None and args.get("auto_fix", True) and getattr(ctx.llm, "is_generative", False))
        attempts = int(args.get("max_attempts") or 3) if can_fix else 1
        baseline = set(safety_scan(_project_files(root)))
        fixes: list[str] = []
        res = None
        for attempt in range(1, attempts + 1):
            files = _project_files(root)
            server = looks_like_server(files)
            # a server never "finishes": give it a few seconds to prove it starts, then stop it
            res = ctx.code_runner.run(argv, root, timeout=min(timeout, 8.0) if server else timeout, stdin=args.get("stdin", ""),
                                      server_like=server)
            if res.ok:
                break
            if res.timed_out and res.server_like:
                cmd = res.display_command()
                return ToolResult.ok(f"{entry.name} is a server: it started and was still running after a few seconds, so I "
                                     f"stopped it. Run it yourself from the project folder with: {cmd}" +
                                     _tail("Output so far", res.stdout), path=str(entry), project_path=str(root),
                                     passed=True, server=True, attempts=attempt, fixes=fixes, stdout=res.stdout, command=cmd)
            mod = missing_module(res.stderr)
            if mod:
                pip = "npm install" if lang.key in ("javascript", "typescript") else "pip install"
                return ToolResult.fail(f"{entry.name} needs the “{mod}” package, which isn't installed. I don't install software "
                                       f"on my own — you can run: {pip} {mod}", path=str(entry), project_path=str(root),
                                       passed=False, missing=mod, attempts=attempt, stderr=res.stderr, fixes=fixes,
                                       issues=[parse_error(res.stderr, root) or {}])
            if attempt >= attempts:
                break
            ctx.events.emit(EventType.STATE_CHANGED, state=AssistantState.RETRYING.value,
                            detail=f"Fixing an error (attempt {attempt + 1} of {attempts})…")
            fixed = self._fix(ctx, root, files, res, args.get("task") or (meta or {}).get("task", ""), baseline)
            if isinstance(fixed, str):  # refusal / failure to fix
                fixes.append(fixed)
                break
            fixes.append(fixed["explanation"])
        assert res is not None
        base = dict(path=str(entry), project_path=str(root), attempts=attempt, fixes=fixes, stdout=res.stdout,
                    stderr=res.stderr, returncode=res.returncode, command=res.display_command())
        fixed_note = f" (after {len(fixes)} fix{'es' if len(fixes) != 1 else ''}: {'; '.join(fixes)})" if fixes and res.ok else ""
        if res.ok:
            return ToolResult.ok(f"Ran {entry.name} successfully{fixed_note}." + _tail("Output", res.stdout), passed=True, **base)
        issue = parse_error(res.stderr, root) or {"path": str(entry), "line": 0, "message": "The program failed.", "kind": "runtime"}
        why = "It took longer than the time limit (it may be waiting for input or stuck in a loop)." if res.timed_out else \
            f"It failed with: {issue['message'].rstrip('.')}."
        tried = f" I tried {len(fixes)} fix{'es' if len(fixes) != 1 else ''} ({'; '.join(fixes)}) but it still fails." if fixes else ""
        hint = "Say “fix it” and I'll propose a fix." if meta is None else "Say “try again” to give it another go."
        return ToolResult.fail(f"{entry.name} didn't run cleanly. {why}{tried}" + _tail("Error output", res.stderr or res.stdout),
                               hint=hint, passed=False, issues=[issue], **base)

    def _fix(self, ctx, root: Path, files: dict[str, str], res, task: str, baseline: set) -> dict | str:
        listing = "\n\n".join(f"--- {p} ---\n{c}" for p, c in files.items())
        output = (res.stderr or "") + ("\n(timed out)" if res.timed_out else "") + ("\n" + res.stdout[-1500:] if res.stdout else "")
        prompt = (f"This program should: {task or 'run without errors'}\nIt failed when run with `{res.display_command()}`.\n"
                  f"{wrap_untrusted(output[-4000:], 'program-output')}\n\nProject files:\n{listing[:30000]}\n\n"
                  "Fix the bug with the smallest change. Do not add input() calls, shell commands, file deletion or network "
                  "access. Return ONLY JSON: {\"explanation\": \"one short sentence\", \"files\": [{\"path\": \"relative path\", "
                  "\"content\": \"complete new file content\"}]} containing only the files you changed.")
        try:
            data = ctx.llm.chat_json([{"role": "user", "content": prompt}], max_tokens=2500, temperature=0.1)
            changed = data.get("files") if isinstance(data, dict) else None
            if not changed:
                return "the model couldn't find a fix"
            clean, _ = _clean_files(changed, "")
        except (LLMError, ValueError, ValidationError) as exc:
            return f"no fix ({exc})"
        new_findings = set(safety_scan({f["path"]: f["content"] for f in clean})) - baseline
        if new_findings:
            return "the proposed fix wanted to " + ", ".join(sorted(f.split(': ', 1)[1] for f in new_findings)) + ", so I rejected it"
        backup = ctx.config.paths.data_dir / "backups" / time.strftime("%Y%m%d-%H%M%S") / root.name
        for f in clean:
            target = (root / f["path"]).resolve()
            try:
                target.relative_to(root.resolve())
            except ValueError:
                return "the fix tried to write outside the project"
            if target.exists():
                (backup / f["path"]).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup / f["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f["content"], encoding="utf-8")
        return {"explanation": str(data.get("explanation") or "fixed an error").strip().rstrip(".")[:200]}


def _tail(label: str, text: str, n: int = 12) -> str:
    lines = [ln for ln in (text or "").rstrip().splitlines()]
    if not lines:
        return ""
    return f"\n{label}:\n" + "\n".join(lines[-n:])


TOOLS = [GenerateCode(), WriteCodeFiles(), RunProgram()]
