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

- PR title: `WOR-NNN Short description`
- PR body: 2–3 bullet summary + test plan checklist + `Closes WOR-NNN`
- Run: `gh pr create`

### 4. Update Linear
Use the Linear MCP server to mark the issue as "In Review" (`save_issue` with the appropriate status).
