# Genesis — Phase 0 Architecture

This document is the **Phase 0** deliverable: architecture, technology choices, schema, API surface,
repo layout, phased roadmap, hardware envelope, and risks. It intentionally contains **no application
code**. Nothing here has been run or benchmarked — recommendations are based on published specs/licenses,
not on measurements I've personally taken. Where I say "not verified," take that literally.

---

## 1. Final System Architecture

Genesis is built as a **pipeline of independently replaceable services**, coordinated by one component
(the Orchestrator) that the frontend never bypasses.

```
                              USER
                          (text / voice)
                                │
                                ▼
                     ┌────────────────────┐
                     │  GENESIS FRONTEND   │  (chat UI, mic, audio player)
                     └─────────┬──────────┘
                                │ HTTPS / WebSocket (SSE for streaming)
                                ▼
                     ┌────────────────────┐
                     │     FASTAPI API     │  auth, request validation, rate limiting
                     └─────────┬──────────┘
                                ▼
                     ┌────────────────────┐
                     │ INPUT / LANGUAGE    │  STT (if voice) → LanguageDetectionService
                     │       LAYER         │
                     └─────────┬──────────┘
                                ▼
                     ┌────────────────────┐
                     │ CONVERSATION        │  loads/creates Conversation, appends Message,
                     │ MANAGER             │  manages history window
                     └─────────┬──────────┘
                                ▼
                     ┌────────────────────────────────────────────┐
                     │              AI ORCHESTRATOR                │
                     │  intent analysis → context assembly →       │
                     │  planning → model/tool selection → execute  │
                     │                                              │
                     │   ┌────────┐ ┌────────┐ ┌─────┐ ┌────────┐  │
                     │   │  LLM   │ │ Memory │ │ RAG │ │  Web   │  │
                     │   │Provider│ │Manager │ │     │ │ Search │  │
                     │   └────────┘ └────────┘ └─────┘ └────────┘  │
                     │   ┌────────┐ ┌──────────────┐                │
                     │   │ Tools  │ │ Security /   │                │
                     │   │Registry│ │ Permissions  │                │
                     │   └────────┘ └──────────────┘                │
                     └─────────┬────────────────────────────────────┘
                                ▼
                     ┌────────────────────┐
                     │  RESPONSE ENGINE    │  stream text; optionally TTS → audio
                     └─────────┬──────────┘
                                ▼
                              USER
```

**Non-negotiable architectural rule:** every arrow into "LLM / Memory / RAG / Web / Tools" passes
through the Orchestrator. The frontend never calls a tool, memory store, or vector DB directly — this
is what keeps the security model enforceable and lets any single component be swapped later.

---

## 2. Component Diagram (logical modules → code boundary)

```
app/
├── api/            → HTTP/WebSocket surface only. No business logic.
├── core/            → config, logging, exceptions, DI wiring
├── ai/
│   ├── llm/         → LLMProvider ABC + HuggingFaceProvider, LocalModelProvider
│   ├── memory/       → WorkingMemory, EpisodicMemory, SemanticMemory, UserPreferences
│   ├── rag/          → ingestion, chunking, embedding, retrieval, reranking
│   ├── orchestration/ → AIOrchestrator, IntentAnalyzer, Planner
│   ├── speech/        → STTProvider ABC, TTSProvider ABC + implementations
│   └── prompting/     → prompt templates, system prompt builder, language-aware prompting
├── database/        → SQLAlchemy engine/session, Alembic migrations
├── models/           → ORM models (users, conversations, messages, …)
├── services/         → ConversationService, MemoryService, DocumentService (business logic)
├── tools/             → ToolRegistry, ToolBase, calculator/time/file_reader/web_search
├── security/          → PermissionChecker, RiskPolicy, ConfirmationFlow
└── utils/             → shared helpers
```

Each arrow in the system diagram maps to a call between exactly these packages — `api/` never imports
`ai/llm` directly, it calls `services/`, which calls `ai/orchestration`. This is what "replace the LLM
without rewriting the app" actually means in code terms.

---

## 3. Recommended Technology Stack

