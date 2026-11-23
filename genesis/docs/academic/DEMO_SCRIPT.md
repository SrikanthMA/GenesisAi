# Demo script

**Duration:** about 6 minutes. **Launch config:** VS Code → *Genesis: Desktop app (dry-run power actions)*. With this config, shutdown is simulated, so you can say “yes” safely on stage.

## Before the demo (5 minutes earlier)

1. `.env` has `HF_TOKEN`, plus `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`. The Google account is connected (**Connected services** shows “✓ Connected as …”).
2. Save the professor contact: type *“Remember that my professor is Professor Rao, rao@university.edu”*. Use your own second address so the draft is real.
3. The Vosk model is downloaded, the microphone indicator shows **● mic on**, and a test “Hey Genesis” works.
4. Close other chat windows. Open the expanded chat on a second screen if possible, so the plan cards are visible.
5. Set **Settings → Assistant window → auto-hide** to 20 s, so the mini assistant stays visible while you talk.

## Demo 1: Conversational AI + e-mail (the NLP showcase)

| Say | Point out |
|---|---|
| **“Hey Genesis.”** | The orb switches to *Listening*; the wake word ran **locally**. |
| **“Draft an email to my professor saying I will submit the NLP assignment tomorrow.”** | *Thinking* → *Executing*. The plan card shows `compose_email` → `gmail_create_draft` ✓ verified. Gmail opens on the draft. |
| *(show the draft)* | “Dear Professor Rao, … I will submit the NLP assignment tomorrow …”. The recipient was resolved from “my professor”. |
| Open the chat, type `/nlu …`, or explain | intent = `draft_email` (rule), entities = recipient, message; coreference and contact resolution |

**Optional multi-turn:** “Draft an email to Rahul.” → *“What should I say?”* → “Tell him I'll reach at 5.” → *(asks for the address)* → “rahul@example.com”. This shows slot filling and coreference.

## Demo 2: Desktop automation

| Say | Point out |
|---|---|
| **“Hey Genesis, open VS Code and open my Genesis project.”** | The compound request is split into two, VS Code opens on the project folder, and only one window opens (the duplicate step is skipped). |
| **“Open my Downloads folder.”** | File Explorer opens. |
| **“Remind me in 2 minutes to stretch.”** | The reminder pops up and is spoken 2 minutes later, during Q&A. |

## Demo 3: Safety

| Say | Point out |
|---|---|
| **“Hey Genesis, shut down my computer.”** | Genesis asks *“This will shut down your computer. Unsaved work will be lost. Do you want me to continue?”*. It listens for the answer without needing the wake word again, and an amber confirmation card appears. |
| **“No.”** | “Okay, I won't shut down the computer. Cancelled.” |
| Open **Activity** | The audit log shows `shutdown_computer · cancelled_by_user`. |
| *(optional)* repeat and say **“Yes.”** | In dry-run mode: “Shutting down in 10 seconds … (dry-run mode: simulated)”. |
| Open **Tools & permissions** | Show that HIGH-risk tools can't be set to “Allow without asking”. |

## Fallbacks if something goes wrong

- **No microphone or a noisy room:** click the mic button (push-to-talk), or just type into the mini assistant. The pipeline is identical.
- **Internet down:** local commands still work, and the “AI: offline mode” badge demonstrates graceful degradation.
- **Gmail not connected:** Genesis opens a pre-filled Gmail compose window and explains why. This demonstrates honest failure reporting and recovery.

## Closing line

> “Genesis doesn't just *talk* about tasks — it understands the request, plans it, checks permission, performs it for real, verifies it, and tells you honestly what happened.”
