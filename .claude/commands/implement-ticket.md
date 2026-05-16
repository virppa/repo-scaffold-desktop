Local worker entrypoint. Reads the execution manifest for $ARGUMENTS and implements the ticket within the declared scope. Does NOT re-read Linear, re-interpret the project, or make architectural decisions — the manifest is the sole source of truth.

### 0. Load the manifest

Read the manifest from `.claude/artifacts/<ticket_id_lower>/manifest.json`
(e.g. for WOR-80: `.claude/artifacts/wor_80/manifest.json`).

If the file does not exist:
```
ABORT: Manifest not found at .claude/artifacts/<ticket_id_lower>/manifest.json
Run /start-ticket $ARGUMENTS first to generate it.
```

If `manifest_version` is not `"1.0"`:
```
ABORT: Unsupported manifest_version '<version>'. This worker supports 1.0 only.
```

Confirm the following fields are present before continuing:
- `ticket_id`, `worker_branch`, `base_branch`, `objective`, `artifact_paths`

### 0.5. Load context snippets (if present)

If `manifest.context_snippets` is non-null and non-empty, treat each entry as
a pre-loaded code excerpt — do NOT re-read these sections from disk unless you
need context beyond what is shown. The snippets are verbatim source with file
path and line numbers in the header comment.

Log the snippet count on startup: `"Loading {N} context snippets from manifest."`

**Per-file 2-read cap (universal — WOR-355).** Each file may be read at most 2 times per session, regardless of whether `context_snippets` is populated. The cap counts every Read tool call against the same file path: if `context_snippets` pre-loads `app/core/X.py`, that counts as the first read; the next Read on it is the second; any further is a violation. If `context_snippets` is empty, the cap still applies — your first explicit Read is read 1, the second is read 2, no third. If you need more content from a file beyond what 2 reads provide (e.g. a function past line 80), note the missing content in the result artifact rather than re-reading. This rule prevents context bloat that drives 30K → 70K input token growth across a session.

**Do NOT re-read a file after Edit-ing it (WOR-355).** The Edit tool returns the change confirmation in its tool result — including the new line numbers and surrounding context. PostToolUse hooks (ruff, mypy, bandit, lint-imports) run automatically and report any issues immediately in the same tool result. Re-reading to "verify the edit applied" wastes a full LLM round-trip (~50s at current local throughput, multiplied by every Edit you do). The most common violation is reading the same source file 5+ times during a multi-edit session — a pattern that turned WOR-322's 4 docstring edits into 76 minutes of wall time. If a hook flags an error you need to inspect in context, that hook output IS in your tool result — don't call Read; act on what's already there.

### 0.1. Inspect last_failure.json for WIP state (WOR-258)

If `.claude/artifacts/<ticket_id_lower>/last_failure.json` exists in the
worktree, read it for a `wip_commit_sha` value. When present:

```bash
git log --oneline -5   # see recent commits, including wip(failed) commits
git show --stat <wip_commit_sha>  # diff of that commit
```

If the worktree contains a commit whose message matches `wip(failed): <ticket_id>`:
- Inspect what code was already written by that commit.
- **Resume from the WIP commit state** without redoing completed work.
- If the WIP commit has conflicts with the current branch tip, resolve them
  before continuing.

Also inspect git log for wip commits from previous workers (WOR-267):

```bash
git log --oneline --grep="^wip: <ticket_id>$"  # find wip commits
```

If wip commits are found on the branch, inspect them and **resume from the last
committed phase** without redoing completed work. The squash_wip_commits function
(worker-side) will squash them into a single commit on success.

This allows retry workers to pick up where the previous worker left off.

### 1. Verify branch

Confirm the current git branch matches `worker_branch` from the manifest:
```bash
git branch --show-current
```

If not on the correct branch:
```
ABORT: Expected branch '<worker_branch>' but current branch is '<actual>'.
Check out the correct branch before running /implement-ticket.
```

