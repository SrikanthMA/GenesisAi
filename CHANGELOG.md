# Changelog

## 3.5.0 - Autonomous Coding Engine (GACE) + real mute

- New: `gace_build` / `gace_resume` / `gace_status` / `gace_rollback`: plan, write, install (after confirmation), test, diagnose,
  repair (bounded, undone if not better), report; checkpoints and rollback; project memory checked against real files.
- New: microphone mute that really stops capture (and wake word, push-to-talk), speaker mute, Ctrl+Shift+M, saved state.
- New: Coding page, mute bar in the main window and the pop-out.
- Existing commands such as "turn off the microphone" are unchanged. See GENESIS_3.5.md for limits.

## 3.4.0 - Business Automation

- New: Excel/CSV, Word, e-mail (read + draft only), data extraction, research with citation checks, six workflow
  templates, task history, a "Business automation" page and natural-language commands. See GENESIS_3.4.md.
- Originals are never changed; totals are computed in code; unverified values are marked, not invented.

## 2.2.4 — Fix: "HTTP 400 invalid argument" from Gemini Flash

- 2.2.2 asked Gemini Flash to answer without "thinking". The current `gemini-flash-latest` rejects that setting with a
  generic "Request contains an invalid argument." (HTTP 400), which the retry didn't recognise, so every chat failed.
  Now any 400 while that setting was sent is retried once without it, and the setting is never sent to that model again.
- 530 tests pass.

## 2.2.3 — Keeps working when Gemini says "too many requests"

- Free Gemini keys have a separate per-minute and per-day allowance for each model. When the configured model is
  rate-limited (or no longer exists), Genesis now asks the next Gemini model (Flash-Lite, Flash, 2.5 Flash-Lite, 2.5
  Flash, 2.0 Flash) instead of answering "I'm having trouble connecting". A busy model rests for a minute (an hour when
  its daily quota is used up). `GENESIS_GEMINI_FALLBACKS` sets your own list.
- Voice transcription tries Flash-Lite first (the largest free allowance), then Flash, so speaking doesn't use up the
  chat model's quota; the offline recogniser remains the last fallback.
- 530 tests pass.

## 2.2.2 — Natural spoken replies, smarter model, friendlier image messages

- **What Genesis says aloud sounds natural.** Code, tables, file names ("Setup_Images.bat" → "the Setup Images file"),
  settings, install commands and (bracketed asides) are no longer read out; at most three sentences are spoken and
  "the code is on your screen" replaces a program read letter by letter. The full text stays on screen.
- **Smarter answers:** Gemini Flash is asked to answer without "thinking", so the stronger model gives full, quick replies
  even when Genesis asks for a short answer. Update_Genesis.bat tests Flash with your key and switches from Flash-Lite
  only if it really answers (your .env is backed up).
- **Images:** "Creating your picture - this can take up to a minute…" shows while a free service works, and when no
  service can create it the reply is plain ("…so here's a real photo of a cat instead") with the reasons in brackets
  (shown, not spoken).
- 528 tests pass.

## 2.2.1 — Free image generation with no signup

- "Generate an image of a cat" works without any account: when Gemini (paid-only for images), Hugging Face (credits used
  up), Cloudflare (not set up) and Pollinations (now needs a key) can't make it, Genesis uses **AI Horde**, a free,
  volunteer-run image network (anonymous; usually under a minute or two; the prompt is seen by the volunteer's computer
  that renders it). Cloudflare via Setup_Images.bat stays the faster option.
- The reply no longer says "no image service is set up" when services were only skipped because they refused a few
  minutes earlier: it says which ones and why, and if all of them were cooling down it asks them again.
- A question mark no longer ends up in the picture's subject ("cat?" → "cat").
- 526 tests pass.

## 2.2.0 — Knows your window, codes anywhere, Colab in your Google account, sharper voice

Add-on again (`genesis/v2/window_context.py`, `coding_router.py`, `coding_tools.py`, `chrome_cdp.py`); existing files got
small hooks marked `v2 hook (2.2)`. 524 tests pass (508 earlier + 16 new); evaluation numbers unchanged.

- **Knows which window you're in.** "which window am I in?" — and coding requests go where you are. When you type into
  Genesis, it uses the window you were in just before.
- **Codes in any editor:** in an online compiler or coding site (Programiz, OnlineGDB, LeetCode, HackerRank, Replit, …)
  "write a program for bubble sort and run it", "change it to take input from the user", "fix the error", "run it".
  Genesis reads what's in the editor (keeps required class names / function signatures), pastes the new code, restores
  your clipboard and presses the page's Run button. Replacing editor content asks first. Never types into terminals or
  password fields. In VS Code/PyCharm, "write … here" types at your cursor.
