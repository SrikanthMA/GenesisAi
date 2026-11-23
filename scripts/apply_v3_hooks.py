"""Connect Genesis 3.0 (genesis/v3) to an existing Genesis 2.x installation.

    python scripts/apply_v3_hooks.py            apply (backs up every file it changes first)
    python scripts/apply_v3_hooks.py --check    report only, change nothing
    python scripts/apply_v3_hooks.py --remove   take the hooks out again (restores 2.x behaviour)

It appends one marked line to the end of each module below. Nothing above that line is modified.
Run by Install_Genesis_3.bat; safe to run more than once.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARK = "# v3 hook (Genesis 3.0)"
HOOKS = [  # (module file, patch function, required?)
    ("genesis/core/config.py", "patch_config", True),
    ("genesis/ai/llm/factory.py", "patch_factory", True),
    ("genesis/tools/registry.py", "patch_registry", True),
    ("genesis/nlp/rules.py", "patch_rules", True),
    ("genesis/memory/embeddings.py", "patch_embeddings", False),
    ("genesis/core/container.py", "patch_container", False),
    ("genesis/v2/voice_ai.py", "patch_v2_voice", False),
    ("genesis/v2/images.py", "patch_v2_images", False),
    ("genesis/voice/manager.py", "patch_voice_manager", False),  # 3.0.2 Friday mode
    ("genesis/core/assistant.py", "patch_assistant", False),
    ("genesis/voice/tts.py", "patch_tts", False),  # 3.1 natural voice (Kokoro)
    ("genesis/ui/main_window.py", "patch_main_window", False),  # 3.4 Business Automation page, 3.5 Coding page + mute bar
    ("genesis/ui/mini_window.py", "patch_mini_window", False),  # 3.5 mute buttons in the pop-out
]


def hook_line(rel: str, fn: str) -> str:
    depth = len(Path(rel).parts) - 2  # genesis/core/config.py -> "..", genesis/v2/images.py -> ".."
    dots = "." * (depth + 1)
    return f"from {dots}v3.hooks import {fn} as _v3_hook; _v3_hook(globals())  {MARK}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--root", default=str(ROOT))
    a = ap.parse_args(argv)
    root = Path(a.root)
    if not (root / "genesis" / "v3" / "hooks.py").is_file():
        print("genesis/v3 is missing - extract the 3.0 update into the Genesis folder first.")
        return 2
    backup = root / "backups" / f"pre-3.0-hooks-{time.strftime('%Y%m%d-%H%M%S')}"
    missing_required = []
    for rel, fn, required in HOOKS:
        p = root / rel
        if not p.is_file():
            print(f"  skip   {rel} (not in this version)")
            if required:
                missing_required.append(rel)
            continue
        text = p.read_text(encoding="utf-8")
        has = MARK in text
        if a.check:
            print(f"  {'hooked' if has else 'MISSING'} {rel}")
            continue
        if a.remove:
            if has:
                backup.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, backup / rel.replace("/", "__"))
                kept = [ln for ln in text.splitlines(keepends=True) if MARK not in ln]
                p.write_text("".join(kept), encoding="utf-8")
                print(f"  removed {rel}")
            continue
        if has:
            print(f"  ok     {rel} (already hooked)")
            continue
        backup.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, backup / rel.replace("/", "__"))
        sep = "" if text.endswith("\n") else "\n"
        p.write_text(text + sep + "\n" + hook_line(rel, fn) + "\n", encoding="utf-8")
        print(f"  hooked {rel}")
    if backup.exists() and not a.check:
        print(f"Backups of changed files: {backup}")
    if missing_required:
        print("WARNING: required module(s) not found: " + ", ".join(missing_required))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