### 2. State transitions are owned by the watcher

The watcher already set state to `InProgressLocal` before launching this session. **Do not call `save_issue` for state transitions** at any point during the worker run — the watcher reads the result artifact and `finalize_worker` handles every subsequent transition (success → push + PR + InReview → MergedToEpic; failure → Blocked or In Progress + escalation comment). Posting Linear *comments* for context is fine; setting *state* is not.

### 3. Implement

Implement the work described in `objective` and `acceptance_criteria`. Obey these hard rules at all times:

**Allowed paths** — only write to paths matching `allowed_paths` globs. If the list is empty, any path under the repo root is allowed (excluding forbidden paths below).

**Forbidden paths** — never write to paths matching `forbidden_paths` globs. If a task seems to require touching a forbidden path, ABORT and write a failed result artifact (see step 5).

**Constraints** — follow every item in `implementation_constraints` exactly.

**No re-planning** — do not re-read Linear, re-query the project, or change scope. If something in the codebase is surprising, implement defensively within the manifest scope and note it in the result artifact summary.

**Hard rule: never invoke ruff, mypy, pytest, bandit, or lint-imports manually during step 3.** After every Edit/Write to a `.py` file, PostToolUse hooks fire: `ruff check --fix` + `ruff format`, `mypy <file>`, `bandit`, and `lint-imports`. After every edit to a `tests/` file, the hook runs `pytest <that_file> --no-cov --tb=short -q`. You will see hook output in the tool result — it is authoritative. Running any of `ruff check .`, `ruff format`, `mypy`, `pytest`, `bandit`, or `lint-imports` as a Bash tool_use outside the `required_checks` sweep at step 4 is a **hook-trust violation**. The worker log will record it; finalize_worker counts violations and emits a WARNING when the count exceeds 1. Only run the final `required_checks` commands at step 4.

**Enforced by hook (WOR-421).** A PreToolUse hook (`.claude/hooks/check_quality_check_budget.py`) enforces this rule with a per-session budget of `len(required_checks)` invocations (typically 4). Once the budget is used, further `ruff` / `mypy` / `pytest` / `bandit` / `lint-imports` Bash calls are **blocked** with an explanatory message — the tool call will fail and you'll see the block reason in the tool result. If you see this block, do not retry: read the most recent PostToolUse hook output instead, or fix the underlying issue your edit didn't address.

**Emit independent tool calls in parallel (WOR-387).** When you need to run multiple tools that don't depend on each other's results — reading multiple unrelated files, grepping multiple unrelated patterns, listing multiple directories, running independent shell probes — emit ALL the `tool_use` blocks in ONE assistant message. The runtime executes them in parallel and returns all tool_results together in one turn. Each turn boundary costs a full prefill + decode warmup (10-30s wall time on long context), so a serial 4-Read sequence pays that cost 4 times for no model benefit. Only serialize when a later call's input genuinely depends on an earlier call's output (e.g. "Read the file we just located via Glob"). The classic anti-pattern this targets: an investigation phase that emits 5-15 single-Read turns to scan the codebase before the first edit — that phase should be 1-3 multi-Read turns instead.

**Worked example (WOR-399).** The manifest's `allowed_paths` is `["app/core/watcher/watcher_helpers.py", "tests/test_watcher_helpers.py"]` and you also want to see how the upstream `manifest.py` defines its model. That's three independent Reads — none depends on the others' output.

❌ **Wrong** — three assistant turns, three single-Read messages, three turn boundaries (≈30-90s of pure overhead):
```
turn 1: assistant: "I'll read the helper first."
        tool_use: Read app/core/watcher/watcher_helpers.py
turn 2: assistant: "Now the test file."
        tool_use: Read tests/test_watcher_helpers.py
turn 3: assistant: "Now the manifest model."
        tool_use: Read app/core/manifest.py
```

