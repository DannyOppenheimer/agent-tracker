# Dashboard test scenarios

These commands populate the real dashboard with synthetic sessions and account
usage. They simulate detected installations regardless of what is on your PC.
They do not scan agent folders, change agent settings, write summary caches, call
agent CLIs, or query accounts. All prices, plans, and allowances are examples.

Run from source on Windows with Python 3.11 or newer:

```powershell
python agent_tracker_dev.py --test-1
python agent_tracker_dev.py --test-2
python agent_tracker_dev.py --help
```

| Command | Simulated setup | What to inspect |
| --- | --- | --- |
| `--test-1` | Only Codex and Muse installed; one active session each | One usage page containing Codex / Muse. Codex is reading; Muse is editing. Session context and tokens appear, with a partial reported cost for Muse. |
| `--test-2` | Only Codex and Muse installed; no active sessions | The same two account cards remain visible above the empty session message. |
| `--test-3` | All five providers installed; two sessions each | Three pages: Claude / Codex, Gemini / Grok, Muse. All five activity animations, per-session metrics, and sample Grok token billing. |
| `--test-4` | Only Muse installed; one active session; explicit no subscription allowance | A single Muse card and its no-allowance message. The active session still has context, tokens, and a partial cost. |
| `--test-5` | Only Codex and Muse installed; two active sessions | Codex usage remains loading; Muse shows a simulated failed read. Neither should claim no subscription. |
| `--test-6` | Only Codex and Muse installed; two active sessions | Codex at 100%; Muse at 97% with a two-hour-old reading and a refresh failure. Check bars, freshness, and error text. |
| `--test-7` | All five providers installed; thirty active sessions | Long task titles and folder names, wrapping, scrolling, narrow terminal sizes, and page switching. |
| `--test-8` | No providers installed; no sessions | No account cards and the empty session message. The dashboard's normal heading still lists supported providers. |

Choose exactly one numbered scenario. Data stays fixed while mascots animate;
reset countdowns use the time the scenario started. Sessions are ordered by fake
prompt timestamps, newest first. No actual processes or locks are created, so
these scenarios test presentation rather than installation discovery or session
detection. **Go to terminal** remains visible for layout review, but clicks do
nothing in scenario mode.

Use Left/Right to switch usage pages, Up/Down, the mouse wheel, Page Up/Down,
or Home/End to scroll, and Q or Ctrl+C to exit. Resize the terminal to inspect
layout at different widths. The notice at the top identifies the test case.

Inspect the injected data without opening the dashboard:

```powershell
python agent_tracker_dev.py --test-1 --json
python agent_tracker_dev.py --test-1 --usage-json
```

## Development executable

With PyInstaller installed, build a separate development executable:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build-windows.ps1 -Development
.\.build\development\dist\agent-tracker\agent-tracker.exe --test-1
.\.build\development\dist\agent-tracker\agent-tracker.exe --test-2
```

If that development folder is on your PATH, the commands are simply
`agent-tracker --test-1`, `agent-tracker --test-2`, and so on. Keep its `_internal`
folder beside the executable. This build requires a numbered scenario and does
not create a release ZIP.

The normal build command (without `-Development`) uses `tracker.py` and explicitly
excludes `agent_tracker_dev` and `dev_scenarios`. The release executable rejects
`--test-1` through `--test-8`; its existing `--demo` command is unchanged. Scenario
fixtures and this guide are not copied into the release archive.

To add a case, extend `SCENARIOS` and `scenario()` in `dev_scenarios.py`, update
this table, and run `python -m unittest discover`.
