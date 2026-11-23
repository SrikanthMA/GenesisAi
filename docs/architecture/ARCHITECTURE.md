# Genesis architecture

Genesis is a single Python process with clearly separated layers. The **core never imports the UI**. The UI and the voice pipeline talk to the core only through `GenesisCore.handle()` / `resolve_confirmation()` and an event bus, which a Qt bridge re-emits on the GUI thread.

**Technology choices**

| Concern | Choice | Why |
|---|---|---|
| Desktop shell | **PySide6 (Qt 6)** | native Windows windows, system tray, frameless translucent widgets, animation, one runtime (no Electron + Python split), ships as a single PyInstaller folder |
| Backend | Python 3.10+ | NLP/ML ecosystem (scikit-learn, faster-whisper, Vosk), OS integration (ctypes, pywin32) |
| IPC | in-process event bus + Qt queued signals; `QLocalServer` for single-instance messaging | no local HTTP port to secure |
| Storage | SQLite (WAL) with versioned migrations | zero-admin, transactional, local |
| Secrets | Windows Credential Manager (`keyring`), falling back to a Fernet vault whose key is DPAPI-protected | OS-level protection |
| LLM | provider abstraction: HF Inference Providers (Qwen2.5-7B-Instruct) / OpenAI-compatible / Ollama / offline | swappable without code changes |

---

## 1. Overall architecture

```mermaid
flowchart TB
  subgraph UI["Desktop UI (PySide6)"]
    MINI[Mini assistant<br/>animated orb]
    CHAT[Expanded chat window<br/>plan & confirmation cards]
    PAGES[Memory · Tools · Activity<br/>Services · Settings]
    TRAY[System tray + global hotkey]
  end
  subgraph VOICE["Voice layer"]
    MIC[Microphone 16 kHz] --> WW[Wake word<br/>Vosk grammar / Porcupine]
    WW --> VAD[VAD end-pointing] --> STT[STT<br/>faster-whisper / Vosk]
    TTS[TTS · SAPI<br/>interruptible]
  end
  subgraph CORE["GenesisCore"]
    NLP[NLP pipeline<br/>rules → classifier → LLM]
    DM[Dialogue manager<br/>state · slots · coreference]
    PL[Planner<br/>rules / LLM]
    EX[Executor]
    VF[Verifier]
  end
  subgraph SEC["Security layer"]
    PM[Permission manager]
    CM[Confirmation manager]
    VAL[Validators · injection guard]
    AUD[Audit log]
  end
  subgraph TOOLS["Tool layer (45 typed tools)"]
    T1[Apps & URLs] --- T2[Files] --- T3[System & power]
    T4[Gmail & Calendar] --- T5[Web & browser] --- T6[VS Code]
    T7[Reminders] --- T8[Memory & knowledge] --- T9[Settings]
  end
  subgraph DATA["Local data"]
    DB[(SQLite)]
    LOGS[(JSON logs)]
    VAULT[(Encrypted credentials)]
  end
  LLM[[LLM provider<br/>HF / OpenAI-compat / Ollama / offline]]

  UI <-->|events / signals| CORE
  STT --> CORE
  CORE --> TTS
  NLP --> DM --> PL --> EX
  EX --> PM & CM & VAL
  EX --> TOOLS
  EX --> AUD
  EX --> VF
  NLP -.-> LLM
  PL -.-> LLM
  TOOLS --> DATA
  CORE --> DB
```

### Module map

| Layer | Package | Key classes |
|---|---|---|
| Desktop UI | `genesis/ui` | `GenesisApp`, `MiniAssistant`, `MainWindow`, `ChatPage`, `TrayController`, `OrbWidget`, `UiBridge`, `GlobalHotkey` |
| Voice | `genesis/voice` | `VoiceManager`, `Microphone`, `VoskWakeWord`/`PorcupineWakeWord`, `Endpointer`, `FasterWhisperSTT`/`VoskSTT`, `SapiTTS` |
| NLP | `genesis/nlp` | `NLUPipeline`, `rules`, `IntentClassifier`, `entities`, `DialogueManager` |
| AI | `genesis/ai` | `LLMProvider`, `HuggingFaceProvider`, `OpenAICompatibleProvider`, `OllamaProvider`, `OfflineProvider`, `ResilientLLM`, `prompts` |
| Agent | `genesis/agent` | `Planner`, `Executor`, `Plan`/`PlanStep`, `verifier.summarize` |
| Tools | `genesis/tools` | `Tool`, `ToolResult`, `ToolRegistry` + 45 tools |
| Services | `genesis/services` | `PlatformBackend` (Windows/macOS/Linux/Recording/DryRun), `AppResolver`, `GoogleAuth`, `GmailService`, `CalendarService`, `ReminderScheduler`, `BrowserController` |
| Memory | `genesis/memory` | `MemoryManager`, `KnowledgeBase`, embedding providers |
| Security | `genesis/security` | `PermissionManager`, `ConfirmationManager`, validators, injection guard, `SecretStore`, redaction |
| Data | `genesis/database` | `Database` (migrations), repositories |
| Core | `genesis/core` | `GenesisCore`, `ServiceContainer`, `GenesisConfig`, `Settings`, `EventBus`, logging |

