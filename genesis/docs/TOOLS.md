# Genesis tool reference

_Generated from the code by `python scripts/gen_tool_docs.py` — do not edit by hand._

Every action Genesis performs is one of these tools. The planner can only choose from this list, every call is
validated against the JSON schema, and the permission manager applies the risk level before anything runs.

| Risk | Meaning |
|---|---|
| **low** | runs directly (you can set it to *always ask*) |
| **medium** | asks for confirmation unless you allow the tool on the Tools page |
| **high** | **always** asks; can never be set to run silently |

Tools marked *always confirm* ask every time regardless of trust settings. Tools marked *remote* send data off the PC.

## Apps

### `close_application` — medium

Close a running application gracefully (unsaved work may prompt).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `app` | string | yes | Application name |

### `open_application` — low

Launch an installed desktop application or a web app (e.g. chrome, vscode, spotify, gmail, youtube).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `app` | string | yes | Application name, e.g. 'chrome', 'vs code', 'gmail' |

## Browser

### `browser_click` — medium, remote

Click a button or link (by its visible text) in Genesis's visible browser window.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `target` | string | yes | Visible text of the button/link |

### `browser_fill` — medium, remote

Type a value into a form field (by label/placeholder) in Genesis's visible browser window.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `target` | string | yes | Field label or placeholder |
| `value` | string | yes | Text to enter |

### `browser_open` — low, remote

Open a URL in Genesis's visible automated browser window (for multi-step web tasks).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `url` | string | yes | http(s) URL |

### `browser_read_page` — low, remote

