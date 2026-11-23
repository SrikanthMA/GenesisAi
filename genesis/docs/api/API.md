# Genesis internal API

Genesis runs in a single process, so there is no network API to secure. Integrations such as the UI, the voice pipeline, the CLI and the tests use the Python API below.

## Bootstrapping

```python
from genesis.core.config import GenesisConfig
from genesis.core.container import build_container
from genesis.core.assistant import GenesisCore

config = GenesisConfig.from_env()          # reads .env
services = build_container(config)         # DB, settings, secrets, LLM, Google, tools' services…
core = GenesisCore(services)
core.start()                               # reminder scheduler, default project registration

resp = core.handle("open my downloads folder")
print(resp.status, resp.text)
```

`build_container(config, backend=…, secrets=…, llm=…, db=…, events=…, http_session=…, embedder_factory=…, apps=…)` accepts fakes for every external dependency (see `tests/helpers.py`).

## `GenesisCore`

| Method | Description |
|---|---|
| `handle(text, source="text") -> AssistantResponse` | full pipeline for one user turn (thread-safe, serialised) |
| `resolve_confirmation(confirmation_id, approved) -> AssistantResponse` | answer a pending confirmation (the UI buttons use this) |
| `new_conversation() / switch_conversation(id) / clear_conversation()` | conversation management |
| `registry` | the `ToolRegistry` |
| `nlu.parse(text)` | run NLU only |
| `stop()` | stop background services |

### `AssistantResponse`

| Field | Meaning |
|---|---|
| `text` | reply to display |
| `speech` | TTS-friendly version |
| `status` | `done`, `partial`, `failed`, `awaiting_confirmation`, `awaiting_input`, `chat`, `error` |
| `plans` | list of executed plans (`Plan.to_dict()`: steps, statuses, risks, results, verification) |
| `confirmation` | `{id, prompt, tool, risk, args}` when waiting for a yes/no |
| `question` | clarifying question when waiting for input |
| `ui_action` | `open_chat`, `hide`, `open_settings`, `new_conversation`, `clear_conversation` |
| `nlu` | NLU results for each sub-request |

## Events (`genesis.core.events.EventType`)

Subscribe with `services.events.subscribe(handler, EventType.X)`. Handlers run on the emitting thread.

| Event | Payload |
|---|---|
| `STATE_CHANGED` | `state` (`IDLE, LISTENING, THINKING, EXECUTING, SPEAKING, WAITING_FOR_CONFIRMATION, WAITING_FOR_INPUT, ERROR`), `detail`, `success` |
| `MESSAGE` | `role`, `text`, `conversation_id`, `metadata` |
| `PLAN_CREATED` | `plan` |
| `STEP_UPDATED` | `plan_id`, `step` |
| `CONFIRMATION_REQUIRED` / `CONFIRMATION_RESOLVED` | confirmation dict / `id, approved` |
| `NOTIFICATION` | `title`, `message` |
| `REMINDER_DUE` | `id`, `text` |
| `LLM_STATUS` | provider status (`online`, `last_error`, `model`, `remote`) |
| `VOICE_STATUS` | `mic_open`, `listening`, engines, errors |
| `TRANSCRIPT` | `text`, `final`, `note` |
| `UI_ACTION` | `action` |
| `SETTINGS_CHANGED` / `SERVICE_CHANGED` | `key, value` / `service, connected, account` |

## Tool API

```python
from genesis.tools.base import Tool, ToolResult, obj, s
from genesis.security.permissions import RiskLevel

class TranslateClipboard(Tool):
    name = "translate_clipboard"
    category = "productivity"
    description = "Translate the clipboard text into a language."
    parameters = obj({"language": s("Target language", maxLength=40)}, ["language"])
    risk = RiskLevel.LOW
    remote = True

    def run(self, args, ctx):          # ctx = ServiceContainer
        ...
        return ToolResult.ok("Translated.", text=translated)

    def verify(self, args, result, ctx):
        return bool(result.data.get("text"))
```

`ToolResult` fields: `success, message, data, error, verified, untrusted_content, recovery ({"tool","args","reason"}), hint`.

## LLM provider API

```python
class LLMProvider:
    name: str; is_remote: bool; is_generative: bool
    def chat(messages, *, temperature=None, max_tokens=700, json_mode=False) -> str
    def chat_json(messages, ...) -> dict | list
    def is_configured() -> bool
```

Errors: `LLMUnavailable` (network, quota, not configured), which triggers offline mode; `LLMAuthError`; and `LLMError`.

## Database schema (SQLite, migration v1)

`settings, preferences, conversations, messages, memory, contacts, tasks, tool_executions, permissions, connected_services, reminders, projects, knowledge_documents, knowledge_chunks, security_events, schema_version`. See `genesis/database/db.py`.

## CLI

```
python -m genesis [--cli [--voice]] [--minimized] [--doctor] [--debug]
```
