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

# CLI — watcher (local worker orchestrator daemon)
python -m app.cli watcher                        # respects each manifest's implementation_mode
python -m app.cli watcher --worker-mode cloud    # force cloud (Anthropic API) for all tickets
python -m app.cli watcher --worker-mode local    # force local (LiteLLM proxy + RTX 5090)
# Also: WORKER_MODE=cloud python -m app.cli watcher
# Concurrency (pools are independent — local is never starved by cloud burst):
python -m app.cli watcher --max-local-workers 1  # default 1; GPU serial bottleneck
python -m app.cli watcher --max-cloud-workers 3  # default 3; parallelisable
python -m app.cli watcher --max-workers 2        # backward-compat alias: sets both to 2


# CLI — metrics
python -m app.cli metrics browse   # open metrics DB in Datasette browser UI

# Lint and format
ruff check .
ruff format .

# Type check (required — fix errors, do not suppress with # type: ignore)
mypy app/

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
- `escalation_policy.py` — `EscalationPolicy` Pydantic model: loads `config/escalation_policy.toml`, classifies result-artifact flags and Sonar findings into watcher actions
- `linear_client.py` — thin Linear GraphQL client (stdlib `urllib` only, no third-party HTTP deps); requires `LINEAR_API_KEY` env var
- `metrics.py` — SQLite-backed store for per-ticket cost and execution metrics; watcher is sole writer, workers emit JSON result files only
- `watcher.py` — orchestrator daemon: polls Linear for `ReadyForLocal` tickets, manages git worktrees, launches worker sessions, collects result artifacts, creates PRs, updates Linear state
- `main.py` — PySide6 `QApplication` entry point

Data flows one way: UI → config model → generator → disk. Post-setup runs after generation.

---

## Engineering principles

- UI stays thin. No branching logic, no file I/O in `app/ui/`.
- Prefer config + templates over conditional generation logic.
- Generated output must be deterministic and easy to diff.
- Avoid over-abstracting v1. Three similar lines beat a premature helper.
- Side effects (git, pre-commit) live only in `post_setup.py`.
- **Architecture contracts are enforced by Import Linter (`lint-imports`).** The contracts live in `.importlinter`. Do not bypass them with `# noqa` or `--noqa`. Do not modify `.importlinter` without explicit cloud LLM approval — contract changes are architecture decisions.

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
                          # Linear: Backlog → Groomed
                          # ↓ human approves — Linear updated only after this

/start-ticket WOR-123     # PO + Architect: restate req, plan files/tests, create branch
                          # auto-creates epic branch if needed; shows parallel-safe siblings
                          # Linear: Groomed → ReadyForLocal (with execution manifest attached)
                          # ↓ human approves plan before any code is written

[watcher picks up ticket] # watcher polls for ReadyForLocal, creates worktree, launches local worker
                          # Linear: ReadyForLocal → InProgressLocal

/implement-ticket WOR-123 # local worker entrypoint: reads manifest, implements within allowed_paths,
                          # runs required_checks, writes result artifact
                          # hooks fire automatically: ruff, mypy, bandit, pytest, lint-imports

/security-check           # bandit scan + OWASP diff review → PASS / WARNINGS / FAIL

/finalize-ticket          # coverage check, docs update, PR creation
                          # PR targets epic branch (auto-merges when CI passes)
                          # Linear: InProgressLocal → MergedToEpic

/close-epic WOR-123       # when all sub-tickets are MergedToEpic: security + coverage + UI tests,
                          # create epic → main PR (human review required)
                          # Linear: epic → EpicReadyForCloudReview → MainPRReady → Done
