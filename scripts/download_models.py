"""Download the offline models Genesis uses.

    python scripts/download_models.py            # Vosk small English model (wake word + fallback STT)
    python scripts/download_models.py --whisper  # also pre-download the faster-whisper model
    python scripts/download_models.py --voice    # natural offline female voice (Piper; needs: pip install piper-tts)

Models are stored in data/models/. Nothing is downloaded at runtime except the
faster-whisper weights on first use (if you skip --whisper).
"""
from __future__ import annotations

import argparse
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from genesis.core.config import GenesisConfig  # noqa: E402

VOSK_URL = "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"


def _progress(block, size, total):
    if total > 0:
        pct = min(100, block * size * 100 // total)
        print(f"\r  {pct:3d}%", end="", flush=True)


def download_vosk(models_dir: Path) -> Path:
    target = models_dir / "vosk-model-small-en-us-0.15"
    if (target / "am").exists() or (target / "conf").exists():
        print(f"✔ Vosk model already present: {target}")
        return target
    models_dir.mkdir(parents=True, exist_ok=True)
    zip_path = models_dir / "vosk-small.zip"
    print(f"Downloading {VOSK_URL}")
    urllib.request.urlretrieve(VOSK_URL, zip_path, _progress)
    print("\n  extracting…")
    with zipfile.ZipFile(zip_path) as z:
        for member in z.namelist():  # guard against zip-slip
            dest = (models_dir / member).resolve()
            if not str(dest).startswith(str(models_dir.resolve())):
                raise RuntimeError(f"unsafe path in archive: {member}")
        z.extractall(models_dir)
    zip_path.unlink(missing_ok=True)
    print(f"✔ Vosk model ready: {target}")
    return target


def download_whisper(models_dir: Path, size: str) -> None:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("! faster-whisper not installed; skipping (pip install faster-whisper)")
        return
    print(f"Downloading faster-whisper '{size}' (~150 MB for base) …")
    WhisperModel(size, device="cpu", compute_type="int8", download_root=str(models_dir / "whisper"))
    print("✔ Whisper model ready")


PIPER_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en"
PIPER_VOICES = {  # name → path under PIPER_BASE (female, English; licences: see the voice's MODEL_CARD on Hugging Face)
    "en_US-hfc_female-medium": "en_US/hfc_female/medium",
    "en_US-amy-medium": "en_US/amy/medium",
    "en_GB-jenny_dioco-medium": "en_GB/jenny_dioco/medium",
}


def download_piper(models_dir: Path, name: str) -> None:
    if name not in PIPER_VOICES:
        print(f"! Unknown voice {name}. Choose one of: {', '.join(PIPER_VOICES)}")
        return
    folder = models_dir / "piper"
    folder.mkdir(parents=True, exist_ok=True)
    for ext in (".onnx", ".onnx.json"):
        target = folder / f"{name}{ext}"
        if target.exists():
            print(f"✔ {target.name} already present")
            continue
        url = f"{PIPER_BASE}/{PIPER_VOICES[name]}/{name}{ext}?download=true"
        print(f"Downloading {name}{ext} …")
        urllib.request.urlretrieve(url, target, _progress)
        print()
    try:
        import piper  # noqa: F401
        print(f"✔ Voice ready. Genesis will use it automatically (or set GENESIS_TTS_ENGINE=piper, GENESIS_PIPER_VOICE={name}).")
    except ImportError:
        print("! Voice downloaded. Now install the engine:  .\\.venv\\Scripts\\python.exe -m pip install piper-tts")


def main() -> int:
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--whisper", action="store_true", help="also download the faster-whisper model")
    ap.add_argument("--force", action="store_true", help="re-download")
    ap.add_argument("--voice", nargs="?", const="en_US-hfc_female-medium", default=None,
                    help="download a natural offline female voice for Piper (default en_US-hfc_female-medium)")
    args = ap.parse_args()
    cfg = GenesisConfig.from_env()
    models = cfg.paths.models_dir
    if args.force and (models / "vosk-model-small-en-us-0.15").exists():
        shutil.rmtree(models / "vosk-model-small-en-us-0.15")
    try:
        download_vosk(models)
    except Exception as exc:
        print(f"\n✘ Could not download the Vosk model: {exc}\n  Download it manually from {VOSK_URL} and unzip it into {models}")
        return 1
    if args.whisper:
        size = cfg.whisper_model_size
        if size.endswith(".en"):  # 1.6: Genesis understands any language → the multilingual model (same size)
            size = size[:-3]
        download_whisper(models, size)
    if args.voice:
        try:
            download_piper(models, args.voice)
        except Exception as exc:
            print(f"\n✘ Could not download the voice: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
