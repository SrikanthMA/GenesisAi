"""List microphones so you can set GENESIS_AUDIO_DEVICE in .env."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from genesis.voice.audio import list_input_devices  # noqa: E402

devices = list_input_devices()
if not devices:
    print("No input devices found (is 'sounddevice' installed and a microphone connected?)")
for d in devices:
    print(f"{d['index']:>3}  {d['name']}")
