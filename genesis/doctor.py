"""`python -m genesis --doctor` — environment diagnostics with actionable fixes."""
from __future__ import annotations

import importlib
import platform
import shutil
import sys

from .core.config import GenesisConfig

OK, WARN, FAIL = "✔", "!", "✘"


def _check_import(mod: str) -> bool:
    try:
        importlib.import_module(mod)
        return True
    except Exception:
        return False


def run_doctor() -> int:
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    cfg = GenesisConfig.from_env()
    problems = 0
    print(f"Genesis doctor — Python {platform.python_version()} on {platform.system()} {platform.release()}\n")
    if sys.version_info < (3, 10):
        print(f" {FAIL} Python 3.10+ is required.")
        problems += 1

    required = [("PySide6", "desktop UI"), ("requests", "HTTP"), ("numpy", "audio/embeddings"), ("sklearn", "intent classifier"),
                ("psutil", "process control"), ("dotenv", ".env loading"), ("cryptography", "credential vault")]
    optional = [("sounddevice", "microphone"), ("vosk", "wake word + offline STT"), ("faster_whisper", "accurate local STT"),
                ("win32com.client", "Windows SAPI voices"), ("pyttsx3", "fallback TTS"), ("keyring", "Windows Credential Manager"),
                ("send2trash", "Recycle Bin deletes"), ("pypdf", "PDF reading"), ("docx", ".docx reading"),
                ("sentence_transformers", "all-MiniLM-L6-v2 embeddings"), ("playwright", "browser automation"),
                ("ddgs", "search result summaries"), ("dateparser", "extra date formats"), ("pyflakes", "code checks"),
                ("PIL", "screen capture (on request only)"), ("pytesseract", "screen text OCR"),
                ("uiautomation", "read/click named on-screen buttons")]
    print("Required packages:")
    for mod, why in required:
        ok = _check_import(mod)
        problems += 0 if ok else 1
        print(f"  {OK if ok else FAIL} {mod:<22} {why}")
    print("\nOptional packages:")
    for mod, why in optional:
        print(f"  {OK if _check_import(mod) else WARN} {mod:<22} {why}")

    print("\nConfiguration:")
    from .ai.llm.factory import ResilientLLM, build_provider

    rep = cfg.env_report
    print(f"  {OK if rep and rep.file else WARN} .env file: {rep.file if rep and rep.file else 'not found — copy .env.example to .env'}")
    for w in ((rep.warnings if rep else []) + cfg.llm_notes):
        print(f"  {WARN} {w}")
    health = ResilientLLM(build_provider(cfg), config=cfg).health(probe=True)
    mark = OK if health["available"] else WARN
    print(f"  {mark} AI model: {health['provider']} · {health['model'] or '(no model set)'} — {health['message']}"
          + (f" ({health['latency_ms']} ms)" if health.get("latency_ms") else ""))
    if not health["available"]:
        print("      Fix: put your key in .env (HF_TOKEN=hf_… for Hugging Face, or LLM_PROVIDER + LLM_API_KEY + LLM_MODEL), "
              "then run: python -m genesis --check-llm")
    g = bool((cfg.google_client_id and cfg.google_client_secret) or cfg.google_client_secrets_file)
    print(f"  {OK if g else WARN} Google OAuth client " + ("configured" if g else "missing — Gmail falls back to compose windows"))
    vm = cfg.resolved_vosk_model_path.exists()
    print(f"  {OK if vm else WARN} Vosk model at {cfg.resolved_vosk_model_path}" + ("" if vm else " — run: python scripts/download_models.py"))
    print(f"  {OK} Data folder: {cfg.paths.home}")
    print(f"  {WARN if cfg.system_dry_run else OK} System dry-run: {cfg.system_dry_run}")

    print("\nDesktop integration:")
    code = shutil.which("code") or shutil.which("Code.exe")
    print(f"  {OK if code else WARN} VS Code CLI: {code or 'not on PATH (Genesis also checks default install folders)'}")
    try:
        import sounddevice as sd  # type: ignore

        inputs = [d for d in sd.query_devices() if d.get("max_input_channels", 0) > 0]
        print(f"  {OK if inputs else WARN} Microphones found: {len(inputs)}")
    except Exception:
        print(f"  {WARN} Could not query audio devices")
    print("\n" + ("All required components are present." if not problems else f"{problems} required component(s) missing — "
                  "run: pip install -r requirements.txt"))
    return 1 if problems else 0