- **Google Colab in your own Google account, in Google Chrome** (not the automated Edge window): Genesis starts your
  installed Chrome with a Genesis profile - sign in once, it stays signed in. Write ("write a program for fibonacci in
  colab and run it" - creates a notebook if needed), run, **edit** ("edit the cell to print the first 20 numbers") and fix
  cells. `GENESIS_COLAB_BROWSER=edge` restores 1.x behaviour.
- **Voice:** transcription uses Gemini's stronger Flash model (thinking off for speed), knows which window you're looking at
  and a coding/app vocabulary (Colab, C++, LeetCode, bubble sort…), and falls back to the same OpenAI-compatible connection
  the chat uses if Google's audio endpoint rejects the key. "what did you hear?" shows exactly what was understood.
- New tools: `active_window`, `code_in_window`, `colab_code`, `colab_edit`, `last_heard` (147 total).

## 2.1.1 — Image generation that works for free

- The key-less Pollinations endpoint used as 2.1's free fallback now refuses anonymous requests, so on accounts where
  Gemini has no image quota and Hugging Face credits are used up, every service said no. 2.1.1 adds **Cloudflare
  Workers AI** (FLUX.1-schnell, free daily allowance of about 170 images on a free account): run `Setup_Images.bat`,
  paste your Account ID and API token; it makes a test image before saving anything.
- Pollinations uses its current endpoint when you add `POLLINATIONS_API_KEY`.
- When no service can create the picture, Genesis now **shows a real photo of the subject instead** and says why each
  service refused (e.g. "Gemini: needs billing for images; Hugging Face: no free image quota left").
- 508 tests pass.

## 2.1.0 — Images that work, accurate voice, voice chat, Google Meet minutes

Add-on again: new code is in `genesis/v2/` (`images.py`, `gemini_native.py`, `voice_ai.py`, `meeting.py`,
`meet_tools.py`); existing files only got small hooks marked `v2 hook (2.1)`. 504 tests pass (488 earlier + 16 new);
evaluation numbers unchanged (set A 95.2 %, set B 90.2 %, scenarios 23/23).

- **"Show me an image of a dragon" no longer opens e-mail.** Speech recognition often hears *image* as *email*; nobody
  asks for "an email of a dragon", so 2.1 repairs it. "image of a cat", "I want a picture of a cat" now show a picture.
- **Image search** opens an image search in the browser when no picture can be downloaded (instead of failing), and a
  display problem can no longer turn a found picture into "something went wrong".
- **Image generation** tries Gemini (accounts with billing) → Hugging Face/OpenAI → the free Pollinations service
  (`GENESIS_IMAGE_PROVIDER=auto`, set by Update_Genesis.bat; your prompt text is sent to that service). A provider that
  says "no quota" is skipped for 30 minutes.
- **More accurate voice.** With a Gemini key, what you say after "Hey Genesis" is transcribed by Gemini (accents,
  context, "image" vs "email"); the offline recogniser is the automatic fallback. The wake word stays offline.
  `GENESIS_STT_ENGINE=faster_whisper` keeps all audio on the PC.
- **No actions on a guess from speech:** a spoken request that only the statistical classifier mapped to an action,
  with low confidence, is answered as conversation instead of being executed.
- **Voice chat mode** ("let's chat" / "stop voice chat"): natural spoken back-and-forth, remembers the conversation,
  keeps listening for 2 minutes between turns, runs only clearly-spoken commands.
- **Google Meet:** "open Google Meet", "start a new meeting", "join abc-defg-hij".
- **Minutes of meeting (MOM):** "take meeting notes" (always asks you to confirm everyone agrees to being recorded) →
  records the computer's sound + your microphone on this PC, transcribes as the meeting goes → "stop meeting notes" →
  Word document with summary, discussion, decisions, action items (owner, due), open questions, plus the transcript.
  Audio is deleted afterwards; recording stops by itself after 3 hours.
- New tools: `meet_open`, `meeting_notes_start`, `meeting_notes_stop`, `meeting_notes_status`, `voice_chat` (142 total).
- `Update_Genesis.bat`: one-click update (unblocks files, installs `soundcard`, enables the image fallback, restarts).

## 2.0.0 — Reliability, safety and task memory (add-on layer)

Existing 1.x code is not rewritten: new behaviour lives in `genesis/v2/`; existing files only received small hooks marked
`v2 hook`. All 460 existing tests pass unchanged; 28 new tests (488 total).

- **Understanding.** Commands quoted from someone else's content ("the email says delete all my files") are never acted on;
  fixes for unread-email questions, "remember that…" facts saved as contacts, "run the tests", "please open X and Y",
  message bodies swallowing "and then open…"; bare app names; dependent goals ("find my report, summarise it and email it")
  go to the planner as one task when a model is available. Held-out accuracy (untuned set B): 85.2 % → 90.2 %.
- **Approval binding.** An approval covers only the exact tool and arguments it was given for (SHA-256 fingerprint).
- **Duplicate guard.** Re-sending/re-creating the same thing within 10 minutes asks again, including when the earlier
  outcome was unknown.
- **Retry rules.** Only low-risk, repeatable tools are retried automatically.
- **Whole-plan review.** Model-made plans are checked for bad step references, capped at 4 consequential steps, and shown in
  full before the first confirmation, with where each step's data comes from.
- **Task memory.** Plans and steps are journaled in SQLite (new table only); "continue where you stopped" resumes after a
  restart, keeping the original protections; "what tasks are unfinished?" / "clear my task history".
- **Errors and cancellation.** Every failure is categorised with advice; "Cancel current task" in the tray stops before the
  next step.
- **Performance.** scikit-learn's import shrank Python's regex cache to 50 entries; restoring it makes parsing ~1 ms instead
  of 43–170 ms and the test suite 88 s → 56 s.
- **Voice.** An honest notice when no voice is installed for the reply language.
- Evaluation scripts (`eval/`) and docs: `docs/GENESIS_ARCHITECTURE_AUDIT.md`, `docs/GENESIS_EVALUATION.md`.

## 1.6.1 — Fixes from a real Windows run

- **Intent classifier works when Windows blocks scikit-learn.** Smart App Control / an Application Control policy can
  block scikit-learn's DLLs ("DLL load failed … An Application Control policy has blocked this file"), which dropped
  Genesis to a weak word-overlap matcher. Genesis now trains the *same* TF-IDF (word 1–2 + char 3–5 grams, sublinear tf,
  smooth idf, L2) + softmax logistic regression (C = 12) with its own NumPy implementation (`nlp/tfidf_numpy.py`):
  same 6,359 features, same 80.2 % cross-validated accuracy, 99.2 % identical predictions, ~1.5 s to train.
- **Turns end in noisy rooms.** The voice detector learned the room's noise only before you spoke; steady noise louder
  than its guess (fan, AC, laptop) counted as speech, so every turn ran to the 15-second cap. It now keeps tracking the
  noise floor (quietest moment of the last 1.5 s) and treats anything far below your speaking level as silence: turns end
  ~0.5 s after you stop, even with loud background noise.
- **No auto-sleep while you talk or while Genesis transcribes** (the first transcription downloads the speech model and
  can take ~30 s — the session used to end in the middle of it).
- **No more self-interruption.** Barge-in calibrated on its own voice before playback had actually started (slower when
  a voice for another language loads), then mistook its own voice for you. It now calibrates once speech is playing, over
  0.6 s, on the loud parts (90th percentile).
- Clearer logs: `nlu: open_application (rules, 0.95) — 12 ms into the turn`, `[LLM] response received in 820 ms`,
  `language: French → translated to English`, `utterance_transcribed (fr)`, `barge_in (speech over the reply)`.
- Silenced harmless warnings: Hugging Face symlink notice on Windows; Qt DirectWrite notices for legacy bitmap fonts.

## 1.6.0 — Any language

Talk to Genesis in your own language. It understands, acts, and answers — and speaks — in that language.

- **Detection, offline** (`nlp/language.py`): non-Latin scripts by Unicode block (Hindi/Devanagari, Kannada, Tamil, Telugu,
  Malayalam, Bengali, Gujarati, Punjabi, Odia, Urdu, Arabic, Persian, Hebrew, Russian/Ukrainian, Greek, Thai, Chinese,
  Japanese, Korean, Georgian, Armenian); Latin-script languages (French, Spanish, German, Italian, Portuguese, Dutch, and
  Romanized Hindi/Hinglish) by function-word profiles and diacritics. A rule-first guard keeps English commands English
  ("open la la land"), and e-mail addresses, URLs and paths are ignored. For voice, Whisper's language ID is used as a hint.
- **Translate in → English pipeline → translate out** (`LanguageService`): one LLM call turns the request into English
  (names, apps, file names, URLs and quoted text kept unchanged); everything downstream — rules, classifier, planner,
  safety, tools — is unchanged. Replies are translated back; questions are answered directly in the user's language by the
  chat model (no extra call). An LRU cache makes repeated phrases free. English costs nothing extra.
- **Voice:** the multilingual Whisper model (`base` instead of `base.en`) with language detection; the English vocabulary
  hint is only used for English. Replies are spoken with an installed Windows voice of that language (SAPI language IDs),
  falling back to speaking the English reply when no such voice exists.
- **Safety kept:** "oui", "nahi", "はい", "нет" … answer confirmations offline; the typed repository name, message bodies,
  e-mail addresses and recipient names are never translated; without the AI model Genesis says it can't translate instead
  of guessing.
- **Preferences:** "always reply in English", "reply in French", "reply in my language", "stop translating",
  "understand all languages", "what languages do you speak?" — plus Settings → *Languages* and *Reply in*.
- A small "🌐 French → “open YouTube”" note under translated replies shows what Genesis understood (shown, not spoken;
  `language.show_translation`).
- Fixed the misplaced comments for `llm.fast_routes` and `performance.prewarm` in `core/settings.py`.
- 19 new tests (`tests/test_multilingual.py`, voice fallback in `tests/test_voice.py`).

## 1.5.1 — Fixes

- **"Open Google Colab" by voice works.** Speech-to-text writes "Colab" as "collab", "co lab", "kolab" or "collaboratory", which used
  to be treated as an unknown app. These spoken forms are now mapped back to Colab when the sentence is clearly about it
  ("open google collab", "create a new collab notebook", "write code in collab to …"); "collab" in ordinary sentences is left
  alone. The speech recogniser's vocabulary hint now includes "Google Colab".
- **AI failures say why.** The log line is now e.g. `[LLM] request failed: HTTP 402 from huggingface (model …) — You have
  exceeded your monthly included credits…` instead of just "request failed". Running out of Hugging Face credits is reported as
  `error_kind: quota` with what to do, and AI calls pause for 5 minutes instead of failing on every request.
- **Mini window:** grows upwards inside the screen instead of past its bottom edge (no more `QWindowsWindow::setGeometry`
  warnings), and the fade animation no longer prints `Failed to disconnect … finished()`.
- "search youtube for X" while already on YouTube no longer searches for "youtube for X".
- Answering "to drink water" to "What should I remind you about?" no longer produces "to to drink water".

## 1.5.0 — GitHub, Word, Excel, PowerPoint

- **GitHub** (`services/github.py`, `tools/github.py`):
  - “open GitHub”, “open my GitHub repositories”, “open the genesis repo on GitHub”, and “show my repos”.
  - “create a private repo called demo-app”: asks first, then checks the repository exists.
  - “delete my GitHub repo demo” / “delete it”: you must type or say the repository's name AND answer yes. A wrong name
    deletes nothing. Afterwards Genesis checks that GitHub really no longer has it. The AI planner can never delete
    repositories on its own. Needs `GITHUB_TOKEN` in `.env` for everything except opening pages.
- **Word / Excel / PowerPoint** (`services/office.py`, `tools/office.py`):
  - Create files: “create a Word document about climate change”, “make a PowerPoint on solar energy with 5 slides”,
    “create an Excel sheet for my monthly budget”. The AI writes the content. The file is saved to
    `Documents\Genesis Documents` (never overwriting anything), opened in Office, and checked by reopening it.
  - Write into the open document: “write hello world in Word”, “write a paragraph about India” (after “open Word”),
    “type 10, 20, 30 in Excel”, “add a slide about results to my presentation”. Text is added at the end, new rows go
    below the data, and new slides go last. Genesis never saves or closes your file (Ctrl+Z undoes the change). If nothing
    is open, it creates a new file.
  - Spreadsheet formulas from the AI are limited to safe arithmetic functions. DDE/`cmd|` payloads are written as text.
- **Colab:** “write code in Colab to load data.csv and run it” in one sentence; “correct the code and run it again”.
- **Pictures:** more ways to ask — “show an elephant image”, “I want to see a picture of a tiger”, “search the internet
  for images of the Eiffel Tower”.
- New dependencies: python-docx, openpyxl, python-pptx.

## 1.4.1 — Faster

Measured where time goes: Genesis's own processing takes well under 0.1 s per request; almost all waiting was calls to
the AI model, and many requests made **two** calls in a row.
- **One model call instead of two:** questions, explanations and writing (“explain recursion”, “what's the capital of
  Japan”, “write a poem about rain”) go straight to the chat model; unfamiliar instructions (“I want to learn machine
  learning”) go straight to the planner. Time-sensitive questions (news, weather, prices) still search the web.
  Setting: `llm.fast_routes` (on).
- **No model call** for more everyday commands: “set volume to 50”, “add eggs to my to do list”, “weather in Bangalore”.
- **Shorter spoken answers:** voice replies ask for one to three sentences (300 tokens max), so they arrive and finish sooner.
- **Warm start:** app shortcuts are indexed and the HTTPS connection to the AI model is opened in the background at
  start-up, so the first request doesn't pay for it (`performance.prewarm`).
- Fix: a crash (“'NoneType' object has no attribute 'capitalize'”) when the AI model read a request as a window action
  without saying which one. The planner now infers it from the words, and any incomplete AI interpretation falls back to
  letting the model plan or answer instead of failing the turn.
- Demo polish: “what's on my to-do list?” (with a question mark) is understood locally; undoing a new to-do names it.
- **`python -m genesis --benchmark-llm`:** times several models with your own token and prints the `.env` line for
  the fastest one.

## 1.4.0 — Desktop agent

Built into the same project; every 1.0–1.3.1 command still works (403 tests, including the 25 scenarios from the brief).

### Hands: generic computer control (`services/computer.py`, `tools/computer.py`)
- `computer_observe / click / type / press / scroll / select / copy / paste / drag` operate **any** application. Targets are
  resolved in order: accessibility tree (UI Automation) → the element's own position → coordinates (always confirmed).
- `computer_task`: an observe → decide → act → verify loop for apps Genesis has no tool for (bounded steps and time; the
  screen text is treated as untrusted). Anything risky inside the loop is handed back for confirmation.
- Safety: never types into terminals, the Run box, Start search, Task Manager, IDE terminals or File Explorer's address bar;
  blocks Win+R/Win+X/Ctrl+Alt+Del and similar; refuses password fields; a newline is never an accidental Enter (Shift+Enter in
  chat apps, refused elsewhere); Send/Submit/Pay/Delete clicks, executables, double-clicks, Enter in forms/IDEs are confirmed;
  a confirmed click is not replayed if the window changed; never acts on Genesis's own window.

### Apps
- **WhatsApp** (`services/whatsapp.py`): WhatsApp Web in the automated Edge window, or the Windows app. Open, read the latest
  message from someone, unread chats, draft, send (**always confirmed**, recipient re-checked, verified in the chat), “reply to
  that”, “send it” / “don't send it (yet)”. “Message/text X” means WhatsApp; “send a message to X” goes to e-mail when X is a
  contact with an e-mail address. QR sign-in is yours; it's never bypassed.
- **Google Colab** (`services/colab.py`): open, new notebook, write code (LLM), run the cell, read output/errors, explain,
  fix and re-run. Code that deletes files, touches Drive credentials, mines crypto or opens tunnels asks first; AI-written code
  that reaches outside the notebook asks first.
- **Images** (`services/images.py`): search (Wikimedia Commons, Openverse — no key), or generate (HF FLUX.1-schnell / OpenAI);
  the picture is saved to `data/images` (small cache) and **displayed in the chat**.
- **Forms** (`tools/forms.py`): “fill this form using my resume” — reads your résumé, maps fields (LLM or offline rules), never
  fills passwords/ID/bank numbers or guesses, asks for what's missing; “change that answer”, “don't submit it yet”, submit
  only after confirmation and only on the same page/window.

### Brain
- Self-diagnosis (`services/diagnostics.py`): failed app launches (not installed / missing executable / permission / crashed /
  blocked), websites (offline / DNS / site down / timeout / browser problem) and the AI model, explained in plain words.
- Tools declare output schemas; tools can return follow-up steps; deferred sends only on explicit “not yet” wording.
- Persona (`core/persona.py`): “Done, Boss.” (`persona.address` setting, blank = none), calmer wording.
- Voice: female voice preference (`voice.gender`), specific voice (`voice.tts_voice`), and an optional offline neural
  **Piper** voice (`python scripts/download_models.py --voice`, `pip install piper-tts`).

### Review fixes
An independent review of this release found ways a postponed message could be sent to the wrong person, commands could be
typed into Explorer/IDE terminals, and some misrouted phrases; all were fixed and have regression tests.

## 1.3.1 — AI brain fix

Genesis said “no API key configured” even when a key had been added. The LLM layer is repaired; nothing else was removed.

### Root causes fixed
- A `.env` saved by Notepad as **UTF-8 with BOM** hid the first key (python-dotenv read it as `\ufeffHF_TOKEN`). UTF-16 files failed too.
- A file saved as **`.env.txt`** (Windows hides extensions) was never read.
- An **empty** `HF_TOKEN` inherited from the terminal/OS blocked the value in `.env` (`override=False`).
- Only `HF_TOKEN`/`OPENAI_API_KEY` were recognised; `LLM_API_KEY`, `HUGGINGFACE_TOKEN` etc. were ignored, and a key for another
  provider was never used.
- `--doctor` reported the LLM as OK when it wasn't. Editing `.env` needed a restart, and a single network blip disabled the model for 60 s.

### New
- `core/envfile.py`: robust `.env` loading (BOM/UTF-16, `.env.txt`, empty variables, live reload) with a report of key *names* only.
- Config: `LLM_PROVIDER`, `LLM_MODEL`, `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_TEMPERATURE`, `LLM_MAX_TOKENS`, `LLM_TIMEOUT` (existing names still
  work), token aliases, key-prefix detection, Anthropic and Gemini providers.
- LLM service: bounded retries with exponential backoff, typed errors (not configured / invalid key / model not found / network /
  timeout / rate limited), short cooldowns, automatic reload when `.env` changes, and `health()`.
- Health check: `python -m genesis --check-llm` (JSON, exit 0/2), `--doctor`, **Settings → Test AI connection**, and the
  `check_ai_connection` tool (“check your AI connection”). Secrets are never logged or returned.
- `[LLM]` structured logs (provider initialised, request started, response received, request failed) without keys or content.
- Routing: questions, explanations and writing (“explain recursion”, “write a Python function that…”) go straight to the LLM.
  Commands still use the fast local path, and multi-step requests still use LLM tool planning.
- Clear error: “I'm having trouble connecting to my AI model — <reason>. <how to fix>.”

## 1.3.0 — Intelligence upgrade

Built into the same project. Nothing was removed: every 1.0–1.2 command, tool, setting and table still works.

### Understand → remember → reason → plan → act → verify → recover → learn
- **Context engine** (`core/context.py`): a compact snapshot is sent to the LLM instead of the whole conversation. It covers the current app,
  website, task, previous command, entities in focus, results on screen, latest correction, preferences and standing instructions.
- **Goals and unseen tasks:** requests Genesis has no template for (“prepare my computer for coding”, “get my newest stuff sorted”)
  are planned by the LLM over the tool registry, using that context. If a step fails, Genesis **re-plans once** with a different
  approach, and never loops. A routine whose name matches the goal is used first. Offline, there's a minimal coding setup.
- **Research goals:** “find five AI internships that I can apply for” searches, reads the top pages **in parallel**, compares them
  and presents the best matches with requirements and links. Nothing is applied for or sent.
- **Corrections:** “No, I meant AI internships”, “actually make it 10 AM”, “no, Spotify”, “not that, jazz”. These **modify the
  previous task** instead of starting over. A reminder or event that was already created is moved, not duplicated.
- **Undo:** “undo that” reverses the last reversible action, and running it again goes one step further back:
  - created, renamed and moved files;
  - calendar events;
  - reminders, tasks and notes;
  - opened apps and the volume;
  - code changes;
  - memories.
  Undo still asks where needed. Irreversible actions say so (“that e-mail has already been sent”).

### Identity & name recognition
- Genesis keeps a canonical name, a preferred name (“call me Sri”), nicknames and learned **speech-recogniser spellings**.
- Spoken variants (“Shri Kant”, “Srikant”, “Shreekanth”) are matched with a phonetic key and a similarity score:
  - **confident** matches are accepted;
  - **uncertain** ones get **one** question (“did you mean Srikanth?”) and the answer is learned;
  - a rejected spelling is never asked about again.
- “It's spelled Shrikanth” fixes just that part of the full name. The name is fed to the speech recogniser as a vocabulary hint, and
  greetings use it (“Yes, Srikanth?”). Works for any user's name; set it in Settings → General.

### Memory
- Typed preferences: “I prefer Gmail/Outlook”, “my default browser is Firefox”, “use Bing for search”, “I like Spotify for music”.
  These are applied automatically: “open my email”, “open my browser”, “play some music”.
- Standing instructions: “from now on always keep emails under 100 words”. They're included in the LLM context.
- “Why did you open Outlook?” explains which preference or recovery drove the action.
- “What do you remember” is organised by category: you, preferences, instructions, facts, contacts, projects, routines, and
  the apps you use most. Nothing is stored unless you ask (or you set a preference).

### Universal web & screen layer
- In Genesis's browser:
  - “what's on this page” / “what can I click” lists the named buttons, links, fields and headings;
  - “find the internship application button”;
  - “click it”, “click the second button”;
  - “scroll down”, “go to the bottom”, “press Enter”;
  - “summarize this page”.
- Elements are found by accessible name and role, **never by screen coordinates**.
- The same commands work on the **active window** through Windows UI Automation when the browser isn't in use.
- “Summarize this page” falls back to one on-demand screen capture if the page isn't in Genesis's browser.

### Coding agent (project level)
- “Add dark mode to my site project”:
  - picks the relevant files and gets exact edits from the LLM;
  - validates them (unique matches, syntax, JSON, risky-code scan);
  - **shows the diff** and asks, keeps backups, applies the change and runs the project's tests;
  - “undo that” restores everything.
- **Safe developer commands:** “run npm test”, “run python -m pytest”, “run git status”. An allow-list; no shell, pipes or chaining.
  Installs and `git push` are HIGH risk.
- `open_terminal` opens a terminal in the project folder.

### Productivity
- **To-do list:** “add buy milk to my to-do list”, “what's on my to-do list”, “mark buy milk as done”.
- **Notes:** “take a note: …”, “what did I note about Rahul”. Both are stored locally (database migration v3) and shown on the Memory page.
- Reminders can be cancelled (and undone).

### Recovery & verification
- If an app isn't installed or won't start, Genesis opens its **web version** (Spotify, WhatsApp, Teams, Outlook, Office, Slack, Discord,
  Zoom, Notion, Telegram, VS Code…) or your default browser, and says so.
- A fully recovered step counts as success, so there's no misleading “failed”.

### Efficiency
- Deterministic LLM calls (NLU, planning, JSON) are cached for 10 minutes.
- YouTube and web search results are cached for 5 minutes, so “play the second one” needs no second request.
- Result pages are read in parallel.
- LLM prompts carry the compact context plus the last few turns instead of the full history.

### Tests
- 305 automated tests (was 263). 42 new tests cover identity, wake phrase, conversation corrections, context, undo,
  preferences/instructions, goals and re-planning, the browser and screen layers, project changes, command validation, recovery,
  permissions, caching, and tasks/notes.

## 1.2.0 — From commands to an agent

Built on top of 1.1 in the same project. Nothing was removed: all 1.0/1.1 commands, tools, settings and data still work.

### How Genesis thinks now
Input → **understanding** (rules → classifier → LLM, now with typo/slang repair) → **context** (focus, lists, errors, history,
preferences) → **planning** (rule templates, or an LLM plan validated against the tool registry) → **tool selection** → **execution**
→ **observation/verification** → **correction/retry** (transient retries, alternative methods, code fix loops) → **natural reply**.
The UI shows the new **Planning** and **Retrying** states.

### New capabilities (16 new tools, 81 in total)
- **YouTube:** “search YouTube for …”, “play Arijit Singh on YouTube”, “play the latest MrBeast video”, then “play the second one”.
  After “open YouTube”, “search for Python tutorials” searches YouTube.
- **Media control:** “pause”, “resume”, “skip”, “previous”, “make it louder”, using system media keys (works for YouTube in the
  browser, Spotify and others).
- **Windows:** “what windows are open”, “switch to Chrome”, “switch to the second one”, “minimize it”, “maximize VS Code”,
  “show the desktop”, “take a screenshot” (saved to Pictures\Screenshots).
- **Calendar (Google):** create (“put a meeting with Rahul tomorrow at 4”), read (“what do I have tomorrow / this week”), move
  (“move my 3 PM meeting to 5 PM”) and cancel (“cancel the meeting tomorrow”). It **asks for the time** instead of guessing, and
  asks which event when more than one matches.
- **Coding agent:** “create a Python script that …”. Genesis writes the code with the LLM, creates a project in
  ~/GenesisProjects, opens it in VS Code, runs it with a timeout, reads the error, fixes it and re-runs (max 3 attempts), then
  explains the result. Runs Python, JavaScript, TypeScript and Java; HTML/CSS opens in the browser. A language registry makes adding more
  languages a one-entry change. “run it”, “fix it”, “search the internet and tell me why this error is happening”.
- **Gmail:** “find the latest email from AMD, summarize it and draft a reply saying I'm interested” works as one request.
  Attachments are listed; “emails with attachments”; “summarize it”; “draft a reply”.
- **Web answers:** “search the internet and tell me …” and “find the latest information about NVIDIA” search, read the results
  and answer with links. Result reading now works without the optional `ddgs` package, and page reading falls back to a plain
  download if browser automation fails.
- **Memory:** preferences (“my preferred coding language is Python” is used by the coding agent; “forget that” removes it) and
  **task memory** (“find that Python tutorial we were talking about yesterday” → “open the first one”).
- **Understanding:** typo repair for command words (“opne youtub”), slang (“pls”, “yt”, “gimme”), “stop, open Spotify instead”,
  loosely phrased requests routed to the new tools by the LLM.

### Safety
- Creating files, running code and changing the calendar are MEDIUM risk and ask first. Cancelling an event always asks.
  Code that deletes files, runs shell commands, edits the registry, touches credentials or hooks the keyboard is flagged and
  running it becomes HIGH risk, which always needs an explicit yes.
- Programs run without a shell, inside their project folder, with a timeout. API keys and tokens are removed from their environment.
  Automatic fixes only touch projects Genesis created, are backed up, and are rejected if they introduce risky operations.
  Genesis never installs packages; it tells you the command.
- The LLM planner still cannot choose sending, deleting, power, code-writing or code-running tools.
- A new, narrow `trusted_input` exemption: rule-planned, read-only steps whose input can't be steered by external content (such as
  opening the e-mail our own search just found, or summarising it) no longer ask for confirmation. LLM-made plans never get the exemption.
