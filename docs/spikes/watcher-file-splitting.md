# Spike: watcher.py file splitting — token savings analysis

**Ticket:** WOR-164
**Date:** 2026-04-24
**Status:** Complete — recommendation: **split**

---

## Context

`app/core/watcher.py` is 1,486 LOC (57,636 chars). Any worker session that touches
watcher logic reads the entire file into context — approximately **14,409 tokens** at
a measured 4 chars/token ratio for this codebase. For local mode (qwen3-coder:30b),
this consumes ~45% of the effective 32k context window before the worker has read
anything else.

The question: is splitting watcher.py into smaller modules worth the refactoring cost?

---

## Current structure — natural seams

```
Lines     LOC    Tokens   Section
─────────────────────────────────────────────────────────────────────
1–112     112     932    Module header, imports, constants,
                          LinearClientProtocol, ActiveWorker
113–292   180   1,580    Pure helpers: check_allowed_paths_overlap,
                          build_worker_env, build_worker_cmd,
                          resolve_effective_mode, _tee_worker_output,
                          _read_result_flags, _parse_ollama_model,
                          _parse_worker_usage
297–368    72     689    Watcher.__init__ + run (poll loop)
370–522   153   1,507    WaitingForDeps promotion (6 methods)
527–622    96     905    Poll + dispatch (_dispatch_next_ticket,
                          _start_ticket)
628–818   191   2,107    Worker lifecycle (reap, finalize_worker,
                          attempt_pr, safe_set_state)
819–829    11     118    Manifest loading (_load_manifest)
830–1005  176   1,786    Worktree management (9 methods)
1010–1115 106   1,058    Worker subprocess (_expand_skill,
                          _build_snippet_tool_restrictions,
                          _launch_worker)
1116–1136  21     206    Check runner (_run_checks)
1137–1185  49     517    SonarCloud (_fetch_sonar_findings)
1186–1278  93     884    PR creation (_create_pr)
1279–1409 131   1,290    LiteLLM + Ollama proxy (5 methods)
1410–1452  43     400    Signal/PID management
1453–1487  35     274    Module-level utilities (is_watcher_running,
                          _to_metrics_mode)
─────────────────────────────────────────────────────────────────────
TOTAL    1,486  14,409
```

The sections are already cleanly delimited with comment banners in the source.

---

## Proposed module structure

Split into five files. Each maps directly to one of the existing comment sections.

### `watcher_types.py` (~147 LOC, ~1,200 tokens)

Constants, shared types, and the LinearClientProtocol:
- Module-level constants (`_CLAUDE_DIR`, `_LITELLM_PORT`, `_WORKTREE_BASE`, etc.)
- `LinearClientProtocol` (Protocol class, currently lines 65–70)
- `ActiveWorker` dataclass
- `is_watcher_running` + `_to_metrics_mode` (module utilities, not class methods)

**Why separate:** LinearClientProtocol needs to be importable by test_watcher.py and
any future module without importing the full Watcher class. Currently it's buried in
watcher.py — moving it here decouples the protocol from the implementation.

### `watcher_helpers.py` (~180 LOC, ~1,580 tokens)

Pure, stateless functions — unit-testable with no mocking:
- `_parse_worker_usage`
- `check_allowed_paths_overlap`
- `build_worker_env`, `build_worker_cmd`
- `resolve_effective_mode`
- `_tee_worker_output`
- `_read_result_flags`
- `_parse_ollama_model`

**No self dependencies** — all can be standalone functions with explicit params.
Existing tests in test_watcher.py cover these thoroughly; they move as-is.

### `watcher_subprocess.py` (~269 LOC, ~2,665 tokens)

Subprocess/IO concerns extracted as module-level functions:
- `_expand_skill(ticket_id, repo_root)` — reads the implement-ticket skill file
- `_build_snippet_tool_restrictions(snippets)` — static, no deps
- `launch_worker(manifest, worktree_path, mode, repo_root, verbose, counter_lock)` — renamed from `_launch_worker`
- `run_checks(manifest, worktree_path)` — renamed from `_run_checks`
- `fetch_sonar_findings(branch)` — renamed from `_fetch_sonar_findings`
- `create_pr(manifest, worktree_path)` — renamed from `_create_pr`

