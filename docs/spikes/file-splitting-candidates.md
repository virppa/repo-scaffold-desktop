# File splitting candidates — codebase survey

**Ticket:** WOR-167
**Date:** 2026-04-24
**Status:** Complete

---

## Context

WOR-164 established token-density thresholds for this codebase (9.7 tokens/LOC, local model working context 21,600 tokens):

| Tier | LOC | Action |
|------|-----|--------|
| Advisory | ≥ 500 | Consider splitting before it grows further |
| Recommend | ≥ 700 | Warn in `/finalize-ticket`; include split plan in PR description |
| Block | ≥ 1,000 | `/finalize-ticket` refuses PR for any ticket that grows file past this |

WOR-165 split `watcher.py` from 1,486 → 844 LOC across six files. This survey checks what remains above threshold and whether Import Linter contracts need updating.

Baseline: `epic/wor-160-code-refactoring-auto-split` (post-WOR-165).

---

## Full file inventory

| File | LOC | Tier |
|------|-----|------|
| `tests/test_watcher.py` | 1,857 | **Block** |
| `app/core/watcher.py` | 844 | **Recommend** |
| `tests/test_generator.py` | 551 | **Advisory** |
| `tests/test_manifest.py` | 381 | OK |
| `tests/test_linear_client.py` | 350 | OK |
| `app/core/metrics.py` | 335 | OK |
| `app/cli.py` | 329 | OK |
| `tests/test_cli.py` | 309 | OK |
| `tests/test_metrics.py` | 304 | OK |
| `tests/test_watcher_helpers.py` | 299 | OK |
| `app/core/manifest.py` | 281 | OK |
| `app/core/linear_client.py` | 253 | OK |
| `app/core/watcher_helpers.py` | 243 | OK |
| `tests/test_post_setup.py` | 230 | OK |
| `tests/test_escalation_policy.py` | 213 | OK |
| `app/core/watcher_worktrees.py` | 198 | OK |
| `app/core/watcher_subprocess.py` | 195 | OK |
| `tests/test_watcher_subprocess.py` | 178 | OK |
| `app/core/escalation_policy.py` | 157 | OK |
| `app/core/watcher_services.py` | 154 | OK |
| `app/core/watcher_types.py` | 100 | OK |
| All others | ≤ 100 | OK |

---

## 1. `tests/test_watcher.py` — 1,857 LOC (Block)

### Root cause

When WOR-165 created `watcher_helpers.py` and `watcher_subprocess.py`, the corresponding tests were written into new dedicated files. But **the originals were not removed from `test_watcher.py`**. There are 42 duplicate test functions running in two places:

- 32 duplicates covered by `test_watcher_helpers.py`: `check_allowed_paths_overlap`, `build_worker_env`, `build_worker_cmd`, `resolve_effective_mode`, `_parse_worker_usage`, `_parse_ollama_model`
- 10 duplicates covered by `test_watcher_subprocess.py`: `_tee_worker_output`, `build_snippet_tool_restrictions`, `fetch_sonar_findings`

Additionally, no test files exist yet for `watcher_types.py`, `watcher_worktrees.py`, or `watcher_services.py`, so their tests sit in `test_watcher.py`.

### Proposed split

**Step 1 — Remove duplicates from `test_watcher.py`** (~630 LOC removed)

Delete the test sections that are already covered in `test_watcher_helpers.py` and `test_watcher_subprocess.py`. These sections are clearly demarcated by comment banners in `test_watcher.py`. After removal: ~1,227 LOC.

**Step 2 — Create `tests/test_watcher_types.py`** (~50 LOC extracted)

Move tests for `is_watcher_running` (3 tests, lines 260–279) and `_write_pid_file`/`_remove_pid_file` (1 test, lines 312–327). After: ~1,177 LOC.

**Step 3 — Create `tests/test_watcher_worktrees.py`** (~100 LOC extracted)

Move tests for `_cleanup_orphaned_worktrees` (lines 285–306), `_preserve_worker_artifacts` (lines 936–989), and `_rebase_worktree_from_base` (lines 995–1012). After: ~1,077 LOC.

**Step 4 — Create `tests/test_watcher_services.py`** (~100 LOC extracted)

Move tests for `_ensure_ollama_running` (lines 1732–1788) and `test_dispatch_calls_ensure_ollama_and_litellm_for_local_effective_mode` (lines 1794–1857). After: ~977 LOC.

**Step 5 — Create `tests/test_watcher_finalize.py`** (~470 LOC extracted)

Move all `_finalize_worker` test suites:
- retry counting (lines 526–601)
- set_state failure paths (lines 607–677)
- usage-to-metrics (lines 1219–1283)
- Sonar metrics wiring (lines 1382–1430)
- Sonar escalation (lines 1432–1519)
- escalation policy flags (lines 1584–1694)

After: ~507 LOC.

**Step 6 — Create `tests/test_watcher_promotion.py`** (~220 LOC extracted)

Move `_promote_waiting_tickets` test suites (lines 720–930 minus fixtures). After: ~287 LOC (Advisory tier).

### Estimated LOC after all steps

`test_watcher.py`: ~287 LOC (Advisory) — Watcher class basics, `_create_pr`, `_dispatch_next_ticket`, pool capacity, spike dispatch.

All other new test files: 50–470 LOC each, well below Advisory.

### Important: update imports

