# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup
python -m venv .venv
pip install -r requirements-dev.txt

# Run app
pip install -r requirements-ui.txt
python -m app.main

# CLI — scaffold
python -m app.cli generate --preset python_basic --repo-name myrepo --output ./out
# With optional toggles: --pre-commit --ci --pr-template --issue-templates --codeowners --claude-files
#                        --playwright (full_agentic only) --linear-mcp / --no-linear-mcp
# With post-setup:       --git-init --install-precommit
# With GitHub:           --github-create [--private | --public]    # create repo via API
#                        --git-push [--remote-url <url>]            # initial commit + push

# CLI — user preferences
python -m app.cli config get
python -m app.cli config set author-name "Your Name"
python -m app.cli config set github-username "your-username"
python -m app.cli config set github-token        # silent prompt; stored in OS keyring
python -m app.cli config delete github-token

# CLI — watcher (local worker orchestrator daemon)
python -m app.cli watcher                        # respects each manifest's implementation_mode
python -m app.cli watcher --worker-mode cloud    # force cloud (Anthropic API) for all tickets
python -m app.cli watcher --worker-mode local    # force local (LiteLLM proxy + RTX 5090)
# Also: WORKER_MODE=cloud python -m app.cli watcher
# Auto-loads .env from cwd (WOR-435) — LINEAR_API_KEY etc. inherited automatically
python -m app.cli watcher --detach               # spawn detached daemon; parent exits; logs → .claude/watcher.log
python -m app.cli watcher --visible              # Windows only: open new cmd.exe window with watcher attached
# Concurrency (pools are independent — local is never starved by cloud burst):
python -m app.cli watcher --max-local-workers 8  # default 8; vLLM handles concurrency
python -m app.cli watcher --max-cloud-workers 3  # default 3; parallelisable
python -m app.cli watcher --max-workers 2        # backward-compat alias: sets both to 2
python -m app.cli watcher --max-concurrent-checks 4  # default max(local//2, 2); parallel finalize-checks (WOR-451)
python -m app.cli watcher --verbose              # stream worker stdout/stderr live, prefixed with [WOR-NN]
python -m app.cli watcher --no-epic-shutdown     # keep daemon running after current epic completes
# Smoke test (5s): LINEAR_API_KEY=dummy python -m app.cli watcher --no-epic-shutdown
#   (timeout 5 python -m app.cli watcher --no-epic-shutdown; exit 0 if killed by timeout)

