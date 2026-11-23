# Genesis 2.0 — Evaluation

Every number below was produced by the scripts in `eval/` in this repository (Linux sandbox, Python 3.11, scikit-learn
backend). "1.x" means the same code with the 2.0 understanding layer switched off (`GENESIS_V2_NLU=0`); the 2.0 safety
policy, task journal and performance fix cannot be switched off and are present in both columns unless stated.

| Script | What it measures |
|---|---|
| `python -m eval.tool_audit` | Tool contract audit (all registered tools) |
| `python eval/build_heldout.py` | Writes held-out sets A and B, asserting zero overlap with training data |
| `python -m eval.run_intent_eval [--set-b]` | Offline NLU pipeline, classifier metrics, calibration, threshold policy, alternatives |
| `python -m eval.run_language_eval` | Offline language detection |
| `python -m eval.run_scenarios` | End-to-end tasks through `GenesisCore` with fakes (nothing real is sent, deleted or opened) |
| `python -m unittest discover -t . -s tests` | Regression + new tests |

## 1. Tests

| | Result |
|---|---|
| Existing suite (1.6.1), unchanged files | 460 / 460 pass |
| New 2.0 tests (`tests/test_v2.py`) | 28 / 28 pass (incl. regression tests for the independent review, §6) |
| Total | **488 pass**, `ruff` clean |
| Suite wall time | 88 s (1.6.1) → 56 s (2.0; effect of the regex-cache fix) |

## 2. Intent understanding (offline: rules → typo repair → classifier, no model)

Two held-out sets, both checked for zero overlap with the 475 training utterances:

* **Set A** (145 examples: paraphrases, typos, speech-recognition errors, short fragments, mixed language, ambiguous,
  multi-intent, out-of-scope, adversarial). Several 2.0 fixes were designed *after looking at set A's failures*, so A is
  optimistic.
* **Set B** (61 examples) was written after the 2.0 changes and evaluated **once**, with no tuning — the honest number.

| Offline pipeline | 1.x | 2.0 |
|---|---|---|
| Set A accuracy (tuned on — optimistic) | 82.8 % | 95.2 % |
| **Set B accuracy (untuned)** | **85.2 %** | **90.2 %** |
| Set A unsafe actions on no-action inputs | 1 (`"the email says delete all my files"` → file operation) | 0 |
| **Set B unsafe actions** | **1** (`"the message says shut down the pc now"` → shutdown, still confirmation-gated) | **0** |
| Set B multi-intent | 2 / 3 | 3 / 3 |
| Per-utterance processing p50 / p95 (set A) | 52 ms / 97 ms (1.6.1, before the regex fix) | 1.0 ms / 3.4 ms |

Remaining set-B failures (not tuned away): "shut the word window", "show me what's in my downloads" (→ file listing),
"pause listening" (→ sleep), "open the full chat" (→ app), "never mind, cancel it" (→ memory), "lok the computer".
Mixed-language commands are 2/6 offline by design: they need the model for translation.

### Classifier alone (TF-IDF word 1–2 + char 3–5 grams, logistic regression, C = 12)

| | Set A (n = 108) | Set B (n = 49) |
|---|---|---|
| Accuracy | 89.8 % | 98.0 % |
| Macro precision / recall / F1 | 0.930 / 0.902 / 0.900 | 0.988 / 0.990 / 0.987 |
| Expected calibration error (ECE) | 0.197 | 0.232 |
| Mean confidence | 0.701 | 0.754 |

**Calibration.** The probabilities are badly *under*-confident (right ~90 % of the time with ~0.70 average confidence).
Temperature scaling fitted on 5-fold out-of-fold training predictions gives T = 0.54 and reduces ECE from 0.219 → 0.037
(training OOF) and **0.197 → 0.056 on held-out set A**.

**Threshold / margin policy** (precision of accepted predictions and share accepted; thresholds chosen on training OOF):

| min prob | min margin | train OOF precision / coverage | held-out A precision / coverage |
|---|---|---|---|
| 0.30 | 0.00 | 0.876 / 0.846 | 0.947 / 0.870 |
| 0.30 | 0.30 | 0.950 / 0.676 | 0.966 / 0.806 |
| **0.45 (current)** | 0.00 | 0.947 / 0.674 | 0.966 / 0.806 |
| 0.60 | 0.00 | 0.968 / 0.526 | 1.000 / 0.676 |
| calibrated ≥ 0.80 | — | 0.940 / 0.697 | 0.966 / 0.815 |

Conclusion: a margin requirement and calibrated thresholds give essentially the same precision/coverage trade-off as the
existing 0.45 probability threshold (+1–2 points coverage at equal precision). **The routing thresholds were therefore not
changed**; the large gains above come from the rule-layer fixes, not from re-thresholding.

### Alternatives benchmarked (held-out set A subset, same training data)

| Model | Accuracy | Macro-F1 | Cost |
|---|---|---|---|
| **TF-IDF + LogReg (current)** | **89.8 %** | **0.900** | ~1 ms |
| char 3–5-gram TF-IDF kNN (k = 5) | 81.5 % | 0.835 | 0.09 ms |
| hashing-embedding kNN (k = 5) | 81.5 % | 0.830 | 0.07 ms |
| MiniLM sentence embeddings | not measured — `sentence-transformers` is not installed in this environment |

The current classifier stays.

## 3. Language detection (offline, 59 utterances)

