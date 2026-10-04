# Contributing to TokenTier

Thanks for helping. TokenTier is small on purpose: Python 3.9+ standard library only, no build step, no runtime dependencies.

## Dev setup

```bash
git clone <repo-url> tokentier && cd tokentier
python3 --version        # 3.9 or newer
python3 bin/tokentier --help
```

There is nothing to install. Optional tools used by CI: `shellcheck` (for `install.sh` and `uninstall.sh`) and Node.js (only for `node --check dashboard/public/app.js`).

To try the dashboard with demo data, without touching your own logs:

```bash
python3 tests/make_sample_logs.py /tmp/tt-demo
TOKENTIER_HOME=/tmp/tt-demo python3 bin/tokentier dashboard --port 8900
```

## Running the checks

```bash
python3 -m unittest discover -s tests
bash -n install.sh uninstall.sh
shellcheck install.sh uninstall.sh      # if installed
node --check dashboard/public/app.js    # if Node is installed
```

CI runs the same checks on Ubuntu and macOS with Python 3.9, 3.11 and 3.12, and on Windows with Python 3.9 and 3.12
(unit tests, a PowerShell syntax check of `install.ps1`/`uninstall.ps1`, an installer dry run and the smoke test below;
the Windows job does not block merges until Windows support has been verified on real machines).

## Windows

Windows behaviour is tested on every OS by simulation: `tests/test_windows.py` runs the CLI with
`TOKENTIER_PLATFORM=win32` (and `TOKENTIER_NO_SERVICE_EXEC=1`) and replaces `msvcrt`, `winreg`, `subprocess.run` and
sockets with fakes. Tests that only make sense on macOS/Linux (`sh -c` command strings, POSIX modes, symlinks, `TZ`) are
skipped on Windows with a reason (`POSIX_ONLY` in `tests/tt_helpers.py`), never silently passed.

On Windows, run the suite with `py -3 -m unittest discover -s tests`. The test helpers set both `HOME` and `USERPROFILE`
to a temp dir, because Python on Windows reads `USERPROFILE`.

### Windows smoke test

Simulation cannot prove how real Windows behaves (Claude Code starting the exec-form hook, `schtasks`, `msvcrt`, console
windows). If you have a Windows 10/11 machine, please run the opt-in smoke test from the repo folder and paste its output
into an issue:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tests\windows_smoke.ps1
```

It installs into a throwaway profile folder (a temp dir with a space in its name; your real `%USERPROFILE%\.claude` is
never touched) with `--no-service` and `TOKENTIER_NO_SERVICE_EXEC=1`, runs `doctor`, fires a synthetic `SessionStart`
through the hook command registered in `settings.json` (exec form), runs the `tokentier.cmd` shim, uninstalls, and checks
that `settings.json` (UTF-8 BOM + CRLF seed) and `CLAUDE.md` are restored byte for byte. Exit code 0 means every check
passed. The always-on service (Task Scheduler) is not part of the smoke test; to try it, run `.\install.ps1` for real,
then `tokentier.cmd status` and `.\uninstall.ps1`.

## Safety rule: never test installers against your real HOME

The installer edits `~/.claude/settings.json`, `~/.claude/CLAUDE.md` and service files. Never run `install`, `uninstall` or the migrate command against your real home while developing. Use a throwaway HOME and keep services from being started:

```bash
export HOME=$(mktemp -d) TOKENTIER_NO_SERVICE_EXEC=1
./install.sh --yes --no-service
# simulate Windows (exec-form hooks, task XML, tokentier.cmd) on macOS/Linux:
TOKENTIER_PLATFORM=win32 ./install.sh --yes
```

Tests must follow the same rule: use the helpers in `tests/tt_helpers.py` (`FakeHome`), which run the CLI with a temporary `HOME`, remove `TOKENTIER_HOME` and `CLAUDE_CONFIG_DIR` from the environment, and set `TOKENTIER_NO_SERVICE_EXEC=1`. Tests must not depend on the gitignored `legacy-data/` directory (skip when it is missing).

## Code style

- Python 3.9 compatible, standard library only. No new dependencies.
- Keep the existing style: small functions, 4-space indent, lines up to about 120 characters, comments that explain why.
- Hooks (`kit/hooks/tokentier_log.py`) must never crash or print: they always exit 0.
- Writes to user files are atomic (temp file plus `os.replace`) and, for files the installer touches, backed up first.
- Shell scripts stay thin and pass `shellcheck`. PowerShell and `.cmd` launchers stay thin and ASCII only.
- Portability: use `os.path`, open user files in binary (keep CRLF/BOM), text files with `encoding="utf-8"`, never
  hard-code `python3` as an executable for Windows, and keep the macOS/Linux hook command string unchanged.
- Plain, direct wording in docs. Keep README flags in sync with `python3 bin/tokentier <command> --help`.

## Proposing changes

1. Open an issue first for anything larger than a small fix, so we can agree on the approach.
2. Keep pull requests focused. Add or update tests, and update `README.md` and `CHANGELOG.md` (under "Unreleased") when behaviour changes.
3. Fill in the pull request template. Describe how you tested, and confirm that you did not run installers against your real HOME.

Security problems: do not open a public issue. See `SECURITY.md`.
