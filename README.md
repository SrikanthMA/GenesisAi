# Genesis

Genesis is a multilingual, voice-enabled, memory-enabled personal AI system, built to grow from a
chatbot into an agentic assistant without needing to be rewritten at each stage.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full system design: architecture,
technology stack, model candidates, database schema, API design, repository structure, phased
roadmap, hardware requirements, and known risks.

**Status: Phase 2 — Multilingual.** `USER → MESSAGE → GENESIS → RESPONSE` works end-to-end: FastAPI
backend, SQLite persistence, streaming responses via Hugging Face Inference Providers, and a React
chat UI. On top of that, Genesis now detects the language of each message (`LanguageDetectionService`,
11 languages: English, Hindi, Kannada, Telugu, Tamil, Malayalam, Marathi, Bengali, Gujarati, Punjabi,
Urdu) and asks the LLM to mirror it, or a conversation can be pinned to one language explicitly
(`LanguagePreferenceService`, exposed via `PATCH /conversations/{id}/language` and a selector in the
UI). Voice, memory, RAG, web search, tools, and planning are not built yet — see the architecture
doc's phased roadmap.

## Running it locally

**Backend**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and set HF_TOKEN to a Hugging Face access token with Inference Providers access

alembic upgrade head
uvicorn app.main:app --reload
```

The API is served at `http://localhost:8000/api/v1`, with `/health` for a liveness check.

**Frontend**

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`. The dev server proxies `/api` requests to `http://localhost:8000`.

**Or both together, via Docker Compose:**

```bash
cp .env.example .env   # set HF_TOKEN first
docker compose up --build
```

## Verifying it

```bash
source .venv/bin/activate
ruff check app tests
mypy app
pytest -v
```

```bash
cd frontend
npm run build
```

All backend tests run against an in-memory SQLite database and a fake LLM provider — no network
calls are made during `pytest`, so they run the same with or without a valid `HF_TOKEN`.

## Known limitations

- Single implicit local user, no authentication yet.
- Stop/regenerate rely on an in-process registry — fine for a single dev `uvicorn` worker, not for
  multiple workers/replicas.
- Language detection (`langdetect`) is unreliable on very short messages (a couple of words) - that's
  a limitation of statistical detection, not something this layer can fully paper over. It's verified
  accurate (>0.99 confidence) on full-sentence input in all 11 supported languages.
- The assistant message's stored `language` is the language it was *asked* to respond in, not a
  verified measurement of the language it actually produced.
- STT/TTS language support (Phase 3) is a separate capability list from what's supported here - see
  `docs/ARCHITECTURE.md`, "Multilingual Support".
- No memory, RAG, voice, web search, or tools yet — see `docs/ARCHITECTURE.md` for the roadmap.
- The Hugging Face streaming path has been verified against real error conditions (missing
  dependency, unreachable host) but not against a live, successful completion — that needs a real
  `HF_TOKEN` and network access, which this development environment doesn't have.
