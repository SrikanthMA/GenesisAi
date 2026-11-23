# Genesis 1.6.1 → 2.0 — Architecture Audit

Audit performed on the actual repository (`genesis/`, 132 Python modules, `tests/`, `scripts/`) before any 2.0 change.
Everything below was checked against the code or by running it; claims that could not be checked are marked **unverified**.

## 1. Current architecture (verified)

The upgrade brief assumes Electron and FastAPI. **Genesis uses neither.** It is a single Python 3.10–3.12 process:

| Layer | Implementation | Key modules |
|---|---|---|
| Desktop UI | PySide6 (Qt) — mini window, chat window, tray, orb, global hotkey | `ui/app.py`, `ui/bridge.py`, `ui/pages/*` |
| UI ↔ core | In-process `EventBus` → Qt signals (`UiBridge`), worker thread pool for requests | `core/events.py`, `ui/bridge.py` |
| Orchestrator | `GenesisCore.handle()` — one entry point per turn, serialised by an `RLock` | `core/assistant.py` |
| Language | Offline detection + LLM translation in/out (1.6) | `nlp/language.py` |
| NLU | normalise → split → follow-ups → rules → typo repair → TF-IDF+LogReg → fast routes → LLM NLU | `nlp/*.py` |
| Dialogue | Frame/slot filling, focus stack, last list, coreference | `nlp/dialogue.py`, `nlp/followups.py` |
| Planning | Rule templates (`Planner.build`) + LLM JSON plans (`plan_with_llm`) validated against tool schemas | `agent/planner.py`, `agent/plan.py` |
| Execution | `Executor._run_step`: templates → schema → risk → permission → confirmation → run → retry → verify → audit | `agent/executor.py` |
| Tools | 135 `Tool` subclasses in one registry | `tools/*.py`, `tools/registry.py` |
| Services | OS backend, Edge/Playwright, Gmail/Calendar (OAuth PKCE), GitHub REST, Office (python-docx/openpyxl/python-pptx + COM), YouTube, Colab, WhatsApp | `services/*.py` |
| LLM | `ResilientLLM` over HF router / OpenAI-compatible / Anthropic / Gemini / Ollama / offline | `ai/llm/*.py` |
| Storage | SQLite (schema v3, 20 tables, versioned migrations), Windows Credential Manager / encrypted vault for secrets | `database/*.py`, `security/secrets.py` |
| Voice | Vosk wake word, energy VAD, faster-whisper (multilingual since 1.6), SAPI/Piper/pyttsx3 TTS, barge-in | `voice/*.py` |

## 2. Request-to-action lifecycle (verified by tracing real runs)

