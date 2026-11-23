"""Energy-based voice activity detection / end-pointing.

Adapts to the room's noise floor, detects speech onset, and ends the
utterance after a configurable stretch of silence.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .audio import FRAME_MS


def frame_rms(frame: bytes) -> float:
    if not frame:
        return 0.0
    samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0


@dataclass
class EndpointConfig:
    silence_ms: int = 900  # silence that ends an utterance
    max_ms: int = 15_000  # hard cap
    no_speech_ms: int = 6_000  # give up if the user never starts talking
    min_speech_ms: int = 250
    threshold_ratio: float = 2.6  # speech must be this much louder than the noise floor
    min_threshold: float = 280.0


class Endpointer:
    """Feed frames; ``done`` becomes True when the utterance ended."""

    def __init__(self, cfg: EndpointConfig | None = None, noise_floor: float = 150.0) -> None:
        self.cfg = cfg or EndpointConfig()
        self.noise = noise_floor
        self.frames: list[bytes] = []
        self.elapsed = 0
        self.speech_ms = 0
        self.silence_run = 0
        self.started = False
        self.done = False
        self.timed_out = False

    @property
    def threshold(self) -> float:
        return max(self.cfg.min_threshold, self.noise * self.cfg.threshold_ratio)

    def feed(self, frame: bytes) -> bool:
        if self.done:
            return True
        level = frame_rms(frame)
        self.elapsed += FRAME_MS
        self.frames.append(frame)
        if level >= self.threshold:
            self.started = True
            self.speech_ms += FRAME_MS
            self.silence_run = 0
        else:
            if not self.started:
                self.noise = 0.95 * self.noise + 0.05 * level  # track the noise floor before speech
            self.silence_run += FRAME_MS
        if self.started and self.speech_ms >= self.cfg.min_speech_ms and self.silence_run >= self.cfg.silence_ms:
            self.done = True
        elif not self.started and self.elapsed >= self.cfg.no_speech_ms:
            self.done = self.timed_out = True
        elif self.elapsed >= self.cfg.max_ms:
            self.done = True
        return self.done

    def audio(self) -> bytes:
        return b"".join(self.frames)
