"""Real-hardware check for local AI (3.0): ``python -m genesis.v3.selftest``

Run on the user's PC with Ollama started. Unlike the unit tests (which use a fake Ollama), this measures the actual
machine: hardware, installed models, and for each agent model the load time, answer time and tokens/second, plus
routing, deterministic maths, and that no external AI is configured in local mode. Prints a plain report and exits
non-zero if something essential fails.
"""
from __future__ import annotations

import os
import sys
import time

from . import catalog, mode
from .agents import build_agents
from .hardware import detect
from .ollama import OllamaClient
from .router import Router, arithmetic


def main() -> int:
    ok = True
    try:  # read the same .env Genesis itself uses, so the test checks YOUR settings (3.4 fix)
        from ..core.config import GenesisConfig

        GenesisConfig.from_env()
    except Exception as exc:  # pragma: no cover
        print(f"  ! could not read .env ({exc}); testing with default settings")
    print("Genesis local AI self-test\n")
    hw = detect()
    print("Hardware :", hw.summary())
    print("Mode     :", mode.describe())
    if mode.is_local() and (os.getenv("GENESIS_LLM_PROVIDER") or "").lower() not in ("genesis", "local", "ollama"):
        print("  ! GENESIS_MODE=local but GENESIS_LLM_PROVIDER isn't a local provider")
        ok = False
    c = OllamaClient(os.getenv("GENESIS_OLLAMA_BASE_URL") or os.getenv("OLLAMA_HOST") or "")
    v = c.version()
    print("Ollama   :", v or "NOT RUNNING")
    if not v:
        print("\nStart the Ollama app, then run this again.")
        return 1
    installed = c.installed_names()
    print("Installed:", ", ".join(sorted(n for n in installed if not n.endswith(":latest"))) or "none")
    rec = catalog.recommend(hw)
    print(f"Recommend: Suzanne {rec['suzanne']}, Andrew {rec['andrew']}, complex {rec['complex']}")

    agents = build_agents()
    print("\nModel timings (first call includes loading from disk):")
    for agent in agents.values():
        if agent.model not in installed:
            print(f"  {agent.title:8} {agent.model}: not installed")
            ok = False
            continue
        try:
            t0 = time.perf_counter()
            r = c.chat(agent.model, [{"role": "user", "content": "Reply with one short friendly sentence."}],
                       max_tokens=60)
            wall = time.perf_counter() - t0
            gen_ms = max(1, r.total_ms - r.load_ms)
            print(f"  {agent.title:8} {agent.model}: load {r.load_ms} ms, total {wall:.1f} s, "
                  f"{r.eval_tokens * 1000 / gen_ms:.1f} tokens/s -> {r.text[:70]!r}")
        except Exception as exc:
            print(f"  {agent.title:8} {agent.model}: FAILED - {exc}")
            ok = False
    loaded = c.loaded()
    print("In memory:", ", ".join(f"{m['name']} ({m['vram_gb']}/{m['size_gb']} GB on GPU)" for m in loaded) or "none")

    r = Router(agents)
    checks = [("hello, how are you?", "suzanne"), ("fix this python error: IndexError", "andrew"),
              ("what is 15% of 80", "python")]
    print("\nRouting:")
    for text, want in checks:
        got = r.route([{"role": "user", "content": text}], installed=installed).agent
        print(f"  {'ok ' if got == want else 'BAD'} {text!r} -> {got}")
        ok = ok and got == want
    print("Maths    :", arithmetic("what is 15% of 80"))
    print("\nRESULT:", "PASS" if ok else "some checks failed (see above)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