---

## 2. NLP pipeline

```mermaid
flowchart LR
  A[Raw text / transcript] --> B[Normalise<br/>strip wake phrase & politeness]
  B --> C[Split compound request<br/>protect message bodies]
  C --> D{High-precision<br/>rule match?}
  D -- yes --> H[Intent + slots<br/>source=rules]
  D -- no --> E[TF-IDF + LogReg<br/>intent classifier]
  E --> F{confident &<br/>non-actionable?}
  F -- no, LLM online --> G[LLM structured NLU<br/>JSON intent + entities]
  F -- yes --> H2[Intent + slot extractors<br/>source=classifier]
  G --> H3[Merge with deterministic<br/>extractors · source=llm]
  H & H2 & H3 --> I[Entity extraction<br/>person · email · url · path · app<br/>folder · datetime · number]
  I --> J[Dialogue manager<br/>coreference · slot filling]
  J --> K{missing slots?}
  K -- yes --> L[Clarifying question]
  K -- no --> M[Planner]
```

## 3. Voice pipeline

```mermaid
sequenceDiagram
  participant Mic as Microphone (local)
  participant WW as Wake-word detector (local)
  participant VAD as End-pointer
  participant STT as Speech-to-text (local)
  participant Core as GenesisCore
  participant TTS as SAPI TTS
  loop every 32 ms frame
    Mic->>WW: PCM16 frame
  end
  WW-->>Core: "Hey Genesis" detected (UI: summon + LISTENING)
  alt user keeps talking
    Mic->>VAD: carry-over frames
  else pause
    Core->>TTS: "I'm listening."
  end
  Mic->>VAD: frames until 0.9 s silence
  VAD->>STT: utterance audio
  STT->>Core: "open chrome"
  Core->>Core: NLU → plan → permission → tool → verify
  Core->>TTS: "Opened Google Chrome."
  Note over WW,TTS: Saying the wake word while Genesis is speaking stops TTS (barge-in)
  opt reply needs an answer (confirmation / question)
    Mic->>VAD: next utterance without a wake word
  end
```

### 3a. Conversation session state machine (1.1)

A wake word, typed message, hotkey or mic click opens a **conversation session** (`genesis/core/session.py`, `SessionManager`). While it is active, voice follow-ups need no wake word. The session phase is shared by voice and text, so both follow the same timeout rules.

```mermaid
stateDiagram-v2
  [*] --> SLEEPING
  SLEEPING --> WAKING: wake word / hotkey / typed message
  WAKING --> ACTIVE_CONVERSATION
  ACTIVE_CONVERSATION --> THINKING: user turn (timer paused)
  THINKING --> EXECUTING: plan
  EXECUTING --> VERIFYING: tool finished
  VERIFYING --> ACTIVE_CONVERSATION: reply spoken → timer restarts
  THINKING --> WAITING_FOR_USER: question / confirmation
  WAITING_FOR_USER --> ACTIVE_CONVERSATION: answer (longer timeout)
  ACTIVE_CONVERSATION --> RETURNING_TO_SLEEP: silence ≥ timeout · "go to sleep" · max session length
  RETURNING_TO_SLEEP --> SLEEPING
```

