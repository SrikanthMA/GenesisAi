"""Project intelligence and Mission Mode (3.1).

Answers "what did I work on yesterday?", "which files changed recently?", "summarize my project", "any unfinished
tasks?", "what should I work on today?", "open TwinSim and check the latest changes" - from **real** data only: your
registered project folders (the existing "remember that my X project is in …"), Git history and status, file
timestamps, README/docs and TODO/FIXME notes. Nothing is invented: if Git isn't there or a folder can't be read, the
answer says so.

Only registered/named project folders are read, on demand (no background scanning of the computer). Git is run with
fixed read-only arguments (log, status, diff --stat, branch) - never a model-generated command.

Mission Mode ("mission: my TwinSim project is due tomorrow - check it and prepare a report") runs a fixed sequence of
read-only checks with progress, cancellation (emergency stop), a time budget and a verified Word report.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from ..tools.base import Tool, ToolResult, obj, s
from . import control

SKIP_DIRS = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", "dist",
             "build", ".idea", ".vscode", ".next", "target", "site-packages", ".tox", "data", "logs", "backups"}
CODE_EXT = {".py": "Python", ".js": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript", ".jsx": "JavaScript",
            ".java": "Java", ".cs": "C#", ".cpp": "C++", ".c": "C", ".h": "C/C++", ".go": "Go", ".rs": "Rust",
            ".html": "HTML", ".css": "CSS", ".ipynb": "Notebook", ".sql": "SQL", ".kt": "Kotlin", ".swift": "Swift",
            ".m": "MATLAB", ".r": "R", ".php": "PHP", ".rb": "Ruby"}
DOC_NAMES = ("readme.md", "readme.txt", "readme", "readme.rst")
TODO_RE = re.compile(r"(?:#|//|/\*|<!--|--)\s*(TODO|FIXME|HACK|XXX)\b[:\s-]*(.{0,120})", re.I)


# ------------------------------------------------------------------ locating projects
def project_dir(ctx, name: str = "") -> Path:
    from ..tools.code import _project_dir

    return _project_dir({"project": name or "main"}, ctx)


def registered(ctx) -> list[tuple[str, Path]]:
    out = []
    try:
        for row in ctx.repos.projects.list():
            p = Path(row["path"])
            if p.is_dir():
                out.append((row["name"], p))
    except Exception:
        pass
    return out


def walk(root: Path, limit: int = 20000):
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            yield Path(dirpath) / fn
            n += 1
            if n >= limit:
                return


# ------------------------------------------------------------------ git (fixed, read-only commands)
def git(root: Path, *args: str, timeout: float = 15) -> Optional[str]:
    exe = shutil.which("git")
    if not exe or not (root / ".git").exists():
        return None
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
    try:
        p = subprocess.run([exe, "-C", str(root), *args], capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace", **kw)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 else None


def git_info(root: Path, since: Optional[datetime] = None, until: Optional[datetime] = None, n: int = 8) -> dict:
    if (root / ".git").exists() and not shutil.which("git"):
        return {"git": False, "reason": "Git isn't installed, so I can't read the history."}
    if not (root / ".git").exists():
        return {"git": False, "reason": "This folder isn't a Git repository."}
    info: dict = {"git": True}
    info["branch"] = (git(root, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    rng = []
    if since:
        rng.append(f"--since={since:%Y-%m-%d %H:%M}")
    if until:
        rng.append(f"--until={until:%Y-%m-%d %H:%M}")
    log = git(root, "log", f"-{n}", "--date=format:%Y-%m-%d %H:%M", "--pretty=format:%h\x1f%ad\x1f%an\x1f%s", *rng) or ""
    info["commits"] = [dict(zip(("hash", "date", "author", "subject"), ln.split("\x1f"))) for ln in log.splitlines()
                       if ln.count("\x1f") == 3]
    status = git(root, "status", "--porcelain") or ""
    changed = [ln[3:].strip() for ln in status.splitlines() if ln.strip()]
    info["uncommitted"] = changed
    info["diffstat"] = ((git(root, "diff", "--stat") or "").strip().splitlines() or [""])[-1].strip()
    return info


# ------------------------------------------------------------------ files
def recent_files(root: Path, since: datetime, until: Optional[datetime] = None, limit: int = 15) -> list[tuple[str, datetime]]:
    t0, t1 = since.timestamp(), (until or datetime.now()).timestamp()
    hits = []
    for f in walk(root):
        try:
            m = f.stat().st_mtime
        except OSError:
            continue
        if t0 <= m <= t1:
            hits.append((str(f.relative_to(root)), datetime.fromtimestamp(m)))
    hits.sort(key=lambda h: h[1], reverse=True)
    return hits[:limit]


def todos(root: Path, limit: int = 30) -> list[dict]:
    out = []
    for f in walk(root, 8000):
        if f.suffix.lower() not in CODE_EXT and f.suffix.lower() not in (".md", ".txt"):
            continue
        try:
            if f.stat().st_size > 1_000_000:
                continue
            for i, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                m = TODO_RE.search(line)
                if m:
                    out.append({"file": str(f.relative_to(root)), "line": i, "tag": m.group(1).upper(),
                                "text": m.group(2).strip()})
                    if len(out) >= limit:
                        return out
        except OSError:
            continue
    return out


def structure(root: Path) -> dict:
    langs: dict[str, int] = {}
    total = 0
    for f in walk(root):
        total += 1
        lang = CODE_EXT.get(f.suffix.lower())
        if lang:
            langs[lang] = langs.get(lang, 0) + 1
    top = sorted([p.name + ("/" if p.is_dir() else "") for p in root.iterdir()
                  if not p.name.startswith(".") and p.name.lower() not in SKIP_DIRS])[:25]
    readme = next((p for p in root.iterdir() if p.name.lower() in DOC_NAMES), None)
    excerpt = ""
    if readme:
        try:
            lines = [ln.strip() for ln in readme.read_text(encoding="utf-8", errors="ignore").splitlines()]
            excerpt = " ".join(ln.lstrip("# ") for ln in lines if ln and not ln.startswith(("!", "[![", "<")))[:500]
        except OSError:
            pass
    docs = [str(p.relative_to(root)) for p in walk(root, 5000) if p.suffix.lower() in (".md", ".rst", ".docx", ".pdf")][:20]
    return {"files": total, "languages": dict(sorted(langs.items(), key=lambda kv: -kv[1])), "top": top,
            "readme": str(readme.name) if readme else "", "readme_excerpt": excerpt, "docs": docs}


def window(when: str) -> tuple[datetime, datetime, str]:
    now = datetime.now()
    today = datetime.combine(date.today(), datetime.min.time())
    w = (when or "yesterday").lower()
    if w == "today":
        return today, now, "today"
    if w in ("week", "this week", "last week", "7 days"):
        return today - timedelta(days=7), now, "in the last 7 days"
    return today - timedelta(days=1), today, "yesterday"


def _projects_for(ctx, name: str) -> list[tuple[str, Path]]:
    if name:
        return [(name, project_dir(ctx, name))]
    regs = registered(ctx)
    if not regs:
        raise ValueError("I don't know your project folders yet. Tell me one, e.g. “remember that my TwinSim project "
                         "is in C:\\Users\\you\\Projects\\TwinSim”.")
    return regs[:6]


# ------------------------------------------------------------------ tools
class ProjectActivity(Tool):
    name = "project_activity"
    category = "code"
    description = ("What the user worked on (yesterday / today / this week) in their projects: Git commits and files "
                   "changed in that period, from the real history.")
    parameters = obj({"project": s("project name; empty = all registered projects", maxLength=100),
                      "when": {"type": "string", "enum": ["yesterday", "today", "week"], "default": "yesterday"}})

    def run(self, args, ctx):
        try:
            projs = _projects_for(ctx, args.get("project", ""))
        except Exception as exc:
            return ToolResult.fail(str(exc))
        since, until, label = window(args.get("when", "yesterday"))
        lines, spoken, data = [], [], []
        for name, root in projs:
            g = git_info(root, since, until, n=15)
            files = recent_files(root, since, until)
            data.append({"project": name, "commits": g.get("commits", []), "files": [f for f, _ in files]})
            if not g.get("commits") and not files:
                continue
            spoken.append(f"{name}: {len(g.get('commits', []))} commit(s), {len(files)} file(s) changed")
            lines.append(f"{name} ({root})")
            lines += [f"  • {c['date']}  {c['subject']}" for c in g.get("commits", [])]
            lines += [f"  – edited {f}  ({t:%d %b %H:%M})" for f, t in files[:10]]
        if not spoken:
            return ToolResult.ok(f"I don't see any commits or edited files {label} in "
                                 f"{', '.join(n for n, _ in projs)}.", projects=data)
        return ToolResult.ok(f"{label.capitalize()} you worked on " + "; ".join(spoken) + ".\n\n" + "\n".join(lines),
                             projects=data)


class ProjectChanges(Tool):
    name = "project_changes"
    category = "code"
    description = ("Check the latest changes in a project: branch, uncommitted files, diff size and recent commits "
                   "(read-only Git). With open=true it also opens the project in VS Code and checks VS Code started.")
    parameters = obj({"project": s("project name", maxLength=100), "open": {"type": "boolean", "default": False}},
                     ["project"])

    def run(self, args, ctx):
        try:
            root = project_dir(ctx, args["project"])
        except Exception as exc:
            return ToolResult.fail(str(exc))
        opened = ""
        if args.get("open"):
            from ..tools.code import VSCodeOpenProject

            res = VSCodeOpenProject().run({"path": str(root)}, ctx)
            opened = (("Opened it in VS Code" + ("" if vscode_running() is not False else
                                               " (but I can't see VS Code running yet)")) if res.success
                      else f"VS Code didn't open: {res.message}") + ". "
        g = git_info(root)
        if not g.get("git"):
            recent = recent_files(root, datetime.now() - timedelta(days=3))
            body = "\n".join(f"  – {f}  ({t:%d %b %H:%M})" for f, t in recent) or "  (no files changed in 3 days)"
            return ToolResult.ok(f"{opened}{g['reason']} Files changed in the last 3 days:\n{body}", git=False,
                                 project_path=str(root))
        unc = g["uncommitted"]
        head = (f"{opened}{args['project']} is on branch {g['branch'] or '?'}"
                f" with {len(unc)} uncommitted change(s)" + (f" ({g['diffstat']})" if g["diffstat"] else "") + ".")
        last = g["commits"][0] if g["commits"] else None
        if last:
            head += f" Last commit: “{last['subject']}” on {last['date']}."
        detail = (["Uncommitted:"] + [f"  • {u}" for u in unc[:25]] if unc else ["No uncommitted changes."]) + \
                 ["Recent commits:"] + [f"  • {c['date']}  {c['hash']}  {c['subject']}" for c in g["commits"]]
        return ToolResult.ok(head + "\n\n" + "\n".join(detail), project_path=str(root), **{k: g[k] for k in
                                                                                         ("branch", "uncommitted", "commits")})


def vscode_running() -> Optional[bool]:
    try:
        import psutil
    except ImportError:
        return None
    end = time.time() + 6
    while time.time() < end:
        if any((p.info.get("name") or "").lower() in ("code.exe", "code") for p in psutil.process_iter(["name"])):
            return True
        time.sleep(0.5)
    return False


class ProjectSummary(Tool):
    name = "project_summary"
    category = "code"
    description = "Summarize a project's current state: purpose (README), structure, languages, Git status, TODOs."
    parameters = obj({"project": s("project name", maxLength=100)}, ["project"])

    def run(self, args, ctx):
        try:
            root = project_dir(ctx, args["project"])
        except Exception as exc:
            return ToolResult.fail(str(exc))
        st, g, td = structure(root), git_info(root), todos(root)
        langs = ", ".join(f"{k} ({v})" for k, v in list(st["languages"].items())[:4]) or "no code files found"
        spoken = (f"{args['project']} has {st['files']} files, mostly {langs.split(' (')[0]}"
                  + (f", {len(g['uncommitted'])} uncommitted changes" if g.get("git") else "")
                  + (f" and {len(td)} TODO notes" if td else "") + ".")
        lines = [f"Folder: {root}", f"Languages: {langs}", f"Top level: {', '.join(st['top'])}"]
        if st["readme_excerpt"]:
            lines.append(f"README: {st['readme_excerpt']}")
        if g.get("git"):
            lines.append(f"Git: branch {g['branch']}, {len(g['uncommitted'])} uncommitted"
                         + (f", last commit “{g['commits'][0]['subject']}” ({g['commits'][0]['date']})" if g["commits"] else ""))
        else:
            lines.append(g["reason"])
        if td:
            lines.append("TODOs: " + "; ".join(f"{t['file']}:{t['line']} {t['text']}" for t in td[:8]))
        return ToolResult.ok(spoken + "\n\n" + "\n".join(lines), structure=st, todos=td)


class ProjectTodos(Tool):
    name = "project_todos"
    category = "code"
    description = "List unfinished work in a project: TODO / FIXME / HACK notes in the code and docs."
    parameters = obj({"project": s("project name", maxLength=100)}, ["project"])

    def run(self, args, ctx):
        try:
            root = project_dir(ctx, args["project"])
        except Exception as exc:
            return ToolResult.fail(str(exc))
        td = todos(root)
        if not td:
            return ToolResult.ok(f"No TODO or FIXME notes in {args['project']}.", todos=[])
        return ToolResult.ok(f"{len(td)}{'+' if len(td) >= 30 else ''} unfinished notes in {args['project']}:\n" +
                             "\n".join(f"  • [{t['tag']}] {t['file']}:{t['line']}  {t['text']}" for t in td), todos=td)


class WhatToWorkOn(Tool):
    name = "what_to_work_on"
    category = "code"
    description = "Suggest what to work on today from real project state: uncommitted work, TODO notes, latest commits."
    parameters = obj({"project": s("project name; empty = registered projects", maxLength=100)})

    def run(self, args, ctx):
        try:
            projs = _projects_for(ctx, args.get("project", ""))
        except Exception as exc:
            return ToolResult.fail(str(exc))
        ideas = []
        for name, root in projs:
            g, td = git_info(root), todos(root, 10)
            if g.get("uncommitted"):
                ideas.append(f"Finish and commit the {len(g['uncommitted'])} uncommitted change(s) in {name} "
                             f"(e.g. {', '.join(g['uncommitted'][:3])}).")
            for t in td[:3]:
                ideas.append(f"{name}: {t['tag']} in {t['file']}:{t['line']} - {t['text'] or '(no description)'}")
            if g.get("commits"):
                ideas.append(f"Continue from {name}'s last commit: “{g['commits'][0]['subject']}”.")
        if not ideas:
            return ToolResult.ok("Everything looks committed and there are no TODO notes - nothing obvious is pending.")
        return ToolResult.ok(f"Based on your projects, {ideas[0][0].lower() + ideas[0][1:]}\n\nAll suggestions:\n" +
                             "\n".join(f"  • {i}" for i in ideas[:10]), ideas=ideas)


# ------------------------------------------------------------------ Mission Mode
class MissionProjectReport(Tool):
    name = "mission_project_report"
    category = "code"
    description = ("Mission Mode for a project: inspect structure, documentation, Git, code problems (read-only checks) "
                   "and unfinished work, then write and open a verified Word report with next actions.")
    parameters = obj({"project": s("project name", maxLength=100),
                      "objective": s("what the user wants, e.g. 'due tomorrow, check missing docs'", maxLength=500)},
                     ["project"])
    BUDGET_S = 180

    def run(self, args, ctx):
        from .provider import runtime

        control.reset()
        store = runtime(getattr(ctx, "config", None))["store"]
        objective = args.get("objective") or f"Check the {args['project']} project and prepare a report"
        mid = store.mission_start(objective)
        steps: list[dict] = []
        t0 = time.time()
        found: dict = {}

        def step(name, fn):
            if control.cancelled():
                steps.append({"step": name, "status": "cancelled"})
                return None
            if time.time() - t0 > self.BUDGET_S:
                steps.append({"step": name, "status": "skipped", "detail": "time budget used up"})
                return None
            _progress(ctx, f"Mission: {name}…")
            try:
                out = fn()
                steps.append({"step": name, "status": "done"})
                return out
            except Exception as exc:
                steps.append({"step": name, "status": "failed", "detail": str(exc)[:200]})
                return None
            finally:
                store.mission_update(mid, steps=steps)

        root = step("Locate the project folder", lambda: project_dir(ctx, args["project"]))
        if root is None:
            store.mission_update(mid, status="failed", steps=steps)
            return ToolResult.fail(f"Mission stopped: I couldn't find the {args['project']} project. "
                                   + (steps[-1].get("detail") or ""))
        found["structure"] = step("Inspect the project structure", lambda: structure(root))
        found["docs"] = step("Check the documentation", lambda: doc_gaps(root, found.get("structure") or structure(root)))
        found["git"] = step("Read the Git history", lambda: git_info(root, n=10))
        found["problems"] = step("Run read-only code checks", lambda: code_checks(ctx, root))
        found["todos"] = step("Collect unfinished work (TODO/FIXME)", lambda: todos(root, 40))
        report = step("Write the report", lambda: write_report(ctx, args["project"], root, objective, found, steps))
        cancelled = control.cancelled()
        failed = [s_ for s_ in steps if s_["status"] == "failed"]
        status = "cancelled" if cancelled else ("done" if report and Path(report).is_file() else "failed")
        store.mission_update(mid, status=status, steps=steps, report_path=str(report or ""))
        if report and Path(report).is_file():
            try:
                ctx.backend.open_path(str(report))
            except Exception:
                pass
        done = sum(1 for s_ in steps if s_["status"] == "done")
        probs = len((found.get("problems") or {}).get("issues", []))
        gaps = len(found.get("docs") or [])
        summary = (f"Mission {'cancelled' if cancelled else 'complete'}: {done} of {len(steps)} steps done"
                   + (f", {len(failed)} failed" if failed else "") + f". I found {probs} code problem(s), {gaps} "
                   f"documentation gap(s) and {len(found.get('todos') or [])} TODO note(s)"
                   + (f". The report is open: {Path(report).name}" if report else ". I couldn't write the report") + ".")
        detail = "\n".join(f"  {'✓' if s_['status'] == 'done' else '✗' if s_['status'] == 'failed' else '–'} "
                           f"{s_['step']}" + (f" ({s_['detail']})" if s_.get("detail") else "") for s_ in steps)
        res = ToolResult.ok(summary + "\n\n" + detail, mission_id=mid, report=str(report or ""), steps=steps,
                            file_path=str(report or ""))
        if status == "failed":
            res.success = False
        return res

    def verify(self, args, result, ctx):
        rp = result.data.get("report")
        return bool(rp and Path(rp).is_file()) if result.success else None


def _progress(ctx, text: str) -> None:
    try:
        from ..core.events import EventType

        ctx.events.emit(EventType.NOTIFICATION, title="Genesis mission", message=text, quiet=True)
    except Exception:
        pass


def doc_gaps(root: Path, st: dict) -> list[str]:
    gaps = []
    if not st.get("readme"):
        gaps.append("No README at the top level - add one with purpose, setup and usage.")
    else:
        text = (root / st["readme"]).read_text(encoding="utf-8", errors="ignore").lower()
        for need, hint in (("install", "installation / setup steps"), ("usage", "usage instructions"),
                           ("requirement", "requirements / dependencies")):
            if need not in text:
                gaps.append(f"README has no {hint}.")
    if "Python" in st.get("languages", {}) and not any((root / n).exists() for n in
                                                       ("requirements.txt", "pyproject.toml", "setup.py", "environment.yml")):
        gaps.append("Python project without requirements.txt / pyproject.toml.")
    if not any((root / n).exists() for n in ("LICENSE", "LICENSE.md", "LICENSE.txt")):
        gaps.append("No LICENSE file.")
    if not any(p.is_dir() and p.name.lower() in ("tests", "test") for p in root.iterdir()):
        gaps.append("No tests folder.")
    return gaps


def code_checks(ctx, root: Path) -> dict:
    from ..tools.code import DiagnoseProject

    res = DiagnoseProject().run({"path": str(root)}, ctx)
    issues = res.data.get("issues", []) if res.data else []
    return {"ok": res.success, "message": res.message.split("\n")[0], "issues": issues[:40]}


def write_report(ctx, name: str, root: Path, objective: str, f: dict, steps: list[dict]) -> str:
    from .work import build_docx, out_dir, slug, unique

    st, g = f.get("structure") or {}, f.get("git") or {}
    secs = [("Objective", f"{objective}\nProject folder: {root}\nChecked: {datetime.now():%d %B %Y %H:%M}")]
    secs.append(("Project overview",
                 f"{st.get('files', '?')} files. Languages: " +
                 (", ".join(f"{k} ({v})" for k, v in st.get("languages", {}).items()) or "none detected") +
                 (f"\nREADME: {st['readme_excerpt']}" if st.get("readme_excerpt") else "")))
    secs.append(("Documentation gaps", "\n".join("• " + x for x in (f.get("docs") or [])) or "None found."))
    if g.get("git"):
        secs.append(("Version control", f"Branch {g.get('branch')}; {len(g.get('uncommitted', []))} uncommitted change(s)."
                     + ("\n" + "\n".join(f"• {c['date']}  {c['subject']}" for c in g.get("commits", [])) if g.get("commits") else "")))
    else:
        secs.append(("Version control", g.get("reason", "Not checked.")))
    pr = f.get("problems") or {}
    secs.append(("Code problems (read-only checks)",
                 "\n".join(f"• {i.get('path', '')}:{i.get('line', '')}  {i.get('message', '')}" for i in pr.get("issues", []))
                 or (pr.get("message") or "No problems found.")))
    td = f.get("todos") or []
    secs.append(("Unfinished work", "\n".join(f"• [{t['tag']}] {t['file']}:{t['line']}  {t['text']}" for t in td)
                 or "No TODO/FIXME notes."))
    nxt = []
    if pr.get("issues"):
        nxt.append(f"Fix the {len(pr['issues'])} code problem(s) first - they can break the project.")
    nxt += [x for x in (f.get("docs") or [])[:3]]
    if g.get("uncommitted"):
        nxt.append(f"Commit the {len(g['uncommitted'])} uncommitted change(s) so nothing is lost.")
    if td:
        nxt.append(f"Resolve or schedule the {len(td)} TODO note(s).")
    secs.append(("Recommended next actions", "\n".join("• " + x for x in nxt) or "Nothing urgent found."))
    secs.append(("Mission log", "\n".join(f"• {s_['step']}: {s_['status']}" + (f" ({s_['detail']})" if s_.get('detail') else "")
                                          for s_ in steps)))
    path = unique(out_dir(ctx, "Genesis Documents") / f"{slug(name)} - project report.docx")
    build_docx(path, f"{name} - project report", secs, "report")
    return str(path)


class EmergencyStop(Tool):
    name = "emergency_stop"
    category = "system"
    description = "Stop everything Genesis is doing right now: speech, running missions and follow-up steps."
    parameters = obj({})

    def run(self, args, ctx):
        control.emergency_stop()
        return ToolResult.ok("Stopped. Nothing else will run until you ask.")


class SetMode(Tool):
    name = "conversation_mode"
    category = "settings"
    description = "Switch Genesis's conversation mode: casual, coding, study, productivity, supportive or mission."
    parameters = obj({"mode": {"type": "string", "enum": ["casual", "coding", "study", "productivity", "supportive",
                                                           "mission", "status"]}}, ["mode"])

    def run(self, args, ctx):
        from .modes import MODES, Conversation
        from .provider import find_provider, runtime

        prov = find_provider(ctx)
        conv = prov.conversation if prov is not None else Conversation(runtime(getattr(ctx, "config", None))["store"])
        if args["mode"] == "status":
            return ToolResult.ok(f"I'm in {MODES[conv.mode][0].lower()} mode.", mode=conv.mode)
        conv.set_mode(args["mode"])
        if prov is not None:
            prov.router.mode = conv.mode
        return ToolResult.ok(f"{MODES[conv.mode][0]} mode on.", mode=conv.mode)


class RegisterSpecialist(Tool):
    name = "register_specialist_model"
    category = "ai"
    description = ("Register a specialised local model (e.g. a TwinSim SLM already in Ollama) and the skills it is good "
                   "at; Genesis then uses it only for requests that mention those skills. Weights are never changed.")
    parameters = obj({"name": s("e.g. TwinSim", maxLength=60), "model": s("Ollama model name", maxLength=120),
                      "skills": s("comma-separated skills, e.g. 'twinsim simulation, sensor data'", maxLength=400)},
                     ["name", "model", "skills"])

    def run(self, args, ctx):
        from .provider import find_provider, runtime

        rt = runtime(getattr(ctx, "config", None))
        skills = [x.strip() for x in args["skills"].split(",") if x.strip()]
        if rt["client"].available() and args["model"] not in rt["client"].installed_names():
            return ToolResult.fail(f"The model {args['model']} isn't in Ollama.",
                                   hint="Import it first, e.g. `ollama create twinsim -f Modelfile` with FROM pointing "
                                        "at your TwinSim GGUF file (this doesn't change the weights).")
        rt["store"].add_specialist(args["name"], args["model"], skills)
        prov = find_provider(ctx)
        if prov is not None:
            prov.router.specialists = rt["store"].specialists()
        return ToolResult.ok(f"Registered {args['name']} ({args['model']}) for: {', '.join(skills)}.")


TOOLS = [ProjectActivity(), ProjectChanges(), ProjectSummary(), ProjectTodos(), WhatToWorkOn(), MissionProjectReport(),
         EmergencyStop(), SetMode(), RegisterSpecialist()]