| Layer | Choice | Why |
|---|---|---|
| Backend framework | **FastAPI** (Python 3.11+) | async-native, Pydantic v2 validation, native SSE/WebSocket, OpenAPI for free |
| ORM / migrations | **SQLAlchemy 2.0 + Alembic** | works unchanged across SQLite → PostgreSQL |
| Dev database | **SQLite** | zero-ops for local dev |
| Prod database | **PostgreSQL** (later) | same SQLAlchemy models, Alembic migration path |
| Vector store (RAG) | **Chroma** (embedded, local) → **pgvector** later | Chroma needs no separate service for V1; pgvector consolidates infra once on Postgres |
| LLM inference (cloud) | **Hugging Face Inference Providers / Inference Endpoints** | no local GPU required to start |
| LLM inference (local) | **llama.cpp / Ollama** via GGUF, or **vLLM** if a real GPU is available | matches hardware-awareness requirement (§10) |
| STT | **faster-whisper** (CTranslate2 build of Whisper) | 4x faster than stock Whisper on CPU, same accuracy |
| TTS | Provider abstraction over **Indic-Parler-TTS** / **Coqui XTTS-v2** / cloud fallback | see §5, licensing differs per option |
| Task/async jobs | **FastAPI BackgroundTasks** now, **Celery + Redis** later | don't add Redis/Celery until ingestion or TTS actually needs a queue |
| Frontend | **React + TypeScript + Vite**, Tailwind for styling | fast dev loop, streaming-friendly (SSE/fetch), no framework lock-in to a specific AI-chat template |
| Frontend state | React Query (server state) + lightweight store (Zustand) | avoids Redux boilerplate for a chat app |
| Containerization | **Docker Compose** (api, frontend, db, optional vector store) | matches §19/§20 deployment requirement |
| Testing | **pytest**, **pytest-asyncio**, **httpx** (ASGI test client) | standard FastAPI testing stack |
| CI | **GitHub Actions** | matches repo host |

---

## 4. Recommended Hugging Face LLM Candidates

Evaluated on: license, context window, multilingual/Indic support, VRAM at common quantizations,
quantization/fine-tuning support, HF availability. **None of these have been benchmarked by me — this
is a reading of published model cards, not a verified result.**

| Model | Params | License | Context | Multilingual / Indic | Quantized VRAM (4-bit) | Notes |
|---|---|---|---|---|---|---|
| **Qwen2.5-7B-Instruct** (primary recommendation) | 7B | Apache-2.0 | 128K | Broad multilingual; usable but not best-in-class on Indic scripts | ~5–6 GB | Best license+ecosystem tradeoff; huge GGUF/AWQ/GPTQ availability; strong instruction following |
| **Qwen2.5-3B-Instruct** (low-hardware fallback) | 3B | Apache-2.0 | 32K | Weaker than 7B but workable | ~2–3 GB | For laptops with 6–8GB VRAM or CPU-only dev |
| **Llama-3.1-8B-Instruct** | 8B | Llama 3.1 Community License (commercial use allowed under threshold) | 128K | Good general multilingual, weak on Indic scripts specifically | ~5–6 GB | Larger ecosystem of fine-tunes; license has a >700M-MAU commercial clause — irrelevant at your scale but note it |
| **google/gemma-2-9b-it** | 9B | Gemma license (permissive, some usage restrictions) | 8K | Good multilingual | ~6–7 GB | Shorter context is a real limitation for RAG |
| **sarvamai/sarvam-1** / **ai4bharat/Airavata** | 2B / 7B | Varies (check per-model card before commercial use) | Shorter | **Purpose-built for Indian languages** (Hindi-first for Airavata) | 1–4 GB | Worth a dedicated evaluation pass once multilingual quality on Kannada/Telugu/Tamil/Malayalam is tested against Qwen2.5 — do not assume it beats a general multilingual model on every language just because it's India-focused |
| **microsoft/Phi-3.5-mini-instruct** | 3.8B | MIT | 128K | Decent, not Indic-specialized | ~2.5 GB | Best license (MIT, no restriction), good reasoning-per-parameter |

**Recommendation for Phase 1:** start with **Qwen2.5-7B-Instruct via Hugging Face Inference Providers**
(cloud, no local GPU needed) behind the `LLMProvider` abstraction, with `Qwen2.5-3B-Instruct` (or the
same 7B, quantized GGUF via Ollama) as the local fallback. This defers the Indic-model bake-off to
Phase 2, where it belongs — it needs actual multilingual eval data, not a guess.

```
LLMProvider (ABC)
├── HuggingFaceProvider(model="Qwen/Qwen2.5-7B-Instruct")   ← Phase 1 default
├── LocalModelProvider(backend="ollama"|"llama.cpp")         ← Phase 1 alt, opt-in
└── CustomModelProvider                                       ← placeholder for §5 (your own fine-tune)
```

---

## 5. Recommended STT/TTS Candidates

### STT

