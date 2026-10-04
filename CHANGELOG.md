# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-10-05

### Fixed
- `tokentier uninstall` now also removes TokenTier's internal files in `~/.tokentier` (`state/`, `hook-ignored.log`,
  `routing-hints.json`, `tokentier-dashboard-task.xml`, plus the existing helper logs), keeps `logs/`, and removes the
  folder itself when nothing is left. `--purge` now deletes the whole `~/.tokentier` folder (logs, `config.json`, everything)
  and refuses to run if the folder looks unsafe (a filesystem root, your home or `~/.claude`, or a folder that is not
  named `.tokentier` and has no TokenTier manifest and app). `--project` and `--hooks-only` leave the shared files alone.
  `--purge` cannot be combined with `--keep-service`.

### Changed (documentation)
- Regenerated all README screenshots from the current dashboard (demo data only) and added a light-theme overview shot.

### Changed (documentation)
- README: Quick start, a "Global or per project?" section, all five logging-hook events listed, and expanded Privacy and
  SECURITY notes (transcripts read only for token counts, stat-only stale detection, no write endpoints, no network
  requests). The legacy-import details moved to `docs/migrating-from-router-kit.md`.
- `tokentier uninstall --hooks-only` now says what was removed ("Removed the TokenTier hooks from project X. Its agents,
  skill and CLAUDE.md line were kept.") instead of "Uninstalled: project X.". Full uninstall wording is unchanged.

### Changed (dashboard redesign)
- New design system for the local dashboard: system sans for text with monospace kept for numbers and ids, a
  4/8/12/16/24/32 spacing scale, refined dark palette and a fully reworked light theme (text and chips measured at
  WCAG AA, 4.5:1 or better), consistent radius, focus rings, hover and selected states, short transitions that respect
  `prefers-reduced-motion`, skeleton loading and clearer empty/error states. Works down to 375 px wide.
- The task list is now a task feed: one card per task with a tier-coloured rail, status chip and the full title
  (two lines), project chip with a stable colour, tier, start time (relative within the last hour), duration, agent
  type, an escalation-chain badge (for example `Haiku → Sonnet`) when attempts share a `tool_use_id`, and on the right
  the cost, tokens, a cost bar relative to the most expensive task in view and the saving versus Opus. Running tasks
  show a spinner and a live elapsed timer (ticks every second, no refetch, paused while the tab is hidden); stale
  tasks are muted with their reason. Day groups have sticky headers with the day's tasks, cost and saving and can be
  collapsed. New controls: status chips with live counts (All, Running, Passed, Failed, Escalated,
  Stale/unverified), tier chips, search with a clear button and `/` to focus, sort (Newest, Cost, Duration, Tokens),
  a router-workers switch and a Comfortable/Compact density toggle (remembered). The feed uses the page scroll
  instead of a small inner scroll box.
- Master-detail: on screens 1100 px and wider the selected task's details sit in a sticky side panel (cost / Opus
  cost / saved, project and path, session, model, times, escalation chain oldest first, token breakdown with $/MTok,
  result, raw events); narrower screens open them as a bottom sheet. Esc closes, arrow keys / Home / End move
  through the feed, Enter opens. Pricing moved into its own collapsible card.
- Projects tab: responsive cards with avatar, path, a "running" pulse, tasks / sessions / spend / saved, a 14-day
  spend sparkline, tier-mix bar, pass / fail / escalated counts, lead-session cost and last activity; search and sort
  (Recent, Spend, Tasks). Opening a card filters the whole dashboard; "Sessions" jumps to that project's sessions.
  Pseudo projects without tasks or lead usage are hidden.
- Sessions tab: cards grouped by day with a short copyable session id, project, time range and duration, an
  Active / Idle / Finished badge, tasks with a tier-mix bar, spend, saved, lead cost and the latest task title;
  sort (Recent, Spend, Tasks). Opening a card filters the task feed by that session.
- API (read only, existing fields unchanged): `/api/projects` rows add `daily` (last 14 local days, zero filled),
  `tier_mix`, `status_counts`, `running`, `baseline_cost`, `saved`, `lead_cost`, `lead_tokens`, `spend` and
  `first_active`; `/api/sessions` rows add `tier_mix`, `status_counts`, `state` (`active`/`idle`/`finished`),
  `last_active`, `first_active`, `spend`, `preview_label` (latest task) and `top_label`/`top_cost` (most expensive
  task). `/api/tasks` accepts `sort=cost|duration|tokens` and a comma-separated `status`, and returns `counts`
  (per status ignoring the status filter, per tier ignoring the tier filter).
