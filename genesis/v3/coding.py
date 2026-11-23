"""GACE tools and commands (3.5): build, resume, roll back and inspect projects; mute the mic or the speaker.

"Build me a complete expense tracker app with a FastAPI backend and a React frontend" becomes: plan -> files in an
approved workspace -> (confirmed) dependency install -> tests -> diagnose/repair -> a report that says exactly what
was and wasn't verified. Nothing outside the workspace is touched; every repair is checkpointed and can be rolled back.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable, Optional

from ..core.logging_setup import get_logger
from ..security.permissions import RiskLevel
from ..security.validators import ValidationError, validate_path
from ..tools.base import Tool, ToolResult, obj, s
from . import control, privacy
from .gace.engine import Engine, Limits, slug
from .gace.memory import ProjectMemory
from .gace.workspace import Workspace, WorkspaceError

log = get_logger("v3.coding")
LISTENERS: list[Callable[[dict], None]] = []  # the Coding page listens here for live progress
LAST: dict = {}
UNPLANNED = "wasn't in the plan"


def _publish(e: dict) -> None:
    LAST.setdefault("events", []).append(e)
    del LAST["events"][:-300]
    for fn in list(LISTENERS):
        try:
            fn(e)
        except Exception:
            pass


def coding_root(ctx) -> Path:
    custom = ctx.settings.get("coding.workspace", "") or ""
    return Path(custom).expanduser() if custom else Path.home() / "GenesisProjects"


def approved(ctx) -> list[Path]:
    return [coding_root(ctx).resolve()] + [Path(p).resolve() for p in ctx.allowed_roots()]


def workspace_for(ctx, name: str = "", folder: str = "", create: bool = True) -> Workspace:
    if folder:
        p = validate_path(folder, allowed_roots=approved(ctx), must_exist=False, for_write=True)
        return Workspace(Path(p), approved(ctx))
    base = coding_root(ctx)
    if not name:
        raise ValidationError("Which project? Give its name or folder.")
    cand = base / slug(name)
    if not cand.exists() and not create:
        found = [d for d in base.glob("*") if d.is_dir() and slug(name) in d.name.lower()] if base.exists() else []
        if found:
            cand = found[0]
    return Workspace(cand, approved(ctx))


def _ask(ctx):
    from .biz import common

    def ask(prompt, system="", max_tokens=1500, purpose="gace"):
        return common.ask(ctx, prompt, system=system, max_tokens=max_tokens, temperature=0.2, purpose=purpose, cache=False)
    return ask


def make_engine(ctx, ws: Workspace, limits: Optional[Limits] = None) -> Engine:
    # one confirmation (given before the tool ran) covers the packages named in the plan; an unplanned package is refused
    return Engine(ws, _ask(ctx), lambda msg: UNPLANNED not in msg, on_event=_publish, limits=limits, cancelled=control.cancelled)


def _message(r) -> str:
    head = {"verified": "Done, Boss. The project is built and verified.", "partial": "Built, but only partly complete.",
            "not_verified": "I built it, but I couldn't verify that it works.", "cancelled": "Stopped.",
            "needs_info": "I need a little more information first.", "failed": "I couldn't build that."}.get(r.status, r.status)
    lines = [head, r.summary]
    if r.questions:
        lines += ["Questions: " + " ".join(f"({i + 1}) {q}" for i, q in enumerate(r.questions))]
    if r.files:
        lines.append(f"Files ({len(r.files)}): " + ", ".join(r.files[:12]) + ("…" if len(r.files) > 12 else ""))
    kept = [x for x in r.repairs if x["result"] == "kept"]
    if r.repairs:
        lines.append(f"Repairs: {len(kept)} kept of {len(r.repairs)} tried.")
    m = r.metrics or {}
    if m:
        lines.append(f"{m.get('seconds')}s, {m.get('model_calls')} model calls, {r.tests_collected} tests run.")
    lines.append(f"Folder: {r.workspace}" + ("\nReport: GENESIS_BUILD_REPORT.md" if r.report else ""))
    return "\n".join(x for x in lines if x)


def _result(r) -> ToolResult:
    data = {"status": r.status, "workspace": r.workspace, "files": r.files, "metrics": r.metrics, "repairs": r.repairs,
            "tests_collected": r.tests_collected, "questions": r.questions, "file_path": r.workspace}
    msg = _message(r)
    return ToolResult.ok(msg, **data) if r.status in ("verified", "partial", "needs_info") else ToolResult.fail(msg, **data)


def _open(ctx, path: str) -> None:
    try:
        ctx.backend.open_path(path)
    except Exception:
        pass


class _Gace(Tool):
    category = "coding"

    def verify(self, args, result, ctx):
        return (result.data or {}).get("status") == "verified" if result.success else None


class GaceBuild(_Gace):
    name = "gace_build"
    description = ("Build a complete software project from a description: plan, write all files in the coding workspace, "
                   "install the planned packages (after confirmation), run the tests, diagnose and repair failures, and "
                   "report honestly what was verified. Use for 'build a complete … app/website/API/dashboard'.")
    parameters = obj({"request": s("What to build (the full description)", maxLength=6000),
                      "name": s("Project folder name", maxLength=60), "folder": s("Existing or new folder inside an approved workspace", maxLength=500),
                      "max_repairs": {"type": "integer", "minimum": 0, "maximum": 15, "default": 8}}, ["request"])
    risk = RiskLevel.MEDIUM
    always_confirm = True

    def confirmation_prompt(self, args):
        nm = args.get("name") or slug(args.get("request", "")[:40])
        return (f"I'll build “{nm}” in your coding workspace: plan it, write the files, install the packages the plan needs "
                f"(downloads from the internet), run the tests and repair failures (up to {args.get('max_repairs', 8)} fixes). "
                "I only touch that folder, and I keep checkpoints so you can roll back. Go ahead?")

    def run(self, args, ctx):
        control.reset()
        LAST.clear()
        try:
            name = args.get("name") or ""
            if not name and not args.get("folder"):
                name = slug(re.sub(r"^(build|create|make|develop)\s+(me\s+)?(a|an|the)?\s*(complete|full|whole)?\s*", "", args["request"], flags=re.I)[:40])
            ws = workspace_for(ctx, name, args.get("folder") or "")
        except (ValidationError, WorkspaceError) as exc:
            return ToolResult.fail(str(exc))
        eng = make_engine(ctx, ws, Limits(max_repairs=int(args.get("max_repairs", 8))))
        res = eng.build(args["request"])
        LAST["result"] = res.to_dict()
        if res.status in ("verified", "partial"):
            _open(ctx, str(ws.root))
        return _result(res)


class GaceResume(_Gace):
    name = "gace_resume"
    description = ("Continue an unfinished Genesis-built project: check memory against the real files, write only the missing "
                   "files, run the tests again and repair. Files you changed yourself are left alone.")
    parameters = obj({"project": s("Project name", maxLength=60), "folder": s("Project folder", maxLength=500)})
    risk = RiskLevel.MEDIUM
    always_confirm = True

    def confirmation_prompt(self, args):
        return (f"I'll continue “{args.get('project') or args.get('folder') or 'the project'}”: write any missing files, "
                "install the planned packages if needed, run the tests and repair failures. Files you changed stay as they are. Go ahead?")

    def run(self, args, ctx):
        control.reset()
        LAST.clear()
        try:
            ws = workspace_for(ctx, args.get("project") or "", args.get("folder") or "", create=False)
        except (ValidationError, WorkspaceError) as exc:
            return ToolResult.fail(str(exc))
        if not (ws.state / "project.json").exists():
            return ToolResult.fail(f"{ws.root.name} wasn't built by Genesis (no project memory), so there's nothing to resume.")
        res = make_engine(ctx, ws).resume()
        LAST["result"] = res.to_dict()
        return _result(res)


class GaceStatus(_Gace):
    name = "gace_status"
    description = "Show a Genesis-built project's state: remembered files vs the real files, last result, checkpoints, review findings."
    parameters = obj({"project": s("Project name", maxLength=60), "folder": s("Project folder", maxLength=500)})
    risk = RiskLevel.LOW

    def run(self, args, ctx):
        try:
            ws = workspace_for(ctx, args.get("project") or "", args.get("folder") or "", create=False)
        except (ValidationError, WorkspaceError) as exc:
            return ToolResult.fail(str(exc))
        mem = ProjectMemory(ws)
        if not mem.data.get("spec"):
            return ToolResult.fail(f"{ws.root.name} has no Genesis project memory.")
        a = mem.audit()
        last = (mem.data.get("history") or [{}])[-1]
        msg = (f"{ws.root.name}: last result {last.get('status', 'unknown')}. {len(a['ok'])} files match my notes, "
               f"{len(a['changed'])} changed since (I trust the files, not my notes, for those), {len(a['missing'])} missing. "
               f"{len(ws.checkpoints())} checkpoints.")
        return ToolResult.ok(msg, audit=a, checkpoints=ws.checkpoints(), status=last.get("status"))


class GaceRollback(_Gace):
    name = "gace_rollback"
    description = "Roll a Genesis-built project back to a checkpoint (default: the latest). Restores changed/deleted files and removes files Genesis added since."
    parameters = obj({"project": s("Project name", maxLength=60), "folder": s("Project folder", maxLength=500),
                      "checkpoint": s("Checkpoint id (default: latest)", maxLength=80)})
    risk = RiskLevel.HIGH
    always_confirm = True

    def confirmation_prompt(self, args):
        return (f"I'll roll “{args.get('project') or args.get('folder') or 'the project'}” back to "
                f"{args.get('checkpoint') or 'the latest checkpoint'}. Files Genesis changed are restored and files Genesis added since "
                "are removed; your own files are not deleted. Go ahead?")

    def run(self, args, ctx):
        try:
            ws = workspace_for(ctx, args.get("project") or "", args.get("folder") or "", create=False)
            cps = ws.checkpoints()
            if not cps:
                return ToolResult.fail("There are no checkpoints for that project.")
            cid = args.get("checkpoint") or cps[-1]
            ws.checkpoint("before-rollback")
            out = ws.rollback(cid, confirm=True)
        except (ValidationError, WorkspaceError) as exc:
            return ToolResult.fail(str(exc))
        return ToolResult.ok(f"Rolled back to {cid}: {len(out['restored'])} file(s) restored, {len(out['removed'])} removed. "
                             "A checkpoint of the state before the rollback was saved.", **out, checkpoint=cid)


class MicMute(Tool):
    name = "privacy_mic"
    category = "privacy"
    description = "Mute or unmute the microphone (really stops capture and wake-word detection) or toggle it."
    parameters = obj({"action": s("mute | unmute | toggle", enum=["mute", "unmute", "toggle"])}, ["action"])
    risk = RiskLevel.LOW

    def run(self, args, ctx):
        g = privacy.GATE
        a = args["action"]
        snap = g.toggle_mic("command") if a == "toggle" else g.set_mic(a == "mute", "command")
        return ToolResult.ok("Microphone muted. I'm not listening and nothing is recorded until you unmute me." if snap["mic_muted"]
                             else "Microphone is on again.", **snap)

    def verify(self, args, result, ctx):
        return bool((result.data or {}).get("mic_muted")) == (args["action"] == "mute") if args["action"] != "toggle" else True


class SpeakerMute(Tool):
    name = "privacy_speaker"
    category = "privacy"
    description = "Mute or unmute Genesis's voice. The microphone is not affected."
    parameters = obj({"action": s("mute | unmute | toggle", enum=["mute", "unmute", "toggle"])}, ["action"])
    risk = RiskLevel.LOW

    def run(self, args, ctx):
        g = privacy.GATE
        a = args["action"]
        snap = g.toggle_speaker("command") if a == "toggle" else g.set_speaker(a == "mute", "command")
        return ToolResult.ok("Speaker muted. I'll reply in text only." if snap["speaker_muted"] else "I can speak again.", **snap)


TOOLS = [GaceBuild(), GaceResume(), GaceStatus(), GaceRollback(), MicMute(), SpeakerMute()]

# ------------------------------------------------------------------ natural-language commands
_PROJECT_NOUN = (r"(?:app|application|web ?app|web ?site|website|api|backend|back-end|frontend|front-end|dashboard|platform|"
                 r"system|project|management system|tracker|portal|game|bot|service|tool|software)")
_BIG = re.compile(r"\b(complete|completely|full|full-?stack|whole|entire|end[- ]to[- ]end|production[- ]ready|from scratch|autonomous(?:ly)?)\b", re.I)
_STACK = re.compile(r"\b(backend|back-end|frontend|front-end|database|sqlite|postgres(?:ql)?|mysql|mongodb|authentication|auth|login|"
                    r"react|vue|angular|next\.?js|fastapi|django|flask|spring|node(?:\.?js)?|express|tests?|unit tests|responsive|rest api|"
                    r"analytics dashboard|typescript)\b", re.I)
_NOT_CODE = re.compile(r"\b(github|repo|repository|calendar|event|meeting|reminder|slack|whatsapp|youtube|folder|file|excel|spreadsheet|"
                       r"workbook|csv|word doc|document|report|invoice|presentation|ppt|slides?|resume|letter|email|e-mail|pdf|poster|"
                       r"image|picture|video|song|story|essay)\b", re.I)


def match(text: str):
    t = " ".join((text or "").strip().split()).rstrip(".!")
    t = re.sub(r"^(?:hey |ok |okay )?(?:genesis|suzanne|andrew)[, ]+", "", t, flags=re.I)
    t = re.sub(r"^(?:please |can you |could you |i want you to |i need you to |would you )+", "", t, flags=re.I)
    # only the explicit forms: "turn off the microphone" / "mute your voice" keep their existing meaning (voice settings)
    m = re.match(r"^(mute|unmute)(?: my| the) (?:mic|microphone|mike)$", t, re.I)
    if m:
        return "privacy_mic", {"action": m.group(1).lower(), "_hard": True}
    m = re.match(r"^(mute|unmute)(?: the| my) speakers?$", t, re.I)
    if m:
        return "privacy_speaker", {"action": m.group(1).lower(), "_hard": True}
    m = re.match(r"^(?:roll ?back|undo|restore)(?: the| my)? (?P<p>[\w\- ]+?)? ?(?:project|app)(?: (?:to|from) (?P<c>[\w\-]+))?$", t, re.I)
    if m:
        a = {"_hard": True}
        if m.group("p"):
            a["project"] = m.group("p").strip()
        if m.group("c"):
            a["checkpoint"] = m.group("c")
        return "gace_rollback", a
    m = re.match(r"^(?:continue|resume|finish)(?: building| working on)?(?: the| my)? (?P<p>[\w\- ]+?) (?:project|app|application)$", t, re.I)
    if m:
        return "gace_resume", {"project": m.group("p").strip(), "_hard": True}
    m = re.match(r"^(?:show |what'?s )?(?:the )?(?:status|state|build report)(?: of| for)?(?: the| my)? (?P<p>[\w\- ]+?) (?:project|app|application)$", t, re.I)
    if m:
        return "gace_status", {"project": m.group("p").strip(), "_hard": True}
    m = re.match(rf"^(?:build|create|develop|make|code|generate|write)(?: me)?(?: a| an| the)? (?P<r>.*\b{_PROJECT_NOUN}\b.*)$", t, re.I)
    big = bool(_BIG.search(t)) or len({x.lower() for x in _STACK.findall(t)}) >= 2
    if m and big and not _NOT_CODE.search(t) and len(t.split()) >= (5 if _BIG.search(t) else 7):
        return "gace_build", {"request": t, "_hard": True}
    return None
