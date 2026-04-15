# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup
python -m venv .venv
pip install -r requirements-dev.txt

# Run app
python -m app.main

# CLI — scaffold
python -m app.cli generate --preset python_basic --repo-name myrepo --output ./out
# With optional toggles: --pre-commit --ci --pr-template --issue-templates --codeowners --claude-files
# With post-setup:       --git-init --install-precommit

# CLI — user preferences
python -m app.cli config get
python -m app.cli config set author-name "Your Name"
python -m app.cli config set github-username "your-username"

# Lint and format
ruff check .
ruff format .

# Tests
pytest
pytest tests/test_generator.py::test_name  # single test

# Pre-commit
pre-commit run --all-files
pre-commit install
```

---

## Architecture

```
app/core/      # All business logic — no UI here
app/ui/        # PySide6 only — calls core, contains no logic
templates/     # Jinja2 template files for scaffold output
tests/         # Tests against core only
schemas/       # Exported JSON Schemas for non-Python consumers
docs/spikes/   # Spike investigation docs
```

Module responsibilities:
- `config.py` — Pydantic input models (repo name, output path, preset, option toggles)
- `presets.py` — preset definitions (maps preset name → file list + options)
- `generator.py` — renders templates and writes files to disk
- `post_setup.py` — side effects: `git init`, `pre-commit install`, etc.
- `user_prefs.py` — `UserPreferences` model + `PrefsStore` (platform-aware JSON persistence)
- `manifest.py` — `ExecutionManifest` Pydantic model: cloud→local worker contract for hybrid execution
- `main.py` — PySide6 `QApplication` entry point

Data flows one way: UI → config model → generator → disk. Post-setup runs after generation.

---

## Engineering principles

- UI stays thin. No branching logic, no file I/O in `app/ui/`.
- Prefer config + templates over conditional generation logic.
- Generated output must be deterministic and easy to diff.
- Avoid over-abstracting v1. Three similar lines beat a premature helper.
- Side effects (git, pre-commit) live only in `post_setup.py`.

---

## Current priorities

1. Generator logic working end-to-end
2. Presets clean and easy to extend
3. Minimal but usable PySide6 UI
4. Optional post-setup actions (git init, pre-commit install)

Do not jump ahead to integrations until the current layer works.

---

## V1 toggles

Good early options to expose in UI: pre-commit, CI workflow, PR template, issue templates, CODEOWNERS, Claude files. Keep the toggle list short.

---

## Development workflow

Each ticket follows these phases. Use the corresponding slash command to enter each phase:

```
/groom-ticket WOR-123     # PO review: scope, acceptance criteria, splitting
                          # ↓ human approves — Linear updated only after this

/start-ticket WOR-123     # PO + Architect: restate req, plan files/tests, create branch
                          # auto-creates epic branch if needed; shows parallel-safe siblings
                          # ↓ human approves plan before any code is written

[Claude implements]       # hooks fire automatically: ruff, bandit, pytest

/security-check           # bandit scan + OWASP diff review → PASS / WARNINGS / FAIL

/finalize-ticket          # coverage check, docs update, PR creation, Linear → In Review
                          # PR targets epic branch (auto-merge) or main (human review)

/close-epic WOR-123       # when all sub-tickets are Done: security + coverage + UI tests,
                          # create epic → main PR (human review required)
```

### Branch topology

```
main
└── wor-49-template-system          ← epic branch (created by first /start-ticket in epic)
    ├── wor-45-add-yaml-preset      ← sub-ticket branch → auto-merges to epic when CI passes
    └── wor-47-jinja-context-fix    ← parallel sub-ticket → its own worktree, isolated
```

### Parallel work

`/start-ticket` checks Linear for other In-Progress tickets in the same epic and flags file-safe parallel candidates. To work in parallel: open a second Claude Code session in the same repo directory and run `/start-ticket WOR-NN` for a candidate ticket. Each session enters its own isolated git worktree.

Human gates: plan approval after `/start-ticket`; explicit PASS from `/security-check` before any main-targeting PR; human review of the epic → main PR created by `/close-epic`. Command files live in `.claude/commands/`.

---

## Claude Code hooks

`.claude/settings.json` ships with hooks that run automatically:

- **PostToolUse** — ruff lint + format after any Python file edit
- **PostToolUse** — bandit security scan after any Python file edit (if bandit is installed)
- **PostToolUse** — pytest with coverage after changes to `app/` or `tests/`
- **Stop** — `pre-commit run --all-files` at the end of every turn
- **PreToolUse** — blocks destructive shell commands and writes to sensitive files (`.env`, `.mcp.json`, `.claude/settings*`)

No setup needed — hooks activate as soon as Claude Code loads the project.

---

## Local model development

To run Claude Code routed to a local model (Ollama) instead of the Anthropic API:

```bash
# 1. Copy the example config and start LiteLLM proxy (keep terminal open)
cp litellm-local.yaml.example litellm-local.yaml
litellm --config litellm-local.yaml --port 8082 --drop_params

# 2. Launch Claude Code in a new terminal
set ANTHROPIC_BASE_URL=http://localhost:8082   # Windows
set ANTHROPIC_API_KEY=sk-dummy
claude --model qwen3-coder:30b
```

`litellm-local.yaml` is gitignored. See `docs/spikes/local-model-setup.md` for VRAM budget, model selection, and benchmark results.

---

## Linear MCP

This repo ships with `.mcp.json` configured to use the Linear MCP server. Claude Code agents can use this to read Linear issues directly — no manual copy-pasting needed.

On first use, run `/mcp` in Claude Code to authenticate via OAuth.

Only interact with the **repo-scaffold-desktop** project in Linear unless explicitly told otherwise.

---

## Git and Linear workflow

- Use branch names generated by Linear (copy-branch-name). Do not add `feat/` or `fix/` prefixes.
- PR title format: `WOR-123 Short description`
- Intermediate commits: `Part of WOR-123 ...`
- Closing commit or PR body: `Closes WOR-123`
- Sub-ticket PRs target the epic branch and auto-merge when CI passes — no manual approval needed
- Epic PRs target main and always require human review

---

## Testing

Test core logic only. Priority: config validation, preset selection, file generation, option toggles, overwrite behavior. Skip UI tests unless the UI contains meaningful logic.

---

## Immediate milestone

**Generate a local repository skeleton from a selected preset and write all files to disk.**
