Batch-plan all groomed sub-tickets of epic $ARGUMENTS and queue them for autonomous local execution.

Fetch the epic with `get_issue($ARGUMENTS, includeRelations: true)` to get its title, milestone, and children.

---

### Watcher status check
Check whether the watcher daemon is running:
```bash
cat .claude/watcher.pid 2>/dev/null && echo "Watcher: running (PID $(cat .claude/watcher.pid))" || echo "Watcher: not running"
```
If not running, print this advisory (do not block):
```
Watcher: not running
  Start with: python -m app.cli watcher
  Start with: python -m app.cli watcher --worker-mode cloud
  Start with: python -m app.cli watcher --worker-mode local
```

---

### 0. Clean up local branches
```bash
git fetch --prune
git checkout main
git pull
git branch --merged main | grep -v '^\*\? *main$' | xargs -r git branch -d
```

---

### 1. Collect eligible sub-tickets

Fetch all children of the epic:
```
list_issues(project: "repo-scaffold-desktop", parentId: "$ARGUMENTS")
```

Keep only tickets in state `Groomed` or `Todo`. Skip anything already `ReadyForLocal`, `InProgressLocal`, `In Progress`, `In Review`, `MergedToEpic`, or `Done`.

If no eligible tickets are found, print:
```
No Groomed/Todo sub-tickets found for $ARGUMENTS. Nothing to queue.
```
…and stop.

---

### 2. Epic branch setup

Derive the epic branch name from the epic's Linear "Copy branch name" format (e.g. `wor-49-template-system`).

Check whether the epic branch exists on the remote:
```bash
git fetch origin
git branch -a | grep <epic-slug>
```

- If it does **not** exist — create it from main and push:
  ```bash
  git checkout -b <epic-branch>
  git push -u origin <epic-branch>
  git checkout main
  ```
- If it already exists — confirm it is present on origin, no further action needed.

If the epic was previously Backlog, promote all eligible sub-tickets (not the ones already past Groomed) to **Todo** now:
```
save_issue(id: "WOR-X", state: "Todo")   ← for each Backlog child
```

---

### 3. Architect pass — plan every eligible ticket

For each eligible ticket (process them all before writing any manifests):

**3a. Read the ticket**
- `get_issue(<id>, includeRelations: true)`
- Restate requirement in one sentence
- Extract or infer acceptance criteria

**3b. Plan the implementation**
- List files likely to change and why (infer from ticket title/description and codebase knowledge)
- List tests to write
- Flag any security surface (new I/O, subprocess calls, user input)
- Assess risk: `low` / `medium` / `high`

**3c. Record inferred file set**
Store `{ ticket_id, branch_name, files: [...] }` for conflict detection in step 4.

---

### 4. Conflict detection and batching

Compare the inferred file sets across all planned tickets:

- **Batch 1 (parallel-safe):** tickets whose file sets do not overlap with any other ticket in Batch 1
- **Batch 2+ (sequential):** tickets that share files with a Batch 1 ticket — must wait until their conflicting predecessor is `MergedToEpic`

Algorithm:
1. Sort tickets by priority (ascending — lower number = higher priority) to prefer high-priority tickets for Batch 1
2. Greedily assign each ticket to Batch 1 if it has no file overlap with already-assigned Batch 1 tickets; otherwise assign to the lowest-numbered batch where no overlap exists

Print the batching plan before writing any manifests:

```
Epic: WOR-49 — Template system
Epic branch: wor-49-template-system

Batch 1 — queuing now (parallel-safe):
  WOR-45  wor-45-add-yaml-preset          files: presets.py, config.py
  WOR-48  wor-48-jinja-template-helpers   files: templates/, generator.py
  WOR-51  wor-51-test-coverage-gap        files: tests/

Batch 2 — blocked until batch 1 merges (file conflicts):
  WOR-46  wor-46-config-schema-update     files: config.py  ← conflicts with WOR-45
  WOR-52  wor-52-generator-refactor       files: generator.py  ← conflicts with WOR-48

Skipped (already past Groomed):
  WOR-47  InProgressLocal
```

**STOP HERE. Do not write any manifests or create branches until the human approves this batching plan.**

---

### 5. After human approves — create branches and write manifests

For each **Batch 1** ticket (in parallel — do not wait between tickets):

**5a. Create the sub-ticket branch**
```bash
git checkout <epic-branch>
git pull origin <epic-branch>
git checkout -b <sub-ticket-branch>
git push -u origin <sub-ticket-branch>
git checkout main
```

**5b. Write the execution manifest**

Write to `.claude/artifacts/<ticket_id_lower>/manifest.json`:

```json
{
  "manifest_version": "1.0",
  "ticket_id": "<TICKET_ID>",
  "epic_id": "$ARGUMENTS",
  "title": "<ticket title>",
  "priority": <0-4>,
  "status": "ReadyForLocal",
  "parallel_safe": true,
  "risk_level": "<low|medium|high>",
  "risk_flags": ["<any specific risk notes>"],
  "implementation_mode": "local",
  "review_mode": "auto",
  "base_branch": "<epic-branch>",
  "worker_branch": "<sub-ticket-branch>",
  "worktree_name": null,
  "objective": "<one-paragraph restatement>",
  "acceptance_criteria": ["<each AC bullet>"],
  "implementation_constraints": ["<hard rules, e.g. do not modify app/ui/>"],
  "allowed_paths": ["<glob patterns from step 3b>"],
  "forbidden_paths": ["app/ui/**", ".env", ".mcp.json", ".claude/settings*"],
  "related_files_hint": ["<files from step 3b>"],
  "required_checks": ["ruff check .", "mypy app/", "pytest"],
  "optional_checks": [],
  "done_definition": "<plain-English done criteria>",
  "failure_policy": {
    "on_check_failure": "abort",
    "max_retries": 0,
    "escalate_to_cloud": false
  },
  "ticket_state_map": {
    "in_progress_local": "InProgressLocal",
    "merged_to_epic": "MergedToEpic",
    "ready_for_review": "EpicReadyForCloudReview",
    "failed": "Blocked"
  },
  "artifact_paths": {
    "result_json": ".claude/artifacts/<ticket_id_lower>/result.json",
    "manifest_copy": ".claude/artifacts/<ticket_id_lower>/manifest.json"
  }
}
```

**5c. Update Linear**
1. `save_issue(id: "<ticket_id>", state: "ReadyForLocal")`
2. `save_comment(issueId: "<ticket_id>", body: "Execution manifest written to .claude/artifacts/<ticket_id_lower>/manifest.json — watcher may now pick up.")`

Repeat 5a–5c for every Batch 1 ticket before moving on.

For **Batch 2+** tickets: do nothing in Linear — leave them at `Todo`. They will be queued in a follow-up `/start-epic` run (or manual `/start-ticket`) once their Batch 1 predecessors have merged.

---

### 6. Final summary

Print:

```
Queued for watcher:
  WOR-45  wor-45-add-yaml-preset        → ReadyForLocal  (manifest: .claude/artifacts/wor_45/manifest.json)
  WOR-48  wor-48-jinja-template-helpers → ReadyForLocal  (manifest: .claude/artifacts/wor_48/manifest.json)
  WOR-51  wor-51-test-coverage-gap      → ReadyForLocal  (manifest: .claude/artifacts/wor_51/manifest.json)

Deferred (run /start-epic $ARGUMENTS again after batch 1 merges):
  WOR-46  wor-46-config-schema-update
  WOR-52  wor-52-generator-refactor
```

**STOP HERE. Do NOT run `/implement-ticket` for any ticket. The watcher daemon will pick up all `ReadyForLocal` tickets automatically.**
