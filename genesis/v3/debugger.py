"""Autonomous debugging loop (3.2): "Genesis, fix the failing tests in my TwinSim project".

Built from the coding tools Genesis already has - nothing is duplicated:
* ``detect_test_command`` / the project's own test runner (pytest, unittest, npm test) in the project's own .venv,
* ``parse_error`` to find the failing file and line in the output,
* ``ProposeFix`` to have the local model write the smallest change (validated: applies exactly, still parses),
* ``safety_scan`` to reject fixes that add shell commands, deletion, credential access …,
* backups in data\\backups like ``apply_patch``.

The loop (one confirmation covers the session, because every step is reversible):
 1. run the tests -> baseline (if they already pass, stop);
 2. locate the failure -> propose the smallest fix -> safety check -> back up -> apply;
 3. run the tests again -> **better**: keep; **same or worse**: restore the backup;
 4. repeat up to ``max_attempts`` (default 3);
 5. report exactly what was fixed, what the tests say now, and what is still failing. "Fixed" is only said when the test
    command actually passes. Every change is logged (data\\code_changes.jsonl) and "undo the last fixes" restores them.

Only folders inside Genesis's allowed roots (registered projects, home folders) are touched; .env / key files never are.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from ..security.permissions import RiskLevel
from ..tools.base import Tool, ToolResult, obj, s
from . import control

SECRET_NAMES = re.compile(r"(^\.env|secret|credential|\.pem$|\.key$|id_rsa|token)", re.I)
COUNT_PATTERNS = [
    (re.compile(r"(\d+) failed"), 1), (re.compile(r"(\d+) errors?\b"), 1),
    (re.compile(r"FAILED \((?:failures=(\d+))?(?:, )?(?:errors=(\d+))?"), 2),
    (re.compile(r"Tests:\s+(\d+) failed"), 1),
]


def failure_count(output: str, returncode: int) -> int:
    """How many tests fail, from pytest / unittest / jest summaries (1 if it failed but no count is printed)."""
    if returncode == 0:
        return 0
    best = 0
    for rx, groups in COUNT_PATTERNS:
        for m in rx.finditer(output or ""):
            n = sum(int(g) for g in m.groups()[:groups] if g)
            best = max(best, n)
    return best or 1


def touch_changed(path: Path, old_mtime: float) -> None:
    """Make sure Python sees the edit: a same-size change in the same second would otherwise reuse the stale .pyc."""
    import os

    new = max(time.time(), old_mtime + 2)
    os.utime(path, (new, new))
    cache = path.parent / "__pycache__"
    if cache.is_dir():
        for pyc in cache.glob(path.stem + ".*.pyc"):
            try:
                pyc.unlink()
            except OSError:
                pass


def run_suite(cmd: list[str], root: Path, timeout: int) -> dict:
    import os

    t0 = time.monotonic()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        p = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=timeout, shell=False, env=env,
                           encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (p.stdout or "") + "\n" + (p.stderr or "")
        return {"ok": p.returncode == 0, "code": p.returncode, "output": out, "failed": failure_count(out, p.returncode),
                "ms": int((time.monotonic() - t0) * 1000)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": None, "output": f"(the tests took longer than {timeout} s)", "failed": 10 ** 6,
                "ms": timeout * 1000}


def locate(output: str, root: Path) -> Optional[dict]:
    """The failing place: prefer the deepest frame in the project that isn't a test file (the code under test)."""
    from ..services.coderunner import parse_error

    frames = []
    for m in re.finditer(r'File "(?P<path>[^"]+)", line (?P<line>\d+)', output or ""):
        p = Path(m.group("path"))
        p = p if p.is_absolute() else root / p
        try:
            p.resolve().relative_to(root.resolve())
        except (ValueError, OSError):
            continue
        frames.append({"path": str(p.resolve()), "line": int(m.group("line"))})
    for m in re.finditer(r"^(?P<path>[\w./\\-]+\.py):(?P<line>\d+): ", output or "", re.M):  # pytest short form
        p = (root / m.group("path")).resolve()
        if p.exists():
            frames.append({"path": str(p), "line": int(m.group("line"))})
    lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip()]
    message = next((ln for ln in reversed(lines) if re.search(r"(Error|Exception|assert|FAIL)", ln)), lines[-1] if lines else "")
    if frames:
        non_test = [f for f in frames if not is_test_file(f["path"])]
        if non_test:
            return {**non_test[-1], "message": message[:300]}
        # only the test itself is in the traceback (e.g. a failed assertEqual): the bug is in the code it tests
        test = frames[-1]
        under = code_under_test(Path(test["path"]), int(test["line"]), root)
        if under is not None:
            return {"path": str(under), "line": 0, "test_path": test["path"],
                    "message": f"The test {Path(test['path']).name} line {test['line']} fails: "
                               f"{failing_line(Path(test['path']), int(test['line']))} -> {message[:200]}. "
                               "Fix the code under test (not the test)."}
        return {**test, "message": message[:300]}
    err = parse_error(output, root)
    return err if err and err.get("path") else None


