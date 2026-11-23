"""Tools for local AI and the Genesis SLM (3.0).

Read-only checks are LOW risk. Anything that downloads, rebuilds or switches models, or deletes local AI data, asks for
confirmation first.
"""
from __future__ import annotations

from pathlib import Path

from ..security.permissions import RiskLevel
from ..tools.base import Tool, ToolResult, obj, s
from . import catalog, mode
from .hardware import detect
from .provider import GenesisLocalProvider, find_provider, runtime
from .slm import SLMManager


def _rt(ctx) -> dict:
    return runtime(getattr(ctx, "config", None))


def _slm(ctx) -> SLMManager:
    rt = _rt(ctx)
    base = ctx.config.paths.data_dir if getattr(ctx, "config", None) else Path.home() / ".genesis"
    return SLMManager(rt["store"], rt["client"], Path(base) / "genesis-slm")


def _last_exchange(ctx) -> tuple[str, str]:
    """The last (user question, Genesis answer) pair, skipping the current "save that …" request."""
    repos = getattr(ctx, "repos", None)
    if repos is None:
        return "", ""
    convs = repos.conversations.list(1)
    if not convs:
        return "", ""
    msgs = repos.conversations.messages(convs[0]["id"], limit=12)
    for i in range(len(msgs) - 1, 0, -1):
        if msgs[i]["role"] == "assistant" and msgs[i - 1]["role"] == "user":
            return msgs[i - 1]["content"], msgs[i]["content"]
    return "", ""


class LocalAIStatus(Tool):
    name = "local_ai_status"
    category = "ai"
    description = ("Show the local AI setup: mode (local/hybrid), whether Ollama is running, installed and loaded "
                   "models, Suzanne's and Andrew's models, hardware (RAM, GPU memory, disk) and which model answered "
                   "last.")
    parameters = obj({})

    def run(self, args, ctx):
        prov = find_provider(ctx)
        hw = detect()
        client = prov.client if prov else _rt(ctx)["client"]
        version = client.version()
        lines = [f"Mode: {mode.mode()} - external AI {'allowed' if mode.external_ai_allowed() else 'off'}.",
                 f"Hardware: {hw.summary()}"]
        if version is None:
            lines.append("Ollama isn't running, so local models can't answer. Start the Ollama app or run "
                         "Setup_Local_AI.bat.")
        else:
            inst = sorted(client.installed_names())
            loaded = client.loaded()
            lines.append(f"Ollama {version}: {len(inst)} model(s) installed"
                         + (f" ({', '.join(n for n in inst if not n.endswith(':latest'))})" if inst else "") + ".")
            lines.append("In memory now: " + (", ".join(f"{m['name']} ({m['vram_gb']} GB on the GPU)" for m in loaded)
                                              if loaded else "nothing (models load when needed)") + ".")
        if prov:
            lines.append(f"Suzanne uses {prov.agents['suzanne'].model}; Andrew uses {prov.agents['andrew'].model}"
                         + (f"; complex tasks use {prov.router.complex_model}" if prov.router.complex_model else "")
                         + ".")
            lr = prov.last_route
            if lr:
                lines.append(f"Last answer: {lr['agent'].title()} with {lr['model'] or 'Python'} (level {lr['level']}, "
                             f"{'local' if lr.get('local', True) else 'EXTERNAL'}) - {lr['reason']}.")
        else:
            lines.append("Genesis isn't using the local AI provider (set GENESIS_LLM_PROVIDER=genesis, or run "
                         "Setup_Local_AI.bat).")
        return ToolResult.ok("\n".join(lines), hardware=hw.to_dict(), ollama=version,
                             last_route=prov.last_route if prov else None)