| Model | License | Languages | Notes |
|---|---|---|---|
| **openai/whisper-large-v3** (via **faster-whisper**) — recommended | MIT | ~99 languages incl. Hindi, Kannada, Tamil, Telugu, Malayalam, Marathi, Bengali | Best accuracy/language-coverage tradeoff; faster-whisper (CTranslate2) makes CPU inference viable |
| **openai/whisper-large-v3-turbo** | MIT | Same coverage, faster, slightly lower accuracy | Good default if latency matters more than peak accuracy |
| **ai4bharat/indicwhisper** | Check card | Fine-tuned for Indian accents/languages | Candidate for Phase 2 if base Whisper's Indic accuracy is unverified as insufficient — needs actual testing, not assumption |

### TTS

| Model | License | Languages | Notes |
|---|---|---|---|
| **ai4bharat/Indic-Parler-TTS** | Check card (research-oriented) | Multiple Indian languages | Purpose-built for Indic TTS; verify license terms before any commercial use |
| **coqui/XTTS-v2** | **CPML — non-commercial** | Multilingual, voice cloning | Great quality, but **cannot be used in a commercial product** without a license from Coqui — flagging explicitly per §31 (no fabrication about license terms) |
| **facebook/mms-tts** (per-language checkpoints) | **CC-BY-NC-4.0 — non-commercial** | 1100+ languages incl. most Indian languages | Same commercial-use caveat as XTTS |
| **Cloud fallback** (e.g., a hosted TTS API) | Commercial terms apply | Broad | Recommended path if Genesis is ever monetized, since the strongest open Indic TTS models above are non-commercial |

**Important:** unlike the LLM landscape, high-quality **commercially-licensed** open TTS for Indian
languages is genuinely scarce right now. Build the `TTSProvider` abstraction so swapping to a paid
cloud TTS later is a config change, not a rewrite. For personal/non-commercial use in V1, XTTS-v2 or
MMS-TTS are fine.

```
SpeechToTextProvider (ABC)          TextToSpeechProvider (ABC)
├── LocalSTTProvider(faster-whisper) ├── LocalTTSProvider(Indic-Parler / XTTS)
├── HuggingFaceSTTProvider            ├── HuggingFaceTTSProvider
└── FutureSTTProvider                 └── FutureTTSProvider (cloud, when commercial)
```

---

## 6. Database Design (SQLite now, Postgres-ready)

```sql
users
  id (pk, uuid)
  email (unique, nullable for anonymous/local use)
  display_name
  preferred_language
  created_at, updated_at

conversations
  id (pk, uuid)
  user_id (fk → users.id)
  title
  preferred_language
  model_name
  metadata_json
  created_at, updated_at

messages
  id (pk, uuid)
  conversation_id (fk → conversations.id)
  role                -- 'user' | 'assistant' | 'system' | 'tool'
  content
  language
  model_name
  prompt_tokens, completion_tokens
  citations_json        -- RAG/web sources, nullable
  tool_calls_json        -- nullable
  attachments_json        -- nullable
  created_at

memories
  id (pk, uuid)
  user_id (fk → users.id)
  type                -- 'episodic' | 'semantic' | 'preference'
  content
  source_conversation_id (fk, nullable)
  confidence
  created_at, expires_at (nullable)

documents
  id (pk, uuid)
  user_id (fk → users.id)
  filename, mime_type, size_bytes
  status                -- 'pending' | 'processing' | 'ready' | 'failed'
  created_at

document_chunks
  id (pk, uuid)
  document_id (fk → documents.id)
  chunk_index
  content
  embedding_ref          -- pointer into vector store, not the vector itself
  token_count

tool_calls
  id (pk, uuid)
  message_id (fk → messages.id)
  tool_name
  input_json, output_json
  risk_level             -- LOW | MEDIUM | HIGH | CRITICAL
  status                 -- 'pending_confirmation' | 'approved' | 'denied' | 'executed' | 'failed'
  created_at

model_usage
  id (pk, uuid)
  message_id (fk → messages.id)
  provider, model_name
  latency_ms
  prompt_tokens, completion_tokens
  cost_estimate           -- nullable, only for paid providers

settings
  user_id (pk/fk → users.id)
  key
  value_json

voice_sessions
  id (pk, uuid)
  conversation_id (fk → conversations.id)
  stt_provider, tts_provider
  stt_latency_ms, tts_latency_ms
  language_detected
  created_at
```

Design notes:
- `document_chunks.embedding_ref` keeps vectors **out of SQLite/Postgres** — they live in Chroma/pgvector.
  This is what lets the vector store be swapped without touching relational schema.
