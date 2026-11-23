"""Voice pipeline tests with synthetic audio and fake engines (no microphone needed)."""
import math
import queue
import struct
import time
import unittest

from genesis.core.events import EventBus, EventType
from genesis.core.settings import Settings
from genesis.database.db import Database
from genesis.database.repositories import Repositories
from genesis.voice.audio import FRAME_SAMPLES, SAMPLE_RATE
from genesis.voice.stt import STTEngine, pcm_to_wav
from genesis.voice.tts import TTSEngine
from genesis.voice.vad import Endpointer, frame_rms
from genesis.voice.manager import VoiceManager
from genesis.voice.wakeword import WakeWordDetector, matches_wake_phrase


def tone(ms: int, amp: int = 6000, freq: float = 440.0) -> list[bytes]:
    frames, n = [], 0
    for _ in range(ms // 32):
        samples = [int(amp * math.sin(2 * math.pi * freq * (n + i) / SAMPLE_RATE)) for i in range(FRAME_SAMPLES)]
        n += FRAME_SAMPLES
        frames.append(struct.pack(f"{FRAME_SAMPLES}h", *samples))
    return frames


def silence(ms: int) -> list[bytes]:
    return [b"\x00\x00" * FRAME_SAMPLES for _ in range(ms // 32)]


WAKE_MARK = b"\x01\x00" * FRAME_SAMPLES  # a distinctive frame the fake detector recognises


class FakeMic:
    def __init__(self, frames):
        self.q = queue.Queue()
        for f in frames:
            self.q.put(f)
        self.is_open = False

    def open(self):
        self.is_open = True

    def close(self):
        self.is_open = False

    def read(self, timeout=0.5):
        try:
            return self.q.get(timeout=0.01)
        except queue.Empty:
            time.sleep(0.01)
            return None

    def flush(self):
        pass


class FakeWake(WakeWordDetector):
    name = "fake"

    def _detect(self, frame):
        return frame == WAKE_MARK


class FakeSTT(STTEngine):
    name = "fake-stt"

    def __init__(self, texts):
        self.texts = list(texts)
        self.audio_lengths = []

    def transcribe(self, pcm16, sample_rate=SAMPLE_RATE):
        self.audio_lengths.append(len(pcm16))
        return self.texts.pop(0) if self.texts else ""


class FakeTTS(TTSEngine):
    name = "fake-tts"

    def __init__(self):
        super().__init__()
        self.spoken, self.stopped = [], 0
        self._speaking = False

    def speak(self, text):
        self.spoken.append(text)

    def stop(self):
        self.stopped += 1

    @property
    def speaking(self):
        return self._speaking


class TestVAD(unittest.TestCase):
    def test_rms(self):
        self.assertEqual(frame_rms(silence(32)[0]), 0.0)
        self.assertGreater(frame_rms(tone(32)[0]), 3000)

    def test_endpointing(self):
        ep = Endpointer()
        for f in silence(300) + tone(800) + silence(1200):
            if ep.feed(f):
                break
        self.assertTrue(ep.done)
        self.assertFalse(ep.timed_out)
        self.assertGreaterEqual(ep.speech_ms, 700)

    def test_turn_ends_in_a_noisy_room(self):
        """A fan/AC louder than the assumed noise floor used to keep the turn open for the full 15 s."""
        import numpy as np

        rng = np.random.default_rng(0)
        n = SAMPLE_RATE * 32 // 1000

        def noise(ms, amp=600):
            return [rng.normal(0, amp, n).astype(np.int16).tobytes() for _ in range(ms // 32)]

        speech = [(rng.normal(0, 600, n) + np.sin(np.arange(n) / 3) * 4000).astype(np.int16).tobytes() for _ in range(2000 // 32)]
        ep = Endpointer()
        for f in noise(300) + speech + noise(10_000):
            if ep.feed(f):
                break
        self.assertTrue(ep.done)
        self.assertFalse(ep.timed_out)
        self.assertLess(ep.elapsed, 4000)  # ends ~0.5 s after the speech, not at the 15 s cap

    def test_no_speech_timeout(self):
        ep = Endpointer()
        for f in silence(7000):
            if ep.feed(f):
                break
        self.assertTrue(ep.timed_out)

    def test_wav(self):
        wav = pcm_to_wav(b"".join(tone(64)))
        self.assertTrue(wav.startswith(b"RIFF") and b"WAVE" in wav[:12])


class TestWakePhrase(unittest.TestCase):
    def test_variants(self):
        for t in ("hey genesis", "Hey, Genesis!", "hi genesis", "okay genesis", "hey jenesis"):
            self.assertTrue(matches_wake_phrase(t), t)
        for t in ("genesis", "the book of genesis", "hey jennifer", ""):
            self.assertFalse(matches_wake_phrase(t), t)
        self.assertTrue(matches_wake_phrase("genesis", allow_bare=True))

    def test_refractory(self):
        w = FakeWake(refractory_s=5)
        self.assertTrue(w.process(WAKE_MARK))
        self.assertFalse(w.process(WAKE_MARK))  # double trigger suppressed


class TestVoicePipeline(unittest.TestCase):
    def make(self, frames, texts, handler):
        db = Database(":memory:")
        settings = Settings(Repositories(db).settings)
        bus = EventBus()
        self.events = []
        bus.subscribe(lambda e: self.events.append((e.type, dict(e.payload))))
        from genesis.core.config import GenesisConfig

        self.tts = FakeTTS()
        self.stt = FakeSTT(texts)
        vm = VoiceManager(GenesisConfig(), settings, bus, handler, mic=FakeMic(frames), wake=FakeWake(), stt=self.stt, tts=self.tts)
        return vm

    def run_until(self, vm, cond, timeout=8.0):
        vm.start()
        end = time.time() + timeout
        while time.time() < end and not cond():
            time.sleep(0.02)
        vm.stop()

    def test_wake_record_transcribe_handle_speak(self):
        handled = []

        def handler(text):
            handled.append(text)
            return "Opened Google Chrome.", False

        frames = silence(200) + [WAKE_MARK] + tone(900) + silence(1200)
        vm = self.make(frames, ["Hey Genesis, open Chrome"], handler)
        self.run_until(vm, lambda: self.tts.spoken and self.tts.spoken[-1] == "Opened Google Chrome.")
        self.assertEqual(handled, ["open Chrome"])  # wake phrase stripped from the transcript
        self.assertIn("Opened Google Chrome.", self.tts.spoken)
        states = [p.get("state") for t, p in self.events if t == EventType.STATE_CHANGED]
        self.assertIn("LISTENING", states)
        self.assertIn("THINKING", states)
        self.assertTrue(any(t == EventType.UI_ACTION and p.get("action") == "wake" for t, p in self.events))

    def test_follow_up_for_confirmation(self):
        answers = []

        def handler(text):
            answers.append(text)
            if text == "shut down the computer":
                return "This will shut down your computer. Do you want me to continue?", True
            return "Okay, cancelled.", False

        frames = [WAKE_MARK] + tone(700) + silence(1100) + tone(500) + silence(1100)
        vm = self.make(frames, ["shut down the computer", "no"], handler)
        self.run_until(vm, lambda: len(answers) >= 2)
        self.assertEqual(answers, ["shut down the computer", "no"])  # second turn needed no wake word

    def test_reply_language_picks_a_voice_or_falls_back_to_english(self):
        def handler(text):
            return "C'est fait — YouTube est ouvert.", False, "fr", "Done — YouTube is open."

        frames = silence(200) + [WAKE_MARK] + tone(900) + silence(1200)
        vm = self.make(frames, ["Hey Genesis, ouvre YouTube"], handler)  # FakeTTS has no French voice
        self.run_until(vm, lambda: bool(self.tts.spoken))
        self.assertEqual(self.tts.spoken, ["Done — YouTube is open."])

        class FrenchTTS(FakeTTS):
            def can_speak(self, lang):
                return lang in ("en", "fr")

            def speak(self, text, lang=None):
                self.spoken.append((text, lang))

        vm = self.make(frames, ["Hey Genesis, ouvre YouTube"], handler)
        vm.tts = self.tts = FrenchTTS()
        self.run_until(vm, lambda: bool(self.tts.spoken))
        self.assertEqual(self.tts.spoken, [("C'est fait — YouTube est ouvert.", "fr")])

    def test_listening_disabled_closes_mic(self):
        vm = self.make(silence(100), [], lambda t: ("", False))
        vm.settings.set("voice.wake_word_enabled", False)
        self.run_until(vm, lambda: False, timeout=0.5)
        self.assertFalse(vm.mic.is_open)


if __name__ == "__main__":
    unittest.main()