✅ **Right** — one assistant message, three `tool_use` blocks together, one turn boundary:
```
turn 1: assistant: "Reading the helper, its test, and the manifest model in parallel."
        tool_use: Read app/core/watcher/watcher_helpers.py
        tool_use: Read tests/test_watcher_helpers.py
        tool_use: Read app/core/manifest.py
```

Same behavior applies to: Glob + Grep + Read together at the start of an investigation; multiple greps for sibling patterns (e.g. `patch("`, `from app.core.X import`); running `git status` + `git diff` + `git log` together; reading every file in `allowed_paths` before the first Edit. If the next call's input does NOT come from a previous call's output, it belongs in the same message.

**New Python files** — read the type signatures of source functions *before* writing a new `.py` file so annotations are correct on the first attempt. The mypy hook will report errors immediately after Write — fix them before moving on.

**New test files** — before writing a new `tests/test_*.py` file, read at least one existing sibling test file to understand the fixture patterns, mock conventions, and how real objects (not MagicMock) are constructed for this codebase. The pytest hook runs the file automatically after each edit — watch its output rather than triggering manual pytest runs.

**Creating new files** — use the Write tool, not Bash heredocs. Heredocs with Python source have shell quoting issues on Windows (single quotes inside the body break the delimiter). The Write tool handles any content without escaping. If the Write tool is unavailable (local model sessions), use a single-quoted Bash heredoc instead — `python3 << 'PYEOF'` with the closing `PYEOF` at column 0; the single-quoted delimiter prevents the shell from interpreting any characters inside, including single quotes in Python source.

**Package reorganizations** — when moving multiple files into a new subpackage directory: (1) move ALL source files first, (2) update ALL imports in every consumer file, (3) write `__init__.py` LAST. Do not run pytest at any intermediate step — the package is broken until every file is in place and every import is updated, so any pytest run before that is noise and will always produce `ModuleNotFoundError`. After all files are moved and imports updated (but before `__init__.py` and pytest), make an intermediate WIP commit to preserve the structural work: `git add -A && git commit -m "WIP: <ticket_id> package structure complete, pre-check"` — this prevents losing all progress if the session ends before checks pass.

### 3.5. Post-implementation checks (before required_checks)

**If any files were moved or renamed to a different module path**, grep for string-based mock patch targets that reference the old path and update them — import fixers do not touch these:

```bash
# replace <old.module.path> with the module that was moved, e.g. app.core.watcher_subprocess
grep -rn 'patch("' tests/ | grep '<old.module.path>'
```

Update every match to the new path before running pytest. Missing this causes tests that use `unittest.mock.patch()` to fail with `AttributeError` or `ModuleNotFoundError` even though all real imports are correct.

**Also grep for bare from-imports** — `patch("...")` grep only finds mock strings, not `from app.core.old_module import X` in conftest.py, fixtures, or helper files. Run a second check:

```bash
# replace <old.module.path> with the moved module, e.g. app.core.watcher_types
grep -rn 'from <old.module.path> import' tests/ app/
```

Update every from-import to the new path. Missing this causes `ModuleNotFoundError` in conftest.py or fixture files that fires before any test runs — all tests fail even though the implementation is correct.

For module renames affecting many files, use `replace_all=True` on the Edit tool rather than updating occurrences one at a time — `Edit(file_path=..., old_string="app.core.old_module", new_string="app.core.new.module", replace_all=True)` replaces every occurrence in the file in one round-trip. One call per (file, module-name) pair covers the whole migration.

**If any instance methods were extracted from a class into a new module-level function**, grep for `patch.object` calls targeting those methods — they must be converted from `patch.object(instance, "method")` to `patch("new.module.path.method")`:

```bash
# replace <ClassName> with the class methods were extracted from, e.g. Watcher
grep -rn 'patch\.object' tests/ | grep '<ClassName>'
```

`patch.object` patches the method on the instance; once the function is module-level it no longer exists on the class and the patch silently does nothing or raises `AttributeError`. Convert every match to a string-path `patch("new.module.path.function_name")`.

