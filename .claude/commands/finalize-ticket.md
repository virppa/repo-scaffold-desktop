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

Fetch the issue with `get_issue(id, includeMilestone: true)` to get its milestone name.

- PR title: `WOR-NNN Short description`
- PR body:
  - 2–3 bullet summary
  - Milestone line: `**Milestone:** <milestone name>`
  - Test plan checklist
  - `Closes WOR-NNN`
- Run: `gh pr create`

### 4. Update Linear
Use the Linear MCP server to:
1. Mark the issue as **In Review**: `save_issue(id: "WOR-NNN", state: "In Review")`
2. Fetch the milestone this issue belongs to with `list_milestones(project: "repo-scaffold-desktop")`. If the milestone's progress has reached 100%, note it explicitly: "🎉 Milestone '<name>' is now complete."

### 5. Update the project page
Update the **repo-scaffold-desktop** project summary to reflect what just shipped.

Call `save_project(id: "87ca9685-f2e6-493f-a022-03ef2425d2ab")` with an updated `summary` (max 255 chars) capturing the current state. Example format:
`MVP Build 88% | WOR-NNN just merged | In Review: WOR-X | Next: WOR-Y`

Only update `summary` here — full description refresh happens in `/prioritize`.

### 6. Return to main
```bash
git checkout main
```
