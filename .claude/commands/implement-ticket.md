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
- `save_issue(id: "<ticket_id>", state: "<ticket_state_map.merged_to_epic>")` — only after the PR is created (run `/finalize-ticket` to create the PR first)
- Actually: at this point just leave the ticket in `InProgressLocal` — `/finalize-ticket` will create the PR and advance the state

**On failure:**
- If `failure_policy.escalate_to_cloud` is `true`: `save_issue(id: "<ticket_id>", state: "In Progress")` and post a Linear comment: `"Local worker failed after <N> checks. Escalating to cloud. See result artifact: <artifact_paths.result_json>"`
- Otherwise: `save_issue(id: "<ticket_id>", state: "Blocked")` and post a comment with the failure reason

### 7. On success — proceed to finalize

Run `/finalize-ticket` to create the PR targeting the epic branch and advance the ticket state to `MergedToEpic` (after CI passes).
