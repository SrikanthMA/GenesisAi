"""Microphone capture (16 kHz mono PCM16) via PortAudio/sounddevice."""
from __future__ import annotations

import queue
import threading
from typing import Optional

from ..core.logging_setup import get_logger

log = get_logger("voice.audio")
SAMPLE_RATE = 16_000
FRAME_MS = 32
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 512 samples — also Porcupine's frame length


class MicrophoneError(RuntimeError):
    pass


class Microphone:
    """Push-style microphone stream; frames are read with :meth:`read`."""

    def __init__(self, device: Optional[str] = None) -> None:
        self.device = device
        self._q: "queue.Queue[bytes]" = queue.Queue(maxsize=400)
        self._stream = None
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        return self._stream is not None

    def open(self) -> None:
        with self._lock:
            if self._stream is not None:
                return
            try:
                import sounddevice as sd  # type: ignore
            except Exception as exc:
                raise MicrophoneError("The 'sounddevice' package (PortAudio) is not installed.") from exc

            def callback(indata, frames, time_info, status):  # runs on PortAudio thread
                try:
                    self._q.put_nowait(bytes(indata))
                except queue.Full:
                    pass  # drop frames rather than block the audio thread

            device = self.device
            if device and device.isdigit():
                device = int(device)
            try:
                self._stream = sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=FRAME_SAMPLES, dtype="int16", channels=1,
                                                 callback=callback, device=device)
                self._stream.start()
            except Exception as exc:
                self._stream = None
                msg = str(exc)
                if "Invalid number of channels" in msg or "Error querying device" in msg or "-9996" in msg:
                    raise MicrophoneError("No microphone was found. Plug one in or pick a device in Settings.") from exc
                if "-9999" in msg or "Unanticipated host error" in msg:
                    raise MicrophoneError("Windows blocked microphone access. Enable it in Settings → Privacy & security → "
                                          "Microphone → 'Let desktop apps access your microphone'.") from exc
                raise MicrophoneError(f"Could not open the microphone: {msg}") from exc
            log.info("microphone opened")

    def close(self) -> None:
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
                log.info("microphone closed")
        self.flush()

    def read(self, timeout: float = 0.5) -> Optional[bytes]:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def flush(self) -> None:
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break


def list_input_devices() -> list[dict]:
    try:
        import sounddevice as sd  # type: ignore

        return [{"index": i, "name": d["name"]} for i, d in enumerate(sd.query_devices()) if d.get("max_input_channels", 0) > 0]
    except Exception:
        return []