- Every table that matters for observability (`messages`, `tool_calls`, `model_usage`) carries enough
  to answer "what happened and why" without needing external logs.
- No table stores raw audio (see §9 voice-privacy requirement) — `voice_sessions` stores metadata only.

---

## 7. API Design (FastAPI, V1 surface)

```
POST   /api/v1/conversations                 create conversation
GET    /api/v1/conversations                 list (paginated)
GET    /api/v1/conversations/{id}             get with messages
DELETE /api/v1/conversations/{id}             delete

POST   /api/v1/conversations/{id}/messages     send message → SSE stream of tokens
POST   /api/v1/conversations/{id}/regenerate   regenerate last assistant message
POST   /api/v1/conversations/{id}/stop           stop in-flight generation

POST   /api/v1/voice/transcribe                audio → text (STT), no persistence of audio
POST   /api/v1/voice/synthesize                 text → audio (TTS)

POST   /api/v1/documents                        upload document → triggers ingestion
GET    /api/v1/documents/{id}                  ingestion status

GET    /api/v1/memories                        list stored memories (user-visible, deletable)
DELETE /api/v1/memories/{id}                   delete a memory

GET    /api/v1/models                          list available LLM providers/models
GET    /api/v1/settings                        get user settings
PUT    /api/v1/settings                        update user settings

GET    /api/v1/health                          liveness/readiness
```

Streaming uses **Server-Sent Events** for `/messages` (simpler than WebSocket for one-directional
token streaming; upgrade to WebSocket only if bidirectional voice-interrupt needs it in Phase 3).
Every response body is a Pydantic model — no bare dicts crossing the API boundary.

---

## 8. Repository Structure

Using the structure from the brief as-is; it's already correctly modular and I have no strong
technical reason to deviate:

```
genesis/
├── app/
│   ├── api/
│   ├── core/
│   ├── ai/
│   │   ├── llm/
│   │   ├── memory/
│   │   ├── rag/
│   │   ├── orchestration/
│   │   ├── speech/
│   │   └── prompting/
│   ├── database/
│   ├── models/
│   ├── services/
│   ├── tools/
│   ├── security/
│   └── utils/
├── frontend/
├── tests/
├── data/
├── scripts/
├── docs/
├── .env.example
├── requirements.txt
├── README.md
└── docker-compose.yml
```

---

## 9. Development Phases (roadmap, unchanged in substance from the brief)

0. **Architecture** — this document.
1. **Basic Genesis Chat** — FastAPI + SQLite + HF LLM + streaming + minimal frontend.
2. **Multilingual** — language detection/preference, multilingual prompting.
3. **Voice** — push-to-talk, STT, TTS.
4. **Memory** — working/episodic/semantic memory with user-visible controls.
5. **RAG** — document upload, chunking, embeddings, retrieval, citations.
6. **Web knowledge** — controlled web search tool.
7. **Tools** — tool registry, permissions, confirmation flow.
8. **Agentic** — planning, multi-step task execution.
9. **Desktop Genesis** — only once 1–8 are stable and evaluated.

Each phase only starts once the previous phase has passing tests and a working demo — not before.

---

## 10. Hardware Requirements

| Mode | What runs where | Minimum | Comfortable |
|---|---|---|---|
| **Cloud mode** | LLM/STT/TTS via HF Inference Providers or another hosted API | Any machine that can run FastAPI + a browser | Same |
| **Local mode — LLM only** | Qwen2.5-7B, 4-bit quantized | 8GB VRAM (GPU) or 16GB RAM (CPU, slow) | 12GB+ VRAM |
| **Local mode — LLM + STT + TTS together** | All three loaded concurrently | 16GB VRAM or high-RAM CPU box (expect slow TTS/STT on CPU) | 24GB VRAM |
| **RAG (embedding model)** | Small embedding model (e.g., a multilingual sentence-transformer, ~500MB–1GB) | Negligible added VRAM | — |

**Not verified**: I have not measured actual tokens/sec, STT real-time-factor, or TTS latency on any
specific hardware — the table above reflects published model requirements, not a benchmark I ran.
Before committing to local mode for any given machine, run a smoke test and measure it.

The `LLMProvider`/`STTProvider`/`TTSProvider` abstractions exist specifically so the same Genesis
codebase can start in cloud mode (no GPU needed) and switch to local mode later via config, not code
changes.

---

## 11. Major Technical Risks

