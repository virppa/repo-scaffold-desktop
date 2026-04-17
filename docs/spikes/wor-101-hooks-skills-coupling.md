# WOR-101 Spike: Hooks and Skills Coupling in Scaffolded Repos

**Question:** Do the Claude Code hooks (`.claude/settings.json`) and skills (`.claude/commands/`) in this repo need to be replicated into repos scaffolded by the `full_agentic` preset?

## Findings

### Hooks

The source repo's `.claude/settings.json` defines:
- 5 × PostToolUse hooks: ruff lint+format, bandit, lint-imports, mypy, pytest with coverage
- 2 × PreToolUse guards: blocks destructive shell commands and writes to sensitive files
- 1 × Stop hook: `pre-commit run --all-files`

The `full_agentic` preset template (`templates/full_agentic/.claude/settings.json.j2`) generates only an empty stub:

```json
{"permissions": {"allow": [], "deny": []}}
```

No hooks are included. A scaffolded `full_agentic` repo gets no quality-gate automation.

Additionally, `bandit`, `mypy`, and `lint-imports` are not in the scaffolded `requirements-dev.txt` template, so even if hooks were copied, they would fail for those tools.

### Skills (slash commands)

Six `.claude/commands/*.md` files exist in this repo: `groom-ticket`, `start-ticket`, `implement-ticket`, `security-check`, `finalize-ticket`, `close-epic`. No template equivalents exist — scaffolded repos receive no slash commands.

**WOR-82 covers this gap** via a `repo-scaffold-skills` standalone repo model: scaffolded repos will reference the skills repo rather than copying command files. This avoids frozen copies that drift. WOR-82 is Todo but scoped correctly.

## Gap Summary

| Item | Source repo | Scaffolded `full_agentic` |
|------|-------------|--------------------------|
| PostToolUse hooks (ruff, bandit, mypy, lint-imports, pytest) | ✅ | ❌ Empty stub |
| PreToolUse guards | ✅ | ❌ |
| Stop hook (pre-commit) | ✅ | ❌ |
| Tool deps (bandit, mypy, lint-imports) | ✅ | ❌ |
| Slash commands (6 skills) | ✅ | ❌ — covered by WOR-82 |

## Recommendation

- **Skills**: No action needed beyond WOR-82 (reference-repo model is the right approach).
- **Hooks**: New sub-ticket needed. The `settings.json.j2` template must be populated with the quality-gate hooks, and the scaffolded `requirements-dev.txt` must include `bandit`, `mypy`, and `lint-imports`. Hooks are project tooling (not methodology), so direct inclusion in the template is appropriate — unlike skills, they don't drift in the same way.

Follow-up: **WOR-102** — Populate `full_agentic` `settings.json.j2` with quality-gate hooks.
