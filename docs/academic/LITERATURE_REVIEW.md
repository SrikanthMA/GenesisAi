# Literature review: Conversational AI and chatbot systems

This review follows the development of conversational AI from rule-based chatbots to tool-using LLM agents. It separates **foundational NLP research** (Sections 1–6) from **modern LLM-based systems** (Sections 7–9), and for each strand notes how Genesis uses it. Full citations are in the [CO1 report](CO1_NLP_Report.md#32-references).

---

## 1. Origins: the imitation game and pattern-matching chatbots

**Turing (1950)** proposed the “imitation game”: a machine would be judged intelligent if its conversation could not be told apart from a human's. This made dialogue a benchmark task for AI.

**ELIZA (Weizenbaum, 1966)** was the first widely known chatbot. It used *decomposition rules* (keyword patterns with wildcards) and *reassembly rules* that reflect the user's words back (“I feel tired” → “Why do you feel tired?”), with pronoun swapping (*my → your*). ELIZA had no understanding, yet users attributed understanding to it (the “ELIZA effect”). Weizenbaum's lesson, that fluent output can mislead users about what a system really does, is directly relevant to modern assistants.
→ *Genesis:* the offline fallback responder is ELIZA-style, and it is explicitly **honest** that it is offline. More importantly, Genesis builds every action report from verified tool results, so fluency never misrepresents what happened.

**PARRY (Colby et al., 1971)** added an internal state (a model of emotions) that influenced responses. This is an early form of *state tracking*.

**ALICE / AIML (Wallace, 2009)** scaled pattern matching to tens of thousands of hand-written categories in the XML-based AIML language and won the Loebner Prize several times. It showed both the reach and the brittleness of rule-based conversation.

## 2. Task-oriented spoken dialogue and the intent–slot paradigm

In the 1990s research shifted to **task-oriented** systems. The **ATIS** corpus (Hemphill et al., 1990) of spoken flight-information queries defined *spoken language understanding* (SLU) as two sub-tasks:

- **intent detection:** classify the utterance (e.g. `flight`, `airfare`);
- **slot filling:** label spans with semantic roles (`from_city=Boston`, `depart_date=tomorrow`).

This formulation is still the backbone of commercial assistants and of Genesis (`draft_email` + `recipient`, `message`, `subject`).

The **pipeline architecture** became standard:

> ASR → NLU → **dialogue state tracking (DST)** → **dialogue policy** → **NLG** → TTS

**Young et al. (2013)** reviewed POMDP-based statistical dialogue systems. They treat the dialogue state as a hidden variable, maintain a *belief state* over it, and learn the policy with reinforcement learning, which makes them robust to ASR errors. The **Dialog State Tracking Challenges** (Williams et al., 2016) standardised DST evaluation and showed the benefit of discriminative trackers.
→ *Genesis:* keeps an explicit dialogue state (frame, pending slot, referents, queue) and a rule-based policy (ask for missing slots, confirm risky actions, plan, or chat). This is transparent and deterministic, which matters when the “system action” is shutting down a computer.

## 3. Text classification and sequence labelling

**Intent classification** is text classification. Classical approaches represent text as sparse bag-of-n-gram vectors weighted by **TF-IDF** (Manning et al., 2008) and train linear models (logistic regression, SVMs). These are strong, fast and data-efficient baselines.

**Entity extraction / slot filling** is sequence labelling. **Conditional random fields** (Lafferty et al., 2001) model label dependencies. **BiLSTM-CRF** architectures (Lample et al., 2016) learn features automatically and reached state-of-the-art NER.

**Joint models** learn intents and slots together: attention-based RNNs (Liu & Lane, 2016), and later a fine-tuned **BERT** with an intent head on the `[CLS]` token and a slot head on the token outputs (Chen et al., 2019), which reported large gains on ATIS and Snips.
→ *Genesis:* uses TF-IDF (word + character n-grams) with logistic regression for intents (80.2 % 5-fold accuracy, 0.82 macro-F1 on 475 utterances across 22 intents), and grammars and gazetteers for entities. A joint BERT model is identified as future work, once enough consented data exists.

## 4. Distributed representations and neural sequence models

**Word2vec** (Mikolov et al., 2013) popularised dense word embeddings. **LSTMs** (Hochreiter & Schmidhuber, 1997) made it possible to model long sequences. **Sequence-to-sequence** learning (Sutskever et al., 2014) with **attention** (Bahdanau et al., 2015) made end-to-end generation possible, and **Vinyals & Le (2015)** trained a *neural conversational model* on dialogue corpora. It was fluent but generic and inconsistent, which showed that pure end-to-end chit-chat does not give reliable task completion. **Wen et al. (2017)** proposed an end-to-end *trainable* task-oriented system that keeps modular structure (intent network, belief trackers, database operator, generator).

## 5. Transformers and pre-trained language models

The **Transformer** (Vaswani et al., 2017) replaced recurrence with multi-head self-attention, allowing parallel training and long-range context. **BERT** (Devlin et al., 2019) pre-trained a bidirectional Transformer encoder with masked-language modelling, which transformed NLU. **GPT-2** (Radford et al., 2019) showed that large decoder-only language models learn many tasks without supervision.

## 6. Speech technologies

- **ASR:** HMM–GMM systems (Rabiner, 1989) dominated for decades. The **Kaldi** toolkit (Povey et al., 2011) made them widely available, and Vosk, used by Genesis, is built on it. **DNN acoustic models** (Hinton et al., 2012) and **RNN/CTC** systems (Graves et al., 2013) brought neural ASR. **Whisper** (Radford et al., 2023) trained a Transformer encoder–decoder on 680k hours of weakly supervised multilingual audio and achieved robust zero-shot recognition. Genesis runs it locally through faster-whisper.
- **Keyword spotting:** Chen et al. (2014) introduced small-footprint DNN keyword spotting for always-on devices, the basis of “Hey Siri / OK Google”-style wake words. Genesis uses constrained-grammar Kaldi decoding (or Porcupine) with confidence and energy gating.
- **TTS:** **WaveNet** (van den Oord et al., 2016) generated raw audio autoregressively, and **Tacotron 2** (Shen et al., 2018) combined a sequence-to-sequence mel-spectrogram predictor with a WaveNet vocoder to reach near-human naturalness. Genesis uses SAPI for low latency, offline use and instant interruption, and lists neural TTS as future scope.

---

## 7. Large language models and instruction following

**GPT-3** (Brown et al., 2020) showed *in-context* few-shot learning at 175B parameters. **InstructGPT** (Ouyang et al., 2022) aligned models to follow instructions using reinforcement learning from human feedback. This produced assistants that respond helpfully to natural requests. Open-weight instruction models such as **Qwen2.5** (Qwen Team, 2024) bring this ability to models small enough (7B) to run through affordable APIs or locally.
→ *Genesis:* uses **Qwen2.5-7B-Instruct** via Hugging Face (or any compatible provider) for fallback NLU, e-mail composition, explanations and conversation, with structured JSON prompting.

Open-domain neural chatbots such as **Meena** (Adiwardana et al., 2020) and **DialoGPT** (Zhang et al., 2020) improved conversational quality but do not act.

## 8. Retrieval-augmented generation

LLMs hallucinate and have no access to private data. **RAG** (Lewis et al., 2020) retrieves relevant passages with a dense retriever and conditions generation on them. **Sentence-BERT** (Reimers & Gurevych, 2019) produced sentence embeddings suited to semantic search. **MiniLM** (Wang et al., 2020) distilled them into small, fast models such as *all-MiniLM-L6-v2*.
→ *Genesis:* has a local knowledge base (chunking, MiniLM embeddings or a hashed-n-gram fallback, cosine top-k retrieval) that produces answers with citations. Retrieved text is treated as untrusted.

## 9. Tool-using LLM agents

**Chain-of-thought prompting** (Wei et al., 2022) improved multi-step reasoning. **ReAct** (Yao et al., 2023) interleaves reasoning traces with *actions* (tool calls) and *observations*. **Toolformer** (Schick et al., 2023) trained a model to decide when to call APIs. Surveys (Xi et al., 2023; Wang et al., 2024) describe the now-standard agent components: *profile, memory, planning and action*.

This power brings new risks. **Greshake et al. (2023)** demonstrated **indirect prompt injection**: instructions hidden in web pages or e-mails can hijack LLM-integrated applications into exfiltrating data or taking actions.
→ *Genesis:* follows the agent pattern (memory + planner + tools + verifier) but **mediates** every action. Tools are typed and schema-validated; there is no shell tool; risky tools need confirmation; LLM plans cannot include power, delete or send tools; and any action planned after reading external content requires confirmation.

---

## 10. Comparative summary

| Approach | Understanding | Actions | Transparency | Robustness offline | Example |
|---|---|---|---|---|---|
| Pattern matching | none | none | high | high | ELIZA, ALICE |
| Statistical task-oriented | intents/slots | fixed domain | medium | high | ATIS systems, Rasa |
| End-to-end neural chat | implicit | none | low | model-dependent | Meena, DialoGPT |
| LLM chat assistants | strong | none/limited | low | low (cloud) | chat apps |
| LLM agents (ReAct/Toolformer) | strong | open-ended | low–medium | low | research agents |
| **Hybrid (Genesis)** | rules + classifier + LLM | 45 typed tools | **high** (plans, audit, confirmations) | **high** (local ASR, NLU, tools) | **Genesis** |

## 11. Research gap and positioning

The literature offers either **reliable but narrow** task-oriented systems or **flexible but unconstrained** LLM agents. Genesis sits between them. Classical dialogue-system structure provides reliability, interpretability and safety; LLM components provide flexibility; and a security-first tool layer makes it acceptable for an assistant to act on a personal computer.