# Benchmark runner (do not run without explicit instruction)
python scripts/bench/run_bench.py --tier speed
python scripts/bench/run_bench.py --resume run_20240101_120000
python scripts/bench/run_bench.py --compare run_20240101 run_20240102
python scripts/bench/run_bench.py --generate-fixtures
python scripts/bench/run_bench.py --browse       # open bench.db in Datasette

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
config/        # escalation_policy.toml + bench-*.toml run configs
scripts/bench/ # run_bench.py CLI entry point + runner/fixtures helpers
```

Module responsibilities:
- `config.py` — Pydantic input models (repo name, output path, preset, option toggles)
- `presets.py` — preset definitions (maps preset name → file list + options)
- `generator.py` — renders templates and writes files to disk
- `post_setup.py` — side effects: `git init`, `pre-commit install`, GitHub repo create/configure/push (`create_github_repo`, `configure_github_repo`, `run_initial_push`)
- `user_prefs.py` — `UserPreferences` model + `PrefsStore` (platform-aware JSON persistence)
- `credentials.py` — GitHub token storage via OS keyring (`save_token`, `get_token`, `delete_token`); falls back to `GITHUB_TOKEN` env var when keyring unavailable
- `manifest.py` — `ExecutionManifest` Pydantic model: cloud→local worker contract; includes `effort` (low/medium/high/xhigh/max) and 7 taxonomy fields (`change_type`, `reasoning_demand`, `scope_clarity`, `constraint_density`, `ac_specificity`, `tech_stack`, `raw_extensions`) populated at /start-ticket plan time
- `escalation_policy.py` — `EscalationPolicy` Pydantic model: loads `config/escalation_policy.toml`, classifies result-artifact flags and Sonar findings into watcher actions
- `linear_client.py` — thin Linear GraphQL client (stdlib `urllib` only, no third-party HTTP deps); requires `LINEAR_API_KEY` env var
- `metrics.py` — SQLite-backed store for per-ticket cost and execution metrics; watcher is sole writer, workers emit JSON result files only. Tables: `ticket_metrics` (per-ticket summary, includes 7 taxonomy + cost columns), `ticket_run_log` (per-attempt records for retry analysis), `check_run_log` (per-check timing/outcome)
- `wizard.py` — interactive `generate --interactive` wizard; pre-fills from `UserPreferences`, walks the operator through preset + toggle selection, prints a completion summary
- `watcher/` — watcher subpackage (subpackage boundary, not flat files):
  - `watcher.py` — orchestrator only: polls Linear for `ReadyForLocal` tickets, delegates to sub-modules above
  - `watcher_dispatch.py` — extracted ticket start logic: `start_ticket` module function plus thin class wrappers
  - `watcher_finalize.py` — worker finalization public API: `finalize_worker`, `attempt_pr`, `safe_set_state`
  - `watcher_finalize_helpers.py` — internal helpers extracted from `watcher_finalize.py` (WOR-404): `_read_result_status`, `_execute_finalization`, `_handle_policy_outcome`, `_sonar_requires_escalation`, `_write_wip_sha_to_last_failure`, `_try_post_comment`
  - `watcher_helpers.py` — pure stateless functions: `check_allowed_paths_overlap`, `build_worker_env`, `build_worker_cmd`, `resolve_effective_mode`, `_tee_worker_output`
  - `watcher_log_parsing.py` — worker JSONL/log parsers extracted from `watcher_helpers.py` (WOR-403): `_parse_worker_usage`, `_parse_worker_subagent_spawns`, `_parse_worker_api_retries`, `format_token_count`, `format_elapsed`, `format_worker_token_count`
  - `watcher_services.py` — `ServiceManager` class: vLLM readiness gate (probe + ensure-Anthropic-mode); no longer spawns subprocesses post-WOR-368
  - `watcher_signals.py` — signals/softstop/lifecycle functions extracted from `watcher.py` (WOR-401): `register_signals`, `handle_signal`, `wait_for_active_workers`, PID file helpers, softstop sentinel handling, `cleanup_orphaned_worktrees`
  - `watcher_subprocess.py` — worker subprocess lifecycle: `launch_worker`, `run_checks`, `fetch_sonar_findings`, `create_pr`, `build_snippet_tool_restrictions`
  - `watcher_tui.py` — `WatcherDisplay` rich-based live TUI: worker table, cost panel, PR auto-merge tracker; activated with `--tui` flag (WOR-272)
  - `watcher_types.py` — constants, `LinearClientProtocol`, `ActiveWorker` dataclass, `is_watcher_running`, `_to_metrics_mode`
  - `watcher_worktrees.py` — git worktree lifecycle: `create_worktree`, `rebase_worktree_from_base`, `copy_manifest_to_worktree`, `preserve_worker_artifacts`, `cleanup_worktree`, `cleanup_orphaned_worktrees`, `backup_plan_files`, `restore_plan_files`, `write_worker_pytest_config`
  - `worker_waste.py` — post-hoc waste-score analysis on worker JSONL logs; `compute_waste_score` flags redundant reads, suppressed loops, and tool-gap patterns; surfaced in `ticket_metrics.waste_score` for retro analysis
- `watcher.py` dispatch loop: multi-dispatch-per-cycle — on each poll cycle the watcher dispatches up to `MAX_DISPATCHES_PER_CYCLE=4` eligible tickets with a `DISPATCH_DELAY_SECONDS=2.5` inter-dispatch gap, saturating the local pool faster than the prior single-dispatch model. Epic-branch overlap gate (WOR-419) blocks dispatch to a new epic/* branch when another is already in-flight.
- `bench_store.py` — `BenchRun` Pydantic model + `BenchStore`: SQLite-backed append-only store for benchmark run records; shares the same `app.db` file as `metrics.py` (separate `bench_run` table); stores hardware/timing/quality columns per run
- `main.py` — PySide6 `QApplication` entry point

Data flows one way: UI → config model → generator → disk. Post-setup runs after generation.

### Schema philosophy

One SQLite file. No gold tables, no marts, no star schemas.

`app.db` holds everything: ticket metrics, run logs, check logs, benchmark runs.
Each domain has its own table. There are no cross-table foreign keys, no views,
no materialized summaries. If you need a join, write a query — don't build a
pipeline to maintain it.

**Bronze/Silver is implicit.** Raw rows are the source of truth. Aggregations
(epic summaries, cost rollups) are computed on read via SQL queries in the
store class. They are never persisted as separate tables. If a query is run
frequently enough to warrant caching, add it as a method — not a table.

**No schema evolution beyond column additions.** ALTER TABLE ADD COLUMN is the
only migration strategy. Never drop columns, rename tables, or restructure rows.
The `_migrate()` method in each store class checks `PRAGMA table_info()` and
adds what's missing. Old columns stay forever.

**One file, separate tables.** `metrics.py` owns `ticket_metrics`,
`ticket_run_log`, `check_run_log`. `bench_store.py` owns `bench_run`. Both
write to the same `app.db` path. No module-level coupling between the two —
they share a file, not an import. The `.importlinter` contract
`bench-store-not-in-watcher` remains valid: file-level coupling is fine;
module-level imports between `metrics` and `bench_store` are not.

---

## Engineering principles

- UI stays thin. No branching logic, no file I/O in `app/ui/`.
- Prefer config + templates over conditional generation logic.
- Generated output must be deterministic and easy to diff.
- Avoid over-abstracting v1. Three similar lines beat a premature helper.
- Side effects (git, pre-commit) live only in `post_setup.py`.
- **Architecture contracts are enforced by Import Linter (`lint-imports`).** The contracts live in `.importlinter`. Do not bypass them with `# noqa` or `--noqa`. Do not modify `.importlinter` without explicit cloud LLM approval — contract changes are architecture decisions.