1. Voice: wake word → VAD end-pointing → Whisper (language hint) → `handle(text, source="voice")`. Text: `handle(text)`.
2. `_understand_language`: offline detection; non-English → one LLM translation call; verbatim slots never translated.
3. Pending yes/no, pending confirmation, pending slot are handled first.
4. `nlu.split` → for each part: follow-up resolver → `NLUPipeline.parse` (rules, typo repair, classifier, fast routes, LLM NLU).
5. Reference resolution and missing-slot questions (`DialogueManager`).
6. `Planner.build` (templates) or `plan_with_llm` (catalogue excludes 25 dangerous tools; every step's tool and arguments validated).
7. `Executor.run`: per step — resolve `{{steps.N.data.x}}` templates, schema, `risk_for`, `PermissionManager.check` (untrusted
   content escalates LOW steps), confirmation (HIGH always), run, retry once if the result says `retryable`, `verify()`, audit
   row + structured log. One LLM re-plan if an LLM plan fails. Limits: 8 sub-requests, 240 s, 12 steps.
8. `summarize(plan)` builds the reply only from real tool results; persona; translation back; TTS in a matching voice.

## 3. Verified features vs. claims

| Claim | Status | Evidence |
|---|---|---|
| 134 tools | **Inaccurate: 135** | `python -m eval.tool_audit` (1.6 added `set_language`; docs not updated) |
| 47 intents | Verified | `len(Intent)` = 47 (22 of them have classifier training data) |
| 460 tests | Verified | `python -m unittest discover -t . -s tests` → `Ran 460 tests … OK` (88 s) |
| 80.2 % classifier accuracy | Verified **on its own training data** (5-fold CV of 475 utterances) | Not measured on unseen phrasings — see §6 and `docs/GENESIS_EVALUATION.md` |
| Rules → classifier → LLM fallback | Verified | `nlp/pipeline.py` |
| Risk classes + confirmation | Verified | 102 LOW / 28 MEDIUM / 5 HIGH; HIGH can't be trusted away (`security/permissions.py`) |
| Post-action verification | **Partial** | 45 of 135 tools implement `verify()`; 90 return "unverified" (many are read-only, but e.g. `create_calendar_event`, `update_calendar_event`, `delete_calendar_event`, `delete_task`, `delete_note`, `run_program`, `set_volume` don't verify) |
| Multilingual input/output | Verified with a scripted model; **unverified** with a live LLM and real Windows voices in this environment |
| Offline wake word, VAD, barge-in | Verified by unit tests with synthetic audio; **unverified** on real microphones here |
| Desktop app starts | **Unverified here** — PySide6 can't be installed in this Linux sandbox; it starts on the user's Windows PC (their logs) |

## 4. Critical bugs and weaknesses found

| # | Problem | Severity |
|---|---|---|
| B1 | "do I have any unread emails" → parsed as **calendar** list | Medium — wrong tool |
| B2 | "remember that my exam is on friday" → saved as a **contact** named "on friday" | Medium — corrupts memory |
| B3 | "run the tests" → tries to open an **app called "tests"** | Low |
| B4 | "please open chrome and spotify" → only Chrome (the leading "please" blocks splitting) | Low |
| B5 | "draft an email to Rahul saying I'll be late **and then open gmail**" → "and then open gmail" becomes part of the e-mail body | Medium — wrong content would be sent if confirmed |
| B6 | Multi-step goals with dependencies ("find my report, summarize **it** and email my professor") are split into independent parts; "it" is lost and the first part isn't understood | High for the agent goal |
| B7 | "continue where you stopped" — no notion of resumable tasks; task state lives only in memory and is lost on restart | Medium |
| B8 | Classifier confidence is used with a single global threshold; no margin check between the top two intents | Medium |
| B9 | No approval binding: an approval is tied to a plan step, not to the exact arguments; a group approval covers later steps by description only | Medium (defence in depth) |
| B10 | Duplicate consequential actions ("send it" twice, re-running a routine) have no guard beyond the normal confirmation | Medium |
| B11 | The executor retries any step whose result says `retryable`; nothing *structurally* prevents a future non-idempotent tool from being retried (today no consequential tool sets it — verified) | Low today |
| B12 | No cancellation of a running multi-step task (the turn lock blocks a second request) | Medium |
| B13 | LLM plan templates are not checked for forward/self references at planning time (they fail at run time instead) | Low |
| B14 | No persistent record of failures by category (auth / network / ambiguous / unknown outcome); recovery decisions are per tool | Low |
| B15 | **Performance:** importing scikit-learn runs `numpy.f2py.crackfortran`, which sets the global `re._MAXCACHE = 50`; the rule parser's several hundred regexes were recompiled on almost every request (measured 43–170 ms per utterance; found during the 2.0 evaluation) | High (latency) |

## 5. Security review (baseline)

* **Strong:** schema validation for every step; LLM catalogue excludes 25 dangerous tools; shutdown/restart can never come from the
  classifier or the LLM; typed repository name + yes for repo deletion; untrusted content (web, mail, files, screen) escalates later
  LOW steps; secrets in Credential Manager; redaction in logs/audit; no shell tool; the code runner strips secrets from its environment.
* **Gaps:** B9 (approval binding), B10 (duplicates), B11 (structural retry rule), cumulative risk of long LLM plans (several MEDIUM steps
  are each confirmed individually, but the user never sees the whole workflow before the first one runs).

## 6. Performance (measured, this sandbox, Python 3.11)

* Rule path: *expected* well under 1 ms, **measured 43–170 ms** because of B15 (about 1 ms after the 2.0 fix); classifier ~1.4 ms; NumPy fallback training ~1.5 s once.
* Genesis processing per turn < 0.1 s; latency is dominated by the LLM (1–16 s observed in the user's logs) and first-time
  speech-model download (~26 s once).
* LLM calls per request: 0 for known commands, 1 for questions/new goals, +1–2 for non-English.

## 7. Ranked changes for 2.0 (impact ÷ risk)

1. Add-on **policy layer** (approval binding, duplicate guard, retry guard, plan dependency check, whole-workflow review) — high impact, low risk.
2. **Task journal** persisted in SQLite (new table, no migration of existing data) with typed states, checkpoints and resume — high impact.
3. **Routing**: margin-aware decision with thresholds chosen on a held-out set; dependency-aware multi-intent routing; fixes for B1–B5 in an override layer — high impact, low risk.
4. **Cancellation** between steps — medium impact, low risk.
5. **Error taxonomy** on every failed result — medium impact, low risk.
6. **Evaluation**: held-out intent set (paraphrases, typos, ASR errors, mixed language, ambiguous, multi-intent, out-of-scope, adversarial) and end-to-end scenarios — required to prove any of the above.
7. Multilingual: interpretation always shown before consequential translated actions; cross-language follow-up tests.

Constraint from the owner: **existing code is not rewritten.** New behaviour lives in `genesis/v2/`; existing files receive only small,
additive hooks, and all 460 existing tests must keep passing unchanged.