def is_test_file(path: str) -> bool:
    return bool(re.search(r"(^|[\\/])(tests?)[\\/]|(^|[\\/])test_[^\\/]*\.py$|_test\.py$|\.test\.[jt]sx?$", path))


def failing_line(path: Path, line: int) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[line - 1].strip()[:200]
    except (OSError, IndexError):
        return ""


def code_under_test(test: Path, line: int, root: Path) -> Optional[Path]:
    """The project module a failing test imports - preferring the one whose names appear on the failing line."""
    try:
        src = test.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    text_line = failing_line(test, line)
    cands = []
    for m in re.finditer(r"^\s*(?:from\s+([\w.]+)\s+import\s+([\w, ()]+)|import\s+([\w.]+))", src, re.M):
        mod = m.group(1) or m.group(3)
        names = [n.strip(" ()") for n in (m.group(2) or "").split(",") if n.strip(" ()")]
        rel = Path(*mod.split("."))
        for base in (root, root / "src", test.parent):
            for f in (base / rel.with_suffix(".py"), base / rel / "__init__.py"):
                if f.is_file() and not is_test_file(str(f)):
                    score = sum(1 for n in names + [mod.split(".")[-1]] if n and n in text_line)
                    cands.append((score, f))
    if not cands:
        return None
    return max(cands, key=lambda c: c[0])[1]


def change_log(ctx) -> Path:
    return Path(ctx.config.paths.data_dir) / "code_changes.jsonl"


def log_change(ctx, entry: dict) -> None:
    try:
        with change_log(ctx).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **entry}) + "\n")
    except OSError:
        pass