Read the text of a web page (the given URL, or the page open in Genesis's browser). Untrusted content.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `url` | string |  | http(s) URL (optional) |

## Code

### `check_code_file` — low

Statically check a Python or JSON file for errors (syntax, undefined names with pyflakes). Never runs the code.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `path` | string | yes | File path |
| `base_dir` | string |  | Project folder |

### `explain_code` — low, remote

Explain an error message or a code file and suggest a fix (uses the language model).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `error_text` | string |  | Error / traceback text |
| `path` | string |  | File path |
| `base_dir` | string |  | Project folder |
| `issues` | string |  | Known issues from check_code_file |

### `vscode_open_file` — low

Open a file in VS Code, optionally at a line number.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `path` | string | yes | File name or path |
| `line` | integer |  |  |
| `base_dir` | string |  | Project folder to resolve relative paths |

### `vscode_open_project` — low

Open a project folder in VS Code (by saved project name, by searching common folders, or by path).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `project` | string |  | Project name, e.g. 'genesis' |
| `path` | string |  | Folder path |

## Email

### `compose_email` — low

Write an e-mail subject and body from what the user wants to say (no side effects).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `recipient_name` | string |  | How to address the recipient |
| `message` | string | yes | What the user wants to say |
| `subject` | string |  | Subject if the user gave one |

### `gmail_compose_web` — low

Open Gmail's compose window in the browser pre-filled with recipient/subject/body (no API needed).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `to` | array |  |  |
| `subject` | string | yes | Subject |
| `body` | string | yes | Body |

### `gmail_create_draft` — low, remote

Create a draft in the user's Gmail (does NOT send). Opens it for review by default.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `to` | array |  |  |
| `cc` | array |  |  |
| `subject` | string | yes | Subject |
| `body` | string | yes | Plain-text body |
| `attachments` | array |  |  |
| `open_after` | boolean |  | (default True) |

### `gmail_search` — low, remote

Search/read the user's Gmail inbox (Gmail search syntax, e.g. 'from:amazon is:unread').

| Argument | Type | Required | Notes |
|---|---|---|---|
| `query` | string |  | Gmail search query |
| `max_results` | integer |  | (default 5) |

### `gmail_send_draft` — medium, always confirm, remote

Send an existing Gmail draft. Always requires explicit confirmation.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `draft_id` | string | yes | Draft id |
| `to` | array |  |  |
| `subject` | string |  | Subject (for the confirmation) |

## Files

### `copy_path` — medium

Copy a file or folder into another folder.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `path` | string | yes | File or folder |
| `destination` | string | yes | Destination folder |
| `base_dir` | string |  | Folder to resolve relative names in |

### `create_file` — medium

Create a new file (never overwrites). Location defaults to the active project or Documents.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | File name, e.g. test.py |
| `location` | string |  | Folder (desktop, documents, a path) |
| `content` | string |  | Initial content |
| `base_dir` | string |  | Active project folder |

### `create_folder` — low

Create a new folder (location defaults to Documents).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | Folder name |
| `location` | string |  | Parent folder |
| `base_dir` | string |  | Active project folder |

### `delete_path` — medium, always confirm

Move a file or folder to the Recycle Bin (recoverable). Always asks for confirmation.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `path` | string | yes | File or folder |
| `base_dir` | string |  | Folder to resolve relative names in |

### `move_path` — medium

Move a file or folder into another folder.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `path` | string | yes | File or folder |
| `destination` | string | yes | Destination folder |
| `base_dir` | string |  | Folder to resolve relative names in |

### `open_file` — low

Open a file with its default application.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `path` | string | yes | File name or path |
| `base_dir` | string |  | Folder to resolve relative names in |

### `open_folder` — low

Open a folder in File Explorer (known names like downloads/documents/desktop, or a path).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `folder` | string | yes | Folder name or path |

### `read_file` — low

Read a text/PDF/DOCX file the user asked about (content is treated as untrusted data).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `path` | string | yes | File name or path |
| `base_dir` | string |  | Folder to resolve relative names in |

### `rename_path` — medium

Rename a file or folder (keeps it in the same folder; keeps the extension if omitted).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `path` | string | yes | Existing file |
| `new_name` | string | yes | New name |
| `base_dir` | string |  | Folder to resolve relative names in |

### `search_files` — low

Search the user's folders for files by name keywords and/or extension; can sort newest first.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `query` | string |  | Keywords in the file name |
| `extension` | string |  | Extension like .pdf, or 'image'/'video' |
| `folder` | string |  | Limit to a folder (downloads, documents, a path) |
| `sort` | string |  | one of: relevance, newest (default relevance) |
| `limit` | integer |  | (default 10) |

## Knowledge

### `knowledge_ingest` — low

Add a document or folder (txt, md, pdf, docx, code) to the local knowledge base.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `path` | string | yes | File or folder |

### `knowledge_query` — low

Answer a question from the user's own indexed documents (retrieval-augmented generation).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `question` | string | yes | Question |
| `k` | integer |  | (default 4) |

## Memory

### `add_contact` — low

Save a contact (name, optional alias like 'professor', optional e-mail).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | Name |
| `alias` | string |  | Relation/alias |
| `email` | string |  | E-mail |

### `forget` — low

Delete remembered facts that match the text.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `text` | string | yes | Text to match |

### `list_memory` — low

Show what Genesis remembers.

_No arguments._

### `remember` — low

Store a fact the user explicitly asked Genesis to remember.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `text` | string | yes | The fact |

## Productivity

### `create_calendar_event` — low, remote

Create an event in the user's Google Calendar.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `title` | string | yes | Event title |
| `start` | string | yes | ISO 8601 local start |
| `duration_minutes` | integer |  | (default 30) |
| `description` | string |  | Details |

### `create_reminder` — low

Create a local reminder that pops up (and is spoken) at the given local date/time.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `text` | string | yes | What to remind about |
| `due_at` | string | yes | ISO 8601 local datetime |

### `list_reminders` — low

List pending reminders.

_No arguments._

## Settings

### `set_setting` — low

Change a Genesis setting: listening, tts (voice replies), memory, start_with_windows.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `setting` | string | yes | one of: listening, tts, memory, start_with_windows |
| `value` | boolean | yes |  |

## System

### `cancel_shutdown` — low

Abort a pending shutdown/restart.

_No arguments._

### `lock_computer` — low

Lock the workstation (you'll need your password/PIN to get back in).

_No arguments._

### `notify` — low

Show a desktop notification.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `title` | string |  | Title |
| `message` | string | yes | Message |

### `restart_computer` — high

Restart the computer (HIGH risk — always asks for explicit confirmation).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `delay_seconds` | integer |  | (default 10) |

### `set_volume` — low

Change the system volume: up, down or mute toggle.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `action` | string | yes | one of: volume_up, volume_down, mute |
| `steps` | integer |  | (default 5) |

### `shutdown_computer` — high

Shut down the computer (HIGH risk — always asks for explicit confirmation).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `delay_seconds` | integer |  | (default 10) |

### `sleep_computer` — medium

Put the computer to sleep.

_No arguments._

### `system_info` — low

Report battery, CPU, memory and disk usage.

_No arguments._

## Web

### `open_url` — low

Open an http(s) URL in the user's default browser.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `url` | string | yes | http(s) URL |

### `web_search` — low, remote

Search the web: opens the results in the user's browser and (if available) returns top results as text.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `query` | string | yes | Search query |
| `open_browser` | boolean |  | (default True) |
