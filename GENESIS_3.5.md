# Genesis 3.5 - Autonomous Coding Engine (GACE) and real microphone / speaker mute

> You asked for "Genesis 2.0". The code base is already at 3.x, so this is 3.5 and it builds on 3.0-3.4 instead of replacing them.

## 1. Architecture assessment (what existed, what was added)
| Need | Already in Genesis | Added in 3.5 |
|---|---|---|
| Write a single script and run it | `generate_code`, `write_code_files`, `run_program` | reused, not duplicated |
| Fix failing tests of an existing project | `debug_project` (3.2) | reused; the new loop uses the same idea with checkpoints |
| Model routing, local first, fallback | `v3/router.py`, `v3/provider.py` | reused (GACE calls the normal router) |
| Project reports, Git info | `v3/projects.py` | reused |
| Multi-file project from one sentence | - | **GACE** (`v3/gace/`) |
| Confined workspace, checkpoints, rollback, diffs | partial (backups for single fixes) | `gace/workspace.py` |
| Error classification and root-cause hints | partial (`parse_error`) | `gace/errors.py` |
| Validated terminal commands | `run_project_command` allow-list | `gace/runner.py` (own stricter list, secrets removed from the environment) |
| Static code review | `safety_scan` | `gace/review.py` |
| Project memory that is checked against real files | - | `gace/memory.py` |
| Real microphone / speaker mute | none (only settings toggles) | `v3/privacy.py` |
| Coding page, progress panel, mute buttons | - | `v3/coding_page.py`, hooks |

## 2. How to use it
* Left menu -> **Coding mode**, or just say: "build me a complete expense tracker app with a FastAPI backend, SQLite and tests".
  Genesis asks once ("downloads packages from the internet, writes only in your coding workspace") and then works.
* "continue the expense tracker project" - writes only the missing files and tests again; files you changed are left alone.
* "roll back the expense tracker project" - restores the latest checkpoint (asks first).
* "status of the expense tracker project" - memory vs the real files.
* Projects go to `~\GenesisProjects\<name>` (setting `coding.workspace`). Each has `GENESIS_BUILD_REPORT.md`.

## 3. The loop
UNDERSTAND -> PLAN (validated JSON plan; asks only if a blocking question remains) -> checkpoint -> BUILD (one file at a time,
with only the interfaces of the other files) -> SETUP (venv + `pip install -r requirements.txt`, `npm install`: after your
confirmation) -> TEST -> DIAGNOSE -> REPAIR -> TEST -> REPORT.
* **Roles** (Architect, Coder, Terminal, Debugger, QA, Reviewer) are functions run by one orchestrator, each with one job; they
  do not talk to each other freely and there is no extra model per role. Models come from the normal Genesis router.
* **Bounded:** at most 8 repairs (setting `max_repairs`), 2 tries per distinct error, 25 minutes, and "Stop" works.
* A fix is **kept only if the failure count goes down**; otherwise it is undone and counted as an unnecessary retry.
  Fixes that delete tests, delete most of a file or add `eval`/`shell=True`-style code are rejected.
* **"Verified" means:** the project's own test command exited 0 *and* at least one test ran. Anything else is reported as
  PARTIAL or NOT VERIFIED, with the reason. The requirement list in the report comes from the plan; it is not claimed to be proven.

## 4. Safety
* Files only inside the approved workspace (no `..`, no absolute paths, no escaping symlinks, never `.env` or key files).
* Commands: no shell; only python/pytest/unittest, pip install (plain package names only), npm install/test/build, node, javac/java,
  mvn/gradle/dotnet/go/cargo build+test, g++. No downloads of scripts, no `curl`, no `git push`, no `rm`.
* API keys and tokens are removed from the environment of every command. A package the plan did not name is **not** installed
  without your explicit yes.
* Checkpoints (zip snapshots in `.genesis/checkpoints`) before the build and before every repair; rollback removes only files
  Genesis created, never yours.

## 5. Microphone and speaker mute
* **Mic mute really stops capture:** the audio stream is closed, wake-word detection and push-to-talk are off, and
  `Microphone.read/open` refuse to work, so nothing is recorded or sent to speech recognition. The state is saved and stays
  muted after a restart until you unmute. The button shows MUTED only while the stream is really closed (it re-reads the state
  every 0.4 s).
* **Speaker mute** stops Genesis's voice (current and new speech) and does not touch the microphone.
* **Ctrl+Shift+M** toggles the microphone from anywhere (Windows). Change it with `GENESIS_MUTE_HOTKEY=ctrl+alt+m`.
* Buttons: in the main window's left bar, on the Coding page, and in the pop-out's bottom row next to Talk and Send.
* Commands: "mute my microphone", "unmute my mic", "mute the speaker", "unmute the speaker". Existing commands such as
  "turn off the microphone" and "mute your voice" keep their old meaning (voice settings).
* You cannot unmute by voice: a muted microphone cannot hear you. Use the button, the hotkey, or type the command.

## 6. Tested vs not tested (honest)
Tested automatically (tests/test_v3_gace.py, 50 tests; real files, real unit-test runs, a scripted stand-in for the AI model):
workspace limits, checkpoints and rollback, error classification, command allow-list, code review, plan validation,
build -> fail -> diagnose -> repair -> verify, bounded loop and undo, resume with memory vs files, tools and commands,
and mute with the real VoiceManager and a fake microphone.

**Not tested here:** quality of the code your local model writes (the stand-in model is scripted); real `pip install`/`npm install`;
React/Node builds; the Coding page and the pop-out buttons inside a real Qt window (PySide6 is not available where this
was built); the Ctrl+Shift+M registration on Windows (the key spec is parsed in a test, the OS call is the existing hotkey class);
a real microphone. Small local models will often need several repairs, or fail on large projects: the report says so.

## 7. Not built (yet)
Git integration (checkpoints are zip snapshots, not commits), a VS Code extension, cost-aware cloud routing beyond the existing
router, semantic search over project memory, Lucide icons (emoji + tooltips are used), a Task Manager button, aggregated metrics
across many builds (each build records its own metrics), sandboxed/containerised execution (commands run in a project-only
virtual environment, not a container).
