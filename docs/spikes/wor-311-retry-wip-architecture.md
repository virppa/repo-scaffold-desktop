# WOR-311 — Watcher retry+WIP architecture (design of record)

**Status:** Decided 2026-05-03 — Option C (in-dispatch retry with `max_retries=1` hardcap).
**Implementation:** WOR-312 (separate ticket).
**Related shipped fixes (2026-05-03):** WOR-329 manual rescue → WOR-334 ghost-slot prevention → WOR-339 LiteLLM probe split → WOR-342 finalize return cleanup → WOR-347 WIP preservation on success path.

This document captures the architecture analysis behind the retry+WIP decision so WOR-312 has a stable design target and the trade-offs aren't re-litigated.

---

## TL;DR

The watcher's `failure_policy.max_retries` field is **vestigial — read by no code in `app/core/watcher/`**. Every check failure routes straight to `Blocked` (or to cloud, if `escalate_to_cloud=true`). There is no in-dispatch retry loop. The "retry path" only fires when a human manually resets `Blocked → ReadyForLocal`, which destroys the worktree, creates a fresh one, and re-runs the worker from scratch (~25M tokens of repeat work for what was usually a transient flake on the last check).

**Decision:** implement in-dispatch retry with **hardcap = 1** (Option C below). One retry catches transient flake (test ordering, race conditions, GPU memory pressure) without ever doing 3-deep retry. Smallest change that captures the value.

---

## Problem (the incident that surfaced this)

WOR-305 was dispatched twice with `max_retries=2` set in the manifest, expecting the daemon to auto-retry within each dispatch on check failure. Both runs went straight to Blocked on first failure, ~50 min combined wall time and 44M tokens lost. Investigation revealed the field has no effect — the manifest accepted it, the worker honored its other fields, but `_execute_finalization` never reads it.

This is the canonical symptom: ops *thinks* the watcher retries (because the field exists), the watcher does not, and we burn cloud tokens re-doing the work via the manual-rescue path.

---

## Architecture as it actually exists today

Verified by code reading on `main` after the 2026-05-03 resilience merges (WOR-334, WOR-339, WOR-342, WOR-347).

### Retry counters and the `max_retries` field

| Symbol | Location | What it actually does |
| --- | --- | --- |
| `Watcher._retry_counters: dict[str, int]` | `app/core/watcher/watcher.py:112` | Initialized to `{}`. **Never written**, never read for flow control. Pure dead code. |
| `ActiveWorker.retry_count: int = 0` | `app/core/watcher/watcher_types.py:68` | Default field. Bumped at `watcher_finalize.py:477` on check failure. **Only consumed by telemetry writes** (`watcher_finalize.py:297, 320`) — never gates control flow. |
| `failure_policy.max_retries: int = 0` | `app/core/manifest.py` (Pydantic model) | Field exists on the schema. Documented in the manifest spec. **Read by no code in `app/core/watcher/`.** Pure documentation field. |
| ~~`dispatch.start_ticket(retry_counters: dict[str, int], ...)`~~ | ~~`app/core/watcher/dispatch.py`~~ | **Already removed** as part of WOR-328 (2026-05-03). Was an unused parameter passed by tests as `retry_counters={}`. |

The cleanup that WOR-328 already performed for `dispatch.start_ticket` should be repeated for the remaining three entries above when WOR-312 lands.

### Failure path (current control flow)

`watcher_finalize.py::_execute_finalization` (around lines 477-495):

```python
checks_ok, failed_checks = run_checks(...)
if not checks_ok:
    worker.retry_count += 1                              # telemetry only
if not checks_ok and manifest.failure_policy.on_check_failure == "abort":
    escalated = bool(manifest.failure_policy.escalate_to_cloud)
    if escalated:
        # → Linear state "In Progress" (cloud picks it up next dispatch)
        safe_set_state(linear, linear_id, _IN_PROGRESS_STATE, ticket_id)
    else:
        # → Linear state Blocked (human must reset to ReadyForLocal to retry)
        safe_set_state(linear, linear_id, manifest.ticket_state_map.failed, ticket_id)
    return "failure", ...                                # NO RETRY LOOP
```

There is no loop. The first failure exits the function.

### The "retry mechanism" that does work today (manual)

When a human resets `Blocked → ReadyForLocal` in Linear:

1. Watcher's poll picks up the ticket via `list_ready_for_local()`
2. `_load_manifest` reads the manifest from disk
3. `_enrich_with_retry_context` (`watcher.py:799`) reads `last_failure.json` and prepends a `RETRY: ...` constraint to `implementation_constraints`
4. `create_worktree` → `git worktree add <path> <worker_branch>` → `git fetch origin <base> && git rebase origin/<base>`
5. Worker starts fresh with the RETRY constraint and (depending on whether the previous run's `commit_wip_state` succeeded) the previous WIP commit
6. Worker re-runs from scratch with one extra hint about what failed before

**Net cost of a manual rescue:** another ~25M tokens because the worker re-does most of the work. The RETRY hint helps but the worker has no in-context memory of what it tried.

### What changed today (2026-05-03)

The retry+WIP architecture has converged on a much safer foundation in the last 24 hours, even before WOR-312 ships:

- **WOR-329 incident** (00:37 UTC): worker succeeded but `safe_set_state` raised an exception → `_reap_pool` never returned → `_local_active` retained the slot for 5+ hours. Manual rescue (PR #642) merged the work after conflict resolution.
- **WOR-334** (PR #678): wraps `_finalize_one_worker` in try/except inside `_reap_pool` and uses in-place mutation. Slot is released even when finalize raises.
- **WOR-339** (PR #679): TCP-probe vs HTTP-probe split for `ensure_litellm_running`. No more orphan-tab spawn races.
- **WOR-342** (PR #677): collapsed `finalize_worker`'s two return paths into one + 2 small Sonar fixes.
- **WOR-347** (PR #684): `commit_wip_state` now runs on the success path too. Workers that pass checks but forget to commit no longer lose their work.

WOR-312 inherits these fixes — the retry loop it adds runs on top of a daemon that no longer leaks slots, no longer loses uncommitted WIP, and no longer spawns redundant LiteLLM tabs. The remaining gap is the actual in-dispatch retry on check failure.

---

## Three options considered

### Option A — Full implementation, no cap

Implement a retry loop with the manifest's `max_retries` honored verbatim. `max_retries=3` means 4 attempts.

* **Pro:** maximum flexibility per ticket.
* **Con:** ~3 days of work for a feature whose value is unproven. We don't know how often a second attempt would succeed where the first failed. Three-deep retries on a stuck failure would waste 100M+ tokens before giving up.
* **Con:** every retry that doesn't succeed is pure waste. Without data, we don't know the right cap.

### Option B — Remove the vestigial field

Delete `failure_policy.max_retries`, `Watcher._retry_counters`, `ActiveWorker.retry_count`, the telemetry writes that read it. Be honest about what we don't have.

* **Pro:** smallest possible change. Removes documentation drift between manifest spec and code behavior.
* **Pro:** bumps the cost of "we should add retries" to a future ticket where someone has actually measured the need.
* **Con:** doesn't fix the real cost of the manual-rescue path. When the worker IS close to done and just hits a flake, we still lose 25M tokens to a Blocked state instead of letting one retry finish the job.

### Option C — `max_retries=1` hardcap (chosen)

Implement an in-dispatch retry loop with a hardcap of 1 retry per dispatch. The manifest's `max_retries` is honored UP TO the hardcap (so `max_retries=0` opts out, `max_retries=1` enables, `max_retries=2+` is silently capped at 1).

* **Pro:** smallest implementation that captures the value. One retry catches transient flake (test ordering, race conditions, GPU memory pressure on first call); two-deep is cheap insurance against the worst case.
* **Pro:** the hardcap makes the "we don't believe in deep retries until data proves otherwise" stance explicit in code rather than in code reviews.
* **Pro:** preserves the manifest field's existence (so the manifest spec still documents "this is configurable") while making its behavior bounded.
* **Con:** still adds complexity. Worse cap (0 vs 1) is a smaller-and-honest choice.

After WOR-312 ships and a few real tickets retry successfully, we can revisit the cap based on data: how many tickets actually succeed on the second attempt, what kinds of failures retry resolves.

---

## What WOR-312 must do

Concrete checklist for the implementation ticket. Updated with line numbers as of 2026-05-03 main.

### 1. Restructure `_execute_finalization` into an attempt loop

Currently single-shot. New shape:

```python
ATTEMPT_HARDCAP = 1  # Option C: never retry more than once per dispatch

for attempt in range(ATTEMPT_HARDCAP + 1):
    if attempt > 0:
        # Write last_failure.json so _enrich_with_retry_context can read it
        write_last_failure_to_worktree(worker, failed_checks, attempt)
        # Relaunch worker in the SAME worktree — commit_wip_state has
        # already preserved the previous attempt's work via WOR-347
        worker.process = launch_worker(
            repo_root, manifest, worker.worktree_path,
            effective_mode, worker_verbose,
            extra_constraint=(
                f"RETRY (attempt {attempt + 1}): previous attempt failed "
                f"`{failed_check}`. Fix this specifically."
            ),
        )
        worker.process.wait()  # block until done

    checks_ok, failed_checks = run_checks(...)
    if checks_ok:
        break

    # Record this attempt's failure in run_log
    record_attempt(attempt + 1, failed_check=last_failed_check)

    # Preserve WIP for next attempt OR for human inspection
    commit_wip_state(...)

    # Cap reached?
    if attempt >= min(ATTEMPT_HARDCAP, manifest.failure_policy.max_retries):
        # Final failure — escalate to cloud or block
        if manifest.failure_policy.escalate_to_cloud:
            safe_set_state(..., _IN_PROGRESS_STATE)
        else:
            safe_set_state(..., manifest.ticket_state_map.failed)
        return "failure", ...
```

### 2. Hold the worktree across attempts

`cleanup_worktree` currently runs in `finalize_worker` (post-WOR-342, after the conditional). With retry, cleanup happens only AFTER the loop terminates (success or final failure). Move the cleanup call out of the per-attempt path. WOR-347's WIP-preservation gate stays valid: cleanup only runs when `wip_result.status in ("clean", "pushed", "backup")`.

### 3. Inject retry context inline

Today `_enrich_with_retry_context` (`watcher.py:799`) runs at *dispatch* time and prepends `RETRY: ...` to `implementation_constraints` based on `last_failure.json` (only populated after a Blocked → ReadyForLocal cycle).

For in-dispatch retry, we want the same hint but injected directly into the worker's prompt or as an `extra_constraint` parameter on relaunch. Specifics depend on `launch_worker`'s contract — may need a new param.

`_enrich_with_retry_context` stays for the manual-rescue path (still useful when in-dispatch retries are exhausted and a human escalates).

### 4. Fix the telemetry bugs (folded from WOR-310)

Bug 1 — `attempt = retry_count + 1` off-by-one at `watcher_finalize.py:320`:

> With proper retry, the loop already maintains `attempt` as 0-indexed iteration counter. Write `attempt=loop_attempt + 1` (1-indexed for human readability) per attempt. First attempt → `attempt=1`, retry → `attempt=2`. No more both-rows-saying-attempt=2.

Bug 2 — `failed_check` first-vs-last vs `last_failure.json`:

> `run_checks` returns the first-failed-check. `last_failure.json` records the last. Pick one semantic and apply to both: store **all failed checks** in run_log as a JSON array, not just one. `last_failure.json` keeps the most recent (it's about driving the next attempt's context).

### 5. Skill template change (folded from WOR-308)

`.claude/commands/start-ticket.md` and `.claude/commands/start-epic.md` currently hardcode `max_retries: 0`. Change default to `max_retries: 1`. Architect can override (e.g. set 0 for security-critical work).

### 6. Remove dead code

Per the table above, after WOR-312 lands:

- Delete `Watcher._retry_counters: dict[str, int]` (`watcher.py:112`) — unused init.
- Rename `ActiveWorker.retry_count` → `attempt_count` (`watcher_types.py:68`) and increment within the loop (clearer semantics; still feeds telemetry).
- Update telemetry sites (`watcher_finalize.py:297, 320, 477`) to use `attempt_count`.

---

## Files WOR-312 will edit

(Updated for current main as of 2026-05-03; line numbers will shift again if WOR-312 doesn't land soon.)

| File | Change |
| --- | --- |
| `app/core/watcher/watcher_finalize.py` | Restructure `_execute_finalization` into a loop; move `cleanup_worktree` decision out of per-attempt path; fix telemetry writes |
| `app/core/watcher/watcher.py` | Remove `_retry_counters` dict (line 112) |
| `app/core/watcher/watcher_types.py` | Rename `retry_count` → `attempt_count` on `ActiveWorker` (line 68) |
| `app/core/watcher/watcher_subprocess.py` | `launch_worker` may need an `extra_constraint` parameter for retry context |
| `app/core/manifest.py` | Keep `max_retries` field; document the hardcap interaction in the docstring |
| `app/core/metrics.py` | `failed_check` column → JSON array semantic, OR add a new `failed_checks_json` column and deprecate the singular field |
| `.claude/commands/start-ticket.md` | Change template default `max_retries: 0` → `1` |
| `.claude/commands/start-epic.md` | Same change |
| `tests/test_watcher_finalize.py` | New tests for the retry loop (success on retry, failure on retry, hardcap honored, no infinite loop) |
| `tests/test_metrics.py` | Update for the failed_check semantic change |
| `CLAUDE.md` | Document the retry behavior in the "Hybrid lifecycle states" section |

---

## Acceptance criteria for WOR-312 (mirror of the implementation ticket)

* `failure_policy.max_retries=1` results in TWO attempts on persistent failure (initial + 1 retry); only after the second failure does the ticket move to Blocked / Cloud.
* `failure_policy.max_retries=0` results in ONE attempt (current behavior) — honors opt-out.
* `failure_policy.max_retries=2` is silently capped at 1 (hardcap); add a debug-level log noting the cap was applied.
* Retry attempt re-launches the worker in the **same worktree** (not a new one).
* Retry attempt receives a `RETRY: ...` hint about what failed (via `extra_constraint` to `launch_worker` or equivalent).
* `ticket_run_log` writes one row per attempt with `attempt=1, 2` (no off-by-one).
* `ticket_run_log.failed_check` matches the actual failure of that attempt (or becomes a JSON array of all failed checks for that attempt).
* `last_failure.json` records the most recent attempt's failure (consistent with `_enrich_with_retry_context`'s expectation).
* `Watcher._retry_counters` is deleted.
* `ActiveWorker.attempt_count` (renamed from `retry_count`) starts at 0 and increments per attempt.
* Skill templates default to `max_retries: 1`.
* CLAUDE.md "Hybrid lifecycle states" section documents the retry behavior.
* `ruff check .`, `mypy app/`, and `pytest` all pass.
* New tests cover: retry succeeds → PR created; retry exhausted → Blocked; manifest opt-out (`max_retries=0`) → no retry; hardcap (`max_retries=2`) → still capped at 1.

---

## Out of scope (deferred)

* Per-effort variable retry caps (was WOR-308's original idea — defer until we have data showing 1-cap is wrong).
* WIP-across-redispatch — already covered by WOR-309 (operator visibility for silent `commit_wip_state` failures) + WOR-347 (WIP preservation on success path).
* Cloud-side retry behavior — this design only changes local worker behavior. Cloud workers continue to escalate immediately (no retry there, they already cost money per attempt).
* Adjusting the `_enrich_with_retry_context` path for human-initiated rescue — that already works; leave it alone.

---

## When to revisit this design

After WOR-312 ships and the metrics DB has captured at least 30 dispatches that hit the retry loop:

1. What % of retries succeed?
2. Of failed retries, what fraction hit the same check vs a different check?
3. Median tokens spent on a successful retry vs a failed retry?
4. Are there ticket categories (taxonomy fields from WOR-262) where retry is reliably futile?

If retry success rate < 20%, consider Option B (remove the field). If > 50%, consider raising the hardcap to 2 for specific taxonomy buckets.

---

## References

### Code paths

* `app/core/watcher/watcher.py:112` — `_retry_counters` initialization (dead).
* `app/core/watcher/watcher.py:483, 799` — `_enrich_with_retry_context` use-site and definition (the manual-retry-only path).
* `app/core/watcher/watcher_finalize.py:477-495` — failure path (no retry loop).
* `app/core/watcher/watcher_finalize.py:325-370` — WIP preservation block (post-WOR-347, runs on both success and failure).
* `app/core/watcher/watcher_worktrees.py::commit_wip_state` — returns `pushed`/`backup`/`failed`/`clean`.
* `app/core/manifest.py` — `FailurePolicy` model with the `max_retries` field.

### Incidents and tickets

* **WOR-305** — the rescue that surfaced this gap (~50 min and 44M tokens lost across two attempts that should have been one in-dispatch retry).
* **WOR-308** — closed as Duplicate; skill template change folded into WOR-312.
* **WOR-310** — telemetry bugs 1 and 2 folded into WOR-312; bug 3 dropped (was wrong).
* **WOR-309** — separate ticket; operator visibility for silent `commit_wip_state` failures.
* **WOR-329** — ghost-slot incident (2026-05-03 overnight) that motivated WOR-334 + WOR-347.
* **WOR-332** — concurrent ticket that surfaced the WIP-preservation gap (worker passed all 96 tests but never committed; watcher destroyed the work). Fixed by WOR-347.
* **WOR-334** (PR #678 merged 2026-05-03) — `_local_active` ghost-slot prevention via try/except + in-place mutation.
* **WOR-339** (PR #679 merged 2026-05-03) — TCP-probe vs HTTP-probe split for LiteLLM (eliminated the orphan-tab race).
* **WOR-342** (PR #677 merged 2026-05-03) — collapsed `finalize_worker` return paths + 2 Sonar fixes.
* **WOR-347** (PR #684 merged 2026-05-03) — `commit_wip_state` runs on the success path too.
* **WOR-312** — implementation ticket for the design captured in this document.
* **WOR-313** — overnight mega-epic that surfaced the bulk of these resilience needs in one night.