- Demo data (`tests/make_sample_logs.py`) uses some longer, realistic task labels; README screenshots regenerated.

### Fixed
- A `task_update` event (or any event) without project fields no longer creates a project or session entry. The
  live dashboard showed a bogus `unknown` project with 0 tasks; such events are now attributed to the project of
  their task or session, and ignored when neither is known. A `task_end` without project fields inherits the project
  of its `task_start`.
- `tokentier install` run as an upgrade now restarts the already-running dashboard service when any app file or the
  service definition changed (launchd `kickstart -k`, systemd `restart`, Task Scheduler `/End` + `/Run`; a Windows
  Run-key fallback prints that a re-login is needed). Before, the old process kept serving the previous code. An
  idempotent re-run, `--no-service`, `TOKENTIER_NO_SERVICE_EXEC` and a stopped service never restart; `--dry-run`
  prints `service: will restart (app files changed)`. A failed restart only warns with the manual command.
- A subagent that is killed (stopped by the user) never fires `SubagentStop`, so it showed as `running` for 2 h. The
  `SubagentStart` hook now logs `subagent_transcript` (absolute path of the worker transcript); the dashboard stats
  that file's mtime (never reads it, only paths ending in `subagents/agent-<id>.jsonl`) and marks a task `stale`
  after 15 minutes without activity. Tasks and events without the path keep the old 2 h rule. Stale tasks expose
  `stale_reason` and `last_activity`, are excluded from the running counts, and their duration ends at the last
  activity. A resumed worker returns to `running`; a later `task_end` always wins.
- Added `.gitattributes` for stable line endings (LF for `.sh`/`.py`, CRLF for `.cmd`/`.ps1`).

### Added (Windows, experimental)
- Windows 10/11 support, verified by simulation (`TOKENTIER_PLATFORM=win32` with fake `msvcrt`/`winreg`/`subprocess`/
  sockets, `tests/test_windows.py`) and a Windows CI job; **not yet verified on a real Windows machine**.
- Launchers `install.ps1` / `uninstall.ps1` (find Python 3.9+ via `py -3`, `python`, `python3`; skip the Microsoft Store
  stub; download nothing) and `install.cmd` / `uninstall.cmd` for machines whose execution policy blocks scripts.
- Hooks on Windows use Claude Code's exec form (`command` = absolute `python.exe`, `args` = script + event, optional
  `--home DIR`; needs Claude Code 2.1.139+), so no shell and no quoting are involved. The macOS/Linux command string is
  unchanged byte for byte. Install/upgrade/uninstall/doctor recognise both forms (`tokentier_log.py` in `command` or
  `args`) and convert an entry in place when the platform changes; the byte-exact uninstall guarantee holds for CRLF and
  UTF-8 BOM files.
- Always-on dashboard via a per-user Task Scheduler task "TokenTier Dashboard" (UTF-16 task XML: logon trigger,
  `pythonw.exe server.py --home ... --port N`, restart every minute up to 999 times, no battery or time limits, one
  instance). If `schtasks` fails, fallback to an `HKCU\...\Run` value (recorded in the manifest and removed on uninstall).
  `status`/`doctor` show `service: scheduled task, running: yes/no`.
- `bin/tokentier.cmd` shim in the app dir, and a PATH tip (TokenTier never edits PATH).
- `doctor`: `hook python` check (the interpreter in exec-form entries exists) and a `hook form` warning for POSIX-form
  entries on Windows.
- Dashboard: `--home DIR`; explicit MIME types for .js/.css/.html/.json/.png/.svg (the Windows registry often maps .js to
  text/plain); SIGTERM/SIGBREAK stop it cleanly; under `pythonw.exe` output goes to `dashboard.log`.
- `tests/windows_smoke.ps1`: opt-in end-to-end check for a real Windows machine (throwaway profile, no service).

