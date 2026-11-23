"""``python -m genesis --benchmark-llm`` — time real requests against a few chat models and recommend the fastest.

Speed on Hugging Face depends on the model *and* on which provider currently serves it, so the only honest way to
choose is to measure with your own token, from your own network. Each model gets a warm-up request (not counted) and
then the same two short, realistic requests; the median time is reported. Nothing is changed automatically — the
command prints the one ``.env`` line to use. The API key is never printed.
"""
from __future__ import annotations

import statistics
import time
from dataclasses import replace
from typing import Optional

# non-"thinking" instruction models on HF Inference Providers that work well with Genesis's prompts
CANDIDATES = [
    "meta-llama/Llama-3.3-70B-Instruct",
    "Qwen/Qwen3-Next-80B-A3B-Instruct",
    "Qwen/Qwen2.5-72B-Instruct",
    "Qwen/Qwen3-235B-A22B-Instruct-2507",
    "meta-llama/Llama-3.1-8B-Instruct",
]
PROMPTS = [
    [{"role": "system", "content": "You are a concise desktop assistant. Answer in one or two sentences."},
     {"role": "user", "content": "What is the difference between a list and a tuple in Python?"}],
    [{"role": "user", "content": 'Reply with JSON only: {"intent": "...", "app": "..."} for the request "open vs code please".'}],
]


def _time_model(cfg, model: str, *, rounds: int = 1) -> dict:
    from .ai.llm.base import LLMError
    from .ai.llm.factory import build_provider

    prov = build_provider(replace(cfg, hf_model_id=model))
    prov.retries = 0  # measure the model, not the retry policy
    out = {"model": model, "ok": False, "median_s": None, "error": None}
    try:
        prov.chat([{"role": "user", "content": "Reply with the single word: OK"}], max_tokens=5)  # warm-up (connection)
        times = []
        for _ in range(rounds):
            for i, msgs in enumerate(PROMPTS):
                t = time.perf_counter()
                prov.chat(msgs, max_tokens=120, json_mode=(i == 1))
                times.append(time.perf_counter() - t)
        out.update(ok=True, median_s=round(statistics.median(times), 2))
    except LLMError as exc:
        out["error"] = getattr(exc, "kind", None) or "error"
    except Exception as exc:  # pragma: no cover - network oddities
        out["error"] = type(exc).__name__
    return out


def run_benchmark(models: Optional[list[str]] = None) -> int:
    from .core.config import GenesisConfig

    cfg = GenesisConfig.from_env()
    if cfg.llm_provider != "huggingface":
        print(f"Your AI provider is '{cfg.llm_provider}'. The model benchmark compares Hugging Face models; "
              "use --check-llm to see the latency of your current model.")
        return 1
    if not cfg.hf_token:
        print("No Hugging Face token found — add HF_TOKEN=hf_… to your .env first (then run this again).")
        return 2
    current = cfg.hf_model_id
    models = models or ([current] + [m for m in CANDIDATES if m != current])
    print(f"Timing {len(models)} models with your token (current: {current}). This takes about a minute…\n")
    results = []
    for m in models:
        r = _time_model(cfg, m)
        results.append(r)
        status = f"{r['median_s']:.2f} s" if r["ok"] else f"not usable ({r['error']})"
        mark = "  ← current" if m == current else ""
        print(f"  {m:<44} {status}{mark}", flush=True)
    ok = sorted((r for r in results if r["ok"]), key=lambda r: r["median_s"])
    if not ok:
        print("\nNo model answered. Run  python -m genesis --check-llm  to see why.")
        return 2
    best = ok[0]
    cur = next((r for r in results if r["model"] == current and r["ok"]), None)
    print()
    if cur and best["model"] == current:
        print(f"Your current model is already the fastest here ({best['median_s']:.2f} s per reply).")
    else:
        gain = f" — about {cur['median_s'] / best['median_s']:.1f}× faster than now" if cur else ""
        print(f"Fastest: {best['model']} ({best['median_s']:.2f} s per reply){gain}.")
        print("To use it, change this line in your .env file (no restart needed):\n")
        print(f"  HF_MODEL_ID={best['model']}\n")
        if "8B" in best["model"]:
            second = next((r for r in ok[1:] if "8B" not in r["model"]), None)
            if second:
                print(f"Note: small 8B models are quick but weaker at multi-step tasks. {second['model']} "
                      f"({second['median_s']:.2f} s) is the fastest larger model.")
    return 0
