# Genesis 3.3: talks like JARVIS, sounds like FRIDAY

Genesis's voice conversation now works the way the JARVIS project (github.com/adewaskar/jarvis, MIT licence) does. Its communication behaviour was ported into Genesis, and "sir" became "Boss". JARVIS is a browser app, so its TypeScript couldn't run inside Genesis directly; each behaviour was re-written in Python instead. The credit and licence are in `THIRD_PARTY_NOTICES.md`.

## How the two compare

| JARVIS behaviour | JARVIS source | Genesis now |
|---|---|---|
| Wake word, then talk | `voice.ts` WAKE | "Hey Genesis" (already there) |
| Wake word and command in one breath ("Jarvis, what's the weather") | `afterWake` | already there: "Hey Genesis, open Gmail" |
| Answers its name with a short line that changes each time | `fillers.ts` ATTENTION | **new:** "Yes, Boss?", "Boss?", "At your service, Boss.", "Standing by.", "Awake, Boss." |
| A pause mid-sentence isn't the end of the turn | `voice.ts` holdFor / makeAssembler | **new:** "what's the weather in… London" is one request. It waits up to 1.6 s after an unfinished-sounding phrase and never more than 6 s; complete sentences go through at once. |
| Says one line the moment work starts, matched to the task | `fillers.ts` forTool | **new:** "Checking your mail.", "Consulting the diary.", "Searching.", "Queuing it up.", "Checking the repository.", "Drafting it now.", "Compiling." The same line is never used twice in a row. |
| Speaks while the answer is still being written | `App.tsx` spk.push | 3.2: sentence by sentence |
| Ignores its own voice coming back through the microphone | `voice.ts` isEcho | 3.2 |
| Stops the moment you talk over it | onSpeechStart | already there (barge-in by voice or wake word) |
| Stays listening for a follow-up | FOLLOW_UP_MS | already there (conversation session) |
| How it speaks (PROMPT_CORE) | `bridge/server.mjs` | **new**, see below |

## How it speaks now (adapted PROMPT_CORE)

These rules apply in casual and productivity modes, with Friday mode on:

- **At most two sentences** in conversation, usually under twelve words. Detail only when it's reading out something you asked for.
- **"Boss" by position:**
  - at the end means routine: "The file is saved, Boss."
  - at the front means urgent or unasked-for: "Boss, the battery is at eleven percent."
  - in the middle means it's correcting you.
- **Success is stated plainly:** "Notepad is open, Boss." Never "Done! I've finished…".
- **Failure is fronted with "I'm afraid…":** "I'm afraid I couldn't find that file." Never "Sorry".
- **No filler or forced enthusiasm:** no "um", "okay so", "great!", "happy to help".
- **Dry, warm wit**, delivered in the same calm tone as a status report.
- **Numbers and times are spoken naturally:** "eight fifteen", not "8:15".

Coding and study modes keep their detail on screen; only the short summary is spoken.

## Voice: FRIDAY

The default voice is **"friday"**: Genesis's own warm, confident, British-leaning female voice. It's an original mix of three Kokoro voices, so no real actor's voice is copied. It runs fully offline once `Setup_Natural_Voice.bat` has installed it.

Kokoro has no Irish voice, so FRIDAY's Irish accent can't be reproduced.

## Switches (`.env`)

| Setting | Effect |
|---|---|
| `GENESIS_FRIDAY=true` | the speaking style, fillers and wake replies |
| `GENESIS_STREAM_SPEECH=true` | speak while thinking |
| `GENESIS_ASSEMBLE_SPEECH=true` | mid-sentence pauses don't cut you off |
| `GENESIS_TTS=kokoro`, `GENESIS_TTS_VOICE=friday` | the voice |

## Tested (on your Genesis 2.2.4 code)

- 642 tests, of which only the `.env.example` check fails, and only because that file wasn't in the zip.
- Your 23 scenarios pass with Friday mode on and off.
- The new tests cover:
  - pause detection, including Whisper's stray full stops;
  - joining "what's the weather in" with "London tomorrow";
  - fillers matched to the task, and lines never repeating;
  - the reporting style;
  - wake replies rotating;
  - the speaking rules reaching the model only in casual and productivity modes.

The real microphone, speakers and voice quality can only be checked on your PC.
