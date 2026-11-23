# Genesis 3.2: communication, FRIDAY voice and autonomous debugging

This builds on your Genesis 2.2.4 code and the 3.0/3.1 updates, without rebuilding anything. All new code is in `genesis/v3`. It connects to the existing code through marked one-line hooks, and `scripts/apply_v3_hooks.py --remove` takes it all out again.

## 1. What Genesis already had (checked in your code)

| Requirement | Already in Genesis 2.2.4 | 3.2 change |
|---|---|---|
| Wake word | `voice/wakeword.py` ("hey genesis") | kept |
| Voice activity detection | Endpointer in `voice/vad.py`, plus Silero VAD inside faster-whisper (`vad_filter=True`) | kept |
| Local speech recognition | faster-whisper (`base.en`, CPU) | 3.1: no cloud fallback in local mode; optional `small.en` |
| Follow-ups without the wake word | conversation sessions in `voice/manager.py` | kept |
| Barge-in | wake word or speech louder than its own echo, with calibration | kept, plus **echo rejection in text** |
| Listening / thinking / speaking indicators | the orb (`ui/orb.py`) | kept |
| Coding tools | `diagnose_project`, `propose_fix`, `apply_patch` (with backup), `restore_backup`, `run_tests`, `run_program` (self-fixing), allow-listed commands | reused by the new **debugging loop** |

## 2. JARVIS reference (adewaskar/jarvis, MIT licence)

JARVIS is a browser app written in TypeScript. Its "brain" is Claude Code and its better voice is ElevenLabs, both cloud services, so its code can't run in Genesis. Four communication ideas were re-implemented in Python instead (no code was copied):

1. **Speak while thinking.** Answers are cut into sentences as they stream in, and each sentence is spoken straight away. Genesis now does this for spoken conversation replies from the local model (`v3/converse.py` and `provider._try_stream`).
2. **Echo rejection in text.** The microphone stays open while it talks, so a transcript that mostly repeats what it just said is ignored. Words like "stop", "wait" and "no" always get through (`converse.is_echo`, `SpokenLog`).
3. **Abbreviation-safe sentence cuts.** "Mr. Stark" or "3.5" is not the end of a sentence.
4. **Acknowledgements never repeat twice in a row** (`friday.pick`).

Not adopted: its 3D interface, Claude/ElevenLabs back ends, camera gestures and music.

## 3. Voice

- **Kokoro** is the main engine (Apache 2.0, about 350 MB, runs on the CPU so the GPU stays free for the language model). **Piper** and then Windows SAPI are the automatic fallbacks (`GENESIS_TTS=auto`).
- **"friday"** is the default voice. It's an **original** mix of three built-in Kokoro voice styles: 55% `bf_isabella`, 25% `bf_emma` and 20% `af_heart`. That gives a warm, confident, British-leaning female voice. No real person's voice is copied.
- **Irish accent:** Kokoro has no Irish voice, so a subtle Irish accent isn't achievable with it. It is not claimed.
- **Settings:**
  - pick a voice with `GENESIS_TTS_VOICE` (`friday`, `bf_isabella`, `bf_emma`, `af_heart`, `bm_george`, …);
  - the speed comes from Genesis's existing voice-rate setting;
  - `Setup_Natural_Voice.bat` lets you listen to the voices and pick one.

## 4. Autonomous debugging (`v3/debugger.py`)

Say "fix the failing tests in TwinSim" or "debug my TwinSim project". Genesis asks for confirmation once, then:

1. Runs the project's own tests (pytest, unittest or npm test, in the project's own `.venv`).
2. Finds the failure.
   - For a failed assertion, it looks at **the code being tested**, not the test file.
3. Has the local model propose the **smallest** fix.
   - The fix must apply exactly, still parse, and pass the existing safety scan: no shell commands, file deletion or access to credentials.
4. Backs up the file, applies the fix, then makes sure Python reloads the file.
   - Same-size edits made in the same second would otherwise run the old cached `.pyc`. The tests caught this bug.
5. Re-runs the tests.
   - If fewer tests fail, the fix is **kept**. If not, it's **undone**.
   - This repeats up to 3 times (configurable).
6. Reports exactly what was fixed and what still fails. It only says "fixed" when the test command really passes.
   - Every change is logged in `data\code_changes.jsonl`.
   - "undo the last fixes" restores the originals.
   - `.env`, key and secret files are never edited.

## 5. Test results (actually run)

On your Genesis 2.2.4 code with 3.2 installed:

| Check | Result |
|---|---|
| Full test suite | **632 of 633 pass.** The 1 failure is because `.env.example` wasn't in the zip you sent, so it doesn't apply on your PC. |
| Your scenario evaluation (`eval.run_scenarios`) | **23 of 23 pass**, the same as before any change |
| Debug loop on a real project with a real bug | it fixes the bug and the real tests pass; undo restores the original |
| Debug loop with a fix that doesn't help | the fix is undone, the code is left unchanged and Genesis reports "couldn't fix" |
| Debug loop with an unsafe fix | rejected; the code is unchanged |
| Streamed speech | sentences are spoken as the model writes them; Genesis doesn't say them twice; JSON steps are never spoken |
| Echo rejection | its own words are ignored; "stop" and "wait" get through |

**Requires your PC (no microphone, speakers, GPU or Windows here):**
- wake word in a real room;
- real interruption timing;
- offline voice with the internet disconnected;
- opening VS Code by voice;
- how the "friday" voice sounds;
- real speed on your RTX 5060.

To test all of these on your PC, run `Update_Genesis_3.2.bat`, then the benchmark (`python -m genesis.v3.bench`), then try the phrases in section 6.

## 6. Try

| What to do | What to say |
|---|---|
| Start a conversation | "Hey Genesis", then "what's happening?" |
| Follow up without the wake word | "and tomorrow?" |
| Interrupt | talk over Genesis while it answers |
| Debug a project | "fix the failing tests in TwinSim", then "undo the last fixes" |
| Turn streaming off | set `GENESIS_STREAM_SPEECH=false` in `.env` |

## 7. Limitations / not done

- You can't yet interrupt **while the model is still writing** a streamed answer, only once Genesis has started speaking. In practice speech starts after the first sentence.
- The debugging loop fixes one file per attempt. Multi-file refactors are proposed but aren't automatic.
- Teaching Genesis a workflow by demonstration is still not built.
