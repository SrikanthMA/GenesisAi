# Setup guide

## 1. Requirements

| | |
|---|---|
| OS | Windows 10 (21H2+) or Windows 11 |
| Python | 3.10, 3.11 or **3.12 (recommended)**. 3.13 may lack wheels for some audio packages |
| Disk | ~1.5 GB with voice models (PySide6 ~350 MB, faster-whisper + model ~250 MB, Vosk model 40 MB) |
| Hardware | any modern CPU; a GPU is **not** required; a microphone for voice |
| Editor | VS Code with the Python extension |

## 2. Install

### Automatic (recommended)

Double-click `setup.bat`, or in VS Code choose **Terminal → Run Task → Genesis: First-time setup**.

### Manual

```powershell
cd path\to\genesis
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-optional.txt   # optional extras (see below)
copy .env.example .env
python scripts\download_models.py          # Vosk wake-word/STT model → data\models
python -m genesis --doctor
python -m pytest -q
python -m genesis
```

In VS Code, press `Ctrl+Shift+P`, run **Python: Select Interpreter**, and choose `.venv\Scripts\python.exe`.

### Optional extras (`requirements-optional.txt`)

| Package | Enables |
|---|---|
| `sentence-transformers` | neural embeddings (all-MiniLM-L6-v2) for the knowledge base; pulls in PyTorch |
| `playwright` + `playwright install chromium` | visible browser automation (click, fill, read the current page) |
| `ddgs` | reading search results so Genesis can summarise them |
| `python-docx` | reading `.docx` files |
| `pvporcupine` | Porcupine wake-word engine |

## 3. Credentials

Genesis never stores credentials in code. Everything goes in `.env`, which is git-ignored, or in the OS credential store.

### Hugging Face token

This is used for the conversational LLM, **Qwen/Qwen2.5-7B-Instruct**, through Hugging Face Inference Providers.

1. Create a free account at <https://huggingface.co>.
2. Go to **Settings → Access Tokens → Create new token**. Choose type **Fine-grained** and tick **“Make calls to Inference Providers”**.
3. Put it in `.env`:
   ```ini
   GENESIS_LLM_PROVIDER=huggingface
   HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxxxxxx
   HF_MODEL_ID=Qwen/Qwen2.5-7B-Instruct
   ```
4. In Genesis, go to **Connected services → Language model → Test connection**.

The free tier includes a small monthly credit. If the model is unavailable or the quota runs out, Genesis switches to offline mode automatically and shows a badge.

**Fully local alternative:** install [Ollama](https://ollama.com), run `ollama pull qwen2.5:7b-instruct`, and set `GENESIS_LLM_PROVIDER=ollama`. No data then leaves your PC. A 7B model needs about 8 GB of RAM.

### Gmail (Google OAuth)

Genesis uses Google's official OAuth flow for desktop apps. **You never give Genesis your password.**

1. Open <https://console.cloud.google.com/> and create a project, for example “Genesis Assistant”.
2. Go to **APIs & Services → Library** and enable **Gmail API** and **Google Calendar API**.
3. Go to **APIs & Services → OAuth consent screen**:
   - User type: **External**. Fill in the app name and your e-mail.
   - Scopes: you can skip this step, because Genesis requests them at sign-in.
   - **Test users: add your own Gmail address.** While the app is in “Testing”, only test users can sign in.
4. Go to **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type: **Desktop app**.
   - Copy the **Client ID** and **Client secret** into `.env`:
     ```ini
     GOOGLE_CLIENT_ID=1234567890-abc.apps.googleusercontent.com
     GOOGLE_CLIENT_SECRET=GOCSPX-xxxxxxxxxxxx
     ```
     Alternatively, download the JSON file and set `GOOGLE_CLIENT_SECRETS_FILE=C:\path\client_secret.json`.
5. Restart Genesis, then go to **Connected services → Connect Google account**. A browser opens; sign in and accept.
   You will see “Google hasn't verified this app”. That is expected for your own testing project: click **Continue**.
6. The token is stored encrypted, in Windows Credential Manager under “Genesis-Desktop-Assistant”. You can revoke it at any time with **Disconnect**.

Scopes requested: `gmail.compose` (create and send drafts), `gmail.readonly` (search and read), `calendar.events`, `openid email`.

**Without OAuth**, e-mail requests still work: Genesis opens a *pre-filled Gmail compose window* in your browser (Gmail auto-saves it as a draft) and says clearly that it did this instead of using the API.

### Save your professor as a contact

Say *“Remember that my professor is Professor Rao, rao@university.edu”*, or add the contact on the **Memory** page.
After that, “my professor” resolves automatically.

## 4. Voice

1. Run `python scripts\download_models.py` to install the Vosk model (wake word and fallback speech recognition).
2. faster-whisper downloads its `base.en` model (~150 MB) on first use. Run `python scripts\download_models.py --whisper` to download it in advance.
3. Windows: **Settings → Privacy & security → Microphone** must allow “desktop apps” to use the microphone.
4. To choose a specific microphone, run `python scripts\list_audio_devices.py` and set `GENESIS_AUDIO_DEVICE=<index>`.
5. Adjust **Settings → Voice → Wake sensitivity**. Higher values trigger more easily but raise the risk of false activations.

**Porcupine (optional, more robust wake word):** create a free account at <https://console.picovoice.ai>, train the keyword “Hey Genesis” for *Windows*, and download the `.ppn` file. Then set:

```ini
GENESIS_WAKE_WORD_ENGINE=porcupine
PICOVOICE_ACCESS_KEY=...
PORCUPINE_KEYWORD_PATH=C:\path\Hey-Genesis_en_windows.ppn
```

## 5. Where things are stored

When running from source, everything is inside the project folder. The packaged .exe uses `%LOCALAPPDATA%\Genesis` instead.

| Path | Contents |
|---|---|
| `data\genesis.db` | SQLite: conversations, memory, contacts, tasks, audit log, settings, reminders, knowledge base |
| `data\models\` | Vosk / Whisper models |
| `data\credentials\` | encrypted vault (only if Windows Credential Manager is unavailable) |
| `logs\` | `genesis.log`, `errors.log`, `tool_execution.log`, `security.log` (JSON lines, secrets redacted) |

## 6. Start with Windows

Turn on **Settings → Start Genesis with Windows**, or say *“start Genesis with Windows”*. This writes a per-user `HKCU\...\Run` entry and needs no admin rights.
