Finalize the current ticket and create a pull request.

### 1. Test coverage
Run pytest with coverage:
```bash
pytest --cov=app --cov-report=term-missing --tb=short -q
```

If coverage is below 80%:
- Identify which code paths are uncovered
- Write the missing tests before proceeding

### 2. Documentation
Check if any of the following need updating:
- New slash commands or workflow steps → update the **Development workflow** section in `CLAUDE.md`
- New CLI commands or setup steps → update the **Commands** section in `CLAUDE.md`
- New architectural decisions → update the **Architecture** section in `CLAUDE.md`

Only update docs if the change is meaningful — do not document implementation details.

### 3. Create the pull request
Derive the Linear identifier from the current branch name (e.g., `WOR-42-short-description` → `WOR-42`).

Fetch the issue with `get_issue(id, includeRelations: true)` to get its milestone and parent epic. If the epic has an in-progress branch (not main), that is the PR target.

**If this ticket has a parent epic with an epic branch:**
```bash
gh pr create --base <epic-branch> \
  --title "WOR-NNN Short description" \
  --body "..."
# Enable auto-merge — merges automatically when CI passes, no manual approval needed
gh pr merge --auto --squash <PR-number>
```

**If no parent epic (targeting main):**
```bash
gh pr create --base main \
  --title "WOR-NNN Short description" \
  --body "..."
# No auto-merge — human review required for main
```

PR body format (both cases):
- 2–3 bullet summary
- `**Milestone:** <milestone name>` (if set)
- `**Epic:** <parent issue title>` (if set)
- Test plan checklist
- `Closes WOR-NNN`

### 4. Update Linear
Mark the issue as **In Review**: `save_issue(id: "WOR-NNN", state: "In Review")`

What "In Review" means depends on the PR target:
- **Sub-ticket → epic branch (auto-merge):** "In Review" = PR is open and will merge automatically when CI passes. `/close-epic` will repair this to Done once the PR is confirmed merged. If CI fails, the PR stays open and `/close-epic` will catch and report it.
- **Ticket → main (human review):** "In Review" = PR is open, awaiting human approval.

Fetch the milestone this issue belongs to with `list_milestones(project: "repo-scaffold-desktop")`. If the milestone's progress has reached 100%, note it explicitly: "🎉 Milestone '<name>' is now complete."

### 5. Update the project page
Update the **repo-scaffold-desktop** project summary to reflect what just shipped.

Call `save_project(id: "87ca9685-f2e6-493f-a022-03ef2425d2ab")` with an updated `summary` (max 255 chars) capturing the current state. Example format:
`MVP Build 88% | WOR-NNN just merged | In Review: WOR-X | Next: WOR-Y`

Only update `summary` here — full description refresh happens in `/prioritize`.

### 6. Return to base context
If this session entered a worktree via `EnterWorktree`, call `ExitWorktree` now to return to the main repo context.

```bash
git checkout main
```

---

### 7. Opportunistic issue capture

Now that implementation is complete, you have the deepest context on this area of the codebase. Surface anything noticed during implementation that falls outside the current ticket's scope:
- Bugs encountered in code you touched or read
- Missing features that would naturally complement what was just built
- Unhandled edge cases or input validation gaps

**Rules:**
- Only surface things genuinely encountered during implementation — no extra scans
- Check existing Linear issues first (`list_issues` with `project: "repo-scaffold-desktop"`) to avoid duplicates
- Maximum 3 suggestions; keep only the most impactful
- Present suggestions and wait for approval before creating anything

If you have suggestions:

```
**Spotted during implementation:**
1. [Bug/Feature/Fix] Title — one-line description
   Suggested: Type=<label>, Stream=<label>, Epic=WOR-NNN or "new epic needed", Milestone=<name>, Priority=<N>
```

On human approval: create each approved issue with `save_issue`, setting labels, `parentId` (epic), and milestone. If the right epic doesn't exist yet, create it first with `save_issue` (no parentId), then set it as parent.

If nothing was spotted, skip this section silently — do not say "nothing spotted."
