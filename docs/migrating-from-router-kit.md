# Migrating from router-kit

This page is only for people who used the earlier hand-written router-kit dashboard, which kept its data in a file called `router-kit-data.json`. If you installed TokenTier fresh, you do not need it.

Preview the import first. It writes nothing:

```bash
tokentier migrate legacy --file router-kit-data.json --dry-run
```

Then run it for real:

```bash
tokentier migrate legacy --file router-kit-data.json
```

## What the import does

The old file has no timestamps. Each session is placed at 12:00 local time on its date, and each task follows the previous one by its recorded duration. If a day's tasks would run past midnight, the start moves earlier so they stay on that date. Imported events are marked `"source":"legacy"`.

The old data has only a total token count per task. It is kept as `tokens_total` and `legacy_tokens_total`, with the token breakdown set to zero. Because there is no breakdown, imported legacy tasks are not priced and show no cost in the dashboard.

Running the import again skips events that are already there.

## Fixing dates with --redate and --replace

If some tasks were saved under the wrong day, move them with `--redate SESSIONDATE:TASKID=YYYY-MM-DD`. TASKID can be one id (`t75`), a list (`t75,t76`) or a range (`t70-t79`).

If those tasks were already imported, add `--replace`. It removes only this project's earlier legacy events and then imports again:

```bash
tokentier migrate legacy --file router-kit-data.json --redate 2026-10-03:t70-t79=2026-10-04 --replace
```

Other options: `--project NAME` overrides the project name, and `--home DIR` uses a different TokenTier home.