class LocalAIRecommend(Tool):
    name = "local_ai_recommend"
    category = "ai"
    description = "Recommend which local models suit this PC's hardware (doesn't download anything)."
    parameters = obj({})

    def run(self, args, ctx):
        hw = detect()
        rec = catalog.recommend(hw)
        msg = [f"For this PC ({hw.summary()}):",
               f"- Suzanne (conversation): {rec['suzanne']} - {catalog.BY_NAME[rec['suzanne']].note}",
               f"- Andrew (technical): {rec['andrew']} - {catalog.BY_NAME[rec['andrew']].note}",
               f"- Complex tasks: {rec['complex'] or 'Andrew' + chr(39) + 's model'}",
               f"Total download about {rec['download_gb']} GB."] + [f"Note: {n}" for n in rec["notes"]]
        return ToolResult.ok("\n".join(msg), **rec)


class LocalAIUsage(Tool):
    name = "local_ai_usage"
    category = "ai"
    description = "Show how the local models have performed this week: calls, failures, average speed."
    parameters = obj({})

    def run(self, args, ctx):
        stats = _rt(ctx)["store"].usage_stats()
        if not stats:
            return ToolResult.ok("No local model calls logged yet.", stats=[])
        rows = [f"- {r['model']} ({r['agent']}): {r['calls']} calls, {r['failures'] or 0} failed, "
                f"{r['avg_ms']} ms average" + (f", {r['tok_per_s']} tokens/s" if r['tok_per_s'] else "")
                + (f", {r['external_calls']} external" if r['external_calls'] else "") for r in stats]
        return ToolResult.ok("Local AI usage (last 7 days):\n" + "\n".join(rows), stats=stats)


class LocalAISetModel(Tool):
    name = "local_ai_set_model"
    category = "ai"
    description = "Change which installed local model an agent uses: agent = suzanne, andrew or complex."
    parameters = obj({"agent": {"type": "string", "enum": ["suzanne", "andrew", "complex"]},
                      "model": s("Ollama model name, e.g. qwen2.5:7b", maxLength=80)}, ["agent", "model"])
    risk = RiskLevel.MEDIUM

    def confirmation_prompt(self, args):
        return f"Switch {args['agent'].title()} to the local model {args['model']}?"

    def run(self, args, ctx):
        rt = _rt(ctx)
        client = rt["client"]
        model = args["model"].strip()
        if client.available() and model not in client.installed_names():
            return ToolResult.fail(f"{model} isn't installed.", hint=f"Say “download the model {model}” first.")
        rt["store"].set_agent_model(args["agent"], model)
        prov = find_provider(ctx)
        if isinstance(prov, GenesisLocalProvider):
            if args["agent"] == "complex":
                prov.router.complex_model = model
            else:
                prov.agents[args["agent"]].model = model
        return ToolResult.ok(f"{args['agent'].title()} now uses {model}.", agent=args["agent"], model=model)


class LocalAIDownload(Tool):
    name = "local_ai_download"
    category = "ai"
    description = ("Download a local model with Ollama, after checking free disk space and memory. Downloads come from "
                   "the Ollama library; nothing about the user is sent.")
    parameters = obj({"model": s("Ollama model name, e.g. llama3.2:3b", maxLength=80)}, ["model"])
    risk = RiskLevel.MEDIUM
    always_confirm = True

    def confirmation_prompt(self, args):
        info = catalog.info(args["model"])
        size = f" (about {info.download_gb} GB, licence: {info.licence})" if info else ""
        return f"Download the local model {args['model']}{size}? This can take several minutes."

    def run(self, args, ctx):
        model = args["model"].strip()
        client = _rt(ctx)["client"]
        if not client.available():
            return ToolResult.fail("Ollama isn't running.", hint="Start the Ollama app or run Setup_Local_AI.bat.")
        hw = detect()
        info = catalog.info(model)
        if info and hw.disk_free_gb is not None and hw.disk_free_gb < info.download_gb + 3:
            return ToolResult.fail(f"Not enough disk space: {model} needs about {info.download_gb} GB and only "
                                   f"{hw.disk_free_gb:.0f} GB is free.")
        warn = ""
        if info and not catalog.fits(info, hw, allow_split=True):
            warn = f" Note: it needs about {info.need_gb} GB of memory, more than is free now, so it may be slow."
        try:
            ok = client.pull(model)
        except Exception as exc:
            return ToolResult.fail(f"The download failed: {exc}")
        if not ok:
            return ToolResult.fail(f"The download of {model} didn't finish.", retryable=True)
        prov = find_provider(ctx)
        if prov:
            prov.installed(refresh=True)
        return ToolResult.ok(f"{model} is downloaded and ready.{warn}", model=model)

    def verify(self, args, result, ctx):
        return args["model"] in _rt(ctx)["client"].installed_names() if result.success else False


