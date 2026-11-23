# Genesis 3.1: conversation, projects, Mission Mode and natural voice

## Install

1. Extract `Genesis_3.1_Update.zip` into your Genesis folder and choose **Replace all**.
2. Run **`Update_Genesis_3.1.bat`**. It connects the new parts and runs the tests. It then offers to:
   - install the natural voice;
   - benchmark your models.

You can also run the natural voice setup on its own at any time with **`Setup_Natural_Voice.bat`**.

## What's new

| Area | What it does | How to use it |
|---|---|---|
| **Conversation modes** | Casual, coding, study, productivity, supportive, mission. Each changes tone and detail. Coding mode sends conversation to Andrew. | "switch to coding mode", "study mode", "what mode are you in" |
| **Natural conversation** | Every reply gets rules against stock openers ("Certainly!"). It doesn't repeat the opening of its last 6 replies, keeps answers short for chat and detailed for technical questions, and uses earlier messages for follow-ups. It says "let me check" instead of guessing. | automatic |
| **Empathy** | When you sound upset, that one reply acknowledges it and asks what happened instead of jumping to fixes. The mode doesn't change. | automatic |
| **Project intelligence** | Answers from your real Git history, file timestamps, README and TODO notes in your registered project folders. Only those folders are read, and only when you ask. | "what did I work on yesterday", "which files changed recently in genesis", "check the latest changes in TwinSim", "open my TwinSim project and check the latest changes", "summarize my TwinSim project", "any unfinished tasks in twinsim", "what should I work on today" |
| **Mission Mode** | Runs fixed read-only steps: locate, structure, docs, Git, code checks, TODOs. It writes a Word report and opens it. Each step's progress is recorded. It has a time limit and can be cancelled. It never changes your project. | "my TwinSim project is due tomorrow - check it, find missing documentation and prepare a report", "mission: genesis project" |
| **Emergency stop** | Stops speech at once, cancels a running mission and blocks follow-up steps for a few seconds. | "stop everything", "emergency stop" |
| **Specialist models (TwinSim)** | Register a specialised model and the skills it's good at. It's used **only** for requests that name those skills, never for general chat. Its weights are never changed. | "register model twinsim-slm as TwinSim for twin simulation, sensor data" |
| **Natural voice (Kokoro)** | Neural offline voice. It streams sentence by sentence, with natural pauses, an adjustable speed, 6+ voices and instant interruption. It's chosen automatically once installed. | `Setup_Natural_Voice.bat` |
| **Benchmark** | Measures each installed model on 8 Genesis tasks: speed, first-token time, memory and GPU use. | `.venv\Scripts\python -m genesis.v3.bench --label baseline` |

To register a project folder, say "remember that my TwinSim project is in C:\Users\you\Projects\TwinSim". This uses Genesis's existing project memory.

## Voice engines compared

| | Windows SAPI (Zira) | Piper (already supported) | **Kokoro (new default when installed)** |
|---|---|---|---|
| Naturalness | robotic | clear but flat | most natural of the three |
| Size | built in | about 60 MB per voice | about 340 MB (model and all voices) |
| Runs on | CPU | CPU | CPU (leaves the GPU for the language model) |
| Offline | yes | yes | yes, after the one-time download |
| Voices | 2 | one per download | 6 English shown in setup (US and UK, female and male) plus Hindi |
| Licence | Windows | MIT / GPL depending on version | Apache 2.0 (model), MIT (kokoro-onnx) |

Genesis chooses Kokoro first, then Piper, then SAPI. To force one, set `GENESIS_TTS=sapi`, `piper` or `kokoro` in `.env`, and change the voice with `GENESIS_TTS_VOICE`.

## What was tested, and what wasn't

- **Checked on your own Genesis 2.2.4 code** (from `Genesis_source.zip`):
  - with 3.1 installed, 618 of 619 tests pass. The one failure only happens because the zip left out `.env.example`, so it can't fail on your PC;
  - your own scenario evaluation (`eval.run_scenarios`) passes 23/23 both before and after 3.1, so nothing that worked broke.
- **Offline speech recognition:** 3.1 sets `GENESIS_CLOUD_VOICE=false`, so Genesis's Whisper recogniser runs on your PC. It already filters silence with Silero VAD (faster-whisper's built-in `vad_filter`). The updater can also upgrade the model from Whisper `base.en` to `small.en`, which is more accurate. It stays on the CPU so the GPU is free for the language model.
- **Already in 2.2.4 and kept as they were:**
  - the assistant orb's listening, thinking, executing, verifying and speaking states;
  - screen reading with local OCR;
  - saved routines;
  - interrupting Genesis while it speaks (barge-in).
