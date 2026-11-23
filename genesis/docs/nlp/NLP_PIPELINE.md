# The Genesis NLP pipeline

Genesis is a **task-oriented conversational agent**. Its NLP pipeline follows the classic spoken-dialogue-system decomposition:

> ASR → NLU (intent + slots) → dialogue state tracking → dialogue policy → NLG → TTS

On top of this it adds modern LLM components: structured-output NLU, LLM planning and generative responses. The code lives in `genesis/nlp/` and `genesis/ai/`.

## 1. Pre-processing (`rules.normalize`, `rules.split_requests`)

- Strips the wake phrase and its common ASR mis-hearings (“hey genesis”, “ok genesis”, “hey jenesis”).
- Strips politeness wrappers (“can you”, “please”, “I'd like you to”).
- **Compound-request segmentation.** The text is split on `and / then / , / ;` only when the next token is an action verb. Message bodies after *saying / tell him / remind me to* are protected, so “…saying I'll open the lab and close it” stays one request.

## 2. Intent classification (three stages)

| Stage | Technique | When |
|---|---|---|
| Rules | ordered regular-expression patterns + deterministic slot extractors | high-precision and safety-critical commands (power actions are **only** recognised here) |
| Statistical classifier | TF-IDF features (word 1–2-grams + character 3–5-grams) → multinomial **logistic regression**, trained at start-up on `nlp/data/intents.json` (~250 labelled utterances, 22 intents) | anything the rules miss; character n-grams make it robust to ASR errors |
| LLM structured NLU | the LLM returns `{"intent", "confidence", "entities"}` JSON; deterministic extractors then override the fields they are reliable for | low classifier confidence (< 0.6), or an actionable prediction whose slots need understanding |

Intents: `conversation, open_application, close_application, open_folder, open_url, search_web, send_email, draft_email, read_email, create_reminder, file_operation, system_control, browser_action, code_action, information_request, shutdown, restart, settings, memory, knowledge, ui_control, cancel, unknown`.

## 3. Entity extraction (`nlp/entities.py`)

| Entity | Method |
|---|---|
| e-mail, URL | regular expressions (e-mail spans are excluded from URL matches) |
| file path / file name | Windows absolute paths, quoted paths, relative `a/b.py`, `~/x`, bare names with known extensions |
| application | longest-match gazetteer over ~60 apps and aliases (“vs code”, “visual studio code” → `vscode`) |
| folder | known-folder lexicon (Downloads, Documents, Desktop…), OneDrive-aware |
| person | honorific patterns (“Professor Rao”), relation phrases (“my professor”, resolved against contacts), capitalised names after *to/email/tell* |
| date/time | a deterministic temporal grammar: relative (“in 10 minutes”), deictic (“tomorrow”, “tonight”), weekdays (“next Monday evening”), dates (“5 October”, ISO), times (“at 9 am”, “17:30”, bare “at 5” → 5 pm), parts of the day. Falls back to `dateparser` |
| number | regex |

Slots specific to each intent (recipient, message, subject, query, file operation, source, destination, new name, reminder text…) are filled by extractors for that intent.

## 4. Dialogue management (`nlp/dialogue.py`)

**Dialogue state tracking.** For each conversation Genesis keeps:

- the history window (24 turns);
- **referents**: the last person, file, folder, app, project and Gmail draft;
- the open **frame** (intent + slots) and the **pending slot** when Genesis has asked a question;
- a **queue** of the remaining sub-requests from a compound utterance.

**Coreference.** *him / her / them* resolve to the last person; *it / this file / that* resolve to the last file; “send it” resolves to the last draft; “find the error in this file” uses the last file.

**Slot filling (the dialogue policy).** If a required slot is missing, Genesis asks a targeted question (“What should I say?”, “When should I remind you?”) and interprets the next utterance *as that slot*. For example, “Tell him I'll reach at 5” becomes `message="I'll reach at 5"`. An answer that is clearly a new command abandons the open frame. “Never mind” cancels it.

**Confirmation sub-dialogue.** When an action needs approval, the next utterance is parsed strictly as yes or no. Anything else re-asks, and a new command cancels the pending action.

## 5. Response generation (NLG)

- **Action results:** built by the verifier **from actual tool results**. This is template-based NLG, so the system cannot hallucinate success.
- **E-mail bodies:** LLM generation with a JSON-schema prompt. If the LLM is unavailable, a deterministic template chooses a formal or informal register from the recipient (honorifics, relation words).
- **Conversation / knowledge questions:** the LLM, given a persona system prompt, the memory context and the conversation history. Retrieval-augmented answers cite their sources.
- **Offline fallback:** an ELIZA-style reflection responder (Weizenbaum, 1966) that is honest about being offline.
- **Speech:** `speech_text()` strips URLs, markdown and emoji and shortens long replies before TTS.

## 6. Speech

- **ASR:** faster-whisper (Whisper, Radford et al. 2023; CTranslate2 int8 on CPU), with a biasing prompt that contains assistant vocabulary. Vosk (Kaldi) is the offline fallback.
- **Wake word:** Vosk with a constrained grammar that acts as a keyword spotter (the search space is only *hey genesis / genesis / [unk]*), plus a word-confidence threshold, a loudness gate and a refractory period. Porcupine is optional.
- **End-pointing:** RMS energy against an adaptive noise floor, ending after 0.9 s of silence, with a 6 s no-speech timeout and a 15 s cap.
- **TTS:** Microsoft SAPI 5 with asynchronous speech and purge, which enables barge-in.

## 7. Inspecting the pipeline

```powershell
python -m genesis --cli
you › /nlu remind me tomorrow at 9 am to submit my assignment
```

This prints the intent, confidence, slots, entities (with character spans), the source stage and the classifier alternatives.
