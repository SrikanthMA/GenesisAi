"""Google Meet + minutes of meeting (MOM) (2.1).

* ``meet_url`` turns "start a new meeting" / "join abc-defg-hij" / "open Google Meet" into the right Meet link.
* ``MeetingRecorder`` records the meeting **on this PC**: the computer's sound output (what the other participants say,
  through Windows' loopback capture) and your microphone (what you say), as two tracks in 60-second chunks. Each chunk is
  transcribed while the meeting is still going (Gemini when configured — better with accents and several voices — or
  the offline Whisper model), so the minutes are ready soon after you stop.
* ``write_minutes`` turns the transcript into minutes: summary, attendees mentioned, discussion points, decisions,
  action items (owner, due date) and open questions, saved as a Word document (plus the full transcript as .txt) in
  Documents\\Genesis Documents\\Meeting Minutes, and opened.

Recording other people needs their agreement: starting always asks you to confirm that everyone agrees, and recording
stops automatically after ``MAX_MINUTES``. Audio chunks are deleted once the minutes are written (the transcript stays).
"""
from __future__ import annotations

import json
import queue
import re
import threading
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from ..core.logging_setup import get_logger

log = get_logger("v2.meeting")
SAMPLE_RATE = 16000
CHUNK_SECONDS = 60
MAX_MINUTES = 180
SILENCE_RMS = 120.0  # chunks quieter than this are skipped (Whisper invents words on silence)

# ------------------------------------------------------------------------------------------------- Meet links
_CODE = re.compile(r"\b([a-z]{3})[\s-]?([a-z]{4})[\s-]?([a-z]{3})\b", re.I)


def meet_url(op: str = "home", code: str = "") -> str:
    if op == "new":
        return "https://meet.new"
    if code:
        if code.lower().startswith("https://meet.google.com/"):
            return code
        m = _CODE.search(code.replace(".", " "))
        if m:
            return "https://meet.google.com/" + "-".join(g.lower() for g in m.groups())
    return "https://meet.google.com/"


def normalise_code(text: str) -> str:
    m = _CODE.search(text or "")
    return "-".join(g.lower() for g in m.groups()) if m else ""


