Show a structured snapshot of a Linear ticket's current state.

### 1. Run the CLI subcommand

```bash
python -m app.cli ticket-status $1
```

Replace `$1` with the ticket ID (e.g. WOR-123). The command requires `LINEAR_API_KEY` in the environment.

Optional flags:
- `--json` — machine-readable JSON output
- `--brief` — single-line summary for status bars
- `--watch` — re-poll every 30s until the ticket reaches a terminal state (Done, MergedToEpic, Cancelled, Duplicate, Blocked)

### 2. Summarise for the user

Read the output and summarise in 2-3 sentences:
- Ticket title and current Linear state
- Whether the ticket has an active worker log, and the last tool calls seen
- Artifact status (manifest/result present or missing)
- Worktree existence
- Any health flags (API retries, subagent spawns, missing artifacts)

If `--json` was requested, parse the JSON and present the key fields in prose.

### Manual setup

This subcommand requires a Bash permission entry in `.claude/settings.json`. The operator must add the following manually — the skill file cannot write to settings files:

```json
{
  "settings.json": {
    "permissions": {
      "allow": ["Bash(python -m app.cli ticket-status*)"]
    }
  }
}
```

Or add the `Bash(python -m app.cli ticket-status*)` entry to the existing allowlist if one is present.

Without this entry, the first invocation will trigger a permission prompt; subsequent invocations will be silent once the allowlist is in place.
