"""Download the offline models Genesis uses.

    python scripts/download_models.py            # Vosk small English model (wake word + fallback STT)
    python scripts/download_models.py --whisper  # also pre-download the faster-whisper model

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
    print(f"Downloading faster-whisper '{size}' (~150 MB for base.en) …")
    WhisperModel(size, device="cpu", compute_type="int8", download_root=str(models_dir / "whisper"))
    print("✔ Whisper model ready")


def main() -> int:
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--whisper", action="store_true", help="also download the faster-whisper model")
    ap.add_argument("--force", action="store_true", help="re-download")
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
        download_whisper(models, cfg.whisper_model_size)
    return 0


if __name__ == "__main__":
    sys.exit(main())