# ------------------------------------------------------------------------------------------------- audio sources
def rms(pcm16: bytes) -> float:
    if len(pcm16) < 2:
        return 0.0
    import numpy as np

    a = np.frombuffer(pcm16[: len(pcm16) - len(pcm16) % 2], dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


class MicSource:
    """Your microphone (the same device Genesis listens on)."""

    track = "You"

    def __init__(self, device: Optional[str] = None) -> None:
        from ..voice.audio import Microphone

        self.mic = Microphone(device)

    def open(self) -> None:
        self.mic.open()

    def read(self) -> Optional[bytes]:
        return self.mic.read(timeout=0.5)

    def close(self) -> None:
        self.mic.close()


class LoopbackSource:
    """What the computer plays (the other participants), via WASAPI loopback (the 'soundcard' package)."""

    track = "Others"
    BLOCK = 1600  # 0.1 s

    def __init__(self) -> None:
        import soundcard as sc  # type: ignore

        speaker = sc.default_speaker()
        self._mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
        self._rec = None

    def open(self) -> None:
        self._rec = self._mic.recorder(samplerate=SAMPLE_RATE, channels=1, blocksize=self.BLOCK)
        self._rec.__enter__()

    def read(self) -> Optional[bytes]:
        import numpy as np

        data = self._rec.record(numframes=self.BLOCK)
        if data is None or not len(data):
            return None
        mono = data.mean(axis=1) if getattr(data, "ndim", 1) > 1 else data
        return (np.clip(mono, -1.0, 1.0) * 32767).astype(np.int16).tobytes()

    def close(self) -> None:
        if self._rec is not None:
            try:
                self._rec.__exit__(None, None, None)
            finally:
                self._rec = None


def default_sources(config: Any) -> tuple[list[Any], list[str]]:
    """Both tracks when possible; returns (sources, notes about what isn't available)."""
    sources, notes = [], []
    try:
        sources.append(LoopbackSource())
    except Exception as exc:
        notes.append("I can't capture the computer's sound on this PC "
                     f"({type(exc).__name__}), so I'll only hear what reaches your microphone - use speakers, not "
                     "headphones, so I can hear the others too.")
    try:
        sources.append(MicSource(getattr(config, "audio_input_device", None)))
    except Exception as exc:
        notes.append(f"Your microphone isn't available ({type(exc).__name__}).")
    return sources, notes


# ------------------------------------------------------------------------------------------------- transcription
def make_transcriber(config: Any) -> Callable[[bytes], str]:
    """Gemini (if a key is set) with the offline Whisper model as fallback."""
    from . import gemini_native

    local_holder: dict = {}

    def local(pcm: bytes) -> str:
        if "stt" not in local_holder:
            from ..voice.stt import FasterWhisperSTT

            size = getattr(config, "whisper_model_size", "base") or "base"
            local_holder["stt"] = FasterWhisperSTT(size, config.paths.models_dir / "whisper", multilingual=lambda: True)
        return local_holder["stt"].transcribe(pcm, SAMPLE_RATE)

    def transcribe(pcm: bytes) -> str:
        if gemini_native.gemini_key(config):
            try:
                return gemini_native.transcribe(config, pcm, SAMPLE_RATE, timeout=90, long_form=True)
            except Exception as exc:
                log.info("meeting chunk: Gemini transcription failed (%s), using Whisper", type(exc).__name__)
        return local(pcm)

    return transcribe


# ------------------------------------------------------------------------------------------------- recorder
@dataclass
class Line:
    chunk: int
    track: str
    text: str
    at: float


@dataclass
class MeetingRecorder:
    folder: Path
    transcriber: Callable[[bytes], str]
    sources: list[Any]
    title: str = "Meeting"
    chunk_seconds: int = CHUNK_SECONDS
    max_minutes: int = MAX_MINUTES
    clock: Callable[[], float] = time.monotonic
    lines: list[Line] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.folder = Path(self.folder)
        self.started_at = datetime.now()
        self._t0 = 0.0
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._work: "queue.Queue[Optional[tuple[int, str, Path]]]" = queue.Queue()
        self._lock = threading.Lock()
        self.running = False
        self.chunks_written = 0

    # -- life cycle
    def start(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        opened = []
        for src in self.sources:
            try:
                src.open()
                opened.append(src)
            except Exception as exc:
                self.errors.append(f"{getattr(src, 'track', 'audio')}: {exc}")
        if not opened:
            raise RuntimeError("No audio source could be opened (" + "; ".join(self.errors) + ")")
        self.sources = opened
        self._t0 = self.clock()
        self.running = True
        for src in self.sources:
            t = threading.Thread(target=self._capture, args=(src,), name=f"meeting-{src.track}", daemon=True)
            t.start()
            self._threads.append(t)
        tw = threading.Thread(target=self._transcribe_loop, name="meeting-transcribe", daemon=True)
        tw.start()
        self._worker = tw

    def elapsed(self) -> float:
        return (self.clock() - self._t0) if self.running or self._t0 else 0.0

    def stop(self, wait: float = 600.0) -> list[Line]:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=10)
        self._work.put(None)
        self._worker.join(timeout=wait)
        self.running = False
        with self._lock:
            return sorted(self.lines, key=lambda ln: (ln.chunk, 0 if ln.track == "Others" else 1))

    # -- workers
    def _capture(self, src: Any) -> None:
        per_chunk = self.chunk_seconds * SAMPLE_RATE * 2
        buf = bytearray()
        idx = 0
        try:
            while not self._stop.is_set():
                if self.elapsed() > self.max_minutes * 60:
                    self.errors.append(f"Recording stopped automatically after {self.max_minutes} minutes.")
                    self._stop.set()
                    break
                frame = src.read()
                if not frame:
                    continue
                buf += frame
                if len(buf) >= per_chunk:
                    self._emit_chunk(idx, src.track, bytes(buf[:per_chunk]))
                    del buf[:per_chunk]
                    idx += 1
        except Exception as exc:
            log.warning("meeting capture (%s) stopped: %s", src.track, exc)
            self.errors.append(f"{src.track} audio stopped: {type(exc).__name__}")
        finally:
            if len(buf) > SAMPLE_RATE:  # keep a last partial chunk longer than a second
                self._emit_chunk(idx, src.track, bytes(buf))
            try:
                src.close()
            except Exception:
                pass

    def _emit_chunk(self, idx: int, track: str, pcm: bytes) -> None:
        path = self.folder / f"chunk-{idx:04d}-{track.lower()}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(pcm)
        self.chunks_written += 1
        self._work.put((idx, track, path))

    def _transcribe_loop(self) -> None:
        while True:
            item = self._work.get()
            if item is None:
                break
            idx, track, path = item
            try:
                with wave.open(str(path), "rb") as w:
                    pcm = w.readframes(w.getnframes())
                if rms(pcm) < SILENCE_RMS:
                    continue
                text = (self.transcriber(pcm) or "").strip()
                if text:
                    with self._lock:
                        self.lines.append(Line(idx, track, text, idx * self.chunk_seconds))
                    self._append_transcript(idx, track, text)
            except Exception as exc:
                log.warning("meeting chunk %s/%s not transcribed: %s", idx, track, exc)
                self.errors.append(f"part {idx + 1} ({track}) couldn't be transcribed")

    def _append_transcript(self, idx: int, track: str, text: str) -> None:
        stamp = time.strftime("%H:%M", time.gmtime(idx * self.chunk_seconds))
        with open(self.folder / "transcript-live.txt", "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {track}: {text}\n")

    def cleanup_audio(self) -> None:
        for p in self.folder.glob("chunk-*.wav"):
            try:
                p.unlink()
            except OSError:
                pass


def transcript_text(lines: list[Line], chunk_seconds: int = CHUNK_SECONDS) -> str:
    out = []
    for ln in lines:
        stamp = time.strftime("%H:%M", time.gmtime(ln.chunk * chunk_seconds))
        out.append(f"[{stamp}] {ln.track}: {ln.text}")
    return "\n".join(out)


# ------------------------------------------------------------------------------------------------- minutes
MOM_PROMPT = """You are writing the minutes of a meeting (MOM) from its transcript.
"You" is the person who recorded the meeting; "Others" is everyone else heard through the computer.
Use only what is in the transcript - never invent names, numbers, dates or decisions. If something isn't mentioned,
leave that list empty.

Return JSON only:
{{"title": "short meeting title",
  "summary": "3-5 sentence overview",
  "attendees": ["names explicitly mentioned as present"],
  "discussion": ["key point", ...],
  "decisions": ["decision", ...],
  "action_items": [{{"task": "...", "owner": "name or empty", "due": "date/time or empty"}}],
  "open_questions": ["...", ...],
  "next_meeting": "date/time if mentioned, else empty"}}

Meeting date: {date}. Recording length: {minutes} minutes.

Transcript:
{transcript}"""

MAX_PROMPT_CHARS = 60000


def _summarise_long(llm: Any, transcript: str) -> str:
    """Very long meetings: condense each part first, then write the minutes from the condensed notes."""
    if len(transcript) <= MAX_PROMPT_CHARS:
        return transcript
    parts, notes = [transcript[i:i + 40000] for i in range(0, len(transcript), 40000)], []
    for n, part in enumerate(parts, 1):
        notes.append(f"(Part {n} notes)\n" + llm.chat([
            {"role": "system", "content": "Condense this part of a meeting transcript into detailed notes. Keep every "
                                          "decision, action item, owner, date and number. Don't invent anything."},
            {"role": "user", "content": part}], max_tokens=1500))
    return "\n\n".join(notes)


def minutes_content(llm: Any, transcript: str, started: datetime, minutes: int) -> dict:
    from ..ai.llm.base import extract_json

    body = _summarise_long(llm, transcript)
    raw = llm.chat([{"role": "user", "content": MOM_PROMPT.format(date=started.strftime("%A %d %B %Y, %H:%M"),
                                                                    minutes=minutes, transcript=body)}],
                   max_tokens=2500, json_mode=True)
    data = extract_json(raw)
    if not isinstance(data, dict):
        raise ValueError("The AI model didn't return minutes in the expected format.")
    return data


def to_document(data: dict, started: datetime, minutes: int, transcript: str) -> dict:
    """Minutes dict → the office builder's {title, sections} format."""
    def lst(key):
        return [str(x).strip() for x in (data.get(key) or []) if str(x).strip()]

    actions = []
    for a in data.get("action_items") or []:
        if isinstance(a, dict) and str(a.get("task", "")).strip():
            extra = ", ".join(x for x in (f"owner: {a.get('owner')}" if a.get("owner") else "",
                                           f"due: {a.get('due')}" if a.get("due") else "") if x)
            actions.append(a["task"].strip() + (f" ({extra})" if extra else ""))
        elif isinstance(a, str) and a.strip():
            actions.append(a.strip())
    title = str(data.get("title") or "Meeting").strip()[:120]
    sections = [{"heading": "Details", "bullets": [f"Date: {started.strftime('%A %d %B %Y, %H:%M')}",
                                                   f"Length: about {minutes} minute{'s' if minutes != 1 else ''}"]
                 + ([f"Attendees mentioned: {', '.join(lst('attendees'))}"] if lst("attendees") else [])}]
    if data.get("summary"):
        sections.append({"heading": "Summary", "paragraphs": [str(data["summary"]).strip()]})
    for key, heading in (("discussion", "Discussion"), ("decisions", "Decisions")):
        if lst(key):
            sections.append({"heading": heading, "bullets": lst(key)})
    if actions:
        sections.append({"heading": "Action items", "bullets": actions})
    if lst("open_questions"):
        sections.append({"heading": "Open questions", "bullets": lst("open_questions")})
    if str(data.get("next_meeting") or "").strip():
        sections.append({"heading": "Next meeting", "paragraphs": [str(data["next_meeting"]).strip()]})
    sections.append({"heading": "Note", "paragraphs": ["Written by Genesis from an automatic transcript - please check "
                                                       "names, numbers and decisions before sharing."]})
    return {"title": f"Minutes - {title}", "sections": sections}


def write_minutes(*, llm: Any, lines: list[Line], out_dir: Path, started: datetime, minutes: int,
                  chunk_seconds: int = CHUNK_SECONDS) -> dict:
    """Write MOM .docx (+ transcript .txt). Returns {"docx", "transcript", "title", "llm_ok", "items"}."""
    from ..services.office import build_docx, unique_path

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = started.strftime("%Y-%m-%d %H%M")
    transcript = transcript_text(lines, chunk_seconds)
    tpath = unique_path(out_dir, f"Transcript {stamp}", ".txt")
    tpath.write_text(transcript or "(nothing was transcribed)", encoding="utf-8")
    llm_ok, data = False, {}
    if transcript.strip():
        try:
            data = minutes_content(llm, transcript, started, minutes)
            llm_ok = True
        except Exception as exc:
            log.warning("minutes: AI summary failed: %s", exc)
    if llm_ok:
        content = to_document(data, started, minutes, transcript)
    else:
        content = {"title": f"Minutes - Meeting {stamp}",
                   "sections": [{"heading": "Note", "paragraphs": [
                       "The AI model wasn't available, so this contains the transcript only." if transcript.strip()
                       else "Nothing could be transcribed from the recording (was anyone speaking, and was the sound on?)."]},
                       {"heading": "Transcript", "paragraphs": transcript.splitlines()[:2000]}]}
    dpath = unique_path(out_dir, f"MOM {stamp} {str(data.get('title') or '').strip()}".strip(), ".docx")
    build_docx(content, dpath)
    return {"docx": str(dpath), "transcript": str(tpath), "title": content["title"], "llm_ok": llm_ok,
            "items": len(data.get("action_items") or []) if llm_ok else 0}


def json_dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)
