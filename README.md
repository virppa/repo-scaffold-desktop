# Repo Scaffold Desktop

A Python CLI tool for generating opinionated starter repositories for agent-driven development.

## Purpose

Creates ready-to-use repository scaffolds for solo developers and small teams, with sensible defaults for CI, pre-commit, issue templates, and Claude/Linear wiring.

> Desktop GUI is planned for V2. The CLI is the primary interface for V1.

## Usage

```bash
# Basic generation
python -m app.cli generate --preset python_basic --repo-name myrepo --output ./out

# With optional file toggles
python -m app.cli generate --preset python_basic --repo-name myrepo --output ./out \
  --pre-commit --ci --pr-template --issue-templates --codeowners --claude-files \
  --playwright --linear-mcp

# With post-setup actions
python -m app.cli generate --preset python_basic --repo-name myrepo --output ./out \
  --git-init --install-precommit

# User preferences
python -m app.cli config get
python -m app.cli config set author-name "Your Name"
python -m app.cli config set github-username "your-username"

# Watcher daemon (local worker orchestrator)
python -m app.cli watcher                        # respects each manifest's implementation_mode
python -m app.cli watcher --worker-mode cloud    # force cloud (Anthropic API)
python -m app.cli watcher --worker-mode local    # force local (LiteLLM proxy)
python -m app.cli watcher --max-local-workers 8  # default 8; vLLM handles concurrency
python -m app.cli watcher --max-cloud-workers 3  # default 3
python -m app.cli watcher --verbose              # stream worker output live to stderr

# Metrics
python -m app.cli metrics browse   # open metrics DB in Datasette browser UI

# Show all options
python -m app.cli generate --help
```

## Running locally

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pre-commit install
```

## Architecture

```
app/core/      # All business logic — no UI here
app/ui/        # PySide6 only — calls core, contains no logic
templates/     # Jinja2 template files for scaffold output
tests/         # Tests against core only
schemas/       # Exported JSON Schemas for non-Python consumers
config/        # escalation_policy.toml + bench-*.toml run configs
docs/spikes/   # Spike investigation docs
scripts/bench/ # Benchmark runner CLI and helpers
```

Module responsibilities:

- `config.py` — Pydantic input models and validation
- `presets.py` — preset definitions (maps preset name → file list + toggles)
- `generator.py` — renders templates and writes files to disk
- `post_setup.py` — side effects: `git init`, `pre-commit install`
- `user_prefs.py` — `UserPreferences` model + `PrefsStore` (platform-aware JSON persistence)
- `manifest.py` — `ExecutionManifest` Pydantic model: cloud→local worker contract
- `watcher.py` — orchestrator only: polls Linear for `ReadyForLocal` tickets, delegates to sub-modules
- `watcher_finalize.py` — worker finalization: outcome classification, PR creation, escalation
- `watcher_subprocess.py` — worker subprocess lifecycle, checks, Sonar integration
- `watcher_worktrees.py` — git worktree setup, teardown, artifact preservation
- `watcher_helpers.py` — pure stateless helpers used by watcher sub-modules
- `watcher_services.py` — LiteLLM proxy and Ollama process management
- `linear_client.py` — thin Linear GraphQL client (stdlib `urllib` only); requires `LINEAR_API_KEY`
- `metrics.py` — SQLite-backed per-ticket cost and execution metrics store
- `bench_store.py` — SQLite-backed benchmark run records store (`bench.db`)
- `escalation_policy.py` — loads `config/escalation_policy.toml`, classifies failures into watcher actions
- `cli.py` — CLI entry point
- `main.py` — PySide6 app entry point (V2)

Data flows one way: CLI → config model → generator → disk. Post-setup runs after generation.

## Available presets

| Preset | Description |
|--------|-------------|
| `python_basic` | Minimal Python project with tests and tooling |
| `python_desktop` | Python project with PySide6 desktop app structure |
| `full_agentic` | Full agentic repo with Claude, Linear, and CI wiring |

## CLI toggles

| Flag | Effect |
|------|--------|
| `--pre-commit` | Include `.pre-commit-config.yaml` |
| `--ci` | Include GitHub Actions CI workflow |
| `--pr-template` | Include pull request template |
| `--issue-templates` | Include bug report and feature request templates |
| `--codeowners` | Include `CODEOWNERS` file |
| `--claude-files` | Include `CLAUDE.md` and `.mcp.json` |
| `--playwright` | Include Playwright browser-test scaffold (`full_agentic` only) |
| `--linear-mcp` / `--no-linear-mcp` | Include/exclude Linear MCP server in `.mcp.json` |
| `--git-init` | Run `git init` in the output directory after generation |
| `--install-precommit` | Run `pre-commit install` in the output directory |

## Stack

- Python 3.12+
- Pydantic — config validation
- Jinja2 — template rendering
- PyYAML — YAML parsing
- PySide6 — desktop UI (V2)
- pytest + pytest-cov — testing
- Ruff — linting and formatting
- mypy — type checking
- bandit — security scanning
- Import Linter — architecture contract enforcement
- pre-commit — git hooks

## Local model development

To run Claude Code against a local vLLM server instead of the Anthropic API:

```bash
# 1. Start vLLM server in WSL2 (keep terminal open)
/home/antti/vllm-env/bin/vllm serve /home/antti/models/Qwen3.6-35B-A3B-NVFP4 \
  --max-model-len 262144 --max-num-seqs 16 \
  --kv-cache-dtype fp8 --max-num-batched-tokens 4096 \
  --reasoning-parser qwen3 --enable-prefix-caching \
  --language-model-only --safetensors-load-strategy prefetch \
  --enable-auto-tool-choice --tool-call-parser qwen3_coder

# 2. Copy the example config and start LiteLLM proxy (keep terminal open)
cp litellm-local.yaml.example litellm-local.yaml
litellm --config litellm-local.yaml --port 8082 --drop_params

# 3. Launch Claude Code in a new terminal
set ANTHROPIC_BASE_URL=http://localhost:8082   # Windows
set ANTHROPIC_API_KEY=sk-dummy
claude --model qwen3-coder:30b
```

`litellm-local.yaml` is gitignored. See [`docs/spikes/local-model-setup.md`](docs/spikes/local-model-setup.md) for VRAM budget, model selection, and benchmark results.

## Claude Code and MCP setup

This repo ships with `.mcp.json` configured to use the [Linear MCP server](https://linear.app/docs/mcp), allowing Claude Code agents to read Linear issues directly.

To authenticate on first use, run `/mcp` in Claude Code and follow the OAuth flow. Only interact with the **repo-scaffold-desktop** project in Linear.

## Git and Linear workflow

- Branch names come from Linear (copy-branch-name) — no custom prefixes
- PR title format: `WOR-123 Short description`
- Intermediate commits: `Part of WOR-123 …`
- Closing commit or PR body: `Closes WOR-123`
- Sub-ticket PRs target the epic branch and auto-merge when CI passes
- Epic PRs target main and always require human review

## License

TBD
