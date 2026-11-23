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

## Testing

`tests/test_security.py` covers the permission matrix, confirmation parsing, validators, redaction, injection wrapping, the absence of a shell tool, argv-only launching, the encrypted vault, and end-to-end safety: shutdown needs a yes, a no cancels, ambiguous answers do nothing, a new command cancels a pending action, delete confirms even when trusted, bulk delete and executables are HIGH risk, paths outside your home folder are refused, and logs never contain tokens.
