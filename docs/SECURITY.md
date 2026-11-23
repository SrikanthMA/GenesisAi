# Security & privacy

Security is enforced **in code, below the LLM**. The language model can only *propose* tool calls. It never gets a shell, a file handle or a network socket.

## Threat model

| Threat | Mitigation |
|---|---|
| LLM hallucinates or is manipulated into a destructive action | no generic shell tool; every tool is narrow and typed; JSON-schema validation; the LLM planner cannot use power, delete or send tools; risk-based confirmation |
| Prompt injection from web pages, e-mails or files | content is wrapped in `<untrusted_content>` delimiters with fake delimiters neutralised; an injection-phrase scanner flags it; the system prompt says the content is data; **any action planned after reading external content requires confirmation** |
| Path traversal / touching system files | `validate_path`: resolves symlinks and `..`; must be inside your user folder, your registered projects or folders you allowed; blocks UNC/device paths and NUL bytes; writes to `C:\Windows`, `Program Files` and `ProgramData` are always refused; credential locations (`.ssh`, `.env`, `.aws`…) are blocked |
| Malicious URLs | `validate_url`: http/https only; `javascript:`, `file:`, `data:`, `ms-msdt:`, `search-ms:` blocked; no embedded credentials; no private or loopback IPs |
| Command injection | the OS backend launches programs **only** from argument lists (`shell=False`), only for resolved executables, shortcuts and URIs; `.cmd`/`.bat` are never built from user text |
| Accidental shutdown or data loss | HIGH risk means **always** asking, with strict yes/no parsing (“maybe” is not yes); confirmations expire after 5 minutes; a new command cancels a pending one; deletions go to the Recycle Bin; bulk deletes (>20 files) become HIGH risk; opening executables or scripts is HIGH risk |
| Credential theft | OAuth only (no passwords); tokens in Windows Credential Manager or a Fernet vault with a DPAPI-protected key; never in SQLite or logs |
| Secrets in logs | `RedactingFilter` on every handler (HF tokens, `sk-…`, `ya29.…`, refresh tokens, `GOCSPX-…`, bearer headers, `password=`), plus key-based redaction for audit arguments |
| CSRF on the OAuth redirect | random `state` checked, PKCE S256, loopback redirect on a random port |

## Risk levels

| Level | Examples | Behaviour |
|---|---|---|
| LOW | open app/folder/URL, search, read a file you named, create a folder, reminders, lock screen | runs directly (you can set a tool to *always ask*) |
| MEDIUM | create/rename/move/copy files, close app, sleep, browser click/fill, send e-mail, delete | asks for confirmation unless you *allow* the tool on the Tools page. **send e-mail** and **delete** always ask |
| HIGH | shutdown, restart, bulk delete, opening executables/scripts | **always** asks. Cannot be allowed (enforced in `PermissionManager.set_policy`) |

## Audit

Every tool execution is written to `tool_executions` (SQLite) and to `logs/tool_execution.log`, with the tool, redacted arguments, risk, status, whether you confirmed it, duration and the result or error. Permission changes, escalations and denials go to `security_events` and `logs/security.log`. You can view both on the **Activity** page.

## Privacy

| Local (never leaves the PC) | Remote (only when needed) |
|---|---|
| wake-word detection, speech-to-text*, TTS | LLM requests (message text) — use Ollama to keep these local |
| intent classification, entity extraction | web search queries / pages you asked to read |
| memory, contacts, conversation history, settings, logs | Gmail / Calendar API calls you asked for |
| knowledge-base documents and embeddings* | |
| all desktop actions | |

\* unless you explicitly choose the Hugging Face STT or embedding providers.

Memory is **explicit**. Genesis stores facts only when you say “remember…”, and an e-mail address only when you type it for a named person (you are shown a “Saved contact…” step, and you can turn this off). You can view and delete everything on the **Memory** page, disable memory completely, or wipe it.

## 1.1 additions

| Feature | Safeguard |
|---|---|
| Continuous conversation | the mic returns to wake-word-only mode after the silence timeout and a hard session cap; speech is never transcribed while SLEEPING; turning listening off closes the mic |
| Follow-ups (“send it”, “close that”, “delete the second one”) | resolved to ordinary tool calls that go through the same permission and confirmation checks; ambiguous references ask |
| Interruptions / barge-in | stop speech only; a pending confirmation is either answered (strict yes/no) or cancelled by a new command, never auto-approved |
| Retries | only for transient failures of read-only operations (network, HTTP 429/5xx); sending, deleting and other side effects are never retried automatically |
| Routines | saving one asks first; when a routine runs, each step is permission-checked (risky steps still ask); routines cannot start routines; max 8 requests per turn |
| Screen | captured only on explicit request; a screenshot sent to an online vision model asks first; clicks only by control name via UI Automation, always confirmed, never by coordinates; screen text is treated as untrusted content |
| Code changes | `apply_patch` always asks, refuses if the file changed since the fix was proposed, backs up to `data/backups/`, restores automatically on a syntax error; `restore_backup` undoes; tests run with `shell=False` and a 5-minute timeout |
| Usage learning | stores only tool/app names and counts (never content); can be turned off and cleared on the Memory page |
| Logs | utterances, e-mail bodies and tool content arguments are logged as lengths unless you enable *Debug: include what I say in log files*; secrets are always redacted |