- **Tested here:** 549 automated tests passed, including:
  - project tools on a real Git repository;
  - Mission Mode, with its report written and checked;
  - emergency stop during a mission;
  - mode routing and the rules added to the prompt;
  - specialist routing;
  - Kokoro sentence streaming and interruption, with a stand-in synthesiser and speaker;
  - the benchmark scoring.
- **Not tested here:**
  - real Kokoro audio, because this environment has no speakers or Windows;
  - real model speed and quality, because there's no GPU or Ollama here;
  - the `.bat` scripts, because there's no Windows here.
- **Measured on your PC:** run the benchmark there. It saves real numbers to `logs\benchmark-*.md`. No benchmark results are claimed until it has run.
- **Not built yet:** teaching Genesis a workflow by demonstration (recording and replaying your actions), and new UI indicators. Genesis's existing screen reading (`read_screen_text`, `analyze_screen`) and saved routines are unchanged.

---

# Genesis 3.0.2 — Friday mode, local AI, digital twin and work tools

## Friday mode (3.0.2)

- **Female voice.** Genesis uses the most natural female voice installed in Windows. If its own speech engine is missing, it falls back to Windows's built-in voice, so it never goes silent.
- **It greets you** when it starts: "Good evening, Boss. Genesis is online." When it hears the wake word, it answers "Yes, Boss?"
- **It acknowledges each task** straight away ("On it, Boss." or, for longer jobs, "Okay Boss, working on it now. This takes a minute."), then **reports back** ("Done, Boss. …" or "Sorry Boss. …"). Questions just get the answer.
- **Typed requests are spoken too.** Spoken replies stay short, and the details stay on screen.
- `.env` switches: `GENESIS_FRIDAY=true` and `GENESIS_SPEAK_TYPED=true`. Set either to `false` to turn that behaviour off. "Voice replies" off in Settings silences everything.

## Updating

Extract `Genesis_3.0.2_Update.zip` into your Genesis folder (Replace all), then double-click **`Update_Genesis_3.0.2.bat`**. It brings voice back, adds everything below, saves a report to `logs\genesis3_diagnose.txt` and restarts Genesis. Your models and settings are kept.

## New in 3.0.1

