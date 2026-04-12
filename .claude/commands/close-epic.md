Finalize an epic and create the pull request to main.

Usage: `/close-epic WOR-NNN` where WOR-NNN is the epic issue identifier.

### 1. Verify all sub-tickets are done
Fetch the epic with `get_issue($ARGUMENTS, includeRelations: true)` to get all child issues.

Check the status of each child in Linear. If any child is not **Done**:
- List the incomplete tickets
- Block and stop: "Cannot close epic — the following tickets are not Done: WOR-X, WOR-Y"

### 2. Pull and verify the epic branch
Derive the epic branch name from the Linear "Copy branch name" format (e.g. `wor-49-template-system`).

```bash
git fetch origin
git checkout <epic-branch>
git pull origin <epic-branch>
```

### 3. Security check
Run a full security scan against main:
```bash
bandit -r app/ -q
```

Run a diff review: examine `git diff main..<epic-branch>` for OWASP Top 10 patterns —
- Unsanitised user input passed to subprocess, eval, or file paths
- Hardcoded credentials or tokens
- SQL/command injection surface
- Insecure file permissions

Report: **PASS**, **WARNINGS** (list them), or **FAIL** (block PR creation until fixed).

### 4. Test suite — full run
```bash
pytest --cov=app --cov-report=term-missing --tb=short -q
```

Coverage must be ≥ 80%. If below threshold: identify uncovered paths and write missing tests before continuing.

### 5. UI / integration tests
Run UI or integration tests if they exist:
```bash
# Try common locations — skip gracefully if none found
pytest tests/ui/ -q 2>/dev/null || echo "[skip] No UI tests found"
pytest tests/integration/ -q 2>/dev/null || echo "[skip] No integration tests found"
```

If no UI tests exist yet, note it explicitly: "No UI tests present — consider adding them before the next epic closes."

### 6. Create the epic → main PR
```bash
gh pr create --base main \
  --title "WOR-NNN <Epic title>" \
  --body "$(cat <<'EOF'
## Summary
- <bullet 1>
- <bullet 2>
- <bullet 3>

## Sub-tickets included
- WOR-X Title
- WOR-Y Title

## Test plan
- [ ] pytest passes with ≥ 80% coverage
- [ ] Security scan: PASS
- [ ] UI tests: <passed | not yet present>

**Milestone:** <milestone name>

Closes WOR-NNN
EOF
)"
```

This PR requires **human review and approval** — no auto-merge.

### 7. Update Linear
1. Mark the epic issue **In Review**: `save_issue(id: "$ARGUMENTS", state: "In Review")`
2. Check milestone progress with `list_milestones(project: "repo-scaffold-desktop")`. If 100%, note: "🎉 Milestone '<name>' is now complete."

### 8. Clean up worktrees
List any worktrees for sub-tickets that have already been merged into the epic branch:
```bash
git worktree list
```

For each sub-ticket worktree whose branch is fully merged (confirm with `git branch --merged <epic-branch>`):
```bash
git worktree remove <worktree-path>
git branch -d <sub-ticket-branch>
```

Do not remove the epic branch worktree yet — wait until the epic PR merges to main.

### 9. Update the project page
Call `save_project(id: "87ca9685-f2e6-493f-a022-03ef2425d2ab")` with an updated `summary` (max 255 chars):
`WOR-NNN epic in review | <N> sub-tickets shipped | Next: <next epic or milestone>`