## 1.2 additions

| Feature | Safeguard |
|---|---|
| Coding agent | new projects only inside the coding workspace (`~/GenesisProjects`, inside your allowed roots); generated file paths validated (no absolute paths, `..`, or hidden control files); creating and running code asks first; programs run with `shell=False`, a timeout, output capped, and **API keys/tokens stripped from the environment** |
| Risky generated code | static scan (recursive delete, file deletion, subprocess/shell, registry, credentials, keyboard hooks, disk/power commands, raw Windows APIs, listening on all interfaces) → running becomes HIGH risk and always asks, listing the reasons |
| Automatic fixes | only for projects Genesis created (`.genesis-project` marker); originals backed up to `data/backups/`; a fix that introduces a new risky operation is rejected; max 3 attempts, so no infinite loops; your own code is never auto-edited (use “fix it” → `apply_patch`, which always asks) |
| Software installation | never automatic: missing packages are reported with the `pip`/`npm` command |
| Calendar | create/move are MEDIUM (ask, showing the exact parsed time); cancel always asks; ambiguous matches ask which event |
| YouTube / web | result titles and pages are untrusted data; video ids are validated; only `https://www.youtube.com/watch?v=<id>` URLs are opened |
| Windows | window actions use Win32 APIs by window handle and never click coordinates; screenshots are saved locally only when you ask |
| LLM planner | cannot choose `run_program`, `write_code_files`, `delete_calendar_event` (nor, as before, send/delete/power tools) |
| Untrusted-content escalation | unchanged for LLM plans. Rule plans may mark a LOW-risk step `trusted_input` when its input can't come from external content (an id our own search returned, a summary with no side effects) |

## 1.3 additions

| Feature | Safeguard |
|---|---|
| Developer commands | allow-list per executable/subcommand; `shlex` parsing, `shell=False`; pipes, redirects, chaining, `$`, backticks and shell/delete/download commands are refused; secrets are stripped from the environment; installs and `git push` are HIGH risk (always asked); the LLM planner can't use this tool |
| Project code changes | the model proposes exact edits; each must match once; Python/JSON are validated; newly introduced risky code makes applying HIGH risk; always asks with a file list and +/- counts; backups for undo; refuses if a file changed after planning |
| Browser/window clicks | by accessible name and role (or an element id Genesis just read), never coordinates; clicking and pressing Enter ask first (MEDIUM); page content is untrusted |
| Goal planning | LLM plans are validated against the registry; destructive, sending, command and code tools are excluded; one re-plan at most; permission blocks are never “worked around” |
| Identity | stored locally in the preferences table; you can edit or forget it (“forget my name”); rejected spellings are remembered so Genesis doesn't keep asking |
| Undo | every undo goes through the normal permission checks; irreversible actions are stated, not faked |
| Memory | only explicit requests, preferences you state, tasks/notes you create, and counts of app/tool names are kept; context is temporary and never written to disk |

## 1.4 additions

- **Computer control** is a typed tool layer, not raw OS access. Refused outright: typing/pressing in terminals, the Run
  box, Start search, Task Manager, IDE terminals and File Explorer's address/search bar; Win+R, Win+X, Win alone,
  Ctrl+Esc, Ctrl+Shift+Esc, Ctrl+Alt+Del; password fields. Confirmed: clicks on send/submit/pay/buy/delete/install/allow…
  controls, clicks on executables/scripts, double-clicks, coordinate clicks, drags, Enter outside plain editors.
  A confirmed click is re-checked against the window the user approved.
- **Messages**: WhatsApp sends and form submits are HIGH risk and always confirmed; a plain “no/cancel” cancels (only
  explicit “not yet” wording postpones, and a postponed send is dropped when another chat is opened). The recipient is
  re-read from the open conversation before sending; success is only reported when the message shows as sent.
- **Screen/page text** read by `computer_task`, WhatsApp, Colab errors and form labels is wrapped as untrusted content for
  the LLM and can never pre-confirm an action.
- **Colab** code is scanned before running; AI-written code that deletes files, runs shells, execs or reaches Drive/the
  network asks first.
- **Images** download only over https (redirects checked before following), size-capped, stored in a small pruned cache.
- **Forms** never fill passwords, ID/bank/card numbers or guessed values; nothing is submitted without confirmation.
- No screenshots or audio are stored; the accessibility tree is read only when a computer tool runs.

## 1.5 additions

- **GitHub:** the token is read from `.env` at call time and never logged or shown. Repository deletion is HIGH risk:
  the user must type or say the repository's name (checked before the yes/no question), then confirm. The deletion is
  verified afterwards. The LLM planner is not allowed to use the delete tool.
- **Office:** new files never overwrite existing ones. Writing into an open document only appends and never saves.
  AI-written spreadsheet formulas are allow-listed (arithmetic plus SUM/AVERAGE/IF…); anything else, including DDE
  payloads such as `=cmd|…`, is stored as text.

## Testing

`tests/test_security.py` covers the permission matrix, confirmation parsing, validators, redaction, injection wrapping, the absence of a shell tool, argv-only launching, the encrypted vault, and end-to-end safety: shutdown needs a yes, a no cancels, ambiguous answers do nothing, a new command cancels a pending action, delete confirms even when trusted, bulk delete and executables are HIGH risk, paths outside your home folder are refused, and logs never contain tokens.