**Refactoring note:** `_launch_worker` currently references `self._verbose`,
`self._worker_counter_lock`, and `self._worker_counter`. Extracting it as a standalone
function requires passing those three values explicitly. All other methods have at
most one `self` dependency (`self._repo_root`).

### `watcher_worktrees.py` (~176 LOC, ~1,786 tokens)

Worktree lifecycle as module-level functions, all taking `repo_root: Path`:
- `create_worktree(manifest, repo_root)`
- `_rebase_worktree_from_base(worktree_path, base_branch)`
- `copy_manifest_to_worktree(manifest, worktree_path, repo_root)`
- `backup_plan_files()`, `restore_plan_files(backed_up)`
- `write_worker_pytest_config(worktree_path)`
- `preserve_worker_artifacts(worker, repo_root)`
- `cleanup_worktree(worktree_path, repo_root)`
- `cleanup_orphaned_worktrees(repo_root)`

**All 9 methods** use only `self._repo_root` from `self` — straightforward to extract.

### `watcher_services.py` (~131 LOC, ~1,290 tokens)

LiteLLM + Ollama process management as a `ServiceManager` class:

```python
class ServiceManager:
    def __init__(self, repo_root: Path) -> None: ...
    def ensure_ollama_running(self) -> None: ...
    def ensure_litellm_running(self) -> None: ...
    def stop(self) -> None: ...  # replaces _stop_litellm_proxy
```

`Watcher.__init__` creates `self._services = ServiceManager(repo_root)` and delegates
to it. The `_litellm_proc` reference moves inside `ServiceManager`.

**Why a class rather than functions:** `_ensure_litellm_running` stores
`self._litellm_proc` for later use by `_stop_litellm_proxy`. These two methods share
state, so a class boundary is cleaner than threading a `Popen | None` handle through
every function signature.

### `watcher.py` (~582 LOC, ~5,820 tokens) — orchestration only

The Watcher class itself, now slimmed to pure orchestration:
- `__init__`, `run` (poll loop)
- WaitingForDeps promotion (6 methods — they use `self._linear` heavily, not worth extracting)
- `_dispatch_next_ticket`, `_start_ticket`
- Worker lifecycle: `_reap_pool`, `_reap_finished_workers`, `_finalize_worker`, `_attempt_pr`, `_safe_set_state`
- `_load_manifest`
- Signal/PID: `_register_signals`, `_handle_signal`, `_wait_for_active_workers`, `_write_pid_file`, `_remove_pid_file`

**Calls out to:** `watcher_worktrees.*`, `watcher_subprocess.*`, `watcher_services.ServiceManager`

---

## Token savings — per-session analysis

System prompt + CLAUDE.md baseline: ~9,875 tokens (fixed overhead).

| Scenario | Current | After split | Saving |
|---|---|---|---|
| Fix worktree bug (e.g. Windows path handling) | 14,409 tok | 1,786 + 1,200 = 2,986 tok | **79%** |
| Add LiteLLM health check | 14,409 tok | 1,290 + 1,200 = 2,490 tok | **83%** |
| Add pure helper function | 14,409 tok | 1,580 + 1,200 = 2,780 tok | **81%** |
| Fix dispatch logic | 14,409 tok | 5,820 tok | **60%** |
| Fix PR creation | 14,409 tok | 2,665 + 1,200 = 3,865 tok | **73%** |
| Worst case (touches watcher.py orchestration) | 14,409 tok | 5,820 tok | **60%** |

Even the worst-case scenario (changing core orchestration in watcher.py) saves 60%.

### Local model context window impact

`qwen3-coder:30b` effective working context: ~32k tokens.

| Module loaded | % of context window |
|---|---|
| Current watcher.py | **45%** |
| watcher.py (post-split) | 18% |
| watcher_worktrees.py | 6% |
| watcher_helpers.py | 5% |
| watcher_services.py | 4% |

