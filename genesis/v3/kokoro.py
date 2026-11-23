"""Natural offline voice: Kokoro TTS (3.1).

Why Kokoro (vs Piper, which Genesis already supports, and Windows SAPI):
* Kokoro-82M (Apache-2.0) is the most natural-sounding small open TTS model available: human-like pacing and
  intonation; ~330 MB once (model + voices); runs faster than real time on a modern CPU (no GPU needed, leaving the RTX
  for the language model).
* Piper is lighter and faster but noticeably flatter; SAPI voices (Zira/David) are the "robotic" baseline.
So: Kokoro when installed, Piper as the next fallback, SAPI last - chosen automatically.

Behaviour:
* sentence-by-sentence streaming: the next sentence is synthesised while the current one plays, so speech starts after
  the first sentence is ready instead of after the whole reply;
* small natural pause between sentences, adjustable speed (Genesis's voice rate setting), several voices;
* interruptible within ~50 ms (barge-in / "stop"), one reply at a time (no overlapping audio);
* if the audio device fails, the error is logged and Genesis carries on (text still shows).

Setup (one-time, needs internet): Setup_Natural_Voice.bat installs ``kokoro-onnx`` and downloads
``kokoro-v1.0.onnx`` + ``voices-v1.0.bin`` into data\\models\\kokoro. After that it is fully offline.
"""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Optional

from ..core.logging_setup import get_logger
from ..voice.tts import _ThreadedTTS

log = get_logger("v3.kokoro")

FRIDAY_BLEND = {"bf_isabella": 0.55, "bf_emma": 0.25, "af_heart": 0.20}
VOICES = {  # the most natural English voices first
    "friday": "Friday - Genesis's own voice: warm, confident British-leaning female (an original blend of 3 voices)",
    "af_heart": "Heart - warm US female", "af_bella": "Bella - bright US female",
    "af_nicole": "Nicole - soft US female", "bf_emma": "Emma - British female", "bf_isabella": "Isabella - British female",
    "am_michael": "Michael - US male", "am_fenrir": "Fenrir - deep US male", "bm_george": "George - British male",
    "hf_alpha": "Alpha - Hindi female", "hm_omega": "Omega - Hindi male",
}
LANG_OF = {"a": "en-us", "b": "en-gb", "h": "hi", "e": "es", "f": "fr-fr", "i": "it", "p": "pt-br", "j": "ja", "z": "cmn"}
PAUSE_S = 0.12


def model_dir() -> Path:
    env = os.getenv("GENESIS_KOKORO_DIR")
    if env:
        return Path(env)
    from ..core.paths import AppPaths

    return AppPaths.default().models_dir / "kokoro"


def model_files() -> Optional[tuple[str, str]]:
    d = model_dir()
    onnx = next((p for p in (d / "kokoro-v1.0.onnx", d / "kokoro-v1.0.fp16.onnx", d / "kokoro-v1.0.int8.onnx") if p.is_file()),
                None)
    voices = d / "voices-v1.0.bin"
    return (str(onnx), str(voices)) if onnx and voices.is_file() else None


def available() -> bool:
    if model_files() is None:
        return False
    try:
        import kokoro_onnx  # noqa: F401
        import sounddevice  # noqa: F401
    except Exception:
        return False
    return True


def sentences(text: str) -> list[str]:
    """Split for streaming: sentences, with very long ones broken at commas; markdown and code removed."""
    t = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    t = re.sub(r"[*_#`>|]+", " ", t)
    t = re.sub(r"https?://\S+", "the link", t)
    t = " ".join(t.split())
    out = []
    for s in re.split(r"(?<=[.!?…])\s+", t):
        while len(s) > 220:
            cut = max(s.rfind(", ", 0, 220), s.rfind("; ", 0, 220))
            cut = cut if cut > 60 else s.rfind(" ", 0, 220)
            out.append(s[:cut + 1].strip())
            s = s[cut + 1:]
        if s.strip():
            out.append(s.strip())
    return out


def speed_for(rate: int) -> float:
    """Genesis voice rate (-10..10, 0 = normal) -> Kokoro speed (0.75..1.35)."""
    return round(max(0.75, min(1.35, 1.0 + int(rate) * 0.035)), 3)


