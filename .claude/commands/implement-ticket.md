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

### 2. Set ticket state to InProgressLocal

`save_issue(id: "<ticket_id>", state: "<ticket_state_map.in_progress_local>")`

### 3. Implement

Implement the work described in `objective` and `acceptance_criteria`. Obey these hard rules at all times:

**Allowed paths** — only write to paths matching `allowed_paths` globs. If the list is empty, any path under the repo root is allowed (excluding forbidden paths below).

**Forbidden paths** — never write to paths matching `forbidden_paths` globs. If a task seems to require touching a forbidden path, ABORT and write a failed result artifact (see step 5).

**Constraints** — follow every item in `implementation_constraints` exactly.

**No re-planning** — do not re-read Linear, re-query the project, or change scope. If something in the codebase is surprising, implement defensively within the manifest scope and note it in the result artifact summary.

### 3.5. Post-implementation checks (before required_checks)

**If any files were moved or renamed to a different module path**, grep for string-based mock patch targets that reference the old path and update them — import fixers do not touch these:

```bash
# replace <old.module.path> with the module that was moved, e.g. app.core.watcher_subprocess
grep -rn 'patch("' tests/ | grep '<old.module.path>'
```

Update every match to the new path before running pytest. Missing this causes tests that use `unittest.mock.patch()` to fail with `AttributeError` or `ModuleNotFoundError` even though all real imports are correct.

### 4. Run required checks

After implementation, run each command in `required_checks` in order:

```bash
<check command 1>
<check command 2>
...
```

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

**On success:**
```json
{
  "ticket_id": "<ticket_id>",
  "status": "success",
  "summary": "<one-paragraph description of what was implemented>",
  "checks_passed": ["<check1>", "<check2>"],
  "checks_failed": [],
  "notes": "<any surprising findings or edge cases encountered>"
}
```

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

Also copy the manifest to `artifact_paths.manifest_copy` for audit purposes.

### 6. Update Linear

**On success:**
Leave the ticket in `InProgressLocal`. The watcher reads the result artifact and handles PR creation and state transitions — do NOT call `/finalize-ticket`.

**On failure:**
- If `failure_policy.escalate_to_cloud` is `true`: `save_issue(id: "<ticket_id>", state: "In Progress")` and post a Linear comment: `"Local worker failed after <N> checks. Escalating to cloud. See result artifact: <artifact_paths.result_json>"`
- Otherwise: `save_issue(id: "<ticket_id>", state: "Blocked")` and post a comment with the failure reason

### 7. Exit

Exit cleanly after writing the result artifact. The watcher will:
1. Detect the result artifact (rc=0)
2. Run `required_checks` in the worktree
3. Create the PR targeting `base_branch`
4. Advance the Linear ticket state to `in_review`, then `merged_to_epic` once CI passes

**Do NOT run `/finalize-ticket`** — calling it from a watcher-spawned session creates a duplicate PR and bypasses the correct state machine.