---

## Current focus

The hybrid execution engine is the primary delivery vehicle: the watcher daemon dispatches groomed Linear tickets to local workers (vLLM-served Qwen3.6-35B-A3B-NVFP4) by default, with cloud (Anthropic API) as fallback for tickets that can't run locally. Active milestone: **Watcher v3 — routing & cost economics** (~67% as of 2026-05-10).

V1 scaffolder priorities (generator logic, presets, PySide6 UI) are largely shipped. The codebase has matured into a self-improving build agent platform. UI work is gated behind the **Wizard CLI** milestone (currently 17%) — interactive CLI substitute for the GUI; PySide6 desktop GUI sits behind that at 11%.

Recent capability shifts that inform planning (full notes in `~/.claude/projects/.../memory/`):

- **Concurrent worker dispatch** (WOR-410): the `__init__.py` overlap carve-out lets package-split waves run multiple workers in parallel against the same epic. Sustained 2-worker concurrency hits ~190 tok/s aggregate vs ~27 tok/s solo (~7× total throughput). The win is duty-cycle, not raw GPU — see `project_vllm_concurrent_throughput.md`.
- **preserve_thinking** (WOR-400): CoT preservation across worker turns is the production default in canonical commands AND watcher auto-start path. Verified across a production wave with no quality regression.
- **Workers default to facade-style splits** (memory: `project_split_pattern_facade_default.md`): qwen3-coder splits to sibling modules without touching `__init__.py` even when the manifest allows it. Concurrent split-ticket dispatch is safer than the WOR-410 carve-out's risk model assumed.
- **Multi-shell argv pattern** (WOR-415, memory: `reference_multishell_quoting_use_script_file.md`): when content has to traverse `subprocess → wt.exe → wsl → bash`, write it to disk and run `bash <script>`. Inline literals and `$(cat …)` substitution both fail; only on-disk scripts read by bash directly survive.

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

### Bulk skills

Two skills operate on epics rather than single tickets:

- `/start-epic WOR-NNN` — batch-plan all groomed sub-tickets of an existing epic, file-conflict detection, queue Batch 1 as ReadyForLocal and Batch 2+ as WaitingForDeps. Use when an epic has 3-8 sub-tickets that need to be queued for the watcher.
- `/prepare-overnight-epic` — auto-mine 20-30 single-bound parallel-safe candidates from existing Linear backlog + SonarQube findings, create a fire-and-forget mega-epic, queue all as ReadyForLocal. Use to fill the watcher with mechanical fixes for an unattended overnight run. Two operator gates (candidate-list approval + launch confirmation) before any worker dispatches. Per-ticket failures are accepted losses; morning workflow is `/close-epic` → epic→main PR with whatever shipped.