class KokoroTTS(_ThreadedTTS):
    name = "kokoro"

    def __init__(self, voice: str = "", synth=None, player=None) -> None:
        self._voice_name = voice or os.getenv("GENESIS_TTS_VOICE") or "friday"
        self._rate = 0
        self._synth = synth    # injectable for tests: (text, voice, speed, lang) -> (samples, sr)
        self._player = player  # injectable for tests: (samples, sr, interrupt_event) -> None
        super().__init__()

    # ------------------------------------------------------------ backend
    def _init_backend(self) -> None:
        if self._synth is None:
            files = model_files()
            if files is None:
                raise FileNotFoundError(f"Kokoro model files not found in {model_dir()} (run Setup_Natural_Voice.bat)")
            from kokoro_onnx import Kokoro  # type: ignore

            k = Kokoro(*files)
            self._known = set(k.get_voices()) if hasattr(k, "get_voices") else set(VOICES) - {"friday"}
            styles = {}
            blend = blend_style(k, FRIDAY_BLEND)
            if blend is not None:
                styles["friday"] = blend
                self._known.add("friday")

            def synth(text, voice, speed, lang):
                return k.create(text, voice=styles.get(voice, voice), speed=speed, lang=lang)

            self._synth = synth
        else:
            self._known = set(VOICES)
        if self._player is None:
            import sounddevice  # noqa: F401
            self._player = _play
        if self._voice_name not in self._known:
            self._voice_name = "af_heart" if "af_heart" in self._known else sorted(self._known)[0]

    def _lang(self) -> str:
        if self._voice_name == "friday":
            return "en-gb"
        return LANG_OF.get(self._voice_name[:1], "en-us")

    def _do_speak(self, text: str) -> None:
        parts = sentences(text)
        if not parts:
            return
        speed, voice, lang = speed_for(self._rate), self._voice_name, self._lang()
        nxt: dict = {}

        def synth(i: int) -> None:
            try:
                nxt[i] = self._synth(parts[i], voice, speed, lang)
            except Exception as exc:  # one bad sentence never kills the reply
                log.warning("kokoro synthesis failed: %s", exc)
                nxt[i] = None

        synth(0)
        for i in range(len(parts)):
            if self._interrupt.is_set():
                return
            worker = None
            if i + 1 < len(parts):  # prepare the next sentence while this one plays
                worker = threading.Thread(target=synth, args=(i + 1,), daemon=True)
                worker.start()
            audio = nxt.pop(i, None)
            if audio is not None:
                samples, sr = audio
                self._player(samples, sr, self._interrupt)
                if i + 1 < len(parts) and not self._interrupt.is_set():
                    self._interrupt.wait(PAUSE_S)
            if worker is not None:
                worker.join()

    def _do_stop(self) -> None:
        try:
            import sounddevice as sd

            sd.stop()
        except Exception:
            pass

    def _do_rate(self, rate: int) -> None:
        self._rate = int(rate)

    def _do_voice(self, frag: str) -> None:
        frag = (frag or "").lower().strip()
        if frag.startswith("gender:"):
            frag = {"female": "friday" if "friday" in self._known else "af_heart",
                    "male": "bm_george"}.get(frag.split(":", 1)[1], "friday")
        match = next((v for v in self._known if v == frag), None) or \
            next((v for v in sorted(self._known) if frag and (frag in v or frag in VOICES.get(v, "").lower())), None)
        if match:
            self._voice_name = match

    def voices(self) -> list[str]:
        return [v for v in VOICES if v in self._known] + sorted(v for v in self._known if v not in VOICES)

    def can_speak(self, lang: Optional[str]) -> bool:
        base = (lang or "en").split("-")[0].lower()
        return base in ("en", "hi") if base else True

    @property
    def voice(self) -> str:
        return self._voice_name


def blend_style(kokoro, weights: dict):
    """Weighted mix of voice style vectors = a new, original voice (no one's voice is cloned)."""
    get = getattr(kokoro, "get_voice_style", None)
    if get is None:
        return None
    try:
        total = None
        for name, w in weights.items():
            v = get(name) * float(w)
            total = v if total is None else total + v
        return total
    except Exception as exc:  # a voice missing from this model version
        log.info("voice blend unavailable: %s", exc)
        return None


def _play(samples, sr: int, interrupt: threading.Event) -> None:
    """Blocking playback in ~40 ms blocks so an interrupt stops it almost at once."""
    import numpy as np
    import sounddevice as sd

    data = np.asarray(samples, dtype=np.float32).reshape(-1)
    block = max(256, int(sr * 0.04))
    with sd.OutputStream(samplerate=int(sr), channels=1, dtype="float32") as out:
        for i in range(0, len(data), block):
            if interrupt.is_set():
                return
            out.write(data[i:i + block])