- `.gitignore` now also blocks `.env.*`, key/credential JSON files and `data/backups/`.

### Tests
- 263 automated tests (was 218), including 45 new ones for NLU/tool selection, YouTube, media, windows, calendar, Gmail agent
  tasks, web answers, the coding agent (programs are really executed), memory/history, permissions, LLM handling and the spec's
  12 end-to-end scenarios in one continuous conversation.

## 1.1.0 — Conversation mode & agent upgrade

Built on top of 1.0 with nothing removed: every 1.0 command, tool, setting and database table still works.

### Conversation
- **Continuous conversation.** Say “Hey Genesis” once and keep talking. Follow-ups don't need the wake word.
- **Automatic sleep** after a configurable silence (default 20 s, or 45 s after Genesis asks you a question). The countdown starts **after** Genesis finishes replying and pauses while a task runs. There is also a hard session cap (default 30 min).
- **Say “go to sleep”, “that's all”, “bye” or “thanks, that's it”** to end the session yourself. Re-waking always works without a restart.
- **Explicit state machine:** SLEEPING → WAKING → ACTIVE_CONVERSATION → THINKING → EXECUTING → VERIFYING → WAITING_FOR_USER → RETURNING_TO_SLEEP. The orb, the mini window, the chat header and the tray all show it.
- **Interruptions.** Say “Hey Genesis” while it's speaking, or (with a headset) just start talking, and it stops and listens. “Stop, open Spotify instead” replaces a pending action. Confirmations are **never** skipped by an interruption.
- **Smarter end-of-turn detection.** The pause needed to end a sentence adapts to longer utterances, and is configurable.
- **Single-command mode** is available (Settings → Conversation mode → turn off continuous conversation).