**Coordination bundles still use the sub→epic→main shipping pattern (WOR-438).** When a bulk skill umbrellas sub-tickets from different Linear parents into one dispatch unit ("coordination bundle"), the bundle is the *dispatch mechanism* — not the *shipping unit*. The shipping pattern is universal: sub-ticket PRs target the epic branch and auto-merge; one epic→main PR is the human review surface. Bundle descriptions that say "PRs target main directly" or "this is a coordination epic, not shipping" are footguns; ignore that language and always create the epic branch in step 2. Surfaced live by WOR-434 where the executor followed such language and produced 10 main-targeting PRs instead of 1.

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

**In-dispatch retry:** When a local worker fails quality checks, the watcher retries the same dispatch cycle by re-launching the worker with a RETRY hint — avoiding the slower Blocked → ReadyForLocal Linear state transition. Maximum 1 retry per dispatch (hard-capped), controlled by the manifest's `failure_policy.max_retries` (0 = no retry).

**`local-ready` label:** A tag on the ticket indicating it is safe for local LLM execution — bounded scope, no cloud-only dependencies, no sensitive credentials needed. The watcher checks for this label as a secondary signal alongside `ReadyForLocal` state. A ticket can carry `local-ready` before `/start-ticket` runs to pre-declare it as a local candidate.

**Escalation:** If the local worker fails beyond the configured retry budget, the watcher moves the ticket back to `In Progress` (cloud) and attaches an escalation artifact. See `app/core/escalation_policy.py` for the rules.

### Branch topology

```
main
└── epic/wor-49-template-system     ← epic branch (created by first /start-ticket in epic)
    ├── wor-45-add-yaml-preset      ← sub-ticket branch → auto-merges to epic when CI passes
    └── wor-47-jinja-context-fix    ← parallel sub-ticket → its own worktree, isolated
```

**Cross-epic principle:** Linear parentId describes the ticket; git base_branch
describes the shipping unit. They can diverge (WOR-419). A ticket whose Linear
parent is epic A can ship on epic B's branch when epic B is the one currently
active in-flight. The `/start-ticket` architect detects this via Linear status
queries and defaults base_branch to the active epic branch.

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

- **PostToolUse** — `.claude/hooks/posttooluse_parallel.py` runs ruff lint+format first (sequential, mutator phase) then mypy + bandit + lint-imports concurrently (WOR-463). Cuts per-edit wall from ~0.6s warm / ~5.4s cold to ~0.3s warm / ~2.4s cold (2× speedup).
- **PostToolUse** — pytest (no coverage, no xdist) on the edited test file after changes to `tests/` files only
- **PreToolUse** — blocks destructive shell commands and writes to sensitive files (`.env`, `.mcp.json`, `.claude/settings*`)

`.pre-commit-config.yaml` uses a **tiered split** — fast checks at `pre-commit`, slow checks at `pre-push` — to keep local commit latency under 3 seconds. The latency investigation (WOR-242) measured per-hook durations on a clean tree: semgrep 7.5s, mypy 1.35s, detect-secrets 1.12s, bandit 0.41s, the rest <0.5s. CI runs the full set plus pytest, deptry, and SonarCloud.

**Fast tier (pre-commit, total ~1.5s on clean tree):** trailing-whitespace, end-of-file-fixer, check-yaml, check-toml, check-merge-conflict, ruff, ruff-format, bandit, check-patch-paths, lint-imports, detect-secrets, check-file-sizes.

**Slow tier (pre-push):** mypy 1.35s, semgrep 7.5s.

**PostToolUse hooks** (Claude Code only): a single consolidated runner (`.claude/hooks/posttooluse_parallel.py`) executes ruff lint+format first (mutator phase, sequential) then mypy + bandit + lint-imports concurrently after any Python file edit; pytest on edited test files. These are separate from the pre-commit config — they run per-tool-use to give immediate feedback during implementation.

---

## Local model development

vLLM 0.20.0 serves the Anthropic Messages API natively (`/v1/messages`), so Claude Code talks to it directly with no proxy in the path (the LiteLLM proxy was retired in WOR-368; spike findings: `docs/spikes/wor-344-vllm-native-anthropic-api.md`).

