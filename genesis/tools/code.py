"""VS Code / development tools. Code is analysed statically — never executed."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
from typing import Optional

from ..ai import prompts
from ..ai.llm.base import LLMError
from ..core.paths import user_known_folders
from ..security.injection import wrap_untrusted
from ..security.validators import ValidationError, validate_path
from ..services.platform import PlatformError
from .base import Tool, ToolResult, obj, s
from .files import resolve_existing

PROJECT_MARKERS = {".git", "pyproject.toml", "package.json", "requirements.txt", ".vscode", "setup.py", "Cargo.toml", "go.mod",
                   "pom.xml", "README.md"}


def project_search_roots() -> list[Path]:
    f = user_known_folders()
    home = f["home"]
    cands = [home / "source" / "repos", home / "Projects", home / "projects", home / "dev", home / "code", home / "Code",
             home / "GitHub", home / "Documents" / "GitHub", f["documents"], f["desktop"], home / "repos", home]
    return [c for c in cands if c.exists()]


def find_project_dir(name: str, roots: Optional[list[Path]] = None, max_depth: int = 3) -> Optional[Path]:
    target = name.lower().replace(" ", "").replace("-", "").replace("_", "")
    best: Optional[Path] = None
    for root in roots or project_search_roots():
        stack = [(root, 0)]
        while stack:
            d, depth = stack.pop()
            try:
                entries = [e for e in os.scandir(d) if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")]
            except OSError:
                continue
            for e in entries:
                norm = e.name.lower().replace(" ", "").replace("-", "").replace("_", "")
                if norm == target or (norm.startswith(target) and len(target) >= 4):
                    p = Path(e.path)
                    if any((p / m).exists() for m in PROJECT_MARKERS):
                        return p
                    best = best or p
                if depth + 1 < max_depth and e.name.lower() not in {"node_modules", "appdata", "venv", ".venv", "library"}:
                    stack.append((Path(e.path), depth + 1))
    return best


class VSCodeOpenProject(Tool):
    name = "vscode_open_project"
    category = "code"
    description = "Open a project folder in VS Code (by saved project name, by searching common folders, or by path)."
    parameters = obj({"project": s("Project name, e.g. 'genesis'", maxLength=200), "path": s("Folder path", maxLength=1000)})

    def run(self, args, ctx):
        name = (args.get("project") or "").strip()
        path: Optional[Path] = None
        try:
            if args.get("path"):
                path = validate_path(args["path"], allowed_roots=ctx.allowed_roots(), must_exist=True)
            elif name:
                saved = ctx.repos.projects.get(name)
                if saved and Path(saved["path"]).is_dir():
                    path = Path(saved["path"])
                else:
                    found = find_project_dir(name)
                    if found:
                        path = validate_path(found, allowed_roots=ctx.allowed_roots(), must_exist=True)
                        ctx.repos.projects.set(name, str(path))
            else:
                return ToolResult.fail("Which project should I open?")
        except ValidationError as exc:
            return ToolResult.fail(str(exc))
        if (path is None or not path.is_dir()) and name == "main":
            return ToolResult.fail("I don't know where your project folder is yet.",
                                   hint="Say “remember that my project folder is C:\\Users\\you\\Projects\\my-app”.")
        if path is None or not path.is_dir():
            return ToolResult.fail(f"I couldn't find a project called '{name}'.",
                                   hint=f"Tell me where it is, e.g. “my {name} project is in C:\\Users\\you\\Projects\\{name}”.")
        exe = ctx.apps.vscode_executable()
        if not exe:
            res = ToolResult.fail("VS Code doesn't seem to be installed (I couldn't find Code.exe).",
                                  hint="Install VS Code from code.visualstudio.com and tick 'Add to PATH'.")
            res.recovery = {"tool": "open_folder", "args": {"folder": str(path)}, "reason": "I opened the project folder in File Explorer instead."}
            return res
        try:
            ctx.backend.launch([exe, str(path)])
        except PlatformError as exc:
            return ToolResult.fail(f"VS Code didn't start: {exc}")
        return ToolResult.ok(f"Opened the {name or path.name} project in VS Code.", project=name or path.name,
                             project_path=str(path), folder_path=str(path))


class VSCodeOpenFile(Tool):
    name = "vscode_open_file"
    category = "code"
    description = "Open a file in VS Code, optionally at a line number."
    parameters = obj({"path": s("File name or path", maxLength=1000), "line": {"type": "integer", "minimum": 1},
                      "base_dir": s("Project folder to resolve relative paths")}, ["path"])

    def run(self, args, ctx):
        try:
            path = resolve_existing(args["path"], ctx, args.get("base_dir"))
        except ValidationError as exc:
            return ToolResult.fail(str(exc))
        exe = ctx.apps.vscode_executable()
        if not exe:
            return ToolResult.fail("VS Code doesn't seem to be installed.", hint="Install VS Code and add it to PATH.")
        argv = [exe, "--reuse-window"]
        argv += ["-g", f"{path}:{args['line']}"] if args.get("line") else [str(path)]
        try:
            ctx.backend.launch(argv)
        except PlatformError as exc:
            return ToolResult.fail(f"VS Code didn't start: {exc}")
        return ToolResult.ok(f"Opened {path.name} in VS Code.", path=str(path))


def check_python(source: str, filename: str) -> list[dict]:
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        return [{"line": exc.lineno or 0, "column": exc.offset or 0, "message": f"SyntaxError: {exc.msg}", "severity": "error"}]
    issues: list[dict] = []
    try:  # optional deeper static analysis
        from pyflakes.api import check as pyflakes_check  # type: ignore
        from pyflakes.reporter import Reporter  # type: ignore
        import io

        out, err = io.StringIO(), io.StringIO()
        pyflakes_check(source, filename, Reporter(out, err))
        for line in out.getvalue().splitlines():
            parts = line.split(":", 3)
            if len(parts) >= 4:
                issues.append({"line": int(parts[1]), "column": int(parts[2]) if parts[2].strip().isdigit() else 0,
                               "message": parts[3].strip(), "severity": "warning"})
    except ImportError:
        compile(tree, filename, "exec")
    return issues


class CheckCodeFile(Tool):
    name = "check_code_file"
    category = "code"
    description = "Statically check a Python or JSON file for errors (syntax, undefined names with pyflakes). Never runs the code."
    parameters = obj({"path": s("File path", maxLength=1000), "base_dir": s("Project folder")}, ["path"])

    def run(self, args, ctx):
        try:
            path = resolve_existing(args["path"], ctx, args.get("base_dir"))
        except ValidationError as exc:
            return ToolResult.fail(str(exc))
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolResult.fail(f"I couldn't read {path.name}: {exc}")
        ext = path.suffix.lower()
        if ext == ".py":
            issues = check_python(source, str(path))
        elif ext == ".json":
            try:
                json.loads(source)
                issues = []
            except json.JSONDecodeError as exc:
                issues = [{"line": exc.lineno, "column": exc.colno, "message": exc.msg, "severity": "error"}]
        else:
            return ToolResult.ok(f"I can statically check Python and JSON files; {path.name} is {ext or 'unknown'}. "
                                 "Ask me to explain it instead.", path=str(path), issues=[], checked=False)
        if not issues:
            return ToolResult.ok(f"No syntax errors found in {path.name}.", path=str(path), issues=[], checked=True)
        first = issues[0]
        lines = [f"line {i['line']}: {i['message']}" for i in issues[:8]]
        return ToolResult.ok(f"Found {len(issues)} issue(s) in {path.name}:\n" + "\n".join(lines), path=str(path), issues=issues,
                             checked=True, first_line=first["line"])


class ExplainCode(Tool):
    name = "explain_code"
    category = "code"
    description = "Explain an error message or a code file and suggest a fix (uses the language model)."
    parameters = obj({"error_text": s("Error / traceback text", maxLength=20_000), "path": s("File path", maxLength=1000),
                      "base_dir": s("Project folder"), "issues": s("Known issues from check_code_file", maxLength=5000)})
    remote = True

    def run(self, args, ctx):
        content = ""
        if args.get("path"):
            try:
                path = resolve_existing(args["path"], ctx, args.get("base_dir"))
                content += wrap_untrusted(path.read_text(encoding="utf-8", errors="replace")[:30_000], f"file:{path.name}")
            except (ValidationError, OSError) as exc:
                return ToolResult.fail(str(exc))
        if args.get("error_text"):
            content += "\n\nError:\n" + wrap_untrusted(args["error_text"], "error message")
        if args.get("issues"):
            content += "\n\nStatic analysis found:\n" + args["issues"]
        if not content:
            return ToolResult.fail("Paste the error text or tell me which file to look at.")
        if not getattr(ctx.llm, "is_generative", False):
            return ToolResult.fail("Explaining code needs the language model, which is offline right now.",
                                   hint="Set HF_TOKEN in .env (or use Ollama) and try again.")
        task = "Explain the following error and how to fix it." if args.get("error_text") else "Find and explain the problems in this file."
        try:
            answer = ctx.llm.chat([{"role": "user", "content": prompts.CODE_EXPLAIN_PROMPT.format(task=task, content=content)}],
                                  max_tokens=900, temperature=0.2)
        except LLMError as exc:
            return ToolResult.fail(f"The language model couldn't answer: {exc}")
        return ToolResult.ok(answer, explanation=answer)


TOOLS = [VSCodeOpenProject(), VSCodeOpenFile(), CheckCodeFile(), ExplainCode()]


# ============================================================ coding agent (v2)
import difflib  # noqa: E402
import hashlib  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time as _time  # noqa: E402

from ..security.permissions import RiskLevel  # noqa: E402

SKIP = {"node_modules", ".git", "__pycache__", ".venv", "venv", "env", "dist", "build", ".next", ".mypy_cache", ".pytest_cache",
        "site-packages", ".idea", ".vscode", "coverage"}
FRONTEND_DIRS = ("frontend", "client", "web", "ui", "app", "src")


def _project_dir(args, ctx) -> Path:
    if args.get("path"):
        p = validate_path(args["path"], allowed_roots=ctx.allowed_roots(), must_exist=True)
        return p if p.is_dir() else p.parent
    name = (args.get("project") or "").strip()
    if name:
        saved = ctx.repos.projects.get(name)
        if saved:
            return validate_path(saved["path"], allowed_roots=ctx.allowed_roots(), must_exist=True)
        found = find_project_dir(name)
        if found:
            return validate_path(found, allowed_roots=ctx.allowed_roots(), must_exist=True)
    raise ValidationError("Which project? Open it first (“open my Genesis project”) or tell me its folder.")


def _iter_files(root: Path, exts: tuple, limit: int = 600):
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP and not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(exts):
                yield Path(dirpath) / fn
                count += 1
                if count >= limit:
                    return


class DiagnoseProject(Tool):
    name = "diagnose_project"
    category = "code"
    description = ("Inspect a project for problems without running it: Python syntax/undefined names, broken JSON, missing "
                   "dependencies (node_modules / venv), missing files referenced by index.html, and recent errors in log files.")
    parameters = obj({"path": s("Project folder", maxLength=1000), "project": s("Saved project name", maxLength=100),
                      "focus": s("Area to focus on, e.g. 'frontend'", maxLength=100)})

    def run(self, args, ctx):
        try:
            root = _project_dir(args, ctx)
        except ValidationError as exc:
            return ToolResult.fail(str(exc))
        focus = (args.get("focus") or "").lower()
        scan_root = root
        if focus in ("frontend", "front end", "client", "ui", "web"):
            for d in ("frontend", "client", "web", "ui"):
                if (root / d).is_dir():
                    scan_root = root / d
                    break
        issues: list[dict] = []
        for f in _iter_files(scan_root, (".py",)):
            try:
                for i in check_python(f.read_text(encoding="utf-8", errors="replace"), str(f)):
                    if i["severity"] == "error" or "undefined name" in i["message"]:
                        issues.append({**i, "path": str(f), "kind": "python"})
            except OSError:
                continue
        for f in _iter_files(scan_root, (".json",), limit=200):
            if f.name in ("tsconfig.json", "jsconfig.json") or ".vscode" in f.parts:
                continue
            try:
                json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                issues.append({"path": str(f), "line": exc.lineno, "message": f"Invalid JSON: {exc.msg}", "severity": "error",
                               "kind": "json"})
            except OSError:
                pass
        pkg = next((p for p in (scan_root / "package.json", root / "package.json") if p.exists()), None)
        if pkg is not None:
            try:
                meta = json.loads(pkg.read_text(encoding="utf-8"))
                if not (pkg.parent / "node_modules").exists() and (meta.get("dependencies") or meta.get("devDependencies")):
                    issues.append({"path": str(pkg), "line": 0, "severity": "error", "kind": "deps",
                                   "message": "Dependencies aren't installed (node_modules is missing) — run npm install."})
                scripts = meta.get("scripts") or {}
                if not any(k in scripts for k in ("start", "dev", "serve")):
                    issues.append({"path": str(pkg), "line": 0, "severity": "warning", "kind": "config",
                                   "message": "package.json has no start/dev script."})
            except (json.JSONDecodeError, OSError):
                pass
        for html in list(_iter_files(scan_root, ("index.html",), limit=5)):
            try:
                text = html.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m in re.finditer(r"""<(?:script|link)[^>]+(?:src|href)=["']([^"':]+?\.(?:js|jsx|ts|tsx|css))["']""", text, re.I):
                ref = m.group(1).lstrip("/")
                if not (html.parent / ref).exists() and not (root / ref).exists() and not (html.parent / "src" / Path(ref).name).exists():
                    line = text[:m.start()].count("\n") + 1
                    issues.append({"path": str(html), "line": line, "severity": "error", "kind": "html",
                                   "message": f"index.html references {ref}, which doesn't exist."})
        for log_file in sorted(root.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:3]:
            try:
                tail = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
            except OSError:
                continue
            for ln in reversed(tail):
                if re.search(r"\b(error|exception|traceback|failed)\b", ln, re.I):
                    issues.append({"path": str(log_file), "line": 0, "severity": "warning", "kind": "log",
                                   "message": f"Recent log error: {ln.strip()[:200]}"})
                    break
        issues.sort(key=lambda i: (i["severity"] != "error", i["path"]))
        if not issues:
            return ToolResult.ok(f"I checked {scan_root.name} and didn't find syntax errors, broken config or missing files. "
                                 "If it still fails, tell me the exact error message.", issues=[], project_path=str(root))
        lines = [f"{n + 1}. {Path(i['path']).name}" + (f":{i['line']}" if i.get("line") else "") + f" — {i['message']}"
                 for n, i in enumerate(issues[:8])]
        return ToolResult.ok(f"I found {len(issues)} problem{'s' if len(issues) != 1 else ''} in {scan_root.name}:\n" + "\n".join(lines)
                             + "\nSay “fix the first one” and I'll propose a fix.", issues=issues[:30], project_path=str(root))


def _apply_edits(source: str, edits: list[dict]) -> str:
    out = source
    for e in edits:
        find, repl = e.get("find", ""), e.get("replace", "")
        if not find or out.count(find) != 1:
            raise ValueError("an edit didn't match the file exactly once")
        out = out.replace(find, repl, 1)
    return out


class ProposeFix(Tool):
    name = "propose_fix"
    category = "code"
    description = "Work out a fix for a problem in a code file (uses the language model). Does NOT change anything."
    parameters = obj({"path": s("File with the problem", maxLength=1000), "issue": s("Problem description", maxLength=2000),
                      "line": {"type": "integer", "minimum": 0}}, ["path", "issue"])
    remote = True

    def run(self, args, ctx):
        try:
            path = validate_path(args["path"], allowed_roots=ctx.allowed_roots(), must_exist=True)
            source = path.read_text(encoding="utf-8")
        except (ValidationError, OSError, UnicodeError) as exc:
            return ToolResult.fail(f"I can't read that file: {exc}")
        if len(source) > 60_000:
            return ToolResult.fail("That file is too large for me to fix safely in one go.")
        if not getattr(ctx.llm, "is_generative", False):
            return ToolResult.fail("Proposing a code fix needs the language model, which is offline right now.",
                                   hint="Set HF_TOKEN in .env (or use Ollama), then say “try again”.")
        prompt = ("Fix the problem in this file with the smallest possible change.\n"
                  f"Problem: {args['issue']}" + (f" (around line {args['line']})" if args.get("line") else "") + "\n\n"
                  + wrap_untrusted(source, f"file:{path.name}") +
                  "\n\nReturn ONLY JSON: {\"summary\": \"one sentence\", \"edits\": [{\"find\": \"exact existing text\", "
                  "\"replace\": \"new text\"}]}. Each 'find' must appear exactly once in the file.")
        try:
            data = ctx.llm.chat_json([{"role": "user", "content": prompt}], max_tokens=1200, temperature=0.1)
            edits = data.get("edits") if isinstance(data, dict) else None
            if not edits:
                return ToolResult.fail("I couldn't work out a safe fix for that.")
            new_source = _apply_edits(source, edits)
        except (LLMError, ValueError) as exc:
            return ToolResult.fail(f"I couldn't produce a safe fix: {exc}")
        if path.suffix == ".py":
            try:
                ast.parse(new_source)
            except SyntaxError as exc:
                return ToolResult.fail(f"My proposed fix would still have a syntax error (line {exc.lineno}), so I discarded it.")
        diff = "".join(difflib.unified_diff(source.splitlines(True), new_source.splitlines(True), f"a/{path.name}", f"b/{path.name}"))
        summary = str(data.get("summary") or "a small fix").strip().rstrip(".")
        return ToolResult.ok(f"I found the issue: {summary}.", path=str(path), new_content=new_source, summary=summary, diff=diff,
                             base_sha=hashlib.sha256(source.encode()).hexdigest())


class ApplyPatch(Tool):
    name = "apply_patch"
    category = "code"
    description = "Write a proposed change to a file (always asks first; keeps a backup so it can be undone)."
    parameters = obj({"path": s("File", maxLength=1000), "new_content": s("New file content", maxLength=200_000),
                      "summary": s("What changes", maxLength=500), "base_sha": s("Hash of the file the fix was made for", maxLength=64)},
                     ["path", "new_content"])
    risk = RiskLevel.MEDIUM
    always_confirm = True

    def confirmation_prompt(self, args):
        return (f"I can make this change to {Path(args['path']).name}: {args.get('summary') or 'the proposed fix'} "
                "(I'll keep a backup). Should I proceed?")

    def run(self, args, ctx):
        try:
            path = validate_path(args["path"], allowed_roots=ctx.allowed_roots(), must_exist=True, for_write=True)
            current = path.read_text(encoding="utf-8")
        except (ValidationError, OSError, UnicodeError) as exc:
            return ToolResult.fail(f"I can't change that file: {exc}")
        if args.get("base_sha") and hashlib.sha256(current.encode()).hexdigest() != args["base_sha"]:
            return ToolResult.fail("The file changed since I looked at it, so I didn't overwrite it. Say “try again”.")
        backup_dir = ctx.config.paths.data_dir / "backups" / _time.strftime("%Y%m%d-%H%M%S")
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / path.name
        shutil.copy2(path, backup)
        path.write_text(args["new_content"], encoding="utf-8")
        if path.suffix == ".py":
            try:
                ast.parse(args["new_content"])
            except SyntaxError:
                shutil.copy2(backup, path)
                return ToolResult.fail("The change broke the file's syntax, so I restored the original.")
        return ToolResult.ok(f"Done — I updated {path.name} (backup saved).", path=str(path), backup=str(backup),
                             summary=args.get("summary", ""))

    def verify(self, args, result, ctx):
        if not result.success:
            return False
        return Path(result.data["path"]).read_text(encoding="utf-8") == args["new_content"]


class RestoreBackup(Tool):
    name = "restore_backup"
    category = "code"
    description = "Undo a change Genesis made to a file by restoring its backup."
    parameters = obj({"path": s("File", maxLength=1000), "backup": s("Backup file", maxLength=1000)}, ["path", "backup"])
    risk = RiskLevel.MEDIUM

    def confirmation_prompt(self, args):
        return f"Restore {Path(args['path']).name} to how it was before my change?"

    def run(self, args, ctx):
        try:
            path = validate_path(args["path"], allowed_roots=ctx.allowed_roots(), for_write=True)
            backup = validate_path(args["backup"], allowed_roots=[ctx.config.paths.data_dir.resolve()], must_exist=True)
        except ValidationError as exc:
            return ToolResult.fail(str(exc))
        shutil.copy2(backup, path)
        return ToolResult.ok(f"Restored {path.name} to the previous version.", path=str(path))


def detect_test_command(root: Path) -> Optional[list[str]]:
    venv_py = next((p for p in (root / ".venv" / "Scripts" / "python.exe", root / ".venv" / "bin" / "python",
                                root / "venv" / "Scripts" / "python.exe") if p.exists()), None)
    py = str(venv_py) if venv_py else sys.executable
    has_pytest_cfg = (root / "pytest.ini").exists() or "[tool.pytest" in ((root / "pyproject.toml").read_text(encoding="utf-8")
                                                                          if (root / "pyproject.toml").exists() else "")
    if (root / "tests").is_dir() or has_pytest_cfg:
        probe = subprocess.run([py, "-c", "import pytest"], capture_output=True, timeout=30, shell=False)
        if probe.returncode == 0:
            return [py, "-m", "pytest", "-q"]
        return [py, "-m", "unittest", "discover", "-s", "tests" if (root / "tests").is_dir() else "."]
    pkg = root / "package.json"
    if pkg.exists():
        try:
            if "test" in (json.loads(pkg.read_text(encoding="utf-8")).get("scripts") or {}):
                npm = shutil.which("npm")
                if npm:
                    return [npm, "test", "--silent"]
        except (json.JSONDecodeError, OSError):
            pass
    return None


class RunTests(Tool):
    name = "run_tests"
    category = "code"
    description = "Run the project's own test suite (auto-detected: pytest / unittest / npm test). Asks first; 5-minute timeout."
    parameters = obj({"path": s("Project folder", maxLength=1000), "project": s("Saved project name", maxLength=100)})
    risk = RiskLevel.MEDIUM

    def confirmation_prompt(self, args):
        return "Run the project's tests?"

    def run(self, args, ctx):
        try:
            root = _project_dir(args, ctx)
        except ValidationError as exc:
            return ToolResult.fail(str(exc))
        cmd = detect_test_command(root)
        if not cmd:
            return ToolResult.fail("I couldn't find a test suite in this project (no tests/ folder or npm test script).")
        try:
            proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=300, shell=False,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            return ToolResult.fail("The tests took longer than 5 minutes, so I stopped them.")
        except OSError as exc:
            return ToolResult.fail(f"I couldn't start the tests: {exc}")
        out = (proc.stdout + "\n" + proc.stderr).strip().splitlines()
        tail = "\n".join(out[-12:])
        if proc.returncode == 0:
            return ToolResult.ok("The tests pass. ✓\n" + "\n".join(out[-3:]), passed=True, output=tail, command=" ".join(cmd[-3:]))
        return ToolResult.fail("The tests failed:\n" + tail, passed=False, output=tail,
                               hint="Say “undo that” to restore the previous version of the file.")


TOOLS += [DiagnoseProject(), ProposeFix(), ApplyPatch(), RestoreBackup(), RunTests()]