### Changed (portability, all platforms)
- Hook: cross-platform lock helper (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows, polling with a ~1 s cap; if the
  lock is not available the event is appended anyway); JSONL appends are binary with `\n` on every OS; the payload is read
  from stdin as UTF-8 bytes (no cp1252 decoding on Windows); the detached label helper gets `TOKENTIER_HOME` explicitly
  and, on Windows, starts without a console (`pythonw.exe`, `DETACHED_PROCESS`, own process group, job breakaway when
  allowed).
- Every atomic write (`os.replace`) is retried 10 x 50 ms on `PermissionError` / WinError 5/32/33.
- Port probe: Windows uses `SO_EXCLUSIVEADDRUSE` (with `SO_REUSEADDR` a busy port looked free); POSIX keeps
  `SO_REUSEADDR`. The dashboard server refuses to share its port on Windows and skips the slow reverse-DNS lookup.
- Backup paths for Windows drive and UNC paths stay inside `backups/<timestamp>/` (`C:\...` -> `C\...`); path comparisons
  use `normcase` (case-insensitive on Windows); console output never crashes on a non-UTF-8 console.

### Fixed
- Task status: Claude Code fires `SubagentStop` twice per worker and the weaker second event (no STATUS line) used to
  overwrite the first, showing `unknown`. The dashboard now merges all `task_end` events of a task field-wise (status
  never downgraded: explicit `STATUS` line > inferred > unknown; tokens, duration, model and end time from the latest
  event that has them), which also repairs historic logs. The hook skips a `task_end` that adds nothing to what is already
  logged for the task.
- Task status: `task_end` gains `status_source` (`status_line`, `inferred` or null). A report without a STATUS line and
  without a problem marker (escalate, blocked, unable to, could not, cannot, failed, error: ...) is inferred `pass`; a
  report with a marker, or an empty one, stays `unknown`. The UI shows `pass` + "inferred", and `? unverified` for unknown.
- Labels: the label/meta file can appear 1-2 s after `SubagentStart`. The hook now waits only ~0.4 s, then starts a
  detached background updater that appends a `task_update` event (label, tool_use_id, model, agent_type) once the meta
  file shows up (up to ~15 s); the store applies it fill-only. Errors go to `hook-errors.log`.
- Worker agents are told that the final report they hand back must end with the STATUS line; the router skill treats a
  report without one as not verified.

- Logging hook: internal helper subagents (empty `agent_type`, no transcript) are no longer logged as tasks (recorded in `hook-ignored.log`); `task_start` waits up to ~1.5 s for the subagent meta file so running tasks have a label; status/result for workers that finish via `SubagentHandback` are derived from the subagent transcript; dashboard falls back to the agent type when a task has no label.

### Added
- Lead session usage: the main ("lead") session's own token usage is now logged, so total spend is complete. A new `Stop`
  hook (and a best-effort flush on `SessionEnd`) reads only the new bytes of the main transcript and appends
  `lead_usage` events (token deltas per model, never double counting a streamed message). Per-session state lives in
  `$TOKENTIER_HOME/state/` (stale files are pruned after 14 days). If hooks were installed mid-session, or a session is
  resumed that we never saw, earlier work is skipped rather than guessed (`history_skipped`). Re-running `install`
  upgrades an existing 4-event install by adding only the `Stop` entry (settings backed up first); `uninstall` removes it
  and still works with an older manifest. `doctor` and `status` now count 5 events.
- Dashboard: `/api/overview` totals gain `lead` (tokens, cost, turns, sessions, by model, unpriced count) and
  `total_spend` (tasks + lead); `daily[]` gains `lead_cost`; `/api/sessions` rows gain `lead_tokens`/`lead_cost`. The KPI row
  is now 10 tiles (adds "Lead session" and "Total spend"), the cost chart shows a muted lead series, and the Sessions tab a
  Lead column. Routing savings are unchanged and still computed on delegated tasks only: the lead model is the user's own
  choice, not a routing decision, so it is excluded from the all-Opus comparison.
- `tests/make_sample_logs.py` also generates `lead_usage` events (separate random stream, task data unchanged).
- README screenshots (`docs/screenshots/`, generated from demo data), a Releases section, and a tagged-release workflow
  (`.github/workflows/release.yml`: tests, tag-vs-`VERSION` check, `git archive` tarball and zip, `SHA256SUMS`, GitHub release).
