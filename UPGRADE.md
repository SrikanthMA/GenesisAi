# Upgrading Genesis (1.0 – 1.4 → 1.5) — replace the files in place

Your `.env` (API keys), `.venv` (installed packages), `data/` (database, memory, contacts, Gmail connection, downloaded voice model) and `logs/` are **not included** in the update zip. Extracting it over your existing folder keeps all of them.

## Steps (Windows)

1. **Quit Genesis.** Right-click the tray orb and choose **Quit Genesis**, or press Ctrl+C in the terminal that's running it.
2. Extract `Genesis_Update.zip` **into the folder that contains your `genesis` folder**, not into the `genesis` folder itself.
   *Example:* if Genesis lives in `C:\Users\you\Desktop\genesis`, extract to `C:\Users\you\Desktop`.
   When Windows asks, choose **“Replace the files in the destination”**.
3. In VS Code, open the `genesis` folder and a terminal (**Terminal → New Terminal**), then run:
   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   .\.venv\Scripts\python.exe -m unittest discover -s tests -t .   # optional: should end with "Ran 434 tests … OK"
   .\.venv\Scripts\python.exe -m genesis
   ```
   This installs three new packages: Playwright (drives Microsoft Edge for WhatsApp Web, Colab and forms — no browser
   download needed), uiautomation (lets Genesis see buttons and fields in apps) and Pillow. The database upgrades itself (migrations v2 and v3) on first start.
   **Calendar:** if you connected Google before, reconnect once (Connected services → Disconnect → Connect) if Genesis says the
   Calendar permission is missing. **Coding agent:** needs HF_TOKEN (or Ollama). To run JavaScript/TypeScript install Node.js 22.6+,
   and for Java install a JDK 11+. Python works out of the box.
4. *(Optional)* For screen understanding: `.\.venv\Scripts\python.exe -m pip install Pillow pytesseract uiautomation`
   (plus the Tesseract OCR program for reading screen text).

## What to try (1.5)

- Run the pip install line above (it adds python-docx, openpyxl and python-pptx).
- “Make a PowerPoint on solar energy with 5 slides” · “create a Word document about climate change” ·
  “create an Excel sheet for my monthly budget”. Files go to Documents\Genesis Documents.
- Open Word yourself (or say “open Word”), then: “write a paragraph about India”.
- GitHub: “open GitHub”. For your repositories add `GITHUB_TOKEN=` to `.env` (see .env.example), then
  “show my repos”, “create a private repo called test-genesis”, “delete my GitHub repo test-genesis”.
- Colab: “open Colab” → “create a notebook” → “write code in Colab to print the first 10 squares and run it”.

## What to try (1.4)

- “Open WhatsApp” (scan the QR code once in the window that opens) → “show me the latest message from Mom” →
  “reply to that saying I'll be there at 7” → “yes”
- “Message Rahul saying I'll call him in ten minutes” — it drafts, asks, sends, checks
- “Open Colab” (sign in to Google once) → “create a notebook” → “write code that prints the first 10 squares” → “run it”
- “Show me a picture of a peacock”, “generate a futuristic image of a city”
- In any app: “what buttons are here?”, “click the second button”, “type hello”, “press ctrl+s”
- On a job-application page: “fill this form using my resume” → “don't submit it yet” → “okay, submit it”
- Voice: Settings → Voice → Voice style *Female*. For a more natural offline voice:
  `.\.venv\Scripts\python.exe -m pip install piper-tts` then `.\.venv\Scripts\python.exe scripts\download_models.py --voice`

## Fix the AI brain (1.3.1)

1. Open `.env` in the `genesis` folder (in VS Code — it shows the real file name; if it's `.env.txt`, rename it to `.env`).
2. Make sure it has a line like `HF_TOKEN=hf_yourtoken` (no quotes, no spaces). A token from huggingface.co → Settings →
   Access Tokens → Fine-grained → **“Make calls to Inference Providers”**.
3. Test: `.\.venv\Scripts\python.exe -m genesis --check-llm` → `"available": true`. Otherwise `error_kind` says why.
4. In Genesis: “what's the capital of Japan?”, “explain recursion”, “tell me a joke”, “check your AI connection”.

## What to try (1.3)

- “My name is <your full name>”, then “call me <short name>”. Say “Hey Genesis” and it greets you by name.
- “Open Chrome” → “search for internships” → “no, I meant AI internships”
- “Remember that I prefer Gmail” → “open my email” → “why did you open Gmail?”
- “Find five AI internships that I can apply for”
- “Prepare my computer for coding”
- “Add dark mode to my <name> project” (shows the diff first) → “undo that”
- “Add buy milk to my to-do list”, “take a note: …”, “run git status in my <name> project”

## What to try (1.2)

- “Open YouTube” → “search for Python tutorials” → “play the second one” → “pause” → “make it louder” → “skip”
- “Put a meeting with Rahul tomorrow at 4” → “what do I have tomorrow?” → “move my 4 PM meeting to 5 PM”
- “Create a Python script that prints the first 20 Fibonacci numbers”
- “Find the latest email from AMD, summarize it and draft a reply saying I'm interested”
- “Switch to Chrome”, “take a screenshot”, “find that Python tutorial we were talking about yesterday”

## What to try (1.1)

- “Hey Genesis” → “open Gmail” → “check my unread emails” → “open the latest one” → “reply saying I'll submit it tomorrow” → “actually change tomorrow to Friday” → “send it” → “yes”. Say the wake word only once.
- Stay silent for 20 seconds and Genesis goes back to sleep (the orb dims and the mini window says so). Say “Hey Genesis” again to wake it.
- “Every Monday at 8 AM, open VS Code and my Genesis project”
- Settings → **Conversation mode** to change the timeouts, barge-in, the wake phrase, or to go back to one-command-per-wake-word.

## Rolling back

Extract your original `Genesis.zip` over the folder again. The v2 tables are ignored by 1.0, so your data keeps working.
