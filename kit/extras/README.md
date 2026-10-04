# Extras

Optional files that the installer does **not** install by default.

## deep-worker-low

`agents/deep-worker-low.md` is an Opus worker with `effort: low`. Use it for simple but high-stakes changes, such as a one-line edit in a critical file.

Enable it by copying it into your agents directory, then restart Claude Code:

```bash
cp kit/extras/agents/deep-worker-low.md ~/.claude/agents/
```

For a single project, copy it to `<project>/.claude/agents/` instead. To remove it, delete the copy. TokenTier's uninstaller does not know about it, because it was not installed by TokenTier.

The `model-router` skill never routes to it on its own. Ask for it by name ("use deep-worker-low for this").
