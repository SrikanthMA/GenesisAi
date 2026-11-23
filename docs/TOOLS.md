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

### `browser_back` — low

Go back one page in Genesis's automated browser window.

_No arguments._

### `browser_click` — medium, remote

Click a button or link in Genesis's visible browser window — by its visible text, or by element_id from browser_list_elements/browser_find.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `target` | string |  | Visible text of the button/link |
| `element_id` | integer |  |  |
| `nth` | integer |  | Click the Nth element of `kind` |
| `kind` | string |  | one of: button, link, field, any (default any) |

### `browser_fill` — medium, remote

Type a value into a form field (by label/placeholder) in Genesis's visible browser window.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `target` | string | yes | Field label or placeholder |
| `value` | string | yes | Text to enter |

### `browser_find` — low, remote

Find the element on the current page that best matches a description, e.g. 'the internship application button'.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `description` | string | yes | What to look for |

### `browser_list_elements` — low, remote

See the current page in Genesis's browser: title, headings, buttons, links and fields (by name).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `kind` | string |  | one of: all, buttons, links, fields, headings (default all) |

### `browser_open` — low, remote

Open a URL in Genesis's visible automated browser window (for multi-step web tasks).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `url` | string | yes | http(s) URL |

### `browser_press` — medium

Press a key in Genesis's browser (Enter submits the focused form).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `key` | string | yes | one of: Enter, Tab, Escape, PageDown, PageUp |

### `browser_read_page` — low, remote

