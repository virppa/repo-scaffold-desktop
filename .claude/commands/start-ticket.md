Look up the Linear issue with identifier $ARGUMENTS in the repo-scaffold-desktop project using the Linear MCP server. Also fetch `get_issue($ARGUMENTS, includeRelations: true)` to see its milestone, labels, priority, parent epic, and any blocking relations.

Work through these phases in order:

### 0. Clean up local branches
Run the following to prune stale remote-tracking refs and delete any local branches that have been merged or whose remote is gone:
```bash
git fetch --prune
git checkout main
git pull
git branch --merged main | grep -v '^\*\? *main$' | xargs -r git branch -d
```

### 0.5. Epic branch setup
Check whether this ticket has a parent epic (`parentId` from `get_issue` relations):

**If a parent epic exists:**
- Derive the epic branch name from the epic issue's Linear "Copy branch name" format (e.g. `wor-49-template-system`)
- Check whether that branch exists on the remote:
  ```bash
  git fetch origin
  git branch -a | grep wor-NN-epic-slug
  ```
- If the epic branch does **not** exist yet — create it from main and push it:
  ```bash
  git checkout -b <epic-branch>
  git push -u origin <epic-branch>
  git checkout main
  ```
- If it already exists — confirm it is present on origin (no further action needed)

**If no parent epic exists:**
- Warn: "This ticket has no parent epic — branch will target main instead of an epic branch."
- Continue with the normal main-targeting flow (step 3 will branch off main)

### 0.6. Coordination check
Query Linear for sibling tickets in the same epic that are currently In Progress:
```
list_issues(project: "repo-scaffold-desktop", state: "In Progress", parentId: <epicId>)
```
For each In-Progress sibling:
- Show ticket ID, title, branch name
- Note which files it likely touches (infer from the ticket title/description or its Linear body)

Also list epic backlog tickets (not In Progress, not Done) and flag which are likely safe to start in parallel vs. likely conflicting based on expected file overlap.

Print a coordination summary before the plan:
```
Parallel work in this epic:
  WOR-45 (wor-45-branch) — likely touches presets.py, config.py — AVOID OVERLAP
Safe to start in another session now:
  WOR-48 — templates/ only — no file conflict expected
  WOR-51 — tests/ only — no file conflict expected
Likely conflicts:
  WOR-46 — also touches config.py
```
If no siblings are In Progress, skip this block silently.

### 1. As Product Owner — understand the requirement
- Restate the requirement in plain terms (one paragraph)
- Flag any ambiguity or missing information
- State the acceptance criteria (from the issue, or infer them if not specified)
- Note the milestone this ticket belongs to and how it fits the current milestone's goal
- Flag any active blockers from Linear — if this ticket is blocked by an open issue, warn before proceeding

### 2. As Architect — plan the implementation
- List which files need to change and what changes are needed
- List what new tests are needed (file, test name, what it verifies)
- Flag any security surface introduced: new I/O, user input handling, file operations, subprocess calls
- Note edge cases and overwrite behavior to consider

### 3. Create the branch and update Linear
Using the branch name from Linear's "Copy branch name" format (usually `WOR-NNN-short-description`):

**If this ticket has a parent epic with an epic branch:**
```bash
git checkout <epic-branch>
git pull origin <epic-branch>
git checkout -b <sub-ticket-branch>
git push -u origin <sub-ticket-branch>
```
Then call `EnterWorktree` so this session operates in the sub-ticket branch's worktree, isolated from other parallel sessions.

**If no parent epic (targeting main):**
```bash
git checkout -b <branch-name>
git push -u origin <branch-name>
```
No worktree needed for solo work.

**If the parent epic was previously Backlog** (i.e., this is the first sub-ticket being started in this epic), also promote all other Backlog children to **Todo**:
```
list_issues(project: "repo-scaffold-desktop", parentId: <epicId>, state: "Backlog")
→ for each result (excluding the current ticket): save_issue(id: "WOR-X", state: "Todo")
```
"Todo" signals "actively queued in this epic, not yet started" — distinguishes from Backlog items that aren't in scope yet. Skip this step if the epic was already In Progress.

