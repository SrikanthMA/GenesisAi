# Genesis implementation plan and status

## Environment inspection (build machine)

| Item | Finding | Consequence |
|---|---|---|
| OS | Linux container (the build sandbox); **target is Windows 10/11** | Windows-specific code (SAPI, `os.startfile`, `LockWorkStation`, `RegisterHotKey`, registry Run key, DPAPI) sits behind a platform backend. Tests use a recording backend. |
| Python | 3.11 | code targets Python ≥ 3.10 |
| Node.js | 22 (not needed) | a pure-Python stack was chosen: one runtime and one installer |
| GPU | none | CPU-only models: faster-whisper int8, a remote/Ollama LLM, TF-IDF classifier |
| Existing Genesis code | none | built from scratch |
| Package index | blocked in the sandbox (PySide6/pytest could not be installed there) | the core is fully tested; the UI was linted and executed against a Qt stub layer. The **real PySide6 run happens on your Windows machine** |

## Architecture decision

**PySide6 desktop shell + in-process Python core + SQLite.** Electron/Tauri with a Python sidecar was rejected because it would mean two runtimes, a local HTTP port to secure, and a harder setup for VS Code users. Qt provides the tray, frameless translucent windows, animation and native dialogs.

## Components and status

| Area | Status |
|---|---|
| Config (.env), paths, JSON logging with redaction, SQLite + migrations, event bus, settings | ✅ |
| LLM abstraction: HF (Qwen2.5-7B-Instruct), OpenAI-compatible, Ollama, offline ELIZA; resilient degradation | ✅ |
| NLP: normalisation, compound splitting, rules, TF-IDF+LR classifier, LLM NLU, entities, temporal grammar | ✅ |
| Dialogue manager: state, referents, coreference, slot filling, confirmation sub-dialogue, request queue | ✅ |
| Agent: rule planner, LLM planner with validation, executor (templates, schema, permissions, confirmation, verify, audit, recovery), verifier | ✅ |
| 45 tools: apps, files, web, browser (Playwright), system/power, Gmail, Calendar, reminders, VS Code, memory, knowledge, settings | ✅ |
| Google OAuth (PKCE, loopback), Gmail drafts/send/search, Calendar events | ✅ (needs your OAuth client) |
| Memory manager + knowledge base (RAG) | ✅ |
| Security: permissions, confirmations, validators, injection defences, secret store (Credential Manager / DPAPI vault) | ✅ |
| Voice: mic, wake word (Vosk/Porcupine), VAD, STT (faster-whisper/Vosk/HF), TTS (SAPI/pyttsx3) with barge-in, state machine | ✅ |
| Desktop UI: mini assistant, expanded chat, pages, tray, orb animations, hotkey, single instance, start with Windows | ✅ |
| Tests (164), docs, CO1 report, literature review, presentation, demo script, build script, installer script | ✅ |

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| UI not executed with real Qt in the sandbox | conservative, well-known PySide6 APIs; ruff static checks; stub-Qt smoke run of every UI code path; the CLI (`--cli`) exposes the same core if the UI has an issue |
| Wake-word false positives/negatives | sensitivity setting, confidence + energy gates, refractory period, Porcupine option, push-to-talk button |
| LLM outages or quota | `ResilientLLM` offline mode; rules and classifier work offline |
| OAuth friction | web-compose fallback; step-by-step guide in SETUP.md |
| Destructive actions | risk levels, confirmations, no shell tool, validators, dry-run mode for demos |

## Testing strategy

Unit tests (NLU, entities, dates, validators, permissions, schema, redaction, DB), integration tests (LLM providers and Gmail with a faked network, tools on a real temporary file system, voice pipeline with synthetic audio), and end-to-end tests (the eight specification scenarios through the complete pipeline). External boundaries are faked (`RecordingBackend`, `FakeGoogle`, `ScriptedLLM`), so all Genesis code above them is exercised for real.