- HTML lint (`html-validate`) in CI with `.htmlvalidate.json`.
- `config.json` in `$TOKENTIER_HOME` with keys `port`, `retention_days`, `pricing_path`, `baseline_token_multiplier`
  (precedence: CLI flag > `TOKENTIER_*` env var > `config.json` > default; the server always binds 127.0.0.1 and ignores
  a `host` key with a warning). `tokentier config list|get|set|unset|path` with strict validation, atomic writes and
  unknown keys preserved. `install --port N` records the port in `config.json`; uninstall removes the file only if the
  installer created it and it is unchanged. `tokentier doctor` checks it. A user-owned `pricing_path` overrides the
  shipped `pricing.json` (hot-reloaded; an invalid file keeps the previous table and is reported in `/api/health`
  `warnings`).
- Log retention: `tokentier prune [--older-than DAYS] [--dry-run] [--yes]` deletes `logs/YYYY-MM-DD.jsonl` files older than
  the retention period (never today's or yesterday's, never other file names). With `retention_days` set, the dashboard
  server prunes at startup and every 24 hours.
- Baseline multiplier is visible and configurable: `/api/pricing` and `/api/health` expose `baseline_token_multiplier`
  and `baseline_source`; the savings banner shows the effective multiplier, an `adjusted` badge when it is not 1, and the
  exact `tokentier config set baseline_token_multiplier 1.5` command. The API remains read-only.
- Initial scaffold for TokenTier project
- `bin/tokentier` CLI (Python 3.9+, stdlib only) with `install`, `uninstall`, `status`, `doctor`,
  `dashboard`, `open`, `migrate legacy` and `version`.
- Installer: copies the app to `~/.tokentier/app/` so the repo can be deleted afterwards; installs the agents and the
  `tokentier-router` skill (backing up any different existing files); adds the router line to `CLAUDE.md` between
  `<!-- tokentier:start/end -->` markers, or records that it was already there; merges four logging hooks into
  `settings.json` (atomic write, backup first, existing keys/hooks untouched, aborts on invalid JSON); `--dry-run`
  with unified diffs, `--yes`, `--project [DIR]` (repeatable), `--no-service`, `--no-hooks`, `--port`,
  `--claude-dir`, `--home`.
- Double-logging guard: a `--project` install skips hooks when global hooks exist; `doctor` warns about duplicates.
- Always-on dashboard service: launchd agent on macOS, systemd user unit on Linux (127.0.0.1 only, default port
  8899); warns, never kills, when the port is taken; falls back to printing the manual command.
- `install-manifest.json` records every file (sha256), backup, hook entry, CLAUDE.md state, service file and project
  install; uninstall works strictly from it and restores `settings.json`/`CLAUDE.md` to their original bytes.
- Uninstaller with `--project`, `--hooks-only`, `--purge`, `--keep-service`, `--dry-run`, `--yes`; files edited
  after install are left in place and reported; logs are kept unless `--purge`.
- User-edited `pricing.json` survives upgrades (new defaults go to `pricing.json.new`).
- `tokentier migrate legacy`: imports the old hand-written `router-kit-data.json` as schema-v1 events
  (`"source":"legacy"`), idempotent, with `--redate`, `--replace` and `--dry-run`.
- `install.sh` / `uninstall.sh` are now thin wrappers around the CLI (they check for Python 3.9+).
- Tests: `tests/test_installer.py`, `tests/test_migrate.py` (temporary HOME, no service commands executed).
- `tokentier stats [--days N] [--json] [--write-hints]`: per task-type routing stats (attempts and pass rate per
  tier, escalation rate, average cost using `dashboard/pricing.json`) and a recommended start tier; `--write-hints`
  writes `~/.tokentier/routing-hints.json` atomically.
- `tokentier-router` skill: type tags (`[bugfix]`, `[feature]`, ...) in the Agent description, optional use of
  `routing-hints.json` (only to start higher than the default table), and escalation reasons passed to the next tier.
- Opt-in extra `kit/extras/agents/deep-worker-low.md` (Opus, low effort), not installed by default.
- CI (`.github/workflows/ci.yml`: unit tests, shellcheck, `node --check`; Ubuntu and macOS, Python 3.9/3.11/3.12),
  bug report issue template and pull request template.
- `CONTRIBUTING.md`, `SECURITY.md`, and README sections: how routing works, routing hints, pricing and savings
  estimate, troubleshooting, roadmap.
