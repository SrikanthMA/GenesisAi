"""Write a short diagnostic report for support: ``python -m genesis.v3.diagnose``  (no keys or personal data)."""
from __future__ import annotations

import io
import logging
import os
import sys


def main() -> int:
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    print("Genesis 3 diagnostics")
    from . import __version__, hooks, mode

    print("version :", __version__)
    print("mode    :", mode.describe(), "| cloud voice allowed:", hooks.cloud_voice_allowed())
    print("provider:", os.getenv("GENESIS_LLM_PROVIDER"), "| Suzanne:", os.getenv("SUZANNE_MODEL"), "| Andrew:",
          os.getenv("ANDREW_MODEL"))
    for mod in ("genesis.core.config", "genesis.ai.llm.factory", "genesis.tools.registry", "genesis.nlp.rules",
                "genesis.memory.embeddings", "genesis.core.container", "genesis.v2.voice_ai", "genesis.v2.images"):
        try:
            __import__(mod)
            print(f"  import ok  {mod}")
        except Exception as exc:
            print(f"  IMPORT FAIL {mod}: {type(exc).__name__}: {exc}")
    print("hooks applied:", hooks.APPLIED)
    try:
        import genesis.v2.voice_ai as v

        gated = [n for n, o in vars(v).items() if isinstance(o, type) and "cloud_enabled" in o.__dict__]
        print("voice classes with a cloud switch:", gated)
    except Exception as exc:
        print("voice module:", exc)
    try:
        from ..nlp import rules

        for t in ("check my local AI setup", "i want to order pizza", "write an IEEE paper on solar energy",
                  "open wikipedia on black holes and copy it into word", "create an excel sheet for my expenses",
                  "write a calculator in python in vs code", "write a bar chart of iris in colab"):
            r = rules.parse(t)
            print(f"  understands {t!r:55} -> {(r.intent.value, r.slots.get('tool')) if r else None}")
    except Exception as exc:
        print("rules:", exc)
    logging.getLogger().removeHandler(handler)
    warn = [ln for ln in buf.getvalue().splitlines() if "v3" in ln]
    print("warnings:", warn or "none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