| Say | What happens |
|---|---|
| my favourite pizza is a Farmhouse from Domino's | remembered as your preference |
| i always order cold coffee from Starbucks | remembered |
| which pizza do I like? / what's my favourite pizza? | answers from your saved preferences only |
| i want to order pizza / order my usual pizza from Zomato | picks *your* usual and opens Swiggy/Zomato/Domino's with it; **you** check the cart and pay; Genesis never orders or pays |
| answer as me: are you free this weekend? / reply like me to: bro where are you | your **digital twin** answers in the first person, in your style, from your saved facts |
| open wikipedia on black holes and copy it into word | opens the article and puts its text (headings + paragraphs + source link) into a new Word document |
| search wikipedia for Alan Turing | opens the article and reads you the start |
| write an IEEE paper on federated learning in healthcare | two-column IEEE conference paper (title, author block, Abstract, Index Terms, I–V sections, References placeholders) in Word |
| draft a report on climate change in India, with 3 case studies | report/essay/letter/proposal/assignment written section by section, opened in Word |
| create an excel sheet for my monthly expenses | Excel sheet with headers, example rows, formulas, totals, opened in Excel |
| write a snake game in python in vs code | Andrew writes the program, saves it in Documents\Genesis Code, opens it in VS Code |
| write a linear regression on the iris dataset in colab | code into a new Google Colab notebook cell (or a notebook file + Colab, if browser control isn't set up) |

Genesis also uses your saved facts in normal conversation (most relevant first), so Suzanne answers like someone who knows you. It never invents facts about you.

Voice: `GENESIS_CLOUD_VOICE=true` (default) keeps your voice working exactly as in 2.2.4. Set it to `false` for fully offline voice. That only works if a local speech engine (Vosk/Whisper) is installed.

---

# Genesis 3.0 — Local AI (Prototype 1: local-first intelligence)

Genesis can now think entirely on your PC with **Ollama**: no API keys, no tokens, nothing sent to the cloud.

## Install (once)

1. Close Genesis.
2. Extract `Genesis_3.0_LocalAI_Update.zip` **into your Genesis folder** (let it add the files; it doesn't replace any of your existing files).
3. Double-click **`Setup_Local_AI.bat`**. It:
   - **0.** connects the 3.0 parts to your Genesis (one marked line at the end of a few files; originals backed up in `backups\`). If Genesis doesn't load afterwards it undoes this by itself.
   - **1.** installs and starts Ollama (asks first).
   - **2.** measures your RAM, GPU memory and disk, and recommends models.
   - **3.** downloads only what you agree to, after checking disk space.
   - **4.** optionally builds Genesis's own model, `genesis-slm` (instant, no training).
   - **5.** switches `.env` to local mode (backed up first).
   - **6.** runs a real self-test on your hardware and restarts Genesis.

Expected for your PC (RTX 5060 8 GB, ~16 GB RAM): Suzanne `llama3.2:3b` (~2 GB), Andrew `qwen2.5-coder:7b` (~4.7 GB), optional `qwen2.5:14b` (~9 GB) for complex tasks. That one shares system RAM and is slower; Genesis loads it only for level-3 requests.

## How it works

| Part | Job |
|---|---|
| **Genesis SLM** (`genesis-slm`) | Genesis's identity and conversational core, used as Suzanne's model |
| **Suzanne** | Personal: conversation, preferences, memory, feelings ("Boss", naturally) |
| **Andrew** | Technical: code, explanations, summaries, plans, and Genesis's own structured steps |
| **Model router** | Picks one agent and the smallest capable model per request |

Routing per request:
1. Plain arithmetic is computed exactly in Python, with no model. File actions are already Genesis tools.
2. Technical or structured requests go to **Andrew**; everything else goes to **Suzanne**.
3. Level: **1** simple, **2** moderate, **3** complex. The thresholds are in `.env` as `GENESIS_LEVEL2_CHARS` and `GENESIS_LEVEL3_CHARS`.
4. Level 3 uses the complex model only if it's installed and fits in free memory.
5. For a simple chat, a model that's already loaded is preferred, so Genesis doesn't swap models.

If a model runs out of memory, Genesis unloads other models and retries, then tries a smaller one. Every call is logged locally (model, agent, level, time, tokens/second).

The chat window's AI badge shows which agent and model answered, and whether it ran locally or externally.

## Things to say

| Say | Does |
|---|---|
| check my local AI setup / which model answered? | mode, Ollama, models in memory, hardware, last route |
| recommend models for my PC | hardware-based suggestions (no download) |
| how fast are the models | speed and failures over the last 7 days |
| download the model phi4-mini | checks disk and memory, asks, downloads |
| use qwen2.5:7b for andrew / set Suzanne's model to gemma3:4b | switch an agent's model |
| free GPU memory | unload models |
| save that as a good answer | adds the last Q&A to the Genesis SLM dataset |
| no, the right answer is: … | saves your correction |
| build the genesis slm | new version + evaluation (not activated) |
| use genesis slm version 2 / roll back to genesis slm version 1 | switch versions |
| export the training dataset | JSONL for fine-tuning |
| export my local AI data / delete my local AI data | privacy |

## Privacy and external AI

`.env` after setup:

```
GENESIS_MODE=local
GENESIS_LLM_PROVIDER=genesis
GENESIS_EXTERNAL_AI_ENABLED=false
GENESIS_MEMORY_MODE=local
SUZANNE_MODEL=genesis-slm
ANDREW_MODEL=qwen2.5-coder:7b
GENESIS_COMPLEX_MODEL=
```

In local mode, Genesis sends nothing to any AI service: not chat, not speech recognition, not image generation, not embeddings, not screen vision. Image *search* (Wikimedia) and web search are not AI, so they keep working.

To allow an external fallback, set both lines in `.env`:

```
GENESIS_EXTERNAL_AI_ENABLED=true
GENESIS_EXTERNAL_PROVIDER=gemini
```

The external model is used only when no local model can answer, and the reply says so. To go back to your 2.x cloud setup, set `GENESIS_MODE=hybrid` and your old `GENESIS_LLM_PROVIDER`. A `.env.before-local-ai-…` backup is in your Genesis folder.

## Genesis SLM: improving it over time

Nothing retrains by itself. You decide every step:

1. After good answers, say "save that as a good answer". For wrong answers, say "no, the right answer is: …".
2. Say "build the genesis slm". This makes a new version with Genesis's identity plus your examples (instant). It's scored on a fixed test (identity, no invented memories, resisting prompt injection, knowing it runs locally, brevity).
3. Compare with "show the genesis slm". Then say "use genesis slm version N", or roll back at any time.
4. Optional real fine-tuning (LoRA/QLoRA, on your GPU), once you have ~50+ examples:
   - "export the training dataset";
   - follow the steps at the top of `scripts\train_genesis_slm.py`;
   - "build the genesis slm with adapter <folder it prints>".

## Removing 3.0

```
.venv\Scripts\python scripts\apply_v3_hooks.py --remove
```

Then set `GENESIS_MODE=hybrid` in `.env`. Genesis behaves exactly like 2.2.4 again.
