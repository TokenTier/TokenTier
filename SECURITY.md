# Security

## Reporting a vulnerability

Please report security problems privately through GitHub security advisories: open the repository's **Security** tab and choose **Report a vulnerability**. Do not file a public issue. We will acknowledge the report and work on a fix with you before anything is disclosed.

## What TokenTier stores

- Only local files under `~/.tokentier` (event logs, install manifest, backups, a copy of the app, optional routing hints). Service files are written to `~/Library/LaunchAgents` (macOS) or `~/.config/systemd/user` (Linux), and the installer edits `~/.claude` (agents, skill, `CLAUDE.md`, `settings.json`).
- Nothing is sent anywhere. There is no telemetry and no network calls to outside services.
- Logs contain project names and paths, task labels and a short excerpt of each result. Treat them as private.
- The `Stop` and `SessionEnd` hooks read the main session transcript only to sum token usage per model. They store counts only, not prompts or replies.
- The dashboard only checks the modification time of subagent transcript files, to detect stale workers. It never reads their contents.
- The dashboard has no write endpoints: POST, PUT, DELETE and PATCH requests are refused.
- The hooks never make network requests.

## Network exposure

The dashboard binds to `127.0.0.1` only, so other machines cannot reach it. It has no authentication, because it is meant for the local user. Do not forward its port to untrusted networks.

## Hooks run commands as you

The logging hooks registered in `settings.json` run `tokentier_log.py` with your user permissions each time a session or subagent starts or stops and after each turn of the main session. The installer shows exactly what it will change before it writes anything (`--dry-run`). Review the code in `kit/hooks/` and `bin/tokentier` before installing, as you would with any tool that edits your configuration.
