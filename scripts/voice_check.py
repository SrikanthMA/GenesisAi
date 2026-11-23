"""Voice check & repair for "Hey Genesis" (run by Fix_Voice.bat, or: python scripts/voice_check.py).

Finds out *why* the wake word doesn't respond and fixes what can be fixed automatically:

* voice / wake word switched off in Genesis's settings          -> switched back on
* wake-word engine set to "none" (or Porcupine without a key)   -> Vosk
* speech recognition set to Hugging Face (credits used up)      -> local recogniser
* Vosk wake-word model missing                                   -> downloaded
* microphone silent / wrong microphone selected                 -> loudest working microphone chosen
* your voice reaches the mic but is too quiet / not confident   -> wake sensitivity raised
* packages blocked by Windows Smart App Control                 -> reported clearly (can't be fixed from a script)

It never sends audio anywhere: everything runs on this PC. Changes to .env are backed up first.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(os.environ.get("GENESIS_FIX_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import logging  # noqa: E402

logging.disable(logging.CRITICAL)

AUTO = os.environ.get("GENESIS_VOICE_CHECK_AUTO") == "1"  # automated tests only: no prompts, no recording
BLOCKED_WORDS = ("application control", "blocked", "smart app control", "dll load failed")
CHANGES: list[str] = []
PROBLEMS: list[str] = []


def ok(m: str) -> None:
    print("  [OK]  " + m, flush=True)


def warn(m: str) -> None:
    print("  [!]   " + m, flush=True)


def bad(m: str) -> None:
    print("  [X]   " + m, flush=True)


def step(m: str) -> None:
    print("\n" + m, flush=True)


def pause(msg: str = "  Press Enter to continue...") -> None:
    if not AUTO:
        try:
            input(msg)
        except EOFError:
            pass


def ask(question: str, default: bool = True) -> bool:
    if AUTO:
        return default
    hint = "[Y/n]" if default else "[y/N]"
    try:
        a = input(f"  {question} {hint} ").strip().lower()
    except EOFError:
        return default
    return default if not a else a.startswith("y")


# ----------------------------------------------------------------------------------------------------------- .env edits
_env_backed_up = False


def set_env(key: str, value: str, why: str) -> None:
    """Set KEY=value in .env: back the file up once, comment out the old line(s), append the new one."""
    global _env_backed_up
    path = ROOT / ".env"
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    if not _env_backed_up and path.exists():
        shutil.copy2(path, ROOT / f".env.before-voice-fix-{datetime.now():%Y%m%d-%H%M%S}")
        _env_backed_up = True
    out = []
    for line in lines:
        s = line.strip()
        if not s.startswith("#") and s.split("=", 1)[0].strip().removeprefix("export ").strip() == key:
            out.append("# " + s)
        else:
            out.append(line)
    out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.environ[key] = value
    CHANGES.append(f"{why} ({key}={value})")


# --------------------------------------------------------------------------------------------------------------- main
def main() -> int:
    from genesis.core.config import GenesisConfig

    cfg = GenesisConfig.from_env()

    # 1. Genesis's own voice settings ------------------------------------------------------------------------------
    step("1/5  Genesis voice settings")
    settings = None
    try:
        from genesis.core.settings import Settings
        from genesis.database.db import Database
        from genesis.database.repositories import SettingsRepo

        settings = Settings(SettingsRepo(Database(cfg.paths.db_path)))
        for key, label in (("voice.enabled", "Voice"), ("voice.wake_word_enabled", "'Hey Genesis' wake word")):
            if settings.get(key, True):
                ok(f"{label} is on.")
            else:
                settings.set(key, True)
                warn(f"{label} was switched OFF in Genesis - switched it back on.")
                CHANGES.append(f"{label} switched back on")
    except Exception as exc:  # pragma: no cover - only on a damaged database
        warn(f"Couldn't read Genesis's saved settings ({type(exc).__name__}); continuing.")
    sens = float(settings.get("voice.wake_sensitivity", 0.6)) if settings else 0.6
    phrase = (settings.get("voice.wake_phrase", "hey genesis") if settings else "hey genesis") or "hey genesis"
    ok(f"Wake phrase: '{phrase}', sensitivity {sens:.2f}")

    if cfg.wake_word_engine == "none":
        set_env("GENESIS_WAKE_WORD_ENGINE", "vosk", "Wake-word engine was set to 'none'")
        warn("The wake-word engine was turned off in .env - set it back to the built-in one (Vosk).")
    elif cfg.wake_word_engine == "porcupine" and not (cfg.porcupine_access_key and cfg.porcupine_keyword_path):
        set_env("GENESIS_WAKE_WORD_ENGINE", "vosk", "Porcupine was selected without its key/keyword file")
        warn("Porcupine was selected but isn't set up - switched to the built-in wake word (Vosk).")
    elif cfg.wake_word_engine not in ("vosk", "porcupine"):
        set_env("GENESIS_WAKE_WORD_ENGINE", "vosk", f"Unknown wake-word engine '{cfg.wake_word_engine}'")
    else:
        ok(f"Wake-word engine: {cfg.wake_word_engine}")
    if cfg.stt_engine == "huggingface":
        set_env("GENESIS_STT_ENGINE", "auto", "Speech recognition used Hugging Face, whose credits are used up")
        warn("Speech recognition was set to Hugging Face (credits used up) - switched to the recogniser on this PC.")

    # 2. packages ----------------------------------------------------------------------------------------------------
    step("2/5  Voice components")
    blocked = []
    missing = []
    for mod, what in (("sounddevice", "microphone access"), ("vosk", "'Hey Genesis' detector"),
                      ("faster_whisper", "speech recognition")):
        try:
            __import__(mod)
            ok(f"{what} ({mod}) works.")
        except ModuleNotFoundError:
            missing.append(mod)
            warn(f"{what} ({mod}) is not installed.")
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            if any(w in msg.lower() for w in BLOCKED_WORDS):
                blocked.append((mod, what))
                bad(f"{what} ({mod}) is BLOCKED by Windows (Smart App Control / Application Control).")
            else:
                bad(f"{what} ({mod}) failed to load: {msg[:160]}")
                PROBLEMS.append(f"{mod} failed to load: {msg[:160]}")
    if missing and not AUTO:
        print("  Installing the missing parts (needs internet)...", flush=True)
        pkgs = {"sounddevice": "sounddevice>=0.4.6", "vosk": "vosk>=0.3.45", "faster_whisper": "faster-whisper"}
        r = subprocess.run([sys.executable, "-m", "pip", "install", *[pkgs[m] for m in missing]])
        if r.returncode == 0:
            ok("Installed: " + ", ".join(missing))
            CHANGES.append("Installed " + ", ".join(missing))
            for m in missing:
                try:
                    __import__(m)
                except Exception as exc:
                    if any(w in str(exc).lower() for w in BLOCKED_WORDS):
                        blocked.append((m, m))
        else:
            bad("Installing failed - check your internet connection and run this again.")
            PROBLEMS.append("Couldn't install " + ", ".join(missing))
    if blocked:
        PROBLEMS.append("Windows Smart App Control blocks: " + ", ".join(m for m, _ in blocked))
        print()
        bad("Windows is blocking part of the voice system, so 'Hey Genesis' cannot work until that's allowed.")
        print("      This is the same Windows feature that blocked scikit-learn before. A script can't override it.", flush=True)
        print("      Option 1: Windows Security > App & browser control > Smart App Control settings > Off", flush=True)
        print("                (Microsoft only lets you turn it back on by resetting Windows.)", flush=True)
        print("      Option 2: keep it on and type to Genesis instead (Ctrl+Alt+G opens the chat).", flush=True)
        if any(m in ("sounddevice", "vosk") for m, _ in blocked):
            return finish(cfg)

    # 3. wake-word model -------------------------------------------------------------------------------------------
    step("3/5  'Hey Genesis' model")
    model_path = cfg.resolved_vosk_model_path
    if model_path.exists():
        ok(f"Model present ({model_path.name}).")
    elif AUTO:
        warn("Model missing (automatic test: not downloading).")
    else:
        warn("The wake-word model is missing - downloading it now (~40 MB)...")
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "download_models.py")])
        cfg = GenesisConfig.from_env()
        model_path = cfg.resolved_vosk_model_path
        if model_path.exists():
            ok("Model downloaded.")
            CHANGES.append("Downloaded the wake-word model")
        else:
            bad("Download failed - check your internet connection and run this again.")
            PROBLEMS.append("Wake-word model missing")
            return finish(cfg)

    if AUTO:
        return finish(cfg)
    for mod in ("sounddevice", "vosk"):
        try:
            __import__(mod)
        except Exception as exc:
            bad(f"Can't test the microphone: {mod} doesn't work ({type(exc).__name__}).")
            PROBLEMS.append(f"{mod} not working")
            return finish(cfg)

    # 4. microphone + live wake test ---------------------------------------------------------------------------------
    import sounddevice as sd

    from genesis.voice.vad import frame_rms
    from genesis.voice.wakeword import VoskWakeWord, matches_wake_phrase

    step("4/5  Microphone test")
    try:
        devices = sd.query_devices()
        default_in = sd.default.device[0] if isinstance(sd.default.device, (list, tuple)) else sd.default.device
    except Exception as exc:
        bad(f"Windows didn't give access to any microphone ({type(exc).__name__}).")
        PROBLEMS.append("No microphone access")
        return finish(cfg)
    hostapis = sd.query_hostapis()
    inputs = [(i, d) for i, d in enumerate(devices) if d.get("max_input_channels", 0) > 0]
    if not inputs:
        bad("No microphone found. Plug in a headset/mic (or turn on the laptop mic) and run this again.")
        PROBLEMS.append("No microphone found")
        return finish(cfg)
    loopback = ("stereo mix", "what u hear", "loopback", "wave out", "mix (")  # record the speakers, not your voice
    inputs = [(i, d) for i, d in inputs if not any(w in d["name"].lower() for w in loopback)] or inputs
    mme = [(i, d) for i, d in inputs if "mme" in hostapis[d["hostapi"]]["name"].lower()] or inputs

    def label(i):
        return devices[i]["name"] if i is not None and i >= 0 else "Windows default microphone"

    def record(device, seconds: float):
        frames: list[bytes] = []
        stream = sd.RawInputStream(samplerate=16000, blocksize=512, dtype="int16", channels=1, device=device,
                                   callback=lambda indata, n, t, s: frames.append(bytes(indata)))
        with stream:
            time.sleep(seconds)
        return frames

    configured = cfg.audio_input_device
    current = (int(configured) if configured and configured.isdigit() else configured) if configured else None
    ok("Microphone in use: " + (str(configured) if configured else label(default_in)))

    def wake_test(device, title: str):
        print(f"\n  >>> {title}", flush=True)
        print("      When you see 'SPEAK NOW', say  \"Hey Genesis\"  clearly, wait a second, and say it again.", flush=True)
        pause("      Press Enter when you're ready...")
        for n in (3, 2, 1):
            print(f"      {n}...", flush=True)
            time.sleep(0.7)
        print("      SPEAK NOW  (6 seconds)", flush=True)
        try:
            frames = record(device, 6.0)
        except Exception as exc:
            msg = str(exc)
            if "-9999" in msg or "Unanticipated host error" in msg:
                return {"error": "privacy"}
            return {"error": msg[:160]}
        print("      ...done.", flush=True)
        peak = max((frame_rms(f) for f in frames), default=0.0)
        det = VoskWakeWord(model_path, sens, phrase=phrase)
        hits = sum(1 for f in frames if det.process(f))
        from vosk import KaldiRecognizer  # type: ignore

        name = phrase.split()[-1]
        rec = KaldiRecognizer(det.model, 16000, json.dumps(sorted({phrase, f"hey {name}", f"hi {name}", f"okay {name}", name})
                                                          + ["[unk]"]))
        rec.SetWords(True)
        heard, confs = [], []
        for f in frames:
            if rec.AcceptWaveform(f):
                r = json.loads(rec.Result())
                if r.get("text"):
                    heard.append(r["text"])
                    confs += [w.get("conf", 0.0) for w in r.get("result", []) if w.get("word") == name]
        r = json.loads(rec.FinalResult())
        if r.get("text"):
            heard.append(r["text"])
            confs += [w.get("conf", 0.0) for w in r.get("result", []) if w.get("word") == name]
        return {"peak": peak, "hits": hits, "heard": [h for h in heard if h != "[unk]"], "conf": max(confs, default=0.0),
                "min_level": det.min_level, "min_conf": det.min_conf,
                "said_name": any(matches_wake_phrase(h, allow_bare=True, phrase=phrase) for h in heard)}

    res = wake_test(current, "Test 1: 'Hey Genesis' with the current microphone")
    if res.get("error") == "privacy":
        bad("Windows blocked the microphone for desktop apps.")
        print("      Opening the setting: turn ON 'Microphone access' AND 'Let desktop apps access your microphone'.", flush=True)
        try:
            os.startfile("ms-settings:privacy-microphone")  # type: ignore[attr-defined]
        except Exception:
            pass
        pause("      Turn both on, then press Enter here...")
        res = wake_test(current, "Test 1 again")
    if res.get("error"):
        bad(f"Couldn't open the microphone: {res['error']}")
        res = {"peak": 0.0, "hits": 0, "heard": [], "conf": 0.0, "said_name": False, "min_level": 240, "min_conf": 0.57}
    print(f"      loudness {res['peak']:.0f} (needed {res['min_level']:.0f}), heard: {res['heard'] or 'nothing'}", flush=True)

    # silent -> look for a better microphone
    if res["peak"] < 60:
        bad("The microphone is completely silent - Genesis can't hear anything through it.")
        print("\n  I'll try every microphone. Keep talking (count slowly from 1 to 20) while I listen to each one.", flush=True)
        pause("  Press Enter, then start counting...")
        scores = []
        for i, d in mme:
            try:
                fr = record(i, 1.6)
                p = max((frame_rms(f) for f in fr), default=0.0)
            except Exception:
                p = -1.0
            print(f"      {p:7.0f}  {d['name']}", flush=True)
            # "name, host API" is unambiguous: the same microphone is usually listed once per Windows audio API
            scores.append((p, i, f'{d["name"]}, {hostapis[d["hostapi"]]["name"]}'))
        best = max(scores) if scores else (-1, None, "")
        if best[0] >= 60:
            ok(f"This microphone hears you: {best[2]}")
            set_env("GENESIS_AUDIO_DEVICE", best[2], "Switched to the microphone that actually hears you")
            current = best[2]
            res = wake_test(current, "Test 2: 'Hey Genesis' with that microphone")
            print(f"      loudness {res.get('peak', 0):.0f}, heard: {res.get('heard') or 'nothing'}", flush=True)
        else:
            bad("None of the microphones picked up any sound.")
            print("      Check: is the mic muted (mute key / headset switch)? Is it plugged in?", flush=True)
            print("      Opening Windows Sound settings > Recording: speak and watch the green bar.", flush=True)
            try:
                subprocess.Popen(["control", "mmsys.cpl,,1"])
            except Exception:
                pass
            PROBLEMS.append("No microphone hears any sound")
            return finish(cfg)

    # heard, but rejected -> raise sensitivity / advise volume
    if res.get("hits", 0) > 0:
        ok("Genesis recognised 'Hey Genesis'!")
    else:
        new_sens = None
        if res.get("said_name") and (res.get("peak", 0) < res.get("min_level", 0) or res.get("conf", 0) < res.get("min_conf", 0)):
            warn("Genesis heard you say it, but your voice was too quiet / unclear to count.")
            new_sens = 0.85
        elif res.get("peak", 0) >= 60:
            warn("The microphone hears you, but the detector didn't catch 'Hey Genesis'.")
            new_sens = 0.85
        if new_sens and settings and sens < new_sens:
            settings.set("voice.wake_sensitivity", new_sens)
            CHANGES.append(f"Wake sensitivity raised from {sens:.2f} to {new_sens:.2f}")
            ok(f"Raised the wake sensitivity from {sens:.2f} to {new_sens:.2f} (it now also accepts just 'Genesis').")
            sens = new_sens
            res = wake_test(current, "Test 3: say 'Hey Genesis' again")
            print(f"      loudness {res.get('peak', 0):.0f} (needed {res.get('min_level', 0):.0f}), "
                  f"heard: {res.get('heard') or 'nothing'}", flush=True)
            if res.get("hits", 0) > 0:
                ok("Genesis recognised 'Hey Genesis' now!")
        if res.get("hits", 0) == 0:
            if res.get("peak", 0) < res.get("min_level", 0):
                warn("Your voice is still very quiet for the microphone.")
                print("      Opening Sound settings: Recording > your mic > Properties > Levels -> set it to 80-100.", flush=True)
                try:
                    subprocess.Popen(["control", "mmsys.cpl,,1"])
                except Exception:
                    pass
            PROBLEMS.append("Wake word still not recognised in the test")

    # 5. speech recognition ------------------------------------------------------------------------------------------
    step("5/5  Speech recognition")
    try:
        from genesis.voice.stt import create_stt

        stt = create_stt(GenesisConfig.from_env(), vosk_model=None)
        ok(f"Speech recogniser ready: {type(stt).__name__}")
    except Exception as exc:
        msg = str(exc)
        bad(f"Speech recogniser problem: {msg[:200]}")
        PROBLEMS.append("Speech recogniser: " + msg[:120])
    return finish(cfg)


def finish(cfg) -> int:
    print("\n==============================================================", flush=True)
    if CHANGES:
        print("  What I fixed:", flush=True)
        for c in CHANGES:
            print("   - " + c, flush=True)
    else:
        print("  I didn't need to change anything.", flush=True)
    if PROBLEMS:
        print("  Still to sort out:", flush=True)
        for p in PROBLEMS:
            print("   - " + p, flush=True)
    else:
        print("  Voice check passed.", flush=True)
    print("==============================================================", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
