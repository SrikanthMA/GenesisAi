# Presentation outline: GENESIS (CO1)

*About 15 minutes plus a 5-minute live demo. Each slide lists its title, its key points, and the visual to show.*

---

**Slide 1 — Title**
GENESIS: Intelligent Desktop AI Assistant · NLP topic: Conversational AI / Chatbots · team names, course, faculty
*Visual:* the animated Genesis orb (screenshot of the mini assistant)

**Slide 2 — The problem**
- Simple intentions (“e-mail my professor”) take ten or more manual steps.
- Chatbots can explain the steps but can't perform them. Voice assistants are cloud-centric with closed action sets.
- **Goal:** a conversational agent that *understands* and safely *acts* on the desktop.

**Slide 3 — Why this is an NLP problem**
ASR → NLU (intent + entities) → dialogue state → policy → NLG → TTS. Almost every syllabus topic appears in one product.

**Slide 4 — Literature timeline**
ELIZA (1966) → ATIS intent/slot (1990) → POMDP dialogue (2013) → seq2seq (2014) → Transformer (2017) → BERT (2019) → GPT-3 (2020) → RAG (2020) → ReAct/Toolformer (2023) → Whisper (2023) → Qwen2.5 (2024)
*Visual:* horizontal timeline

**Slide 5 — Genesis at a glance**
Mini assistant + full chat + tray · “Hey Genesis” · 45 real tools · Gmail · VS Code · reminders · power actions with confirmation
*Visual:* two screenshots side by side (mini and expanded)

**Slide 6 — Architecture**
*Visual:* the overall architecture diagram (ARCHITECTURE.md §1)
Layers: UI · Voice · NLP/AI · Agent · Tools · Memory · Security · Data

**Slide 7 — NLP pipeline**
Normalise → split compound requests → rules → TF-IDF + logistic regression → LLM structured NLU → entities → dialogue manager
*Visual:* NLP pipeline diagram

**Slide 8 — Intent classification and evaluation**
- TF-IDF word 1–2 + char 3–5-grams, softmax logistic regression; 475 utterances, 22 intents
- 5-fold CV: word-only 76.4 % → word+char **80.2 % accuracy / 0.82 macro-F1**
- Rules: 44/44 specification commands correct
*Visual:* bar chart and per-intent F1 table (from `scripts/evaluate_nlu.py`)

**Slide 9 — Entities and dialogue management**
Temporal grammar (“tomorrow at 9 AM”), contact resolution (“my professor”), coreference (“tell *him*…”), slot filling
*Visual:* the Rahul dialogue trace

**Slide 10 — Voice pipeline**
Local wake word (constrained Kaldi grammar) → energy VAD → Whisper ASR → core → SAPI TTS with barge-in
*Visual:* voice sequence diagram

**Slide 11 — Agent: planner → executor → verifier**
Templates and LLM JSON plans, schema validation, `{{steps.N.data}}` chaining, safe recovery, honest summaries
*Visual:* agent execution flowchart

**Slide 12 — Safety and privacy**
LOW/MEDIUM/HIGH risk levels, confirmations, validators, no shell tool, prompt-injection escalation, OAuth + encrypted tokens, audit log, local-first processing
*Visual:* permission flow diagram and the privacy table

**Slide 13 — Live demo** (see DEMO_SCRIPT.md)
1. “Hey Genesis” → draft e-mail to professor
2. “Open VS Code and open my Genesis project”
3. “Shut down my computer” → “No”

**Slide 14 — Testing**
164 automated tests: NLU, dialogue, tools, security, agent, LLM/Gmail, voice pipeline, end-to-end scenarios

**Slide 15 — Limitations and future scope**
Small training set → joint BERT intent/slot model · neural wake word + speaker ID · neural TTS · multilingual · ReAct replanning · usability study

**Slide 16 — Conclusion**
Classical dialogue structure + LLM flexibility + mediated tools = an assistant that *acts* and is safe and honest.

**Slide 17 — References and Q&A**