### 4. Present the plan
Summarize as:
```
Branch: <branch-name> (off <epic-branch | main>)
Milestone: <milestone name> (<progress>%)
Epic: <parent issue title or "none">
Files to change:
  - path/to/file.py — what changes
Tests to write:
  - tests/test_X.py::test_name — what it verifies
Security surface: <none | description>
Edge cases: <list>
```

If parallel-safe sibling tickets exist, append:
```
To work in parallel: open a new Claude Code session in this repo and run
`/start-ticket WOR-NN` for any ticket marked safe above.
```

**STOP HERE. Do not write any code until the human approves this plan.**

---

### 4.5. After human approves the plan — generate the execution manifest

Once the human says to proceed, generate and write an `ExecutionManifest` JSON to disk. This is the handoff artifact the local worker reads — it must not require re-reading Linear or re-planning.

Construct the manifest from the planning context gathered in steps 1–4:

```json
{
  "manifest_version": "1.0",
  "ticket_id": "<TICKET_ID>",
  "epic_id": "<EPIC_ID or null>",
  "title": "<ticket title from Linear>",
  "priority": <0-4 from Linear>,
  "status": "ReadyForLocal",
  "parallel_safe": <true if no file conflicts with In-Progress siblings>,
  "risk_level": "<low|medium|high — from security surface assessment>",
  "risk_flags": ["<any specific risk notes>"],
  "implementation_mode": "local",
  "review_mode": "auto",
  "base_branch": "<epic-branch or main>",
  "worker_branch": "<sub-ticket-branch>",
  "worktree_name": null,
  "objective": "<one-paragraph restatement from step 1>",
  "acceptance_criteria": ["<each AC bullet from step 1>"],
  "implementation_constraints": ["<hard rules from step 2, e.g. do not modify app/ui/>"],
  "allowed_paths": ["<glob patterns for files to change, from step 2>"],
  "forbidden_paths": ["app/ui/**", ".env", ".mcp.json", ".claude/settings*"],
  "related_files_hint": ["<files listed as relevant in step 2>"],
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

Write this JSON to `.claude/artifacts/<ticket_id_lower>/manifest.json` (e.g. `.claude/artifacts/wor_80/manifest.json`). Create parent dirs as needed.

Then:
1. Set the ticket to **ReadyForLocal** in Linear: `save_issue(id: "$ARGUMENTS", state: "ReadyForLocal")`
2. Post a Linear comment with the manifest path: `save_comment(issueId: "$ARGUMENTS", body: "Execution manifest written to .claude/artifacts/<ticket_id_lower>/manifest.json — watcher may now pick up.")`

The cloud preflight is now complete. The local worker will pick this up via `/implement-ticket $ARGUMENTS`.

---

### 5. Opportunistic issue capture (after plan is shown — do not delay the plan for this)

While reading the codebase to plan this ticket you may have noticed things outside the current scope. Surface anything that looks like:
- An apparent bug in code you read (not in scope for this ticket)
- A missing feature that pairs naturally with this work
- An unhandled edge case that could cause a real problem

**Rules:**
- Only surface things genuinely encountered while reading — no extra scans
- Check existing Linear issues first (`list_issues` with `project: "repo-scaffold-desktop"`) to avoid duplicates
- Maximum 3 suggestions; if you spotted more, keep only the most impactful
- Do not create anything — present suggestions and wait for approval

If you have suggestions, append them after the plan summary:

```
**Spotted while planning:**
1. [Bug/Feature/Fix] Title — one-line description
   Suggested: Type=<label>, Stream=<label>, Epic=WOR-NNN or "new epic needed", Milestone=<name>, Priority=<N>
```

On human approval: create each approved issue with `save_issue`, setting labels, `parentId` (epic), and milestone. If the right epic doesn't exist yet, create it first with `save_issue` (no parentId), then set it as parent on the new issue.

If nothing was spotted, skip this section silently — do not say "nothing spotted."