### Context & follow-ups
- “open it”, “close that”, “open the second one”, “the latest one”, “no, the other one”, “open the one from Rahul”
- “do the same thing again”, “try again” (retries the last failed request), “do that for Rahul too”
- “go back” (previous folder or browser page), “use the one we discussed earlier”
- “reply saying …”, “actually change tomorrow to Friday”, “make it shorter”, “add that …”, “change the subject to …”, “send it”
- Genuinely ambiguous references get a short question instead of a guess.

### Agent
- **Full e-mail workflow in one conversation:** open Gmail → check unread → open the latest → reply (correctly threaded) → revise → send (with confirmation).
- New Gmail abilities: open/read one message, reply in-thread, update an existing draft.
- **Recovery.** Transient read failures (network, HTTP 429/5xx) are retried once. Sends, deletes and other side effects are never retried automatically. App launches try alternative install locations.
- **Verification.** Apps are checked after open/close. Genesis says “I couldn't verify…” instead of claiming success.
- Compound requests are capped (8 per turn, 240 s budget, 12 plan steps) so nothing can run away.
- “open notepad and spotify” opens both.

### Memory & routines
- “Remember that my project folder is …”, “what do you remember about …”, “forget that”
- **Routines:** “Every Monday at 8 AM, open VS Code and my Genesis project”, “create a routine called focus that opens notepad and spotify”, “run my focus routine”, “list my routines”. A routine runs through the same permission checks, so risky steps still ask. A routine still runs if the PC was busy at the exact minute (10-minute catch-up, once per day).
- **Usage learning** (names of tools and apps only, never content). Genesis offers “save that as a routine” when you repeat the same pair of commands. You can turn this off, view it and clear it.