```

### Hybrid lifecycle states

Linear workflow states for the hybrid execution model. The watcher daemon uses these as its action triggers:

| State | Set by | Meaning |
|-------|--------|---------|
| `Backlog` | default | Not yet groomed or scoped |
| `Todo` | epic kickoff | Queued in the active epic, not yet started |
| `Groomed` | `/groom-ticket` | PO has reviewed scope and AC; ready for planning |
| `ReadyForLocal` | `/start-ticket` | Execution manifest attached; watcher will pick up |
| `InProgressLocal` | watcher | Local worker session is actively running |
| `In Progress` | `/start-ticket` (cloud) | Cloud LLM is implementing directly (no local worker) |
| `In Review` | `/finalize-ticket` | PR open, awaiting CI / human review |
| `MergedToEpic` | watcher / CI | Sub-ticket PR merged to epic branch |
| `EpicReadyForCloudReview` | `/close-epic` | All sub-tickets merged; epic PR open for cloud review |
| `MainPRReady` | `/close-epic` | Epic → main PR is open awaiting human review |
| `Done` | human merge | Merged to main |

**`local-ready` label:** A tag on the ticket indicating it is safe for local LLM execution — bounded scope, no cloud-only dependencies, no sensitive credentials needed. The watcher checks for this label as a secondary signal alongside `ReadyForLocal` state. A ticket can carry `local-ready` before `/start-ticket` runs to pre-declare it as a local candidate.

**Escalation:** If the local worker fails beyond the configured retry budget, the watcher moves the ticket back to `In Progress` (cloud) and attaches an escalation artifact. See `app/core/escalation_policy.py` for the rules.

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

### CI quality gate tiers

Two-tier SonarCloud strategy:

| PR target | SonarCloud step | Blocks merge? |
|-----------|----------------|---------------|
| sub→epic  | "SonarCloud scan (informational)" — `continue-on-error: true` | No — findings logged, advisory only |
| epic→main | "SonarCloud scan" — blocking | Yes — gate must pass |

The informational scan runs on `github.base_ref != 'main'`; the blocking scan runs on `github.base_ref == 'main'`. Both use the same `SonarSource/sonarcloud-github-action@master` and the same `SONAR_TOKEN`. The sub→epic tier lets the LLM see and fix code smells cheaply before they surface as blocking findings at the epic→main gate.

---

## Claude Code hooks

`.claude/settings.json` ships with hooks that run automatically:

- **PostToolUse** — ruff lint + format after any Python file edit
- **PostToolUse** — bandit security scan after any Python file edit (if bandit is installed)
- **PostToolUse** — `lint-imports` architecture contract check after any Python file edit
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

## Escalation policy

The watcher reads `config/escalation_policy.toml` at startup to decide when to stop a local worker session and escalate to cloud LLM. Rules are data-driven — no hardcoded logic in the watcher.

**Location:** `config/escalation_policy.toml`
**Model:** `app/core/escalation_policy.py` — `EscalationPolicy.from_toml()`

Key sections:
- `[retry]` — `max_consecutive_failures`: how many consecutive check failures before escalating
- `[auto_escalate]` — flags in the result artifact that trigger automatic cloud escalation (e.g. `scope_drift`, `forbidden_path_touched`)
- `[human_escalate]` — conditions requiring a human/cloud decision (watcher posts a Linear comment and pauses)
- `[sonar]` — maps SonarLint/SonarCloud severity → action: `blocker`/`critical` → `escalate`; `major`/`minor`/`info` → `fix_locally`

To change escalation rules, edit `config/escalation_policy.toml` and commit — no code changes required.

---

## Spike workflow

Spike tickets are investigative — findings must be reviewed by a human before merging. They bypass the watcher entirely.

**Detecting a spike:** Any ticket with the **Spike** label (case-insensitive).

**`/start-ticket` behaviour:** If the Spike label is present, the command sets state to `In Progress` and prints the interactive workflow below. It does **not** write a ReadyForLocal manifest.

**`watcher` behaviour:** Any `ReadyForLocal` ticket that still carries the Spike label is skipped with a WARNING log. This is a safety net — `/start-ticket` should have caught it first.

**Interactive spike workflow:**
```bash
# 1. Create a branch (use Linear's "Copy branch name")
git checkout -b wor-NNN-spike-slug

# 2. Investigate and document findings
mkdir -p docs/spikes
# write findings to docs/spikes/<slug>.md

# 3. Commit findings
git commit -m "Part of WOR-NNN: spike findings — <topic>"

# 4. Open a PR for human review (no auto-merge)
# Run /finalize-ticket — it will open a PR targeting main (or epic branch)
# The PR requires human review before merge

# 5. After merge, close the Linear ticket manually
```

Spike PRs always require human review. Do not enable auto-merge on spike PRs.

---

## Immediate milestone

**Generate a local repository skeleton from a selected preset and write all files to disk.**
