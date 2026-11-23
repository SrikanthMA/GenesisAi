# Development guide

## Environment

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
```

Open the folder in VS Code. The debugger configurations are in `.vscode/launch.json`:

| Configuration | Runs |
|---|---|
| Genesis: Desktop app | `python -m genesis` |
| Genesis: Desktop app (dry-run power actions) | same, with power actions simulated |
| Genesis: Terminal chat (CLI) | `python -m genesis --cli` |
| Genesis: Terminal + voice | `python -m genesis --cli --voice` |
| Genesis: Doctor | `python -m genesis --doctor` |
| Genesis: Run all tests | `pytest -q` |

## Tests

```powershell
python -m pytest -q                 # all tests (~5 s)
python -m pytest tests/test_security.py -q
python -m pytest --cov=genesis      # coverage
python -m unittest discover -s tests -t .   # works without pytest too
```

| File | Covers |
|---|---|
| `test_nlu.py` | rules for 45 utterances, splitting, entities, date grammar, classifier accuracy, LLM-NLU fallback, power actions never inferred by the LLM or classifier |
| `test_dialogue.py` | slot filling, “him” coreference, clarification, cancel, re-ask, history |
| `test_security.py` | permission matrix, confirmations, validators, redaction, injection, no shell tool, vault, end-to-end safety |
| `test_tools.py` | real file operations in a temporary home folder, apps, web, system, reminders, code check, VS Code, memory, knowledge base (RAG), settings |
| `test_agent.py` | templates, verification failure, crash isolation, schema rejection, deny policy, untrusted escalation, LLM-plan validation, multi-step find, rename, attach and draft |
| `test_llm_gmail.py` | HF / OpenAI-compatible provider requests and errors, degraded mode, JSON extraction, OAuth refresh, revoked tokens, Gmail draft MIME, send, search, disconnect, main e-mail demo, recovery |
| `test_e2e.py` | the eight scenarios from the specification, plus degraded offline mode |
| `test_database_config.py` | migrations, cascade, contacts lookup, constraints, transactions, settings types, config, `.env.example` |
| `test_voice.py` | VAD on synthetic audio, wake-phrase matching, refractory period, **full voice pipeline** (wake → record → STT → core → TTS → follow-up turn) with fake devices |

The OS, Google and the LLM are faked at their boundaries (`RecordingBackend`, `FakeGoogle`, `ScriptedLLM`), so every code path above them is the real production code.

## Linting

```powershell
ruff check genesis tests
```

## Building a Windows executable

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
# → dist\Genesis\Genesis.exe
```

To build an installer, open `installer\genesis.iss` in Inno Setup and compile it to get `dist\GenesisSetup.exe`. The installer can register *start with Windows*.

The packaged app stores data in `%LOCALAPPDATA%\Genesis`. Put your `.env` there, or next to `Genesis.exe`, and copy `data\models` to `%LOCALAPPDATA%\Genesis\data\models`.

## Adding capabilities

See the **Extending Genesis** section of [architecture/ARCHITECTURE.md](architecture/ARCHITECTURE.md). After adding tools, regenerate the tool reference:

```powershell
python scripts\gen_tool_docs.py
```