### Screen understanding (foundation)
- “What's on my screen?”, “read the screen”, “what buttons are there?”, “click Save”
- The screen is captured **only when you ask**. Output is structured (text lines, named controls, window title). Clicks go **by control name through Windows UI Automation** and always ask first. There are no coordinate clicks. Sending a screenshot to an online vision model asks first.

### Coding agent (foundation)
- “Check my calc project for errors” → “fix the first one” → Genesis proposes a minimal fix and **asks** before editing, backs up the file, applies the fix, runs the project's own tests and reports the result. “undo that” restores the backup.

### Settings, privacy & logging
- New Settings sections: **Conversation mode** and **Privacy & safety**. Memory page: **Routines** and **Usage patterns**.
- New structured log `logs/session.log` records state transitions, wake-word detections, utterance start/end, session start/end and timeouts.
- **Logs no longer contain what you say or e-mail bodies by default.** They record lengths, intents and tool names instead. You can opt in with Settings → Privacy → “Debug: include what I say in log files”.

### Database
- Migration **v2** (automatic, additive): `routines`, `usage_stats` and `sequence_stats` tables. Existing data is untouched.

### Tests
- 218 automated tests (was 164), including 54 new tests for sessions, voice conversation and barge-in, follow-ups, the e-mail workflow, recovery and verification, memory, routines, screen, the coding loop and migrations.
