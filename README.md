# AI Agent Tracker

A Windows terminal dashboard for open Claude Code, Codex, and Antigravity chats, with animated mascots and account usage cards. Claude appears in orange, Codex in blue, and Antigravity in purple. The terminal tab is titled **AI Agent Tracker** while the dashboard is open.

## Download and run

1. Open this repository's **Releases** page and download `agent-tracker-windows-x64.zip`.
2. Extract the entire ZIP to a permanent folder, such as `%LOCALAPPDATA%\Programs\AgentTracker`.
3. Double-click `agent-tracker.exe`, or run `.\agent-tracker.exe` from PowerShell in that folder.
4. Keep the bundled `_internal` folder beside the executable.

The download includes Python. The current release targets Windows 10/11 x64. It has been tested on the development computer with an empty user profile and an empty PATH. A separate clean Windows installation, 32-bit Windows, and ARM64 Windows have not been verified.

To run `agent-tracker` from any directory, open **Edit environment variables for your account**, edit your user **Path**, and add the extracted folder containing `agent-tracker.exe`. Open a new terminal afterward.

## Connect your agents

Install and sign in to the agents you want to track using your own accounts. Open an agent chat, then launch the tracker.

- **Claude Code** sessions come from local session registrations and transcripts. Usage comes from Claude's status-line payload.
- **Codex** sessions come from the local state database and held thread-writer locks. Account usage requires the Codex CLI on PATH and a working login. The tracker refreshes account limits every 60 seconds.
- **Antigravity CLI (`agy`)** sessions come from local presence locks and conversation summaries. Gemini usage comes from Antigravity's status-line payload.

On interactive startup, the tracker adds usage collectors to detected Claude and Antigravity installations when their settings have no `statusLine` entry. Reopen the affected agents and send a prompt to begin collecting. Existing custom status lines are preserved. You can also run `agent-tracker --setup-usage` to configure available collectors explicitly.

Usage cards appear after a provider reports data. If Gemini is the only provider with data, it appears on the main page. When Claude or Codex also has data, Gemini appears on the page to the right.

Keep the tracker in its permanent location after configuring collectors because their commands refer to that location. If you move it, update the collector command in the affected agent's settings.

### Existing custom status lines

Integrate a collector into your existing status-line script by passing the same JSON payload to the appropriate command on standard input. Quote the executable path when it contains spaces.

```powershell
& 'C:\path\to\agent-tracker.exe' --capture-claude-usage
& 'C:\path\to\agent-tracker.exe' --capture-antigravity-usage
```

These commands expect JSON on stdin and emit a short status message. Your script can retain its own display output. Claude settings are in `%USERPROFILE%\.claude\settings.json`, or the directory selected by `CLAUDE_CONFIG_DIR`. Antigravity settings are in `%USERPROFILE%\.gemini\antigravity-cli\settings.json`.

## Switch to an agent terminal

Click **[Go to terminal]** beside a chat to activate its terminal window or select its Windows Terminal tab. The tracker resolves Claude's live PID or the process holding a Codex/Antigravity session lock. Navigation runs in a helper process so the dashboard keeps responding.

Windows Terminal must allow application title changes. The helper temporarily assigns the target console a unique title to find its tab through Windows UI Automation, then restores the previous title. This works with ordinary terminal tabs and classic console windows. Manually renamed tabs, suppressed application titles, split panes, embedded IDE terminals, desktop app chats, and elevated agents may prevent exact selection. A message appears in the dashboard when selection fails. Run the tracker and agents at the same elevation level.

## Controls

| Control | Action |
| --- | --- |
| Click [Go to terminal] | Activate the chat?s terminal tab or window |
| Right Arrow | Show Gemini usage when a second usage page is available |
| Left Arrow | Return to Claude / Codex usage |
| Up / Down | Scroll one line |
| Mouse wheel | Scroll three lines per notch |
| Page Up / Page Down | Scroll a screen |
| Home / End | Jump to the beginning / end |
| Ctrl+C or Q | Exit and restore terminal settings and title |

Changing usage pages returns the viewport to the top. The chat list remains part of the same scrollable dashboard.

## Run from source

Use Python 3.11 or newer on Windows. Runtime code uses the Python standard library.

```powershell
git clone https://github.com/DannyOppenheimer/agent-tracker.git
cd agent-tracker
python tracker.py
```

Useful commands work with either the executable or `python tracker.py`.

```powershell
agent-tracker --demo
agent-tracker --json
agent-tracker --usage-json
agent-tracker --setup-usage
```

`--demo` previews sample sessions. `--json` prints open sessions and waits for pending Claude task summaries. `--usage-json` prints usage without generating summaries.

## Build the Windows ZIP

Build on Windows with Python matching the desired architecture. Only the x64 artifact is currently verified.

```powershell
python -m pip install pyinstaller==6.22.3
powershell -NoProfile -ExecutionPolicy Bypass -File .\build-windows.ps1 -Python python
```

The archive is written to `release/agent-tracker-windows-<architecture>.zip`. The build script bundles the executable, runtime libraries, and quick-start instructions. `-ExecutionPolicy Bypass` applies to that build process only.

Run the tests with:

```powershell
python -m unittest test_tracker test_usage
```

## Data and limitations

Session detection depends on local agent file formats and Windows locking behavior, which may change between agent versions. Activity is approximate. Antigravity currently reports working or waiting based on its conversation summary. Closed historical chats are filtered using held presence locks.

Claude task titles are generated through background `codex exec` calls using your existing Codex login. These calls send selected Claude user-request text to Codex and consume your Codex allowance. Cached titles are reused, and failed calls retry after five minutes. If Codex is unavailable, the tracker displays the original request or a previously cached title.

Status-line collectors store selected quota, model, context, plan, and cost display fields in each agent's local `tracker-usage` folder. Usage freshness and reset times are displayed. Missing quota readings are labelled unavailable, and previous readings may remain visible after refresh failures.

Source runs cache task titles and request hashes in `task-summaries.json` beside `tracker.py`. Packaged runs use `%LOCALAPPDATA%\Agent Tracker\task-summaries.json`. These caches and build outputs are excluded from Git.

## Source files

- `tracker.py` contains session detection, terminal input, mascots, and the dashboard.
- `terminal_focus.py` contains process lookup and terminal activation.
- `usage.py` contains status-line collection, account usage retrieval, and usage cards.
- `test_tracker.py` and `test_usage.py` contain the tests.
- `build-windows.ps1` creates the portable Windows archive.
