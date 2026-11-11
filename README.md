# AI Agent Tracker

A Windows terminal dashboard for open Claude Code, Codex, Antigravity, Muse Code, and Grok Build chats, with animated mascots and account usage cards. Claude appears in orange, Codex in blue, Antigravity in purple, Muse in pink, and Grok in light gray. The terminal tab is titled **AI Agent Tracker** while the dashboard is open.

Version **0.2.0** adds Muse and Grok tracking and account usage, shared usage cards, per-session metrics, prompt-based ordering, and grouped message exchanges. See [CHANGELOG.md](CHANGELOG.md) for release notes.

## Download and run

1. Open the [latest release](https://github.com/DannyOppenheimer/agent-tracker/releases/latest) and download `agent-tracker-windows-x64.zip`.
2. Extract the entire ZIP to a permanent folder, such as `%LOCALAPPDATA%\Programs\AgentTracker`.
3. Double-click `agent-tracker.exe`, or run `.\agent-tracker.exe` from PowerShell in that folder.
4. Keep the bundled `_internal` folder beside the executable.

The download includes Python. The current release targets Windows 10/11 x64. It has been tested on the development computer with an empty user profile and an empty PATH. A separate clean Windows installation, 32-bit Windows, and ARM64 Windows have not been verified.

To run `agent-tracker` from any directory, open **Edit environment variables for your account**, edit your user **Path**, and add the extracted folder containing `agent-tracker.exe`. Open a new terminal afterward.

## Connect your agents

Install and sign in to the agents you want to track using your own accounts. Open an agent chat, then launch the tracker.

Active chats are ordered by their latest user prompt, newest first, across all providers and folders. Replies and tool activity do not change that order. Chats without a recorded prompt time appear last, grouped alphabetically by provider and then by folder; new untimed prompts are ordered when the tracker observes them.

- **Claude Code** sessions come from local session registrations and transcripts. Task titles are the ones Claude Code itself saves in each transcript. Usage comes from Claude's status-line payload.
- **Codex** sessions come from the local state database and held thread-writer locks. Account usage requires the Codex CLI on PATH and a working login. The tracker refreshes account limits every 60 seconds.
- **Antigravity CLI (`agy`)** sessions come from local presence locks and conversation summaries. Gemini usage comes from Antigravity's status-line payload.
- **Meta Muse Code (`muse`)** sessions come from held `.session.lock` files and event logs under `%USERPROFILE%\.local\share\muse\sessions` (or `%XDG_DATA_HOME%\muse\sessions`). Muse saves no chat title, so the tracker extracts one offline from the requests; activity comes from recorded run and tool events.
- **Grok Build (`grok`, the official xAI CLI)** sessions come from `%USERPROFILE%\.grok\active_sessions.json`, checked against live process IDs. Task titles and activity come from each session's `summary.json` and ACP `updates.jsonl`. Set `GROK_HOME` to select a different Grok directory. Saved sessions without a live registration are excluded.

Muse and Grok also have account usage cards, in pink and light gray respectively. Muse requires Muse Code 1.4.13 or newer and reads subscription allowance through `muse usage --json`. Grok reads account billing through the official CLI's ACP `x.ai/billing` extension, showing the reported weekly/monthly allowance, reset time, and prepaid/on-demand credits when available. Both use the CLI's existing login, refresh every 60 seconds in the background, and retain previous readings after refresh failures. No model prompts are sent to collect usage. Missing allowances are shown as unavailable, including Muse API-key logins without subscription data.

CLI discovery and account queries have been verified with Muse Code 1.4.15 and Grok 1.0.77 on the development computer. Quota meters are also covered by account-response fixtures and subprocess protocol tests. Newly installed Windows CLIs are found in their standard install folders even when the tracker terminal has an older PATH. Muse displays the account's explicit no-allowance response when no subscription allowance is reported; a missing Grok usage percentage is shown as unavailable.

On interactive startup, the tracker adds usage collectors to detected Claude and Antigravity installations when their settings have no `statusLine` entry, and Grok when its TOML settings have no `ui.status_line` entry. Reopen the affected agents to begin collecting. Existing custom status lines are preserved. You can also run `agent-tracker --setup-usage` to configure available collectors explicitly.

Detected usage cards are sorted alphabetically and packed two per page. With all five providers present, the pages are Claude / Codex, Gemini / Grok, and Muse. Muse and Grok cards appear when their CLI or data directory is detected, including loading/error states. Use Left/Right Arrow to switch pages.

All account cards use one shared tile model and renderer. Tiles support an explicit **no active subscription allowance** state, one or more **percentage windows** with reset times, and **token spend** when the collector explicitly identifies token billing. Unknown or failed reads remain distinct from an absent subscription. Token spend is never inferred from a missing quota, a Free plan name, or an API-equivalent cost on a subscription session. Unknown amounts stay unavailable; reported zero remains zero.

Plan names, freshness, refresh failures, credit balances, reset credits, and warnings are optional shared fields. This keeps useful information without giving each agent its own layout. Estimated costs include their scope and are labelled as estimates. Per-session context and directly reported session costs remain beside the individual sessions. There is no aggregate spend summary or spend-history ledger.

Keep the tracker in its permanent location after configuring collectors because their commands refer to that location. If you move it, update the collector command in the affected agent's settings.

### Existing custom status lines

Integrate a collector into your existing status-line script by passing the same JSON payload to the appropriate command on standard input. Quote the executable path when it contains spaces.

```powershell
& 'C:\path\to\agent-tracker.exe' --capture-claude-usage
& 'C:\path\to\agent-tracker.exe' --capture-antigravity-usage
& 'C:\path\to\agent-tracker.exe' --capture-grok-usage
```

These commands expect JSON on stdin and emit a short status message. Your script can retain its own display output. Claude settings are in `%USERPROFILE%\.claude\settings.json`, or the directory selected by `CLAUDE_CONFIG_DIR`. Antigravity settings are in `%USERPROFILE%\.gemini\antigravity-cli\settings.json`.

## Grouped message exchanges

Sessions with detected message exchanges appear together in a rounded box titled **Exchanged messages**, using their provider's color. After an agent receives three further human prompts of its own without another exchange, it leaves the group. A new exchange resets the two participants' counters. Remaining single agents appear normally. Groups stay at their first member's normal position in the session ordering.

The outer border uses the first member's provider color. Inner dividers use the default dim gray, inset with space on both sides so they do not touch the outer border. Each member keeps its own provider color and terminal button.

Detection uses successful Claude/Codex message-tool results, recognized Grok ACP message-tool results, and Muse peer-message records or new peer items from its read-only view. Failed sends, idle subscriptions, tool output, and recognizable incoming agent messages do not count as human prompts. Both endpoints must be live and identifiable by an unambiguous session name or ID. Remote or unlisted agents, unknown log formats, and messages sent through arbitrary scripts cannot be reliably grouped. Muse's first view read establishes a baseline so old inbox history does not revive expired groups. Antigravity currently has no verified message source. The tracker sends no messages to discover groups.

## Switch to an agent terminal

While navigation runs, the clicked button reads **[Navigating to terminal, please wait...]**, wrapping within that agent's row on narrow terminals. It returns to **[Go to terminal]** when navigation completes.

Click **[Go to terminal]** beside a chat to activate its terminal window or select its Windows Terminal tab. The tracker resolves Claude and Grok live PIDs and the processes holding Codex, Antigravity, and Muse session locks. Navigation runs in a helper process so the dashboard keeps responding.

Windows Terminal must allow application title changes. The helper temporarily assigns the target console a unique title to find its tab through Windows UI Automation, then restores the previous title. When Codex uses a shared background server, navigation instead matches the session title and folder name against terminal tab labels, including the working spinner; duplicate matches are rejected. Codex app chats also attempt to select a uniquely matching chat title through Windows UI Automation. Navigation preserves maximized and fullscreen windows and restores minimized windows. Manually renamed tabs, suppressed application titles, split panes, embedded IDE terminals, hidden or ambiguous app sidebar entries, and elevated agents may prevent exact selection. A message appears in the dashboard when selection fails. Run the tracker and agents at the same elevation level.

## Controls

| Control | Action |
| --- | --- |
| Click [Go to terminal] | Activate the chat's terminal tab or window |
| Right Arrow | Show the next available usage page |
| Left Arrow | Show the previous available usage page |
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

`--demo` previews sample sessions for all five providers and illustrates percentage, no-subscription, and token-spend tiles using sample billing states. `--json` prints open sessions once. `--usage-json` refreshes and prints usage for all detected providers.

## Build the Windows ZIP

For isolated dashboard setups with fake installations, sessions, and account
usage, see [TEST-SCENARIOS.md](TEST-SCENARIOS.md). Run
`python agent_tracker_dev.py --test-1` from source, or build with `-Development`
for `agent-tracker --test-1` through `--test-8`. These commands and their fixtures
are excluded from the normal release build.

Build on Windows with Python matching the desired architecture. Only the x64 artifact is currently verified.

```powershell
python -m pip install pyinstaller==6.22.3
powershell -NoProfile -ExecutionPolicy Bypass -File .\build-windows.ps1 -Python python
```

The archive is written to `release/agent-tracker-windows-<architecture>.zip`. The build script bundles the executable, runtime libraries, and quick-start instructions. `-ExecutionPolicy Bypass` applies to that build process only.

Run the tests with:

```powershell
python -m unittest discover
```

## Data and limitations

Context and directly reported session costs appear beside the individual agent that reported them. The account usage cards show shared limits. Claude, Antigravity, and Grok session readings match by session ID; Codex metrics come from each thread's rollout. Muse reads its own server-computed cumulative token usage, cost (including its partial flag), and context through read-only MSP `session/read` and `view/page` calls in the background every 15 seconds. These reads do not attach to, resume, or prompt the session. Muse and Grok launchers are also detected in their default Windows install directories if the tracker's PATH predates installation.

Session costs are displayed only when supplied directly by the provider. Provider-reported values can themselves be estimates; they are not presented as verified charges. Missing costs are omitted. The tracker does not convert tokens or subscription-limit percentages to USD, calculate aggregate spend, or read/write the old `tracker-spend.json` ledger. Codex shows its recorded token counts and approximate context usage without a calculated dollar cost.

Session detection depends on local agent file formats and Windows locking behavior, which may change between agent versions. Activity is approximate. Antigravity currently reports working or waiting based on its conversation summary. Closed historical chats are filtered using held presence locks.

Task titles never involve a model call or send chat text anywhere. Each provider's own saved title is used where one exists: a Claude `/rename` title, then Claude Code's `ai-title`; Codex, Antigravity, and Grok titles from their local state. Muse sessions, and Claude chats before Claude Code has saved a title, use an offline extract (`titles.py`): the first sentence that makes a request, preferring requests and imperatives over scene-setting, with greetings, politeness wrappers, code, URLs, and long paths removed. Grok falls back to a user prompt from recent updates.

Status-line collectors store selected quota, model, context, plan, and cost display fields in each agent's local `tracker-usage` folder. Usage freshness and reset times are displayed. Missing quota readings are labelled unavailable, and previous readings may remain visible after refresh failures.

The tracker keeps no title cache. Earlier versions wrote `task-summaries.json` beside `tracker.py` or in `%LOCALAPPDATA%\Agent Tracker\`; that file is no longer used and can be deleted. Build outputs are excluded from Git.

## Source files

- `tracker.py` contains session detection, terminal input, mascots, and the dashboard.
- `terminal_focus.py` contains process lookup and terminal activation.
- `usage.py` contains status-line collection, account usage retrieval, and dashboard integration.
- `usage_adapters.py` contains the provider registry and payload adapters. Claude, Codex, and Gemini translate their native payloads; Muse and Grok share the normalized allowance adapter.
- `usage_tiles.py` contains the reusable tile data model, plain-text output, and terminal renderer. It has no provider-specific rendering branches.
- `session_usage.py` contains per-session context, token counts, and directly reported costs.
- `quota.py` reads Muse subscription allowance and Grok billing through their CLIs, keeping credentials inside the CLI.
- `message_groups.py` detects message exchanges, expires group membership, and fits colored text to terminal columns.
- `titles.py` extracts task titles offline from user requests when an agent saves no title.
- `agent_tracker_dev.py` and `dev_scenarios.py` provide eight isolated dashboard scenarios; see [TEST-SCENARIOS.md](TEST-SCENARIOS.md).
- `test_*.py` cover tracking, collectors, session metrics, and the shared tile contract.
- `build-windows.ps1` creates the portable Windows archive or a separate development executable.

## Adding a usage provider

Write a collector that reads the agent's own account data, then register a `ProviderSpec` in `usage_adapters.PROVIDERS` with its name, color, and adapter. Paging and rendering use that registry automatically. A provider returning the common schema can use the default adapter, with no custom layout code:

```python
ProviderSpec('New Agent', '\x1b[38;2;120;180;200m')
```

Its snapshot entry uses `detected`, `updated_at`, `error`, and `data`. The common `data` fields are:

```python
{
    'billing_mode': 'subscription',  # subscription, token, or unknown
    'plan': 'Example plan',
    'windows': [{'label': '5h', 'usedPercent': 25, 'resetsAt': 1893456000}],
    'details': [{'label': 'Prepaid credits', 'value': '$12.50'}],
}
```

For an explicit absence of subscription allowance, set `subscription_status: 'inactive'`. Do not set it on a timeout, missing percentage, or unrecognized response. For token billing, set `billing_mode: 'token'` and provide `token_spend` with optional `tokens`, `cost_usd`, `estimated`, and `scope` fields. `scope` should identify whether the readings cover a session, billing period, or another measured interval. Each window can have its own duration and reset time; it need not be a 5-hour or weekly window. A custom adapter can instead return a `UsageTile` directly. Renderer changes are unnecessary.