```bash
# 1. Start vLLM server in WSL2 (keep terminal open)
/home/antti/vllm-env/bin/vllm serve /home/antti/models/Qwen3.6-35B-A3B-NVFP4 \
  --served-model-name qwen3-coder \
  --max-model-len 262144 --max-num-seqs 16 \
  --kv-cache-dtype fp8 --max-num-batched-tokens 4096 \
  --reasoning-parser qwen3 --enable-prefix-caching \
  --language-model-only --safetensors-load-strategy prefetch \
  --enable-auto-tool-choice --tool-call-parser qwen3_coder \
  --default-chat-template-kwargs '{"preserve_thinking": true}'

# 2. Launch Claude Code in a new terminal (Windows / cmd.exe)
set ANTHROPIC_BASE_URL=http://localhost:8000
set ANTHROPIC_API_KEY=dummy
set ANTHROPIC_AUTH_TOKEN=dummy
set ANTHROPIC_DEFAULT_OPUS_MODEL=qwen3-coder
set ANTHROPIC_DEFAULT_SONNET_MODEL=qwen3-coder
set ANTHROPIC_DEFAULT_HAIKU_MODEL=qwen3-coder
claude
```

The three `ANTHROPIC_DEFAULT_*_MODEL` env vars route by tier (Opus / Sonnet / Haiku) — Claude Code substitutes them when `--model` is not passed. **Do not pass `--model claude-sonnet-4-6`** unless you also serve that name via `--served-model-name qwen3-coder claude-sonnet-4-6 claude-opus-4-7 claude-haiku-4-5-20251001` (the flag accepts a list, lets `--model` continue to work for muscle memory).

See `docs/spikes/vllm-benchmark-plan.md` for the production model config. `docs/spikes/local-model-setup.md` covers the original Ollama setup (historical).

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

## Worker efficiency

Rules for local worker sessions (watcher-spawned `claude` processes). Each tool call is a ~40s round-trip — minimising call count directly reduces wall time.

**Emit independent tool calls in parallel (WOR-387).** When multiple tool calls don't depend on each other's results, emit ALL the `tool_use` blocks in ONE assistant message. The runtime executes them in parallel and returns all `tool_result` blocks in one user turn. A serial 4-Read sequence pays the turn-boundary cost (prefill + decode warmup, 10-30s on long context) 4 times; one parallel 4-Read pays it once. Only serialize when a later call's input genuinely depends on an earlier result (e.g. "Read the file we just located via Glob"). The investigation phase before the first edit is the highest-leverage place to apply this — replace 5-15 single-Read turns with 1-3 multi-Read turns.

**The four required checks specifically should always be parallel (WOR-413).** `ruff check .`, `mypy app/`, `pytest`, `lint-imports` have no data dependencies on each other. Emit all four as `tool_use` blocks in one assistant message. WOR-412 measured the per-session parallel-tool-use rate at 9-25%; the check sweep is the easiest 100% target. Halves wall-time of the pre-finalize phase.

**pytest runs `-n 8` by default (WOR-464).** `pytest-xdist` is in `requirements-dev.txt` and `-n 8` is in `pyproject.toml` addopts, so every `pytest` invocation (worker manual runs, `required_checks`, CI) parallelises across 8 workers. On a 16C/32T box the full 1830-test suite drops from ~125s serial to ~34s. The PostToolUse single-file pytest hook explicitly passes `-p no:xdist` because worker-spawn overhead dominates short single-file runs. Override globally with `pytest -p no:xdist` when debugging serial behaviour.

**No standalone `cd` commands.** Every `cd` is a wasted round-trip. Use absolute paths or chain with the actual command:
```bash
# bad  — two round-trips
cd /path/to/dir
ruff check .

# good — one round-trip
cd /path/to/dir && ruff check .
# or use absolute path directly
ruff check /path/to/dir
```

**Batch file reads.** When you need the contents of multiple related files, read them in one round-trip with a shell one-liner rather than issuing individual Read calls:
```bash
# reads 5 files in one tool call instead of 5
python3 -c "
import sys
for f in ['app/core/watcher.py', 'app/core/watcher_types.py', ...]:
    print(f'=== {f} ==='); print(open(f).read())
"
```

**Trust the Edit tool and hooks — do not re-read after editing.** The Edit tool confirms the change was applied. PostToolUse hooks (ruff, mypy, bandit, lint-imports) report any issues immediately in the tool result. Re-reading a file after editing to "verify" wastes a round-trip per edit. Only re-read if a hook explicitly reported an error you need to inspect in context. **Per-file 2-read cap is universal** (WOR-355) — a file may be read at most twice per session whether or not `context_snippets` populated it; the cap applies even when the manifest's snippets list is empty. WOR-322 evidence: 27 reads of a single 480-LOC file across one session contributed materially to the 76-minute wall time.

