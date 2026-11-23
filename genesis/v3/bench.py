"""Benchmark local conversational models on real Genesis tasks (3.1).

    .venv\\Scripts\\python -m genesis.v3.bench                      # all installed candidates
    .venv\\Scripts\\python -m genesis.v3.bench --models gemma3:4b,llama3.2:3b --label baseline

For each model (one at a time, unloaded in between so memory figures are honest):
* speed: load time, time to first token, total time, tokens/second (from Ollama's own counters);
* memory: model size in RAM and how much of it is on the GPU (Ollama /api/ps);
* quality: 8 Genesis probes scored by fixed rules - greeting, empathy, topic change, remembering an earlier message,
  JSON intent, tool call, technical explanation, and *not* inventing project activity.

Results go to logs\\benchmark-<date>-<label>.md and .json. Nothing is estimated: a probe that errors scores 0.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .agents import GENESIS_IDENTITY, SUZANNE_PROMPT
from .modes import MODES, NATURAL_RULES
from .ollama import OllamaClient

CANDIDATES = ["genesis-slm", "llama3.2:3b", "gemma3:4b", "qwen3:4b", "phi4-mini", "qwen2.5:3b", "qwen2.5:7b",
              "qwen2.5-coder:7b", "llama3.1:8b"]
STOCK = re.compile(r"^\s*(certainly|of course|great question|as an ai|i'?m here to help|absolutely)\b", re.I)
SYSTEM = "\n\n".join([GENESIS_IDENTITY, SUZANNE_PROMPT, MODES["casual"][1], NATURAL_RULES])


def _sentences(t: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?])\s+", t.strip()) if s])


def _json(t: str) -> Optional[dict]:
    m = re.search(r"\{.*\}", t or "", re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except ValueError:
        return None


Check = Callable[[str], tuple[bool, str]]


def chat(*turns: str) -> list[dict]:
    msgs = [{"role": "system", "content": SYSTEM}]
    for i, t in enumerate(turns):
        msgs.append({"role": "user" if i % 2 == 0 else "assistant", "content": t})
    return msgs


PROBES: list[tuple[str, list[dict], bool, Check]] = [
    ("greeting", chat("Hey Genesis, what's happening?"), False,
     lambda t: (len(t) < 220 and _sentences(t) <= 3 and not STOCK.match(t), "short, natural, no stock opener")),
    ("empathy", chat("Hey Genesis, what are you doing?", "Hey boss! I'm here. What's up?",
                     "I'm feeling pretty frustrated today."), False,
     lambda t: (bool(re.search(r"\?", t)) and len(t) < 400 and not re.search(r"^\s*(\d\.|-|\*)", t, re.M),
                "asks what happened, short, no list of fixes")),
    ("topic change", chat("I'm feeling frustrated.", "What happened? Want to talk about it?",
                          "Actually, forget it. Let's work on my project."), False,
     lambda t: (bool(re.search(r"\b(which|what) project\b|\bcheck\b|\blook\b", t, re.I)),
                "asks which project or offers to check - doesn't invent one")),
    ("remembers earlier message", chat("By the way, my favourite colour is teal.", "Nice, teal it is!",
                                       "Random question - what colour do I like?"), False,
     lambda t: ("teal" in t.lower(), "recalls 'teal' from earlier in the chat")),
    ("JSON intent", [{"role": "system", "content": "Classify the request. Return only JSON: {\"intent\": "
                      "\"open_app\"|\"chat\"|\"search\", \"app\": string or null}."},
                     {"role": "user", "content": "could you open notepad for me"}], True,
     lambda t: ((_json(t) or {}).get("intent") == "open_app" and "notepad" in str((_json(t) or {}).get("app", "")).lower(),
                "valid JSON, intent open_app, app notepad")),
    ("tool call", [{"role": "system", "content": "You can call tools. Tools: create_reminder(text, time), open_app(name), "
                    "web_search(query). Return only JSON: {\"tool\": name, \"args\": {...}}."},
                   {"role": "user", "content": "remind me to call mom at 6 pm"}], True,
     lambda t: ((_json(t) or {}).get("tool") == "create_reminder" and "6" in json.dumps((_json(t) or {}).get("args", {})),
                "picks create_reminder with the time")),
    ("technical", chat("Explain what a Python list comprehension is, with a short example."), False,
     lambda t: (bool(re.search(r"\[[^\]]*\bfor\b[^\]]*\]", t)), "contains a correct-looking [... for ...] example")),
    ("no invention", chat("What did I work on yesterday?"), False,
     lambda t: (bool(re.search(r"(don'?t|do not|can'?t|cannot|no) .{0,40}(know|access|see|record|information|history)|"
                               r"check|let me look", t, re.I)) and not re.search(r"\byou (worked|were working) on\b", t, re.I),
                "admits it needs to check; doesn't invent activity")),
]


def run_model(c: OllamaClient, model: str) -> dict:
    res: dict = {"model": model, "probes": [], "errors": 0}
    for other in c.loaded():
        c.unload(other["name"])
    t0 = time.perf_counter()
    first = None
    text = ""
    try:  # cold start + time to first token (streamed)
        for piece in c.stream(model, chat("Say hello in one short sentence."), max_tokens=40, temperature=0.3):
            if first is None:
                first = time.perf_counter() - t0
            text += piece
        res["cold_first_token_s"] = round(first or 0, 2)
    except Exception as exc:
        res["error"] = str(exc)[:200]
        return res
    t1 = time.perf_counter()
    first = None
    for _piece in c.stream(model, chat("Hi there, how's it going?"), max_tokens=60, temperature=0.3):
        if first is None:
            first = time.perf_counter() - t1
    res["warm_first_token_s"] = round(first or 0, 2)
    ps = {m["name"]: m for m in c.loaded()}
    mem = ps.get(model) or ps.get(model + ":latest") or next(iter(ps.values()), {})
    res["memory_gb"], res["gpu_gb"] = mem.get("size_gb"), mem.get("vram_gb")
    speeds = []
    for name, msgs, json_mode, check in PROBES:
        try:
            r = c.chat(model, msgs, temperature=0.3, max_tokens=300, json_mode=json_mode)
            ok, why = check(r.text)
            gen_ms = max(1, r.total_ms - r.load_ms)
            speeds.append(r.eval_tokens * 1000 / gen_ms)
            res["probes"].append({"probe": name, "pass": bool(ok), "rule": why, "ms": r.total_ms, "reply": r.text[:400]})
        except Exception as exc:
            res["errors"] += 1
            res["probes"].append({"probe": name, "pass": False, "rule": "error", "reply": str(exc)[:200]})
    res["score"] = sum(p["pass"] for p in res["probes"])
    res["of"] = len(PROBES)
    res["tokens_per_s"] = round(sorted(speeds)[len(speeds) // 2], 1) if speeds else None
    res["avg_reply_ms"] = int(sum(p.get("ms", 0) for p in res["probes"]) / max(1, len(res["probes"])))
    c.unload(model)
    return res


def report_md(results: list[dict], label: str, hw: str) -> str:
    lines = [f"# Genesis model benchmark - {label}", "", f"{datetime.now():%d %B %Y %H:%M} · {hw}", "",
             "| Model | Quality | Tokens/s | First token (warm) | First token (cold) | Avg reply | Memory | On GPU |",
             "|---|---|---|---|---|---|---|---|"]
    for r in results:
        if r.get("error"):
            lines.append(f"| {r['model']} | error: {r['error'][:60]} | | | | | | |")
            continue
        lines.append(f"| {r['model']} | {r['score']}/{r['of']} | {r['tokens_per_s']} | {r['warm_first_token_s']} s | "
                     f"{r['cold_first_token_s']} s | {r['avg_reply_ms']} ms | {r['memory_gb']} GB | {r['gpu_gb']} GB |")
    for r in results:
        if r.get("error"):
            continue
        lines += ["", f"## {r['model']}"] + [f"- {'PASS' if p['pass'] else 'fail'} **{p['probe']}** ({p['rule']}): "
                                             f"{p['reply'][:160]!r}" for p in r["probes"]]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="")
    ap.add_argument("--label", default="run")
    ap.add_argument("--out", default="logs")
    a = ap.parse_args(argv)
    c = OllamaClient(auto_start=True)
    if not c.ensure_running():
        print("Ollama isn't running.")
        return 1
    have = c.installed_names()
    models = [m.strip() for m in a.models.split(",") if m.strip()] or [m for m in CANDIDATES if m in have]
    models = [m for m in models if m in have or m + ":latest" in have]
    if not models:
        print("None of the models to test are installed.")
        return 1
    from .hardware import detect

    hw = detect().summary()
    results = []
    for m in models:
        print(f"Testing {m} ...", flush=True)
        r = run_model(c, m)
        results.append(r)
        print(f"  {m}: {r.get('score', 0)}/{r.get('of', len(PROBES))}, {r.get('tokens_per_s')} tok/s"
              + (f"  ERROR {r['error']}" if r.get("error") else ""), flush=True)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    tag = re.sub(r"[^\w-]", "", a.label)
    stem = out / f"benchmark-{datetime.now():%Y%m%d-%H%M}-{tag}"
    stem.with_suffix(".json").write_text(json.dumps({"label": a.label, "hardware": hw, "results": results}, indent=2),
                                         encoding="utf-8")
    stem.with_suffix(".md").write_text(report_md(results, a.label, hw), encoding="utf-8")
    print(f"\nSaved {stem}.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