### 3.5. Commit WIP state (WOR-267)

After completing all implementation (step 3), make an unconditional WIP commit
so that squash_wip_commits can squash it on the success path:

```bash
git add -A && git commit -m "wip: <ticket_id> implementation complete"
```

After writing any new test files, make a separate WIP commit for tests:

```bash
git add tests/ && git commit -m "wip: <ticket_id> tests written"
```

These instructions are UNCONDITIONAL — do not gate on check results or
implementation quality. The squash_wip_commits function (worker-side) will
squash all wip commits since the diverge point into a single commit before
the PR is created, giving fine-grained retry resume.

### 4. Run required checks

After implementation, run each command in `required_checks`. Run them
**verbatim** as written in the manifest — do NOT scope them down to specific
files for "speed", because the watcher will run them at the manifest's full
scope and any regression you missed will fail the ticket (WOR-353).

**Run the four checks in parallel — they have no data dependencies (WOR-413).**
`ruff check .`, `mypy app/`, `pytest`, `lint-imports` each read source files
independently and produce their own findings. Emit all four as separate
`tool_use` blocks in the **same assistant message** rather than as four
sequential Bash calls. The runtime executes them concurrently and returns all
four `tool_result` blocks in one user turn. Wall-time of the check phase
becomes `max(individual durations)` instead of `sum`, typically halving it.

```
# CORRECT: one assistant message with four parallel Bash tool_use blocks
[Bash] ruff check .
[Bash] mypy app/
[Bash] lint-imports
[Bash] pytest
```

```
# WRONG: four sequential Bash calls across four assistant messages
turn 1: [Bash] ruff check .  →  tool_result
turn 2: [Bash] mypy app/     →  tool_result
turn 3: [Bash] lint-imports  →  tool_result
turn 4: [Bash] pytest        →  tool_result
```

WOR-412 measured the parallel-tool-use rate at 9-25% across observed worker
sessions, with the check sweep being the largest reliably-parallel phase per
ticket. Aim for 100% parallel here. Top observed parallel combos are
`Bash + Bash`, `Read + Read`, `Bash + Read` — the same pattern applied to
`Read + Read + Read` during the investigation phase before the first edit.

