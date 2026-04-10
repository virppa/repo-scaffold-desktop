Look up the Linear issue with identifier $ARGUMENTS in the repo-scaffold-desktop project using the Linear MCP server.

Work through these phases in order:

### 1. As Product Owner — understand the requirement
- Restate the requirement in plain terms (one paragraph)
- Flag any ambiguity or missing information
- State the acceptance criteria (from the issue, or infer them if not specified)

### 2. As Architect — plan the implementation
- List which files need to change and what changes are needed
- List what new tests are needed (file, test name, what it verifies)
- Flag any security surface introduced: new I/O, user input handling, file operations, subprocess calls
- Note edge cases and overwrite behavior to consider

### 3. Create the branch
Run `git checkout -b <branch-name>` using the branch name from Linear's "Copy branch name" format (usually `WOR-NNN-short-description`).

### 4. Present the plan
Summarize as:
```
Branch: <branch-name>
Files to change:
  - path/to/file.py — what changes
Tests to write:
  - tests/test_X.py::test_name — what it verifies
Security surface: <none | description>
Edge cases: <list>
```

**STOP HERE. Do not write any code until the human approves this plan.**