`test_watcher.py` currently imports everything via backward-compat re-exports from `app.core.watcher`. When duplicates are removed, the new test files should import directly from the sub-modules (`app.core.watcher_helpers`, `app.core.watcher_types`, etc.).

### Priority: HIGH

1,857 LOC is the largest file in the codebase. The duplicate tests run 42 tests twice on every `pytest` invocation — wasted CI time and confusing coverage numbers.

---

## 2. `app/core/watcher.py` — 844 LOC (Recommend)

### Root cause

WOR-165 targeted ~582 LOC for `watcher.py`. It landed at 844 for three reasons:

| Section | LOC | Why it stayed |
|---------|-----|---------------|
| Backward-compat re-exports (lines 38–69) | 32 | test_watcher.py still imports via watcher.py |
| Worktree/subprocess delegation shims (lines 621–688) | 68 | thin wrappers kept for test backward-compat |
| Service shims (lines 783–802) | 20 | test_watcher.py accesses `_litellm_proc` via Watcher |
| `_create_pr` method (lines 694–781) | 88 | not extracted to watcher_subprocess.py in WOR-165 |
| `_finalize_worker` method (lines 472–609) | 138 | complex orchestration; was not split |

The re-exports and shims are load-bearing for `test_watcher.py` in its current form. Once the test split (item 1 above) removes the duplicate tests and updates imports, these 120 LOC of boilerplate can be deleted.

### Recommended sequence

1. **After test split completes:** remove backward-compat re-exports (-32 LOC) and delegation shims (-88 LOC) → ~724 LOC (still Recommend)
2. **Extract `_create_pr`** to `watcher_subprocess.py` (-88 LOC) → ~636 LOC (Advisory tier)
3. **Optional:** extract `_finalize_worker` to a new `watcher_finalize.py` (-138 LOC) → ~498 LOC (below Advisory)

Step 3 is optional because 636 LOC is functional at Advisory and `_finalize_worker` is tightly coupled to Watcher instance state (`self._linear`, `self._escalation_policy`, `self._metrics`). Extracting it as a free function would require passing those as arguments; a separate ticket should assess whether that trade-off is worth it.

### Priority: MEDIUM (depends on test split completing first)

---

## 3. `tests/test_generator.py` — 551 LOC (Advisory)

### Assessment

Well-structured. All 48 tests cover a single module (`app/core/generator.py`, 60 LOC). The tests are organized by feature toggle and preset — there are no natural seams for a module-aligned split.

The Advisory flag here is cosmetic: generator.py itself is 60 LOC, so the test-to-production ratio is 9:1. No worker session ever needs to load both simultaneously.

### Recommendation: MONITOR

No action now. If it grows past 700 LOC, consider splitting by preset category: `test_generator_python_basic.py`, `test_generator_python_desktop.py`, `test_generator_full_agentic.py`.

---

## Import Linter gap analysis

Current contracts in `.importlinter` (4 total, all correct):

| Contract | Enforces |
|----------|---------|
| `ui-above-core` | `app.ui` may import `app.core`; not vice versa |
| `core-no-entry-points` | `app.core` may not import `app.cli` or `app.main` |
| `watcher-layers` | Watcher sub-module strict dependency hierarchy |
| `watcher-types-is-leaf` | Belt-and-suspenders: watcher_types imports nothing from siblings |

### Gap 1: Entry-point cross-imports (RECOMMEND adding)

No contract prevents `app.ui` from importing `app.cli` (or vice versa). As the UI is built out, someone could accidentally pull in argument-parsing code or vice versa. Suggested addition:

```ini
[importlinter:contract:entry-points-no-cross-import]
name = Entry point modules must not import each other
type = forbidden
# app.cli (CLI entry point) and app.ui (GUI entry point) are independent
# invocation paths. Neither should import from the other — they both
# call into app.core, but they must not be coupled to each other.
# CONTRACT OWNER: cloud LLM only — do not modify without explicit approval.
source_modules =
    app.cli
    app.ui
forbidden_modules =
    app.cli
    app.ui
```

Note: Import Linter's `forbidden` type applies to the `source_modules`→`forbidden_modules` direction. To make this bidirectional, both need to be in both lists (each becomes a "cannot import the other").

### Gap 2: Watcher backward-compat re-exports (tracking note, not a new contract)

Currently `app.core.watcher` re-exports symbols from sub-modules. The `watcher-layers` contract treats `app.core.watcher` as the top-layer module, which is correct — it can import from everything below it. No contract change is needed; the re-exports are a temporary backward-compat shim to be removed after the test split.

### Gap 3: No gaps in watcher sub-module hierarchy

The `watcher-layers` contract correctly uses the `:` (independent-at-same-tier) syntax to prevent `watcher_subprocess`, `watcher_worktrees`, and `watcher_services` from importing each other. This was verified: the contract runs clean on the current epic branch.

---

## Action items

| Priority | Action | Ticket |
|----------|--------|--------|
| HIGH | Split `tests/test_watcher.py` per steps 1–6 above | New ticket (WOR-160 epic) |
| MEDIUM | Remove backward-compat re-exports + delegation shims from `watcher.py` | Depends on test split |
| MEDIUM | Move `_create_pr` to `watcher_subprocess.py` | Bundle with re-export cleanup |
| LOW | Add `entry-points-no-cross-import` contract to `.importlinter` | New ticket |
| WATCH | Monitor `tests/test_generator.py` at 551 LOC; split if > 700 | No ticket needed yet |
