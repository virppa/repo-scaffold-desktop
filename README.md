# Repo Scaffold Desktop

A Python CLI tool for generating opinionated starter repositories for agent-driven development.

## Purpose

This project helps create ready-to-use repository scaffolds for solo developers and small teams.

The tool makes it faster to start a new project with sensible defaults such as:

- pre-commit setup
- GitHub Actions CI
- PR and issue templates
- CODEOWNERS
- Claude project files
- starter folder structures
- preset-based repository templates

## Usage

> Note: Desktop GUI is planned for V2. The CLI is the primary interface for V1.

```bash
# Basic generation
python -m app.cli generate --preset python_basic --repo-name myrepo --output ./out

# With optional file toggles
python -m app.cli generate --preset python_basic --repo-name myrepo --output ./out \
  --pre-commit --ci --pr-template --issue-templates --codeowners --claude-files

# With post-setup actions
python -m app.cli generate --preset python_basic --repo-name myrepo --output ./out \
  --git-init --install-precommit

# Show all options
python -m app.cli generate --help
```

## Running locally

```bash
# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements-dev.txt

# Install pre-commit hooks
pre-commit install
```

## Architecture

The project is split into clear layers:

- `app/core/` — all business logic: config validation, preset handling, file generation, post-setup
- `app/ui/` — PySide6 desktop UI (deferred to V2; CLI is V1 primary interface)
- `templates/` — Jinja2 template files for scaffold output
- `tests/` — tests for core logic only

Module responsibilities:

- `config.py` — Pydantic input models and validation
- `generator.py` — renders templates and writes files to disk
- `presets.py` — preset definitions (maps preset name → file list + toggles)
- `post_setup.py` — side effects: `git init`, `pre-commit install`
- `cli.py` — argparse CLI entry point
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
| `--git-init` | Run `git init` in the output directory after generation |
| `--install-precommit` | Run `pre-commit install` in the output directory |

## Engineering principles

- UI stays thin — no branching logic or file I/O in `app/ui/`
- Prefer config + templates over conditional generation logic
- Generated output must be deterministic and easy to diff
- Side effects (git, pre-commit) live only in `post_setup.py`
- Avoid over-abstracting V1

## Stack

- Python 3.12+
- Pydantic — config validation
- Jinja2 — template rendering
- PyYAML — YAML parsing
- PySide6 — desktop UI (V2)
- pytest + pytest-cov — testing
- Ruff — linting and formatting
- bandit — security scanning
- pre-commit — git hooks

## Project structure

```
repo-scaffold-desktop/
├─ app/
│  ├─ cli.py
│  ├─ main.py
│  ├─ core/
│  │  ├─ config.py
│  │  ├─ generator.py
│  │  ├─ post_setup.py
│  │  └─ presets.py
│  └─ ui/
│     └─ main_window.py
├─ templates/
│  ├─ python_basic/
│  ├─ python_desktop/
│  └─ full_agentic/
├─ tests/
│  ├─ test_cli.py
│  ├─ test_config.py
│  ├─ test_generator.py
│  ├─ test_post_setup.py
│  └─ test_presets.py
├─ .github/
│  ├─ workflows/
│  │  ├─ lint-and-test.yml
│  │  └─ claude-code-review.yml
│  ├─ ISSUE_TEMPLATE/
│  └─ pull_request_template.md
├─ CLAUDE.md
├─ pyproject.toml
└─ README.md
```

## Local model development

To run Claude Code against a local model via Ollama instead of the Anthropic API:

```bash
# 1. Pull the model
ollama pull qwen3-coder:30b

# 2. Copy the example config and start LiteLLM proxy (keep terminal open)
cp litellm-local.yaml.example litellm-local.yaml
litellm --config litellm-local.yaml --port 8082 --drop_params

# 3. Launch Claude Code in a new terminal
set ANTHROPIC_BASE_URL=http://localhost:8082   # Windows
set ANTHROPIC_API_KEY=sk-dummy
claude --model qwen3-coder:30b
```

`litellm-local.yaml` is gitignored. See [`docs/spikes/local-model-setup.md`](docs/spikes/local-model-setup.md) for VRAM requirements, benchmark results, and go/no-go recommendation.

## Claude Code and MCP setup

This repo ships with `.mcp.json` configured to use the [Linear MCP server](https://linear.app/docs/mcp), allowing Claude Code agents to read Linear issues directly.

To authenticate on first use, run `/mcp` in Claude Code and follow the OAuth flow.

## Git and Linear workflow

- Branch names come from Linear (copy-branch-name) — no custom prefixes
- PR title format: `WOR-123 Short description`
- Intermediate commits: `Part of WOR-123 …`
- Closing commit or PR body: `Closes WOR-123`

## License

TBD
