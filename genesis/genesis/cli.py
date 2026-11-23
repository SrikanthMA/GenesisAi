"""Terminal interface — same core, no GUI (useful for headless use, debugging and demos)."""
from __future__ import annotations

import json
import sys

from .core.assistant import GenesisCore
from .core.config import GenesisConfig
from .core.container import build_container
from .core.events import EventType
from .core.logging_setup import setup_logging

CRIMSON = "\033[38;5;161m"
DIM = "\033[2m"
RESET = "\033[0m"


def _enable_ansi() -> None:
    if sys.platform.startswith("win"):
        try:
            import ctypes

            k = ctypes.windll.kernel32  # type: ignore[attr-defined]
            k.SetConsoleMode(k.GetStdHandle(-11), 7)
        except Exception:
            pass


def repl(debug: bool = False, voice: bool = False) -> int:
    _enable_ansi()
    cfg = GenesisConfig.from_env()
    setup_logging(cfg.paths.logs_dir, cfg.log_level, console=debug)
    svc = build_container(cfg)
    core = GenesisCore(svc)
    core.start()
    svc.events.subscribe(lambda e: print(f"{DIM}  ⚙ {e.payload['step']['description']} → {e.payload['step']['status']}{RESET}"),
                         EventType.STEP_UPDATED)
    svc.events.subscribe(lambda e: print(f"\n{CRIMSON}⏰ {e.payload['text']}{RESET}"), EventType.REMINDER_DUE)
    llm = svc.llm.status() if hasattr(svc.llm, "status") else {}
    print(f"{CRIMSON}GENESIS{RESET} — terminal mode. LLM: {llm.get('name')} ({'online' if llm.get('online') else 'offline mode'})."
          "\nType a request (e.g. 'open my downloads folder'), '/nlu <text>' to inspect NLU, or 'exit'.\n")
    vm = None
    if voice:
        from .voice.manager import VoiceManager

        def handler(text: str):
            print(f"\n{DIM}(heard){RESET} {text}")
            r = core.handle(text, source="voice")
            print(f"{CRIMSON}Genesis:{RESET} {r.text}\n")
            return r.speech, r.status in ("awaiting_confirmation", "awaiting_input")

        vm = VoiceManager(cfg, svc.settings, svc.events, handler)
        vm.start()
        print("Voice on: say “Hey Genesis”.\n")
    try:
        while True:
            try:
                text = input("you › ").strip()
            except EOFError:
                break
            if not text:
                continue
            if text.lower() in {"exit", "quit", ":q"}:
                break
            if text.startswith("/nlu "):
                print(json.dumps(core.nlu.parse(text[5:]).to_dict(), indent=2, default=str))
                continue
            r = core.handle(text)
            print(f"{CRIMSON}Genesis:{RESET} {r.text}")
            if r.status == "awaiting_confirmation":
                print(f"{DIM}  (answer yes or no){RESET}")
            print()
    except KeyboardInterrupt:
        pass
    finally:
        if vm:
            vm.stop()
        core.stop()
    return 0