1. **TTS commercial licensing gap** (§5) — the strongest open Indic TTS options are non-commercial.
   If Genesis is ever monetized, this needs a paid provider or a license negotiated before shipping,
   not after.
2. **Indic-language quality is unverified** — no LLM/STT/TTS choice above has been tested against
   actual Kannada/Telugu/Tamil/Malayalam/Marathi/Bengali/Gujarati/Punjabi/Urdu text or speech by me.
   Phase 2 needs a real evaluation set before claiming multilingual support "works."
3. **Tool/agentic security surface** — once Phase 7–8 land, a prompt-injected document or web page
   could attempt to trigger a HIGH/CRITICAL tool call. The permission+confirmation gate (§15) is the
   entire mitigation; it must be enforced server-side and never bypassable from the LLM's output alone.
4. **Memory scope creep** — "remember this" is an easy feature to over-build into "store everything."
   Without an explicit allow-list policy in `MemoryManager`, this becomes a privacy liability fast.
5. **Local-mode hardware mismatch** — recommending a 7B local model to a user with 6GB VRAM produces a
   broken, not degraded, experience. The provider abstraction must include a hardware-capability check,
   not just a model name in config.
6. **Voice audio retention drift** — the "discard after STT" policy (§9 of the brief) is a design intent,
   not an automatic guarantee. Anywhere a temp audio file is written to disk, it needs deterministic
   cleanup on both success and failure paths, verified by a test, not just written into the ingestion code.
7. **RAG citation trustworthiness** — if the orchestrator ever answers using model knowledge but a
   citation block is rendered anyway, that's a fabrication risk at the product level, not just the
   model level. The response engine must only attach citations to content it actually retrieved.

---

## 12. Exact Phase 1 Implementation Plan

**Goal:** `USER → MESSAGE → GENESIS → RESPONSE`, with real persistence and real streaming, nothing else.

1. **Repo skeleton** — create the directories from §8, `requirements.txt`, `.env.example`,
   `.gitignore`, `README.md` stub, `docker-compose.yml` (api + frontend services; no vector DB or
   Redis yet — not needed until Phase 5+).
2. **Core config** — `app/core/config.py` (Pydantic Settings reading from env), `app/core/logging.py`.
3. **Database** — SQLAlchemy models for `users`, `conversations`, `messages` only (the other tables
   in §6 arrive with their owning phase, not now). Alembic initialized with the first migration.
4. **LLM abstraction** — `app/ai/llm/base.py` (`LLMProvider` ABC: `generate()`, `stream()`),
   `app/ai/llm/huggingface_provider.py` implementing it against HF Inference Providers for
   `Qwen/Qwen2.5-7B-Instruct`.
5. **Conversation service** — `app/services/conversation_service.py`: create conversation, append
   message, fetch history, enforce a context-window trim policy.
6. **Minimal orchestrator** — `app/ai/orchestration/orchestrator.py` — for Phase 1 this is
   intentionally thin: build prompt from history → call `LLMProvider.stream()` → return. No
   memory/RAG/web/tools wired in yet (those are stubs raising `NotImplementedError` so the interface
   shape is settled early without the behavior existing yet).
7. **API** — implement exactly: `POST /conversations`, `GET /conversations`, `GET /conversations/{id}`,
   `POST /conversations/{id}/messages` (SSE streaming), `GET /health`.
8. **Frontend** — single-page React app: sidebar with conversation list + "New chat", main pane with
   message bubbles, streaming text render, a text input. No voice, no language selector, no model
   selector yet — those are Phase 2/3 UI.
9. **Tests** — `tests/test_conversation_service.py`, `tests/test_api_conversations.py` (httpx ASGI
   client), `tests/test_llm_provider.py` (mocked HF call, not a live network test).
10. **CI** — GitHub Actions workflow: install → lint (ruff) → typecheck (mypy) → pytest.

**Explicitly out of scope for Phase 1** (per the brief's own phase boundaries): language detection,
voice, memory, RAG, web search, tools, planning. Their package directories exist as empty scaffolding
so Phase 1's module boundaries don't need to be reshuffled later, but no logic goes in them yet.

**How Phase 1 will be verified before being called done:** `pytest` passing locally and in CI, a
manual run of `docker-compose up` followed by sending one message through the UI and confirming a
streamed, persisted response — screenshotted or described, not just claimed. Nothing gets marked
"done" without that.

---

## Stop point

This is the end of the Phase 0 deliverable. Per the brief: no Phase 1 code has been written. Waiting
for approval — or requested changes to any choice above — before generating anything in `app/` or
`frontend/`.
