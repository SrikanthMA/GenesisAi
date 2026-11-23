"""Energy-based voice activity detection / end-pointing.

Adapts to the room's noise floor, detects speech onset, and ends the
utterance after a configurable stretch of silence.
"""
from __future__ import annotations

from collections import deque
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
    adaptive_extra_ms: int = 400  # longer utterances get a little more pause tolerance (people pause mid-sentence)
    floor_window_ms: int = 1500  # the quietest moment in this window ≈ the room's noise (fans, AC) — tracked during speech too
    peak_ratio: float = 0.1  # after speech starts, anything below 10 % of your (decaying) speaking level counts as silence


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
        self.peak = 0.0
        self._recent: deque = deque(maxlen=max(4, self.cfg.floor_window_ms // FRAME_MS))

    @property
    def threshold(self) -> float:
        return max(self.cfg.min_threshold, self.noise * self.cfg.threshold_ratio)

    def feed(self, frame: bytes) -> bool:
        if self.done:
            return True
        level = frame_rms(frame)
        self.elapsed += FRAME_MS
        self.frames.append(frame)
        self._recent.append(level)
        if self.started and len(self._recent) == self._recent.maxlen:
            floor = min(self._recent)
            if floor > self.noise:  # steady noise louder than we assumed: learn it, or the turn would never end
                self.noise = 0.8 * self.noise + 0.2 * floor
        thr = self.threshold
        if self.started:
            thr = max(thr, self.cfg.peak_ratio * self.peak)
        if level >= thr:
            self.started = True
            self.speech_ms += FRAME_MS
            self.silence_run = 0
            self.peak = max(self.peak * 0.995, level)
        else:
            if not self.started:
                self.noise = 0.95 * self.noise + 0.05 * level  # track the noise floor before speech
            self.silence_run += FRAME_MS
        needed_silence = self.cfg.silence_ms + min(self.cfg.adaptive_extra_ms, int(self.speech_ms * 0.1))
        if self.started and self.speech_ms >= self.cfg.min_speech_ms and self.silence_run >= needed_silence:
            self.done = True
        elif not self.started and self.elapsed >= self.cfg.no_speech_ms:
            self.done = self.timed_out = True
        elif self.elapsed >= self.cfg.max_ms:
            self.done = True
        return self.done

    def audio(self) -> bytes:
        return b"".join(self.frames)