class LocalAIUnload(Tool):
    name = "local_ai_unload"
    category = "ai"
    description = "Free memory by unloading local models (all of them, or one by name)."
    parameters = obj({"model": s("model name; empty = all", maxLength=80)})

    def run(self, args, ctx):
        client = _rt(ctx)["client"]
        names = [args["model"]] if args.get("model") else [m["name"] for m in client.loaded()]
        if not names:
            return ToolResult.ok("No local model is in memory.")
        done = [n for n in names if client.unload(n)]
        return ToolResult.ok(f"Unloaded {', '.join(done)}." if done else "Nothing was unloaded.", unloaded=done)


# ------------------------------------------------------------------ Genesis SLM
class SLMApprove(Tool):
    name = "slm_approve_answer"
    category = "ai"
    description = ("Save an approved example for training the Genesis SLM. Without arguments it saves the last "
                   "question and Genesis's answer; with 'answer' it saves the user's corrected answer instead.")
    parameters = obj({"prompt": s("question", maxLength=4000), "answer": s("the approved/corrected answer",
                                                                            maxLength=8000)})

    def run(self, args, ctx):
        last_q, last_a = _last_exchange(ctx)
        prompt = (args.get("prompt") or last_q).strip()
        answer = (args.get("answer") or last_a).strip()
        if not prompt or not answer:
            return ToolResult.fail("There's no recent answer to save yet.")
        mgr = _slm(ctx)
        ex_id = mgr.correct(prompt, answer) if args.get("answer") else mgr.approve(prompt, answer)
        n = len(mgr.store.examples())
        return ToolResult.ok(f"Saved as a training example for the Genesis SLM ({n} approved so far). Nothing is "
                             "retrained until you ask me to build a new version.", id=ex_id, total=n)


class SLMStatus(Tool):
    name = "slm_status"
    category = "ai"
    description = "Show the Genesis SLM: approved examples, versions with their evaluation scores, and the active one."
    parameters = obj({})

    def run(self, args, ctx):
        st = _slm(ctx).status()
        lines = [f"Approved training examples: {st['examples']}."]
        if not st["versions"]:
            lines.append("No Genesis SLM has been built yet. Say “build the Genesis SLM” to create version 1.")
        for v in st["versions"]:
            lines.append(f"- v{v['version']}: {v['method']} on {v['base_model']}, {v['examples']} examples, score "
                         f"{v['score'] if v['score'] is not None else 'not evaluated'}"
                         + (" (active)" if v["active"] else ""))
        return ToolResult.ok("\n".join(lines), **{k: st[k] for k in ("examples", "versions", "active")})


class SLMBuild(Tool):
    name = "slm_build"
    category = "ai"
    description = ("Build a new Genesis SLM version locally from an open-weight base model plus Genesis's identity and "
                   "the approved examples, then evaluate it. It is not activated until the user says so.")
    parameters = obj({"base": s("installed base model; default = Suzanne's model", maxLength=80),
                      "adapter": s("folder of a LoRA adapter made by scripts/train_genesis_slm.py", maxLength=500)})
    risk = RiskLevel.MEDIUM
    always_confirm = True

    def confirmation_prompt(self, args):
        return (f"Build a new Genesis SLM version from {args.get('base') or 'Suzanne’s model'}"
                + (" with your trained adapter" if args.get("adapter") else "") + "? It won't replace the active one.")

    def run(self, args, ctx):
        prov = find_provider(ctx)
        base = args.get("base") or (prov.agents["suzanne"].model if prov else "llama3.2:3b")
        mgr = _slm(ctx)
        res = mgr.register_adapter(base, args["adapter"]) if args.get("adapter") else mgr.build(base)
        if not res["ok"]:
            return ToolResult.fail(f"Building the Genesis SLM failed: {res['detail']}")
        rep = mgr.evaluate_version(res["version"]) or {}
        return ToolResult.ok(f"Built {res['model']} (version {res['version']}); evaluation score {rep.get('score')}%. "
                             f"Say “use Genesis SLM version {res['version']}” to make it active.",
                             version=res["version"], evaluation=rep)