| Rule | Where |
|---|---|
| `WAITING_FOR_USER` is `AssistantState.WAITING_FOR_USER` (alias of `WAITING_FOR_INPUT`, a question), and `WAITING_FOR_CONFIRMATION` is used for yes/no confirmations | `core/events.py` |
| Inactivity countdown starts when the **reply has finished** (`response_finished`) and is paused while a turn is processed (`begin_turn`/`end_turn`) | `SessionManager` |
| `conversation.timeout_seconds` (20), `question_timeout_seconds` (45) when Genesis asked something, `max_session_minutes` (30) hard cap even with auto-sleep off | settings |
| `sleep()` refuses while a task is running, so Genesis never falls asleep mid-task | `SessionManager.sleep` |
| Voice waits for speech at most `session.remaining()` seconds, then returns to wake-word-only listening | `VoiceManager._record_and_transcribe` |
| Barge-in: wake word during TTS, or sustained speech louder than the calibrated echo level → TTS stops, the captured audio starts the next turn | `VoiceManager._speak_interruptible` |
| Every transition is logged to `logs/session.log` (no speech content) and emitted as `STATE_CHANGED` / `SESSION_CHANGED` for the UI | `logging_setup`, `ui/app.py` |

### 3b. Follow-up resolution (1.1)

`genesis/nlp/followups.py` runs **before** normal NLU for every sub-request. It uses the dialogue state's **focus stack** (the last opened app, file, folder, e-mail, draft or URL), the **last list** shown (e-mails, files, search results, issues), `last_request` / `last_failed` and the folder history. It resolves the request to a concrete tool call or intent (`FOLLOW_UP`, `REPLY_EMAIL`, `REVISE_DRAFT`, …). The result still goes through the planner → executor → permission manager, so follow-ups cannot bypass confirmations. If nothing matches unambiguously, it returns a short question.

### 3c. The agent loop (1.2)

```mermaid
flowchart LR
  I[Input: voice / text] --> U[Understanding<br/>rules · typo repair · classifier · LLM NLU]
  U --> C[Context<br/>focus stack · last list · errors · preferences · task history]
  C --> P[Planning<br/>rule templates / validated LLM plan]
  P --> S[Tool selection<br/>registry: 81 typed tools]
  S --> X[Execution<br/>permission → confirmation → tool]
  X --> O[Observation<br/>verify · parse errors]
  O -->|transient failure| R[Correction / retry<br/>retry · alternative method · code fix loop ≤3]
  R --> X
  O --> A[Natural response + context update]
```

| Layer (spec) | Where it lives |
|---|---|
| Understanding | `nlp/rules.py`, `nlp/agent_rules.py` (1.2 intents), `nlp/pipeline.py` (typo repair → classifier → LLM NLU) |
| Context | `nlp/dialogue.py` (focus stack, lists, errors), `nlp/followups.py`, `memory/manager.py` (preferences), `tools/history.py` (task memory) |
| Planning | `agent/planner.py` (rules + LLM plans validated against the registry) |
| Tool selection / registry | `tools/registry.py`. Every tool declares name, description, JSON-schema parameters, risk, `run()`, `verify()`, and a confirmation prompt |
| Execution & permissions | `agent/executor.py`, `security/permissions.py`, `security/confirmation.py` |
| Observation / correction | `Tool.verify`, executor retries (`ToolResult.retryable`), alternatives inside tools (app launch), `tools/coding_agent.py` fix loop |
| Conversation manager | `core/session.py` (state machine), `voice/manager.py` |
| Memory | short-term: dialogue history; working: dialogue state and plan; long-term: facts/contacts/projects/preferences; task memory: `tasks` table + `search_history` |

Adding a capability = one `Tool` subclass in `genesis/tools/` (plus, optionally, a rule in `nlp/agent_rules.py` for fast
deterministic routing). The LLM planner discovers it from the registry automatically.

### 3d. Intelligence architecture (1.3)

```mermaid
flowchart TB
  V[Voice input] --> STT[Speech recognition<br/>faster-whisper + name vocabulary hint]
  T[Text input] --> N
  STT --> N[Intent + entity understanding<br/>rules · typo/slang repair · classifier · LLM NLU]
  N --> F[Follow-ups & corrections<br/>it/that/the second one · no, I meant · undo]
  F --> CX[Context engine<br/>app · site · task · focus · results · correction]
  CX --> M[Memory<br/>identity · preferences · instructions · facts · contacts · tasks/notes · history]
  M --> P[Reasoning / planner<br/>rule templates · LLM goal planning · re-plan once]
  P --> E[Task executor<br/>permissions → confirmation → tool]
  E --> TL[Tools: Windows · Browser · YouTube · Gmail · Calendar · Files · Coding · Productivity · Screen]
  TL --> VF[Verification<br/>tool.verify · recovery · honest summary]
  VF --> R[Response<br/>short, natural; TTS]
  R --> MU[Memory update<br/>context · undo stack · usage counts; long-term only when asked]
```