Read the text of a web page (the given URL, or the page open in Genesis's browser). Untrusted content.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `url` | string |  | http(s) URL (optional) |

### `browser_scroll` — low

Scroll the page in Genesis's browser (down, up, top, bottom).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `direction` | string |  | one of: down, up, top, bottom (default down) |
| `amount` | integer |  | (default 1) |

## Calendar

### `create_calendar_event` — medium, remote

Create an event in the user's Google Calendar (asks first, showing the exact time).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `title` | string | yes | Event title |
| `start` | string | yes | ISO 8601 local start |
| `duration_minutes` | integer |  | (default 30) |
| `description` | string |  | Details |
| `attendees` | array |  |  |

### `delete_calendar_event` — medium, always confirm, remote

Cancel (delete) a calendar event. Always asks first.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `event_id` | string | yes | Event id |
| `title` | string |  | Title (for the confirmation) |
| `start` | string |  | Start ISO (for the confirmation) |

### `find_calendar_event` — low, remote

Find ONE calendar event by day, time (HH:MM) and/or text — used before moving or cancelling it.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `day` | string |  | YYYY-MM-DD (default: today, then the next 14 days) |
| `time` | string |  | HH:MM |
| `query` | string |  | Text in the title, e.g. 'Rahul' |

### `list_calendar_events` — low, remote

List calendar events for a day (YYYY-MM-DD, default today) or a date range, optionally matching text.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `day` | string |  | YYYY-MM-DD |
| `start` | string |  | ISO start of range |
| `end` | string |  | ISO end of range |
| `query` | string |  | Text to match |
| `label` | string |  | How to describe the range, e.g. 'tomorrow' |

### `update_calendar_event` — medium, remote

Move and/or rename a calendar event. new_time (HH:MM) keeps the day; new_start (ISO) sets both.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `event_id` | string | yes | Event id |
| `title` | string |  | Current title (for the confirmation) |
| `current_start` | string |  | Current start ISO (for the confirmation) |
| `new_start` | string |  | ISO 8601 local start |
| `new_time` | string |  | HH:MM |
| `new_date` | string |  | YYYY-MM-DD |
| `new_title` | string |  | New title |
| `duration_minutes` | integer |  |  |

## Code

### `apply_patch` — medium, always confirm

Write a proposed change to a file (always asks first; keeps a backup so it can be undone).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `path` | string | yes | File |
| `new_content` | string | yes | New file content |
| `summary` | string |  | What changes |
| `base_sha` | string |  | Hash of the file the fix was made for |

### `check_code_file` — low

Statically check a Python or JSON file for errors (syntax, undefined names with pyflakes). Never runs the code.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `path` | string | yes | File path |
| `base_dir` | string |  | Project folder |

### `diagnose_project` — low

Inspect a project for problems without running it: Python syntax/undefined names, broken JSON, missing dependencies (node_modules / venv), missing files referenced by index.html, and recent errors in log files.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `path` | string |  | Project folder |
| `project` | string |  | Saved project name |
| `focus` | string |  | Area to focus on, e.g. 'frontend' |

### `explain_code` — low, remote

Explain an error message or a code file and suggest a fix (uses the language model).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `error_text` | string |  | Error / traceback text |
| `path` | string |  | File path |
| `base_dir` | string |  | Project folder |
| `issues` | string |  | Known issues from check_code_file |

### `propose_fix` — low, remote

Work out a fix for a problem in a code file (uses the language model). Does NOT change anything.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `path` | string | yes | File with the problem |
| `issue` | string | yes | Problem description |
| `line` | integer |  |  |

### `restore_backup` — medium

Undo a change Genesis made to a file by restoring its backup.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `path` | string | yes | File |
| `backup` | string | yes | Backup file |

### `run_tests` — medium

Run the project's own test suite (auto-detected: pytest / unittest / npm test). Asks first; 5-minute timeout.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `path` | string |  | Project folder |
| `project` | string |  | Saved project name |

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

## Coding

### `apply_code_change` — medium, always confirm

Apply a reviewed multi-file change from plan_code_change (always asks; backs up every file; undoable).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `project_path` | string | yes | Project folder |
| `summary` | string |  | What changes |
| `changes` | array |  |  |
| `created` | array |  |  |
| `files` | array |  |  |
| `plus` | integer |  |  |
| `minus` | integer |  |  |
| `risky` | array |  |  |

### `generate_code` — low, remote

Write a small program for a task with the language model (returns files; writes nothing). Languages: html, java, javascript, python, typescript.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `task` | string | yes | What the program should do |
| `language` | string |  | Programming language |
| `name` | string |  | Short project name |
| `context_file` | string |  | A file the program should use |
| `preferences` | string |  | User coding preferences |

### `plan_code_change` — low, remote

Work out how to implement a change/feature in an existing project (reads the relevant files; the language model proposes exact edits). Changes nothing — returns a diff for review.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `request` | string | yes | What to change, e.g. 'add dark mode' |
| `path` | string | yes | Project folder |

### `restore_code_change` — medium

Undo a change made by apply_code_change (restores the backups, removes files it created).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `backups` | array | yes |  |

### `run_program` — medium

Run a program file or project (Python, JavaScript, TypeScript, Java; HTML opens in the browser) with a timeout and capture its output. For projects Genesis created, automatically fixes errors and re-runs (max 3 attempts).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `path` | string | yes | Program file or project folder |
| `language` | string |  | Language |
| `stdin` | string |  | Input to send |
| `timeout` | integer |  |  |
| `auto_fix` | boolean |  | (default True) |
| `max_attempts` | integer |  | (default 3) |
| `task` | string |  | What the program is supposed to do (helps fixing) |

### `run_project_command` — medium

Run an allow-listed developer command in a project folder (tests, builds, git status/diff, npm scripts…). No shell; installs and git push are high risk.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `command` | string | yes | e.g. 'npm test', 'python -m pytest', 'git status' |
| `path` | string | yes | Project folder |
| `timeout` | integer |  |  |

### `write_code_files` — medium

Create a NEW project folder in the coding workspace (~/GenesisProjects) with the given files.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | Project folder name |
| `language` | string |  | Language |
| `entry` | string |  | Entry file |
| `summary` | string |  | What it does |
| `task` | string |  | Original request |
| `files` | array | yes |  |

## Colab

### `colab_explain_error` — low

Explain why the last Colab cell failed.

_No arguments._

### `colab_fix` — low, remote

Fix the code of the Colab cell that just failed (rewrites that cell); rerun=true also runs it again.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `instructions` | string |  | Extra instructions for the fix (optional) |
| `rerun` | boolean |  | (default False) |

### `colab_new_notebook` — low, remote

Create a new Python notebook in Google Colab.

_No arguments._

### `colab_open` — low, remote

Open Google Colab (in Genesis's automated browser when available, so cells can be written and run).

_No arguments._

### `colab_run` — low, remote

Run the current Colab cell and read its output (errors included). Risky-looking code needs confirmation.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `timeout` | integer |  | (default 180) |

### `colab_write_code` — low, remote

Put code into a Colab cell (a new cell by default). Give `code`, or describe the `task` and it is written for you. Does not run it.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `code` | string |  | Exact code |
| `task` | string |  | What the code should do |
| `new_cell` | boolean |  | (default True) |

## Computer

### `computer_click` — low

Click a control in the active window by name/description ('Save', 'the second button', 'Next'). Uses accessibility first, the control's position second. x/y coordinates only as a last resort. Set button='right' for a context menu or double=true to open items.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `target` | string |  | What to act on, by its visible name or description: 'Save', 'the search box', 'the second button' |
| `role` | string |  | button/link/field/tab/checkbox (optional) |
| `nth` | integer |  |  |
| `element_id` | integer |  |  |
| `button` | string |  | one of: left, right, middle (default left) |
| `double` | boolean |  | (default False) |
| `x` | integer |  |  |
| `y` | integer |  |  |

### `computer_copy` — low

Copy the selected text in the active window (returns it), or put the given `text` on the clipboard.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `text` | string |  | Text to place on the clipboard instead of copying the selection |

### `computer_drag` — medium

Drag one control onto another in the active window (e.g. a file onto a folder), by their names.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `source` | string | yes | What to act on, by its visible name or description: 'Save', 'the search box', 'the second button' |
| `destination` | string | yes | What to act on, by its visible name or description: 'Save', 'the search box', 'the second button' |

### `computer_observe` — low

Look at the active window: its title and the named buttons, links, fields and tabs in it (accessibility tree — no screenshot). Use before clicking/typing in an unfamiliar app.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `role` | string |  | one of: all, button, link, field, tab, checkbox, list item, menu item (default all) |

### `computer_paste` — low

Paste the clipboard (or the given `text`) at the cursor in the active window.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `text` | string |  | Text to paste (optional) |

### `computer_press` — low

Press a key or shortcut in the active window: 'enter', 'esc', 'tab', 'ctrl+s', 'alt+left' (back), 'ctrl+t'. Enter in chat/e-mail apps sends a message, so it is confirmed first.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `keys` | string | yes | Key or combination, e.g. 'ctrl+s' |
| `times` | integer |  | (default 1) |

### `computer_scroll` — low

Scroll the active window up, down, left or right.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `direction` | string |  | one of: up, down, left, right (default down) |
| `amount` | integer |  | (default 5) |

### `computer_select` — low

Select text: everything in the active control ('all'), or all text in a named field.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `field` | string |  | Field to select in (optional) |

### `computer_task` — low

Carry out a goal inside the active desktop application step by step (look → act → check), for apps and dialogs without a dedicated tool, e.g. 'turn on dark mode in this app's settings', 'rename this layer'. Sensitive actions are handed back for confirmation.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `goal` | string | yes | What to achieve, in plain words |
| `max_steps` | integer |  | (default 10) |

### `computer_type` — low

Type text into the active window — into a named field if `field` is given, otherwise where the cursor is. Never presses Enter by itself (use computer_press). replace=true clears the field first.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `text` | string | yes | Text to type |
| `field` | string |  | Field name/description (optional) |
| `replace` | boolean |  | (default False) |

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
| `thread_id` | string |  | Gmail thread id when this is a reply |
| `in_reply_to` | string |  | Message-ID header of the e-mail being replied to |

### `gmail_open_message` — low, remote

Open one e-mail (by Gmail message id) in the browser and read its contents (untrusted content).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `message_id` | string | yes | Gmail message id |
| `open_browser` | boolean |  | (default True) |

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

### `gmail_update_draft` — low, remote

Replace the contents of an existing Gmail draft (after a revision). Does not send.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `draft_id` | string | yes | Draft id |
| `to` | array |  |  |
| `subject` | string | yes | Subject |
| `body` | string | yes | Body |
| `thread_id` | string |  | Thread id |
| `in_reply_to` | string |  | In-Reply-To header |
| `open_after` | boolean |  | (default False) |

### `revise_text` — low

Revise an e-mail/draft text: replace words, add a sentence, change subject, or restyle (shorter, more formal…).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `body` | string | yes | Current body |
| `subject` | string |  | Current subject |
| `revision` | string | yes | one of: replace, add, style, subject |
| `find` | string |  | Text to replace |
| `replace` | string |  | Replacement |
| `addition` | string |  | Sentence to add |
| `style` | string |  | Desired style |
| `new_subject` | string |  | New subject |

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

## Forms

### `fill_form` — low, remote

Fill the form on the current page/window with the user's own information (their résumé by default). Never guesses, never fills sensitive fields, never submits (that's form_submit).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `source` | string |  | 'resume' (default), 'profile', or a path to the document to use |

### `form_set_field` — low

Change one answer in the form Genesis is filling ('change my phone number to …', 'set the city to Pune').

| Argument | Type | Required | Notes |
|---|---|---|---|
| `field` | string |  | Field label/description, or 'that' for the one just discussed |
| `value` | string | yes | New value |

### `form_submit` — high, always confirm, remote

Submit the form currently being filled (clicks its Submit/Apply button). Always asks first.

_No arguments._

## Github

### `github_create_repo` — medium, remote

Create a new GitHub repository for the user (private unless they say public).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | Repository name |
| `private` | boolean |  | (default True) |
| `description` | string |  | Short description |

### `github_delete_repo` — high, always confirm, remote

Permanently delete one of the user's GitHub repositories. Irreversible: requires the repository name typed back by the user (confirm_name) and an explicit yes.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `repo` | string | yes | Repository name |
| `confirm_name` | string | yes | The name as typed by the user to confirm |

### `github_list_repos` — low, remote

List the user's GitHub repositories (most recently updated first).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `limit` | integer |  | (default 10) |

### `github_open` — low

Open GitHub in the browser: the home page, the user's repositories, the 'new repository' page, or a specific repository by name.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `target` | string |  | 'home', 'repos', 'new', or a repository name (e.g. 'genesis' or 'owner/genesis') |

## Images

### `image_generate` — low, remote

Create a new image from a description with an image-generation model, and show it in the chat window.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `prompt` | string | yes | Description of the image to create |

### `image_search` — low, remote

Find a real photo/picture of something (animals, places, objects, landmarks) and show it to the user in the chat window.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `query` | string | yes | What the picture should show, e.g. 'peacock', 'Eiffel Tower' |
| `count` | integer |  | (default 1) |

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

## Media

### `media_control` — low

Control whatever is playing (YouTube, Spotify, …) with the system media keys: pause, resume, play_pause, next, previous, volume_up, volume_down, mute.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `action` | string | yes | one of: pause, resume, play_pause, next, previous, volume_up, volume_down, mute |
| `steps` | integer |  | (default 5) |

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

### `forget_item` — low

Delete one specific memory item (the one just saved or mentioned).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `kind` | string | yes | one of: fact, contact, project, routine, preference |
| `ref` | string | yes | Item id, project name, or 'key|fact_id' for a preference |

### `list_memory` — low

Show what Genesis remembers.

_No arguments._

### `recall` — low

Say what Genesis remembers about a topic (projects, people, facts, routines).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `topic` | string | yes | Topic, e.g. 'my genesis project', 'Rahul' |

### `remember` — low

Store a fact the user explicitly asked Genesis to remember.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `text` | string | yes | The fact |
| `kind` | string |  | one of: fact, instruction (default fact) |

### `remember_preference` — low

Remember a user preference Genesis should apply later (e.g. coding.language = python).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `key` | string | yes | Preference key, e.g. coding.language |
| `value` | string | yes | Value |
| `text` | string |  | How the user said it |

### `remember_project` — low

Remember that a named project lives in a folder (so 'open my <name> project' works).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | Project name |
| `path` | string | yes | Folder path |

### `search_history` — low

Search earlier conversations and completed tasks (links, videos, files) by topic and time.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `query` | string | yes | What to look for |
| `when` | string |  | one of: today, yesterday, this_week, last_week, any (default any) |

### `set_identity` — low

Store the user's name: set_name (full name), set_preferred (what to call them), add_nickname, learn_variant (another spelling the speech recogniser produces), forget.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `action` | string | yes | one of: set_name, fix_spelling, set_preferred, add_nickname, learn_variant, forget |
| `name` | string |  | Name |

## Office

### `office_create` — low

Create a NEW Word document, Excel spreadsheet or PowerPoint presentation and open it. Describe what it should contain in `request` (the content is written for you), or give the exact `text`.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `app` | string | yes | one of: word, excel, powerpoint |
| `request` | string |  | What the document should contain / be about |
| `text` | string |  | Exact text to put in (instead of generating) |
| `title` | string |  | Title / file name |
| `slides` | integer |  |  |

### `office_write` — low

Write into the Word document / Excel sheet / PowerPoint presentation that is OPEN right now: text is added at the end, rows below the data, or new slides. Give exact `text`, or a `request` to have it written. If none is open, a new file is created instead.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `app` | string | yes | one of: word, excel, powerpoint |
| `request` | string |  | What to write (e.g. 'a paragraph about India', 'a slide about results') |
| `text` | string |  | Exact text / values to insert |
| `slides` | integer |  |  |

## Productivity

### `add_note` — low

Save a note (local, private).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `content` | string | yes | Note text |
| `title` | string |  | Short title |

### `add_task` — low

Add an item to the user's to-do list (optionally with a due date).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `text` | string | yes | The task |
| `due` | string |  | ISO 8601 due date/time |

### `cancel_reminder` — low

Cancel a pending reminder (by id).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `reminder_id` | integer | yes |  |
| `text` | string |  | What it was about (for the reply) |

### `complete_task` — low

Mark a to-do item as done (by id or by words from it).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `ref` | string | yes | Task id or words from it |

### `create_reminder` — low

Create a local reminder that pops up (and is spoken) at the given local date/time.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `text` | string | yes | What to remind about |
| `due_at` | string | yes | ISO 8601 local datetime |

### `delete_note` — medium

Delete a note.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `ref` | string | yes | Note id or words from it |

### `delete_task` — medium

Remove an item from the to-do list.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `ref` | string | yes | Task id or words from it |

### `find_notes` — low

List notes, or find notes containing some words, and read the best match.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `query` | string |  | Words to look for (empty = all notes) |

### `list_reminders` — low

List pending reminders.

_No arguments._

### `list_tasks` — low

Show the to-do list.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `include_done` | boolean |  | (default False) |

## Reasoning

### `llm_analyze` — low, remote

Reason over content gathered by earlier steps (compare options, extract requirements, summarise). No side effects; content is treated as untrusted data.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `instruction` | string | yes | What to do with the content |
| `content` | string | yes | Material from earlier steps |
| `allow_own_knowledge` | boolean |  | If the material is empty, answer from general knowledge (and say so) (default False) |

## Routines

### `create_routine` — medium

Save a routine (a sequence of spoken commands), optionally scheduled weekly at a time.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | Routine name |
| `commands` | string | yes | Commands, e.g. 'open VS Code and then open my Genesis project' |
| `days` | array |  |  |
| `time_of_day` | string |  | HH:MM 24h |

### `delete_routine` — low

Delete a saved routine by name.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | Routine name |

### `list_routines` — low

List saved routines.

_No arguments._

## Screen

### `analyze_screen` — low

Capture the screen once (only when asked) and describe/explain what's visible, e.g. an error message.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `question` | string |  | What the user wants to know about the screen |

### `click_screen_element` — medium, always confirm

Click a named control (button/link) in the active window via UI Automation. Never uses raw coordinates.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `name` | string |  | Visible name of the control, e.g. 'Save' |
| `nth` | integer |  |  |
| `kind` | string |  | one of: button, link, any (default button) |

### `find_screen_element` — low

Find the control in the active window that best matches a description (UI Automation names; no clicking).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `description` | string | yes | What to look for |

### `list_screen_elements` — low

List named buttons/links/fields in the active window (Windows UI Automation).

_No arguments._

### `read_screen_text` — low

Read the text currently visible on screen (local OCR / UI Automation).

_No arguments._

## Settings

### `check_ai_connection` — low

Check (and reload) Genesis's AI model configuration: key present, provider reachable, key valid, model available.

_No arguments._

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

Change the system volume: up, down, mute toggle, or set it to a level (0-100 %).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `action` | string | yes | one of: volume_up, volume_down, mute, set |
| `steps` | integer |  | (default 5) |
| `level` | integer |  |  |

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

### `diagnose_connection` — low

Work out why a website (or the internet) isn't working: no internet, DNS problem, website down, timeout, or a browser problem.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `url` | string |  | Website to check (optional — without it, checks the internet connection) |

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
| `read_pages` | integer |  | Also read the top N result pages (in parallel) for better answers (default 0) |

## Whatsapp

### `whatsapp_draft` — low, remote

Open a WhatsApp chat and type a message into the box WITHOUT sending it (sending is whatsapp_send).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `chat` | string | yes | Contact or group name |
| `text` | string | yes | The message, as it should be sent |

### `whatsapp_open` — low, remote

Open WhatsApp (WhatsApp Web in Genesis's browser, or the desktop app) so chats can be read and messages drafted.

_No arguments._

### `whatsapp_read` — low, remote

Read the latest messages in a WhatsApp chat (by contact/group name), newest last.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `chat` | string |  | Contact or group name, e.g. 'Mom' |
| `count` | integer |  | (default 5) |
| `incoming_only` | boolean |  | (default False) |

### `whatsapp_send` — high, always confirm, remote

Send a WhatsApp message to a chat (re-checks the recipient and exact text first). Always asks for confirmation.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `chat` | string | yes | Contact or group name |
| `text` | string | yes | The exact message |

### `whatsapp_unread` — low, remote

List WhatsApp chats with unread messages.

_No arguments._

## Windows

### `list_windows` — low

List the open windows on the desktop.

_No arguments._

### `manage_window` — low

Switch to, minimize, maximize or restore a window, found by app name or title (e.g. 'chrome', 'notepad').

| Argument | Type | Required | Notes |
|---|---|---|---|
| `window` | string |  | App name or part of the window title |
| `action` | string | yes | one of: focus, minimize, maximize, restore |
| `window_id` | integer |  |  |

### `open_terminal` — low

Open a terminal window (Windows Terminal / PowerShell) in a folder, e.g. the current project.

| Argument | Type | Required | Notes |
|---|---|---|---|
| `path` | string |  | Folder to open the terminal in |

### `show_desktop` — low

Minimize all windows (show the desktop).

_No arguments._

### `take_screenshot` — low

Take a screenshot and save it as a PNG in Pictures/Screenshots (stays on this PC).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `open_after` | boolean |  | (default False) |

## Youtube

### `youtube_play` — low, remote

Play a YouTube video: by video_id/url, or search for `query` and play result number `index` (1 = first; set latest=true for the newest upload).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `video_id` | string |  | 11-character YouTube id |
| `url` | string |  | YouTube video URL |
| `query` | string |  | Search terms |
| `index` | integer |  | (default 1) |
| `latest` | boolean |  | (default False) |
| `title` | string |  | Title (for the reply) |

### `youtube_search` — low, remote

Search YouTube: opens the results in the browser and returns the list of videos (so 'play the 2nd one' works).

| Argument | Type | Required | Notes |
|---|---|---|---|
| `query` | string | yes | What to search for |
| `latest` | boolean |  | Sort by upload date (default False) |
| `open_browser` | boolean |  | (default True) |
