# Changelog

## 0.2.0 - 2026-11-11

Changes since v0.1.0, including the terminal-navigation commits already on main.

### Added

- Track live Meta Muse Code and official xAI Grok Build sessions alongside Claude Code, Codex, and Antigravity, with provider colors and activity animations.
- Read Muse subscription allowance and Grok account billing through their authenticated CLIs. Refresh account readings in the background and retain previous data after refresh failures.
- Share one usage-card model across providers, with alphabetical paging, percentage windows and reset times, explicit no-subscription states, and token billing when identified by the collector.
- Show per-session context, token counts, and directly reported costs where available. Read Codex metrics from rollouts and Muse metrics through read-only local session queries.
- Collect Grok status-line usage while preserving existing custom status lines.
- Group live sessions with detected message exchanges in an **Exchanged messages** box. Membership expires after three further human prompts without an exchange. The provider-colored outer border contains inset dim-gray dividers.
- Provide eight isolated development scenarios and a separate development executable for dashboard layout checks.

### Changed

- Order sessions by latest user prompt across providers and folders; replies and tool activity do not move chats to the top.
- Keep account usage separate from individual session metrics. Missing amounts remain unavailable, and zero remains zero; token counts and subscription percentages are not converted into dollar costs.
- Replace background `codex exec` task summaries with titles that cost nothing and stay local: Claude uses `/rename` titles, then Claude Code's own `ai-title`; Muse and untitled Claude chats use an offline request extractor (`titles.py`). Chat text is no longer sent to Codex, and no title cache is written.
- Discover Muse/Grok launchers in standard Windows install locations when PATH is stale.
- Read Claude transcript titles incrementally instead of re-reading the whole transcript on every change.
- Document provider setup, usage states, grouping detection limits, development scenarios, and extension points.

### Fixed

- Activate terminal tabs for Muse and Grok, and select uniquely matching Codex app chats.
- Locate Windows Terminal tabs even when their pseudoconsole window is visible; support Codex's shared background server through exact session-title/folder matching, including working spinners.
- Preserve maximized and fullscreen windows while restoring minimized windows; reject ambiguous navigation matches.
- Display navigation progress inside the clicked session's button, including wrapped and scrolled grouped sessions.
- Never write an invalid Grok `config.toml` when connecting the status line (for example when `ui` is an inline table).
- Infinite timestamps in agent logs no longer hang the dashboard.
- A provider scan that hits unexpected data shows a notice instead of closing the dashboard; Muse background reads no longer print tracebacks over the display.

### Validation and distribution

- 92 automated tests cover session detection, prompt order, task titles, usage adapters and cards, CLI protocols, session metrics, message groups, navigation, and development scenarios.
- Portable Windows x64 ZIP with Python bundled. Development scenarios are excluded from the normal executable.
- Windows 10/11 x64 remains the target; a separate clean Windows installation, 32-bit Windows, and ARM64 Windows have not been verified.

## 0.1.0 - 2026-11-11

- Initial portable Windows release with Claude Code, Codex, and Antigravity tracking, animated mascots, usage cards, status-line collectors, and terminal navigation.