class DebugProject(Tool):
    name = "debug_project"
    category = "code"
    description = ("Autonomously debug a project: run its tests, find the failing code, apply the smallest fix (with a "
                   "backup), re-run the tests, keep fixes that help and undo ones that don't, up to a retry limit; then "
                   "report what was fixed and what still fails.")
    parameters = obj({"project": s("project name", maxLength=100), "path": s("project folder", maxLength=1000),
                      "max_attempts": {"type": "integer", "minimum": 1, "maximum": 6, "default": 3},
                      "timeout_s": {"type": "integer", "minimum": 30, "maximum": 900, "default": 300}})
    risk = RiskLevel.MEDIUM
    always_confirm = True

    def confirmation_prompt(self, args):
        name = args.get("project") or Path(args.get("path") or "").name or "the project"
        return (f"I'll run {name}'s tests and try up to {args.get('max_attempts', 3)} small fixes. Every changed file is "
                "backed up, fixes that don't help are undone, and nothing outside the project is touched. Go ahead?")

    def run(self, args, ctx):
        from ..tools.code import ProposeFix, _project_dir, detect_test_command
        from ..services.coderunner import safety_scan

        control.reset()
        try:
            root = _project_dir(args, ctx)
        except Exception as exc:
            return ToolResult.fail(str(exc))
        cmd = detect_test_command(root)
        if not cmd:
            return ToolResult.fail(f"I couldn't find tests in {root.name} (no tests folder, pytest config or npm test).",
                                   hint="Say “diagnose my project” for a static check instead.")
        timeout = int(args.get("timeout_s") or 300)
        session = time.strftime("%Y%m%d-%H%M%S")
        backup_root = Path(ctx.config.paths.data_dir) / "backups" / f"debug-{session}"
        before = run_suite(cmd, root, timeout)
        if before["ok"]:
            return ToolResult.ok(f"The tests in {root.name} already pass - nothing to fix.", passed=True, attempts=[])
        attempts, current = [], before
        for n in range(1, int(args.get("max_attempts") or 3) + 1):
            if control.cancelled():
                attempts.append({"n": n, "result": "cancelled"})
                break
            where = locate(current["output"], root)
            if not where:
                attempts.append({"n": n, "result": "couldn't locate the failure in the output"})
                break
            target = Path(where["path"])
            if SECRET_NAMES.search(target.name):
                attempts.append({"n": n, "file": target.name, "result": "refused: sensitive file"})
                break
            prop = ProposeFix().run({"path": str(target), "issue": where.get("message") or "a failing test",
                                     "line": int(where.get("line") or 0)}, ctx)
            if not prop.success:
                attempts.append({"n": n, "file": target.name, "result": f"no fix: {prop.message}"})
                break
            new = prop.data["new_content"]
            danger = set(safety_scan({target.name: new})) - set(safety_scan({target.name: target.read_text(encoding='utf-8')}))
            if danger:
                attempts.append({"n": n, "file": target.name, "result": "rejected unsafe fix (" + "; ".join(sorted(danger)) + ")"})
                break
            rel = target.resolve().relative_to(root.resolve())
            backup = backup_root / str(n) / rel
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
            old_mtime = target.stat().st_mtime
            target.write_text(new, encoding="utf-8")
            touch_changed(target, old_mtime)
            after = run_suite(cmd, root, timeout)
            entry = {"n": n, "file": str(rel), "line": where.get("line"), "summary": prop.data.get("summary"),
                     "diff": prop.data.get("diff", ""), "before_failed": current["failed"], "after_failed": after["failed"]}
            if after["ok"] or after["failed"] < current["failed"]:
                entry["result"] = "kept"
                current = after
                log_change(ctx, {"session": session, "project": str(root), "file": str(rel), "backup": str(backup),
                                 "summary": entry["summary"], "kept": True})
            else:
                m = target.stat().st_mtime
                shutil.copy2(backup, target)  # didn't help -> undo
                touch_changed(target, m)
                entry["result"] = "undone (tests didn't improve)"
                log_change(ctx, {"session": session, "project": str(root), "file": str(rel), "backup": str(backup),
                                 "summary": entry["summary"], "kept": False})
            attempts.append(entry)
            if current["ok"]:
                break
        kept = [a for a in attempts if a.get("result") == "kept"]
        tail = "\n".join([ln for ln in current["output"].strip().splitlines() if ln.strip()][-8:])
        if current["ok"]:
            head = (f"Fixed, Boss: the tests in {root.name} now pass after {len(kept)} change(s): "
                    + "; ".join(f"{a['file']} - {a['summary']}" for a in kept) + ".")
        elif kept:
            head = (f"Partly fixed: {before['failed']} failing before, {current['failed']} now. I kept "
                    f"{len(kept)} change(s) that helped; the rest still fails.")
        else:
            head = f"I couldn't fix it: {before['failed']} test(s) still fail and I left your code unchanged."
        detail = "\n".join(f"  {a['n']}. {a.get('file', '')}: {a['result']}" + (f" - {a['summary']}" if a.get("summary") else "")
                           for a in attempts)
        diffs = "\n".join(a["diff"] for a in kept if a.get("diff"))
        msg = head + "\n\nAttempts:\n" + detail + ("\n\nChanges:\n```diff\n" + diffs + "```" if diffs else "") + \
            "\n\nTest output now:\n" + tail + ("\n\nSay “undo the last fixes” to restore the originals." if kept else "")
        res = ToolResult.ok(msg, passed=current["ok"], attempts=attempts, session=session, project_path=str(root),
                            failed_before=before["failed"], failed_after=current["failed"])
        if not current["ok"] and not kept:
            res.success = False
        return res


class UndoLastFixes(Tool):
    name = "undo_last_fixes"
    category = "code"
    description = "Undo the code changes Genesis kept in its last debugging session (restores the backups)."
    parameters = obj({})
    risk = RiskLevel.MEDIUM

    def confirmation_prompt(self, args):
        return "Restore every file I changed in my last debugging session?"

    def run(self, args, ctx):
        path = change_log(ctx)
        if not path.exists():
            return ToolResult.fail("I haven't changed any code yet.")
        entries = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        kept = [e for e in entries if e.get("kept")]
        if not kept:
            return ToolResult.fail("There are no kept changes to undo.")
        session = kept[-1]["session"]
        restored = []
        for e in reversed([e for e in kept if e["session"] == session]):
            target = Path(e["project"]) / e["file"]
            if Path(e["backup"]).is_file():
                m = target.stat().st_mtime if target.exists() else 0
                shutil.copy2(e["backup"], target)
                touch_changed(target, m)
                restored.append(e["file"])
        log_change(ctx, {"session": session, "undo": restored})
        return ToolResult.ok(f"Restored {len(restored)} file(s): {', '.join(dict.fromkeys(restored))}.", restored=restored)


TOOLS = [DebugProject(), UndoLastFixes()]