class SLMActivate(Tool):
    name = "slm_activate"
    category = "ai"
    description = "Make a Genesis SLM version the active one (also used to roll back to an earlier version)."
    parameters = obj({"version": {"type": "integer"}}, ["version"])
    risk = RiskLevel.MEDIUM
    always_confirm = True

    def confirmation_prompt(self, args):
        return f"Switch the Genesis SLM to version {args['version']}?"

    def run(self, args, ctx):
        res = _slm(ctx).activate(int(args["version"]))
        return ToolResult.ok(res["detail"]) if res["ok"] else ToolResult.fail(res["detail"])


class SLMExportDataset(Tool):
    name = "slm_export_dataset"
    category = "ai"
    description = "Write the approved examples as a JSONL training file for scripts/train_genesis_slm.py."
    parameters = obj({})

    def run(self, args, ctx):
        path, n = _slm(ctx).export_dataset()
        if not n:
            return ToolResult.fail("There are no approved examples yet.",
                                   hint="After a good answer, say “save that as a good answer”.")
        return ToolResult.ok(f"Exported {n} approved examples to {path}.", path=str(path), examples=n)


class LocalAIDataExport(Tool):
    name = "local_ai_export"
    category = "ai"
    description = "Export the local AI data (usage log, summaries, approved examples, SLM versions) to a JSON file."
    parameters = obj({})

    def run(self, args, ctx):
        base = ctx.config.paths.data_dir if getattr(ctx, "config", None) else Path.home()
        p = _rt(ctx)["store"].export_json(Path(base) / "genesis_local_export.json")
        return ToolResult.ok(f"Exported the local AI data to {p}.", path=str(p))


class LocalAIDataWipe(Tool):
    name = "local_ai_wipe"
    category = "ai"
    description = "Delete the local AI data: usage log, conversation summaries and approved training examples."
    parameters = obj({"include_versions": {"type": "boolean", "default": False}})
    risk = RiskLevel.HIGH
    always_confirm = True

    def confirmation_prompt(self, args):
        return ("Delete the local AI usage log, summaries and approved training examples"
                + (" and the Genesis SLM version history" if args.get("include_versions") else "")
                + "? This can't be undone.")

    def run(self, args, ctx):
        _rt(ctx)["store"].wipe(keep_versions=not args.get("include_versions"))
        return ToolResult.ok("Deleted the local AI data.")


TOOLS = [LocalAIStatus(), LocalAIRecommend(), LocalAIUsage(), LocalAISetModel(), LocalAIDownload(), LocalAIUnload(),
         SLMApprove(), SLMStatus(), SLMBuild(), SLMActivate(), SLMExportDataset(), LocalAIDataExport(),
         LocalAIDataWipe()]

from .work import TOOLS as _WORK_TOOLS  # noqa: E402  (3.0.1: Wikipedia, documents, Excel, code, digital twin)

TOOLS += _WORK_TOOLS

from .projects import TOOLS as _PROJECT_TOOLS  # noqa: E402  (3.1: projects, Mission Mode, modes, stop, specialists)

TOOLS += _PROJECT_TOOLS

from .debugger import TOOLS as _DEBUG_TOOLS  # noqa: E402  (3.2: autonomous debugging loop)

TOOLS += _DEBUG_TOOLS

from .business import TOOLS as _BUSINESS_TOOLS  # noqa: E402  (3.4 Business Automation)

TOOLS += _BUSINESS_TOOLS
from .coding import TOOLS as _CODING_TOOLS  # noqa: E402  (3.5 GACE + mute)

TOOLS += _CODING_TOOLS