| Component | Module |
|---|---|
| Identity & name recognition | `memory/identity.py` (phonetic key + similarity; accept ≥ 0.9, confirm once ≥ 0.72; learned variants and rejections) |
| Context engine | `core/context.py` (compact snapshot used by NLU, planner and chat prompts) |
| Corrections, undo, element/ordinal follow-ups | `nlp/followups.py`, `nlp/dialogue.py` (`undo_for`) |
| Goal planning and re-planning | `agent/planner.py` (`_goal`, `plan_with_llm`), `core/assistant.py` (`_replan`, `looks_like_request`) |
| Universal browser layer | `services/browser.py` (`elements`, `click_element`, `scroll`, `press`), `tools/web.py` (`rank_elements`) |
| Project changes & commands | `tools/code_changes.py` |
| Tasks & notes | `tools/productivity.py`, migration v3 |
| Caches | `ai/llm/factory.py` (LLM), `services/youtube.py`, `services/web.py` |

## 4. Agent execution pipeline

```mermaid
flowchart TB
  R[User request] --> N[NLU + dialogue]
  N --> P{Planner}
  P -->|known intent| RP[Rule template plan]
  P -->|unknown, command-like| LP[LLM JSON plan]
  LP --> LV{every step:<br/>tool exists? not forbidden?<br/>schema valid?}
  LV -- no --> CHAT[Fall back to conversation]
  LV -- yes --> PLAN
  RP --> PLAN[Plan: ordered PlanSteps]
  PLAN --> S1[Resolve templates<br/>steps.N.data.key]
  S1 --> S2[Validate JSON schema]
  S2 --> S3[Assess risk · risk_for]
  S3 --> S4{Permission manager}
  S4 -- denied --> BLK[Blocked + audit]
  S4 -- needs confirmation --> CF[Pause · ask user]
  CF -- yes --> S5
  CF -- no --> CAN[Cancelled + audit]
  S4 -- allowed --> S5[Run tool]
  S5 --> S6[Tool.verify post-condition]
  S6 --> S7[Audit record]
  S7 --> OK{success?}
  OK -- yes --> NEXT[Next step]
  OK -- no --> REC{safe LOW-risk<br/>recovery offered?}
  REC -- yes --> RR[Run recovery step] --> STOP
  REC -- no --> STOP[Skip remaining steps]
  NEXT --> VF[Verifier: reply built only from real results]
  STOP --> VF
```

## 5. E-mail workflow

```mermaid
sequenceDiagram
  actor U as User
  participant C as GenesisCore
  participant M as Memory (contacts)
  participant L as LLM / template NLG
  participant G as Gmail API
  participant B as Browser
  U->>C: "Draft an email to my professor saying I will submit the NLP assignment tomorrow"
  C->>C: intent=draft_email, recipient="my professor", message=…
  C->>M: resolve "my professor"
  alt contact known
    M-->>C: Professor Rao <rao@university.edu>
  else unknown
    C-->>U: "I don't have an e-mail address for your professor. What is it?"
    U->>C: rao@university.edu (saved as a contact, visibly)
  end
  C->>L: compose_email(recipient, message)
  L-->>C: subject + body ("Dear Professor Rao, …")
  alt Google connected
    C->>G: POST /drafts (OAuth bearer)
    G-->>C: draft id + message id
    C->>G: GET /drafts/{id} (verify)
    C->>B: open #drafts?compose=<id>
    C-->>U: "Created a Gmail draft … say 'send it' when ready."
    U->>C: "send it"
    C-->>U: "Send the e-mail … to rao@university.edu now?"
    U->>C: "yes"
    C->>G: POST /drafts/send
  else not connected / token expired
    C->>B: open pre-filled Gmail compose window (safe recovery)
    C-->>U: honest explanation + how to connect Google
  end
```

## 6. Tool execution workflow

```mermaid
flowchart LR
  A[PlanStep tool + args] --> B[ToolRegistry.get]
  B -->|unknown| X1[fail]
  B --> C[tool.validate · JSON schema]
  C -->|invalid| X2[fail · nothing executed]
  C --> D[validators<br/>path · url · filename · email]
  D -->|rejected| X3[fail · security event]
  D --> E[PlatformBackend<br/>argv lists only · no shell]
  E --> F[(Windows APIs /<br/>ShellExecute / shutdown.exe /<br/>LockWorkStation / SAPI)]
  E --> G[ToolResult success, message, data]
  G --> H[tool.verify]
  H --> I[audit: tool_executions + tool_execution.log]
```