**Update mock patch paths after any module move.** `unittest.mock.patch()` targets are string literals — they are not updated by import fixers and will silently break tests. After moving or renaming any module, run two greps — one for mock strings, one for bare from-imports (conftest.py and fixture files use these and they are missed by the patch grep):
```bash
grep -rn 'patch("' tests/ | grep '<old.module.path>'
grep -rn 'from <old.module.path> import' tests/ app/
```
and update every match to the new path before running pytest. Missing the from-import grep causes `ModuleNotFoundError` in conftest.py that fires before any test runs — all tests fail even though the implementation is correct.

**Convert `patch.object` when extracting instance methods to module-level functions.** `patch.object(instance, "method")` patches the method on the class; once the function is module-level it no longer exists on the class and the patch silently does nothing. After extracting any method from a class, run:
```bash
grep -rn 'patch\.object' tests/ | grep '<ClassName>'
```
and convert every match to `patch("new.module.path.function_name")`.

**Edit existing files with the Edit tool, not `python3 -c`, `sed`, or Bash one-liners.** Any approach that passes Python source through a shell command will break on Windows quoting. The Edit tool takes `old_string`/`new_string` with no shell quoting involved.

**Create new files with the Write tool, not Bash heredocs.** Heredocs containing Python source break on Windows when the file body contains single quotes — the shell misinterprets them as closing the delimiter. The Write tool handles any content without escaping and avoids the multi-attempt retry loop. If the Write tool is unavailable (local model sessions), fall back to a single-quoted Bash heredoc: `python3 << 'PYEOF'` with the closing `PYEOF` at column 0 — the single-quoted delimiter prevents the shell from interpreting anything inside, including single quotes in Python source.

**Run mypy on each new Python file immediately after creating it.** Do not defer to the final `mypy app/` check — type errors in new files compound across the session and each late fix costs a full tool round-trip. Read the type signatures of the source functions *before* writing the new file so annotations are correct on the first attempt.

**Package reorganizations: move all files first, __init__.py last.** When moving multiple files into a new subpackage: (1) move all source files, (2) update all imports in every consumer, (3) write `__init__.py` last. Do not run pytest at any intermediate step — the package is invalid mid-move and pytest will always fail with `ModuleNotFoundError` until every file is in place. After all files are moved and imports updated (but before `__init__.py`), make a WIP commit: `git add -A && git commit -m "WIP: ..."` — preserves structural work if the session ends before checks pass.

**Use `replace_all=True` for bulk patch string migrations.** When updating `unittest.mock.patch()` strings after a module rename, use `Edit(old_string="app.core.old_module", new_string="app.core.new.module", replace_all=True)` rather than replacing each occurrence individually. One Edit call per (file, module-name) pair covers the entire migration in a single round-trip.

**Run the unscoped `pytest` from `required_checks` before declaring success.** When the manifest's `required_checks` contains plain `pytest` (no args), the worker MUST run the full suite once before writing the success result artifact — not just the test files in `allowed_paths`. Sibling test files (e.g. `tests/test_X_metrics.py`, `tests/test_X_recovery.py`) often import the same source module the worker modified and fail when their fixtures don't anticipate the change. Skipping the unscoped run causes the watcher's `required_checks` step to catch the regression after the session has ended, marking the ticket Blocked even though the worker reported success. Targeted pytest is fine for fast iteration; the unscoped final run is mandatory. (See WOR-353.)

**Test allowed_paths are auto-globbed at `/start-ticket` time.** When the architect lists `tests/test_X.py` in `allowed_paths`, the manifest writer expands it to `tests/test_X*.py` so sibling test files are explicitly in scope. Architects do NOT need to enumerate sibling tests manually. Already-globbed entries (e.g. `tests/test_*.py`) are left unchanged.

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

**`watcher` behaviour:** Any `ReadyForLocal` ticket that still carries the Spike label is skipped with a WARNING log. This is a safety net — `/start-ticket` should have caught it first. Detection: `app/core/watcher/watcher.py:503` — `any(label.lower() == "spike" for label in labels)`.

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
