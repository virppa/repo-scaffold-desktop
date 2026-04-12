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
list_issues(project: "repo-scaffold-desktop", filter: { parent: <epicId>, state: "In Progress" })
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

Then immediately set the issue status to **In Progress** in Linear:
`save_issue(id: "$ARGUMENTS", state: "In Progress")`

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
