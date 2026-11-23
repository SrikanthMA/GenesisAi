# Troubleshooting

Start with the diagnostics, which tell you what is missing and how to fix it:

```powershell
.\.venv\Scripts\python.exe -m genesis --doctor
```

Logs are in `logs\` (JSON lines): `errors.log` holds problems, `tool_execution.log` holds every action, and `security.log` holds permission events.

| Symptom | Fix |
|---|---|
| `setup.bat` says Python not found | Install Python 3.12 from python.org and tick **Add to PATH**. Re-open the terminal. |
| `Activate.ps1 cannot be loaded because running scripts is disabled` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or just use `.\.venv\Scripts\python.exe` directly. |
| `pip install` fails on `vosk`/`faster-whisper`/`PySide6` | Use Python 3.10–3.12 (64-bit). 3.13 may lack wheels. Upgrade pip: `python -m pip install -U pip`. |
| VS Code runs the wrong Python | `Ctrl+Shift+P`, then **Python: Select Interpreter**, then `.venv\Scripts\python.exe`. |
| Nothing appears when I start Genesis | It lives in the **system tray**. Click the `^` arrow near the clock and drag the orb out to pin it. Press **Ctrl+Alt+G**. If it's already running, starting it again just shows the existing instance. |
| “AI: offline mode” badge | `HF_TOKEN` is missing or invalid, or you're offline or out of quota. Local commands still work. Check **Connected services → Test connection**. |
| HTTP 401/403 from Hugging Face | The token needs the **“Make calls to Inference Providers”** permission. Create a new fine-grained token. |
| Model not available / HTTP 400 “model not supported” | Set `HF_MODEL_ID` to a model served by Inference Providers (e.g. `Qwen/Qwen2.5-7B-Instruct`, `meta-llama/Llama-3.1-8B-Instruct`), or use Ollama. |
| Wake word never triggers | Run `python scripts\download_models.py`. Check **Settings → Voice → Listen for “Hey Genesis”**. Raise the sensitivity. Check the Windows microphone privacy settings. Choose the right mic with `GENESIS_AUDIO_DEVICE`. The mic indicator must show **● mic on**. |
| Wake word triggers too often | Lower the sensitivity, or switch to Porcupine with a custom keyword. |
| “Windows blocked microphone access” | **Settings → Privacy & security → Microphone**: turn on *Microphone access* and *Let desktop apps access your microphone*. |
| Speech is transcribed badly | Install faster-whisper (in `requirements.txt`). The first use downloads `base.en`. For better accuracy use `GENESIS_WHISPER_MODEL=small.en` (slower). |
| No voice replies | **Settings → Voice → Speak replies**. `pywin32` must be installed (in requirements). Check that a Windows voice exists (**Settings → Time & language → Speech**). |
| Gmail: “isn't connected” / compose window opens instead | Configure `GOOGLE_CLIENT_ID/SECRET` (docs/SETUP.md) and click **Connect Google account**. |
| Google: “Access blocked: app has not completed verification” | Add your Gmail address as a **Test user** on the OAuth consent screen. |
| Google: “authorization has expired or was revoked” | Refresh tokens of apps in *Testing* mode expire after 7 days. Click **Reconnect**, or publish the consent screen. |
| “I couldn't find an application called …” | Genesis searches PATH, known install folders, the App Paths registry and Start-menu shortcuts. Use the name shown in the Start menu. |
| VS Code not found | Install VS Code (user installer) and tick **Add to PATH**, or make sure `%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe` exists. |
| “outside the folders Genesis is allowed to access” | Add the folder in **Settings → File access → Allow another folder…**. |
| Shutdown didn't happen after I said yes | Check whether dry-run mode is on (`GENESIS_SYSTEM_DRY_RUN=1` or the dry-run launch configuration). Windows shows its own shutdown notice with a 10-second delay; say “cancel shutdown” to abort. |
| Browser click/fill says Playwright is needed | `pip install playwright` and then `playwright install chromium`. |
| Tests fail on import | Activate the venv and install the requirements. Run from the project root: `python -m pytest -q`. |
| Reset everything | Quit Genesis and delete `data\genesis.db`. Credentials: **Services → Disconnect**, or remove “Genesis-Desktop-Assistant” entries in Windows Credential Manager. |
