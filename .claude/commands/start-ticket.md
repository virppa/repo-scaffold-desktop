Look up the Linear issue with identifier $ARGUMENTS in the repo-scaffold-desktop project using the Linear MCP server. Also fetch `get_issue($ARGUMENTS, includeRelations: true)` to see its milestone, labels, priority, and any blocking relations.

Work through these phases in order:

### 0. Clean up local branches
Run the following to prune stale remote-tracking refs and delete any local branches that have been merged or whose remote is gone:
```bash
git fetch --prune
git checkout main
git pull
git branch --merged main | grep -v '^\*\? *main$' | xargs -r git branch -d
```

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
Run `git checkout -b <branch-name>` using the branch name from Linear's "Copy branch name" format (usually `WOR-NNN-short-description`).

Then immediately set the issue status to **In Progress** in Linear:
`save_issue(id: "$ARGUMENTS", state: "In Progress")`

Also prune stale local branches:
```bash
git fetch --prune
git branch --merged main | grep -v "^\*\? *main$"
# delete any listed branches with: git branch -d <branch>
```

### 4. Present the plan
Summarize as:
```
Branch: <branch-name>
Milestone: <milestone name> (<progress>%)
Files to change:
  - path/to/file.py — what changes
Tests to write:
  - tests/test_X.py::test_name — what it verifies
Security surface: <none | description>
Edge cases: <list>
```

**STOP HERE. Do not write any code until the human approves this plan.**
