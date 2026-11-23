"""VS Code / development tools. Code is analysed statically — never executed."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
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