## 7. Security & permission flow

```mermaid
flowchart TB
  S[Step ready to run] --> P{policy = deny?}
  P -- yes --> D[BLOCKED]
  P -- no --> H{risk = HIGH?}
  H -- yes --> ASK[Ask for explicit confirmation<br/>strict yes/no parsing, 5-min expiry]
  H -- no --> AC{always_confirm tool?<br/>e.g. send e-mail, delete}
  AC -- yes --> ASK
  AC -- no --> M{risk = MEDIUM?}
  M -- yes --> T{user trusted this tool?}
  T -- no --> ASK
  T -- yes --> U
  M -- no --> U{untrusted external content<br/>read earlier in this plan?}
  U -- yes --> ASK
  U -- no --> RUN[Run]
  ASK -- "yes" --> RUN
  ASK -- "no" / new command / expiry --> CANCEL[Cancel · audit]
```

## 8. Memory architecture

```mermaid
flowchart LR
  subgraph ST["Short-term (RAM)"]
    H[Dialogue history window<br/>24 turns]
    R[Referents: last person, file,<br/>folder, app, project, draft]
    F[Open frame + pending slot<br/>+ queued sub-requests]
  end
  subgraph SE["Session (RAM)"]
    TS[Tasks performed since start]
  end
  subgraph LT["Long-term (SQLite, user-controlled)"]
    CONV[Conversations & messages]
    FACT[Facts - only on explicit remember]
    CON[Contacts - name · alias · e-mail]
    PREF[Preferences & projects]
    KB[Knowledge base chunks + vectors]
  end
  H --> CONV
  R --> CON
  CONTROL[Memory page:<br/>view · delete · disable · wipe] --> LT
  LT --> PROMPT[Memory context in LLM system prompt]
  KB --> RAG[Retrieval → answer with citations]
```

## 9. Data flow (what stays local)

```mermaid
flowchart LR
  subgraph PC["This PC (local)"]
    MIC[Mic audio] --> WAKE[Wake word] --> STT[STT]
    STT --> NLU[Rules + classifier]
    NLU --> AG[Agent + tools]
    AG --> OS[(Apps, files, power)]
    AG --> DB[(SQLite, logs, vault)]
    KB[(Knowledge base)] --> AG
  end
  subgraph CLOUD["Remote (only when needed)"]
    LLM[LLM provider]
    GM[Gmail / Calendar API]
    WEB[Search engine / websites]
  end
  NLU -. unclear request / chat text .-> LLM
  AG -. compose / explain / summarise .-> LLM
  AG -. e-mail actions .-> GM
  AG -. search / read page .-> WEB
```

Raw audio never leaves the machine unless you explicitly select the Hugging Face STT engine. With `GENESIS_LLM_PROVIDER=ollama`, no request text leaves the machine either.

---

## Threading model

| Thread | Work |
|---|---|
| Qt GUI thread | all widgets; receives events through queued signals |
| `genesis-ui-worker` pool (2) | `core.handle()` for typed input, OAuth, indexing |
| `genesis-voice` | microphone loop, wake word, recording, STT, calls `core.handle()` |
| `genesis-tts-*` | SAPI COM object (thread-affine) |
| `genesis-reminders` | reminder scheduler (10 s tick) |
| `genesis-hotkey` | Win32 message loop for `RegisterHotKey` |
| `genesis-browser` | Playwright (thread-bound sync API) |

`GenesisCore.handle` is serialised with a re-entrant lock, so voice and text requests never interleave.

## Extending Genesis

- **New tool:** subclass `Tool` in `genesis/tools/<module>.py`. Define `name`, `description`, `parameters` (JSON schema), `risk`, `run()` and optionally `verify()`. Add it to that module's `TOOLS` list. The registry, the Tools page, the LLM planner catalogue and `docs/TOOLS.md` (run `scripts/gen_tool_docs.py`) pick it up automatically.
- **New intent:** add it to `Intent` in `nlp/intents.py`, add examples to `nlp/data/intents.json` (the classifier retrains at start-up), optionally add a rule in `nlp/rules.py`, and add a mapping in `Planner.build`.
- **New LLM provider:** subclass `LLMProvider` (or reuse `OpenAICompatibleProvider`) and register it in `ai/llm/factory.py`.