If a check fails, fix it and re-run only the failing one (no need to re-run
the whole sweep until you're declaring success).

**Pytest scope rule (WOR-353):** When `required_checks` contains `pytest`
without arguments, run the **full** test suite — not just the test files
listed in `allowed_paths`. Sibling test files (e.g. `tests/test_X_metrics.py`,
`tests/test_X_recovery.py`) often import the same module you modified and
fail when their fixtures don't anticipate your changes.

If you want a fast iteration loop while implementing, you may run
`pytest <subset>` early — but **always run the unscoped `pytest` from
`required_checks` once before declaring success**. If it fails, you still
have the session context to fix it; if you skip it, the watcher catches
the failure after your session ends and the ticket is marked Blocked.

To proactively widen iteration scope without running the full suite, grep
for tests importing each modified source module:

```bash
# Find test files that import any module you modified
git diff --name-only $BASE_BRANCH...HEAD -- 'app/**/*.py' | while read f; do
  module="${f%.py}"
  module_path=$(echo "$module" | sed 's|/|.|g')
  grep -l "from $module_path" tests/*.py 2>/dev/null
done | sort -u
```

Pass that list (plus your scoped tests) to pytest during iteration. The
final unscoped `pytest` from `required_checks` is still mandatory.

**Never pipe pytest output through `tail`/`head` for self-validation (WOR-392).**
The pipe swallows pytest's exit code — `pytest 2>&1 | tail -20` returns
`tail`'s exit code (always 0), so even if pytest fails the surrounding
shell sees a successful command. Worse, `tail -20` may not include the
FAILED summary lines depending on output volume + warnings, so the worker
sees only passing dots and concludes "all green" — then writes
`result.json: status=success`, the watcher's unscoped pytest fails on the
real failures, and the worker's diff is lost.

Either run pytest unpiped:

```bash
pytest -q
```

Or redirect to a file and check the exit code separately:

```bash
pytest -q > /tmp/pytest-out.txt 2>&1; echo "exit=$?"; tail -50 /tmp/pytest-out.txt
```

The `echo $?` line is the gate, not the visible output. If `exit != 0`,
inspect `/tmp/pytest-out.txt` directly with Read or grep — never trust a
truncated tail to tell you the test summary.

**A failing test is never "pre-existing" without verification (WOR-389).**
If pytest fails, the temptation is to dismiss the failure as "unrelated to
my ticket" and write `result.json: status=success` with a footnote. That
is forbidden. Workflow when pytest fails:

1. **Investigate.** Is it caused by your change? Most often yes — a
   sibling test imports a module you edited and its fixtures don't
   anticipate your change. The failure name is a strong signal.
2. **If yes, fix it.** Do not write success. The fix is part of the
   ticket scope by definition (your change broke a test).
3. **Only if you genuinely believe the failure is pre-existing**
   (failure existed on the `base_branch` tip BEFORE your first commit),
   verify with:

   ```bash
   git stash
   pytest <failing_test_path>::<failing_test_name>
   git stash pop
   ```

   If the test still fails on the stashed (pre-your-change) tree,
   the failure is genuinely pre-existing. Document it explicitly in
   the `notes` field of `result.json` and you may write
   `status=success`. The watcher will see the same failure on its
   `required_checks` run; it tags `success_outcome_state_mismatch`,
   which the operator reviews.
4. **Never** write `status=success` with a footnote like "one
   pre-existing failure unrelated to this ticket" without performing
   step 3. Workers that did this on WOR-135 wave-1 and WOR-331 lost
   real diffs because the watcher's pytest correctly invalidated the
   self-reported success.

If any required check fails:
- Record the failure in the result artifact (step 5)
- If `failure_policy.on_check_failure` is `"abort"`: stop here, write a failed result
- If `failure_policy.on_check_failure` is `"warn"`: log the failure and continue

Run each command in `optional_checks` for information only — failures do not block.

### 4.5. Commit changes

After all required checks pass, stage and commit everything:

```bash
git add -A
git commit -m "Part of <ticket_id>: <one-line summary of what was implemented>"
```

If there is nothing to commit (no changes made), write a failed result artifact with `failure_reason: "No changes were made — nothing to commit"` and stop.

If the commit is rejected by a pre-commit hook, fix the issue and retry the commit once. If it still fails, write a failed result artifact with the hook output as `failure_reason`.

### 5. Write the result artifact

Write a JSON result file to `artifact_paths.result_json`. Create parent dirs as needed.

**Field semantics:** `summary` holds the ticket-scope summary (what was implemented).
`notes` holds side-discoveries — bugs, quirks, or improvements observed during this
worker session that fall outside this ticket's scope. The watcher auto-posts `notes`
to the WOR-254 improvement log when it exceeds ~50 characters. Do not duplicate the
ticket summary in `notes`; keep it focused on unexpected findings.

**On success:**
```json
{
  "ticket_id": "<ticket_id>",
  "status": "success",
  "summary": "<one-paragraph description of what was implemented>",
  "checks_passed": ["<check1>", "<check2>"],
  "checks_failed": [],
  "notes": "<side-discoveries (bugs/quirks/improvements outside this ticket's scope); auto-posted to WOR-254 if >50 chars — do not duplicate ticket-scope summary here>"
}
```

**HARD RULE — `checks_passed` must list the manifest's exact `required_checks` strings (WOR-456).** `checks_passed` records that you ran the manifest's `required_checks`, NOT that pre-commit / PostToolUse hooks passed. Copy the exact command strings from `manifest.required_checks` (e.g. `"ruff check ."`, `"mypy app/"`, `"pytest"`, `"lint-imports"`) verbatim into `checks_passed`. Do **not** list pre-commit hook names (`ruff`, `ruff-format`, `bandit`, `trailing-whitespace`, `end-of-file-fixer`, …) — those are not the contract checks, and the watcher now cross-checks the two lists at finalize time. If `checks_passed` does not contain every `required_checks` entry (exact string match), the watcher rejects the result as a contract violation and marks the ticket Blocked — even though you wrote `status: success`. Only write `status: success` after you have actually run every `required_checks` command and it passed.

**On failure:**
```json
{
  "ticket_id": "<ticket_id>",
  "status": "failed",
  "summary": "<what was attempted>",
  "checks_passed": ["<any that passed>"],
  "checks_failed": ["<failed check command>"],
  "failure_reason": "<specific error or reason>",
  "notes": "<context for the watcher or cloud escalation>"
}
```

**Do NOT copy the manifest anywhere.** `artifact_paths.manifest_copy` already
points at the path where `/start-ticket` wrote the manifest (the same one you
loaded in step 0). The watcher's `copy_manifest_to_worktree` step also placed
it there before launching this session. There is nothing to copy — the manifest
is already at its documented audit path. (Earlier skill text instructed a
"copy manifest" step; that was a no-op that wasted 2-3 turns per session as
the model attempted to `cp` a file onto itself before realizing the
redundancy. WOR-322 paid ~12 minutes of wall time to this. WOR-356.)

**Do NOT `git add` / commit `.claude/artifacts/**` into the branch.**
`result.json` and `last_failure.json` live under `.claude/artifacts/`, which is
gitignored on purpose. Never `git add -f` (force) them or otherwise commit them
into the worker branch. The watcher reads `result.json` from the **main-repo**
artifact path, not from a branch commit — a committed-into-branch result.json is
invisible to `finalize_worker` (the run is then treated as "no result" → Blocked
even when the work is perfect), and the committed file also bloats the diff,
which can itself trip the allowed_paths gate. Write the result artifact in place
(step 5) and leave it **uncommitted**; the watcher preserves it. (WOR-501: a
sound max-effort implementation was lost exactly this way — finalize is now also
tolerant of a worktree-located result.json as a backstop, but workers must still
never commit it.)

### 6. Linear updates (comments only — state is owned by the watcher)

**On success:** do nothing in Linear. The watcher reads the result artifact and handles PR creation and state transitions. Do not call `/finalize-ticket`.

**On failure:** post a single Linear comment summarising the failure for human visibility — do **not** call `save_issue` to set state. The watcher's `finalize_worker` reads the result artifact + escalation policy and applies the correct state transition (`Blocked`, or `In Progress` with an escalation note when `failure_policy.escalate_to_cloud` is true).

```
save_comment(issueId: "<ticket_id>", body: "Local worker failed: <one-line reason>. See result artifact: <artifact_paths.result_json>")
```

### 7. Exit

Exit cleanly after writing the result artifact. The watcher will:
1. Detect the result artifact (rc=0)
2. Run `required_checks` in the worktree
3. Create the PR targeting `base_branch`
4. Advance the Linear ticket state to `in_review`, then `merged_to_epic` once CI passes

**Do NOT run `/finalize-ticket`** — calling it from a watcher-spawned session creates a duplicate PR and bypasses the correct state machine.

**Do NOT call `gh pr create`, `gh pr edit`, `gh pr merge`, or `git push origin`** — the PreToolUse hook `check_no_worker_pr.py` (WOR-444) will block these in worker sessions. The watcher rebases the worker branch onto the latest base and opens the PR itself at finalize time (WOR-445). The WOR-67 incident on 2026-05-11 — where a worker opened its own PR and the watcher's later `attempt_pr` returned "already exists", marking the ticket Blocked despite an open PR — is the exact failure this hook prevents.
