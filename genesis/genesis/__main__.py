"""Entry point: ``python -m genesis`` (desktop), ``--cli`` (terminal), ``--doctor`` (diagnostics)."""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="genesis", description="Genesis — Intelligent Desktop AI Assistant")
    parser.add_argument("--cli", action="store_true", help="run the text-only terminal interface")
    parser.add_argument("--voice", action="store_true", help="with --cli: also enable the voice pipeline")
    parser.add_argument("--minimized", action="store_true", help="start in the system tray without showing the assistant")
    parser.add_argument("--doctor", action="store_true", help="check dependencies, models and configuration")
    parser.add_argument("--debug", action="store_true", help="verbose console logging")
    args = parser.parse_args(argv)

    if args.doctor:
        from .doctor import run_doctor

        return run_doctor()
    if args.cli:
        from .cli import repl

        return repl(debug=args.debug, voice=args.voice)
    try:
        from .ui.app import run
    except ImportError as exc:
        print(f"The desktop UI could not start ({exc}).\nInstall dependencies with:  pip install -r requirements.txt\n"
              "or use the terminal interface:  python -m genesis --cli", file=sys.stderr)
        return 1
    return run(minimized=args.minimized)


if __name__ == "__main__":
    sys.exit(main())