The current file consumes nearly half the local model's context before the worker
reads the manifest, skill prompt, test output, or any related files. Post-split,
the worst case is 18%, leaving 82% for working context.

### Cloud cost estimate

- Sonnet-4.6 input: $3/MTok
- Average token saving per watcher-related session: ~9,000 tokens
- At 20 watcher-ticket sessions/month: ~180k tokens = **$0.54/month**

The cloud cost saving is modest. The dominant benefit is **context quality at local
model capacity**: less context pressure means fewer hallucinations, better recall of
module internals, and smaller risk of the model pattern-matching against irrelevant
sections of a 1,486-line file.

---

## Coupling risks and mitigations

### LinearClientProtocol (lines 65–70)

Currently defined in watcher.py but logically a shared interface. After the split it
moves to `watcher_types.py`. `cli.py` and `test_watcher.py` import it from there.
No breaking change if the public import path is kept backwards-compatible via
`watcher.py` re-exporting it:

```python
# app/core/watcher.py (backwards compat re-export)
from app.core.watcher_types import LinearClientProtocol as LinearClientProtocol  # noqa: F401
```

### Import Linter contracts

Existing contracts (`ui-above-core`, `core-no-entry-points`) are unaffected — all
new modules are in `app.core.*` and do not import `app.cli` or `app.main`.
No `.importlinter` changes needed.

Verify after split:
```bash
lint-imports
```

### test_watcher.py (1,857 LOC, ~16,329 tokens)

Currently imports 10 symbols from `app.core.watcher`:
- `ActiveWorker`, `Watcher` → stay in `watcher.py` (ActiveWorker moves to `watcher_types.py`)
- `_parse_ollama_model`, `_parse_worker_usage`, `_tee_worker_output` → move to `watcher_helpers.py`
- `build_worker_cmd`, `build_worker_env`, `check_allowed_paths_overlap`, `is_watcher_running`, `resolve_effective_mode` → move to `watcher_helpers.py`

After the split, `test_watcher.py` would update its imports. The test file is also a
candidate for splitting into `test_watcher_helpers.py`, `test_watcher_subprocess.py`,
etc. — but that is optional scope for WOR-165.

---

## Implementation plan for WOR-165

Sequenced steps to keep CI green throughout:

1. Create `watcher_types.py` with constants, `LinearClientProtocol`, `ActiveWorker`,
   `is_watcher_running`, `_to_metrics_mode`. Update `watcher.py` to import from there.
   Run tests. ✓

2. Create `watcher_helpers.py` with all pure functions. Update `watcher.py` imports.
   Update `test_watcher.py` imports. Run tests. ✓

3. Create `watcher_services.py` with `ServiceManager`. Replace the 5 LiteLLM/Ollama
   methods in `Watcher` with delegation to `self._services`. Run tests. ✓

4. Create `watcher_worktrees.py` as module-level functions. Update `Watcher` to call
   them. Run tests. ✓

5. Create `watcher_subprocess.py` as module-level functions. Update `Watcher` to call
   them. Run tests. ✓

6. Verify `watcher.py` is ≤600 LOC, all modules ≤300 LOC, `lint-imports` passes.

Each step is a separate commit — partial splits remain green because watcher.py
keeps working imports throughout.

---

## Decision

**Split.** The token savings are substantial (60–83% per session), the seams are
already clearly demarcated with comment banners, and the refactoring complexity is
low (most extracted methods have ≤1 self dependency). The `LinearClientProtocol`
extraction in particular improves the design beyond token savings — the protocol
should not be buried in the orchestrator module.

The largest risk is `_launch_worker` (3 self deps) and the WaitingForDeps promotion
methods (tightly coupled to `self._linear`). The recommendation is to leave the
WaitingForDeps group in `watcher.py` (it's genuinely orchestration logic) and
handle `_launch_worker`'s dependencies by passing them explicitly.

Estimated implementation effort: 1 focused day, split across 5 atomic commits.
