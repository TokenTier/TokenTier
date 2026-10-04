---
name: model-router
description: Cascade routing to save tokens. Use for any non-trivial coding or writing task the user hands off. Starts with the cheapest suitable worker (Haiku), verifies the result, and escalates to Sonnet then Opus only if the check fails.
---

# Model Router

Route each task through cheaper models first and escalate only on failure. You (the main session) are the router. The workers are subagents: `fast-worker` (Haiku), `mid-worker` (Sonnet), `deep-worker` (Opus).

## Step 1: Classify the task and pick a start tier

| Task type | Start at |
|---|---|
| Rename, format, boilerplate, small single-file edit, lookup, summary | fast-worker |
| Multi-file change, bug fix with a known cause, feature with clear spec, tests | mid-worker |
| Architecture, unclear or hard-to-reproduce bug, security, large refactor, ambiguous requirements | deep-worker |

If the user says "use opus" or "use haiku", obey them and skip routing.

Start the Agent `description` with a one-word type tag, for example `[bugfix]`, `[feature]`, `[docs]`, `[refactor]`, `[test]` or `[research]`, then a short label (`[bugfix] Fix login redirect`). TokenTier learns from outcomes per tag.

If `~/.tokentier/routing-hints.json` exists, read it once per session. When it recommends a tier for the task's type that is HIGHER than the table's default, start at that tier. Never start lower than the table because of a hint: going cheaper than the table needs the user's say-so.

## Step 2: Run the worker

Call the Agent tool with the chosen `subagent_type`. Give it the full task, relevant file paths, and the success check from Step 3. Do not do the task yourself.

## Step 3: Verify (the part that makes escalation real)

Never judge success by how confident the worker sounds. Use something concrete:
- Tests pass (`npm test`, `pytest`, etc.)
- Build, type check or linter passes
- For non-code tasks: a short checklist of requirements, each confirmed in the output

The result FAILS if: the check fails, the worker replied `STATUS: escalate` or `STATUS: blocked`, or required parts of the task are missing. A report without a STATUS line counts as 'not verified': verify it concretely as usual.

## Step 4: Escalate on failure

Order: fast-worker -> mid-worker -> deep-worker.

When escalating, include in the next prompt: the original task, a 2-3 line summary of what the previous worker tried, and the exact failure (error output or missing item). Do not resend full transcripts.

Rules:
- Workers must put the reason in one line right after `ESCALATE:`. Pass that line on to the next worker as part of the failure summary.
- Max one attempt per tier. No retry loops on the same model.
- If deep-worker fails, stop and report to the user what was tried and what is blocking.
- If tasks of the same type escalate more than once in a session, start at the higher tier next time.

## Step 5: Report briefly

Tell the user: which tier finished the task, how many escalations happened, and the result. One or two lines.

## Effort tuning (goat mode)

Effort is tied to the worker: fast-worker has no effort setting, mid-worker uses medium, deep-worker uses high. For finer control, create extra agent files such as `deep-worker-low` (opus, effort low) and route to them for simple but high-stakes tasks, like a one-line change in a critical file.