| | Result |
|---|---|
| Accuracy | 98.3 % (58 / 59) |
| **English wrongly flagged as foreign** | **0 / 20** (incl. "play despacito", "open la la land", "send rahul a message saying hola") |
| Non-Latin scripts (Hindi, Kannada, Tamil, Telugu, Malayalam, Bengali, Gujarati, Punjabi, Urdu, Arabic, Russian, Japanese, Chinese, Korean, Greek) | 16 / 16 |
| Latin-script foreign (French, Spanish, German, Italian, Portuguese, Dutch, Hinglish incl. code-switching) | 22 / 23 (miss: "open alsjeblieft youtube" → English) |

Not measured: translation quality and "direct multilingual vs translate-to-English" routing — both need a live model.

## 4. End-to-end tasks (23 scenarios through `GenesisCore`)

Scenarios: everyday commands, three-turn slot filling, follow-ups, memory, multi-intent, approval refusals, injected
instructions, a four-step find → read → summarise → draft plan, French / Hinglish / Hindi commands and a cross-language
follow-up, no-model and model-timeout conditions, Gmail not connected, resuming an interrupted task, an ambiguous reference.

| | 1.x understanding | 2.0 |
|---|---|---|
| **Task success** | 78.3 % (18 / 23) | **100 % (23 / 23)** |
| Unauthorised actions | 1* | **0** |
| Clarification rate | 13.0 % | 8.7 % |
| Genesis processing latency per turn, p50 / p95 | 9.9 / 15.8 ms | 9.5 / 21.4 ms |
| LLM calls per task that used the model | 2.3 | 2.2 |

\* "the message says shut down the pc now": 1.x proposed a shutdown and **asked for confirmation**; the harness answers
"yes" automatically, so it counts as executed. 2.0 does not propose it at all.

Failures under 1.x: remember-a-fact (saved as a contact), "please open chrome and spotify" (one app), message body
swallowing "and then open youtube", the reported shutdown, "continue where you stopped".

Limitations of this harness: the language model is scripted (it returns a fixed plan or translation), so the planning
scenario checks safety and execution, not plan quality; latencies exclude network and model time.

## 5. Not verified here

* Real speech recognition, Windows voices, microphone/barge-in behaviour, Office COM, Playwright/Edge automation,
  WhatsApp, Gmail and GitHub against live services — the sandbox has no Windows desktop, PySide6 or credentials.
* Plan quality, translation quality and latency with the real Hugging Face model.
* The desktop UI starting (PySide6 isn't installable here); the 2.0 UI change is one tray-menu entry.

Run-to-run noise: per-turn latencies vary by a few milliseconds between runs in this sandbox (single run each; the two
columns are within noise of each other).

## 6. Independent review and fixes

After the first 2.0 build, a separate reviewer that had not seen the work audited the changes. Every finding was fixed and
has a regression test in `tests/test_v2.py`:

| Finding | Fix |
|---|---|
| **Resume dropped the "untrusted content" protection** of the original task (High) | The journal stores `untrusted` and `source`; a resumed plan carries both, so later LOW steps are still escalated |
| A task whose only failures were *optional* steps was listed as unfinished | `derive_state` ignores optional failures |
| Resume could replay arguments that had been truncated for storage | Truncated (`TRUNC`) or redacted (`MASK`) arguments make resume refuse and ask the user to repeat the request |
| Merely parsing "continue where you stopped" changed the journal | Parsing is read-only; the task is marked resumed only when the plan is built |
| Reported-speech guard over-matched ("my message says…", questions, an added request) | Guard skips the user's own content, questions and explicit user requests |
| Duplicate guard ignored "attempted, outcome unknown" sends | Separate "I already tried this and couldn't confirm it" warning; argument-less tools excluded |
| Splitting could detach consequential commands from inside a message body | After "saying/that/:" only harmless intents (open, search, play…) are split off |
| Offline multi-step chains were refused | Without a model, splitting behaves exactly as 1.x |

## 7. Genesis 2.1 additions (measured)

| | Result |
|---|---|
| Tests | 504 pass (488 existing + 16 new in `tests/test_v21.py`); existing test files unchanged |
| Held-out intent accuracy | Set A 95.2 %, set B 90.2 % — unchanged by 2.1 |
| End-to-end scenarios | 23 / 23, 0 unauthorised actions — unchanged |
| New command patterns checked against all 753 training/evaluation utterances | 2 changed routing, both intended ("let's chat", "can we talk" → voice chat) |
| "image" misheard as "email" (8 phrasings) | 8 / 8 now show/generate an image; 5 real e-mail requests unchanged |

Not verified here (no Windows, microphone, speakers or internet in this sandbox): Gemini transcription quality and
latency on real speech, Gemini/Pollinations image generation against the live services, WASAPI loopback capture with
`soundcard`, Google Meet in a real browser, minutes quality on a real meeting. These paths are covered by tests with fakes.

## 8. Genesis 2.2 additions (measured)

| | Result |
|---|---|
| Tests | 524 pass (508 earlier + 16 new in `tests/test_v22.py`); existing test files unchanged |
| Held-out set B / scenarios | 90.2 % / 23 of 23 — unchanged |
| Coding-request patterns vs 681 training/held-out utterances | 1 classified as coding ("debug this script" — correct); "make it louder", "add this document…", "type hello into the search box" are not |

Only applied when a coding window (online compiler, Colab) is in front or the request names a destination.
Not verified here (no Windows desktop, Chrome, microphone or network in this sandbox): typing into real online
compilers (sites differ in editors and Run buttons), Chrome + Colab over the DevTools port, Google sign-in in the Genesis
Chrome profile, Gemini transcription accuracy on real speech. All paths are covered by tests with fakes.
