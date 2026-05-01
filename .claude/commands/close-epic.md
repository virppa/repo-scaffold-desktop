Finalize an epic and create the pull request to main.

Usage: `/close-epic WOR-NNN` where WOR-NNN is the epic issue identifier.

### 1. Verify all sub-tickets are merged (GitHub is the source of truth)

Fetch the epic with `get_issue($ARGUMENTS, includeRelations: true)` to get all child issues and the epic branch name.

**Step 1a — List all PRs that targeted the epic branch:**
```bash
gh pr list --base <epic-branch> --state all \
  --json number,title,headRefName,state,mergedAt
```
Match each child issue to a PR by branch name (Linear branch format: `wor-NN-short-description`). A child with no corresponding PR is treated as unmerged.

**Step 1b — Classify each PR and repair Linear state:**

For each **merged** PR (`mergedAt` not null): if the corresponding Linear issue is not already Done, mark it Done now:
`save_issue(id: "WOR-X", state: "Done")`
(Linear's "merge → Done" automation never fires for epic-branch-targeting PRs — this is the repair step.)

For each **open** PR: check CI status:
```bash
gh pr checks <PR-number> --json name,state,conclusion
```
Classify as:
- **CI failing** — any check has `conclusion == "FAILURE"` or `"TIMED_OUT"`
- **CI pending** — checks still running (`state == "IN_PROGRESS"`), none failing
- **CI passing but not merged** — all checks pass; auto-merge may not have triggered, flag for investigation

**Step 1c — Block if anything is unmerged:**

If there are open PRs or children with no PR at all, stop:
```
Cannot close epic — the following sub-tickets are not yet merged:

CI failing:
  WOR-X: #<N> "<title>" — <failing check names>
CI pending:
  WOR-Y: #<M> "<title>" — checks still running
CI passing, not merged (investigate auto-merge):
  WOR-Z: #<P> "<title>"
No PR found:
  WOR-W — no PR was opened against <epic-branch>

Fix these before running /close-epic again.
```

If all child PRs are merged: confirm "All N sub-tickets confirmed merged via GitHub. Linear state repaired where stale." and continue.

### 2. Pull and verify the epic branch
Derive the epic branch name using the `epic/wor-NNN-slug` prefix (e.g. `epic/wor-49-template-system`).

```bash
git fetch origin
git checkout epic/<epic-slug>
git pull origin epic/<epic-slug>
```

### 2.5. File-size gate

Thresholds anchored to cloud API token cost and single-responsibility for parallel worker isolation (not local model context — vLLM FP8 throughput is flat 16K→262K, so file size has no throughput impact). See `docs/spikes/vllm-context-thresholds.md`.

```
ADVISORY_LOC = 500    # worth planning a split
RECOMMEND_LOC = 700   # real split needed soon; include recommendation in epic PR description
BLOCK_LOC = 1200      # cloud token cost + mixed responsibility — split before this PR
```

Check the LOC of every `.py` file modified across the full epic diff (all sub-tickets combined):

```bash
MODIFIED_PY=$(git diff --name-only main..<epic-branch> | grep '\.py$')
for f in $MODIFIED_PY; do
  [ -f "$f" ] && echo "$f: $(wc -l < "$f") LOC"
done
```

Skip files that no longer exist in the working tree (deleted). Classify each file by LOC and emit the appropriate message:

**Advisory (≥ 500 LOC):**
```
Note: <filename> is <N> LOC (≥ 500 — advisory). Consider splitting before this file grows further.
```
Continue — non-blocking.

**Recommend (≥ 700 LOC):**
```
Warning: <filename> is <N> LOC (≥ 700 — recommend). Split soon; include a recommendation in the epic PR description.
```
Continue — but flag the file in the PR body.

**Block (≥ 1,200 LOC):**
```
BLOCKED: <filename> is <N> LOC (≥ 1,200 — block threshold).
Cloud token cost and mixed responsibility — split <filename> before creating the epic → main PR.
```
**Stop here. Do not proceed to step 3 or create the PR.** Ask the user how to proceed.

This block is unconditional — it applies regardless of implementation mode.

### 2.6. Import Linter review

Check whether any new `.py` files were added across the entire epic diff (an indicator that a file split may have occurred):

```bash
git diff --diff-filter=A --name-only main..<epic-branch> | grep '\.py$'
```

If any new `.py` files appear in the output, print:

```
New Python module(s) detected: <list of files>
If these were created by splitting an existing module, review .importlinter and consider
adding contracts to enforce the new module boundaries. See existing contracts for examples.
```

Skip silently if no new `.py` files were added.

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

### 6. Epic Reviewer subagent
Gather the following inputs:
```bash
# Full diff of epic branch against main
git diff main..<epic-branch>

# Coverage report (reuse from step 4 if still in context)
pytest --cov=app --cov-report=term-missing --tb=short -q 2>&1 | tail -60
```

Fetch each child issue's title and acceptance criteria with `get_issue(id: "WOR-X")` (limit to sub-tickets identified in step 1).

Read the full content of `CLAUDE.md` now so it can be included inline in the subagent prompt.

Spawn the **epic-reviewer** subagent with a prompt containing:
1. List of sub-ticket identifiers, titles, and acceptance criteria (full text)
2. Full content of CLAUDE.md (pasted inline — do not pass a file path)
3. The full git diff
4. The pytest coverage output

The subagent returns a structured verdict. Read **only** the verdict — do not load raw diffs or coverage logs into the main session yourself.

**Act on the verdict:**
- **READY** — proceed to step 7 (Create the epic → main PR).
- **NEEDS_ATTENTION** — print the verdict to the user, then proceed to step 7 (Create the epic → main PR).
- **BLOCKED** — print the verdict and specific blocker list, then **stop**. Do not create a PR. Ask the user how to proceed.

### 7. Create the epic → main PR
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
Closes WOR-X
Closes WOR-Y
EOF
)"
```

Enumerate a `Closes WOR-X` line for **every child issue** in the epic (from Step 1 relations), one per line after `Closes WOR-NNN`. This triggers Linear's "merge to main → Done" automation for all sub-tickets as a final backstop.

This PR requires **human review and approval** — no auto-merge.

### 8. Update Linear
1. Mark the epic issue **In Review**: `save_issue(id: "$ARGUMENTS", state: "In Review")`
2. Check milestone progress with `list_milestones(project: "{{ linear_project }}")`. If 100%, note: "🎉 Milestone '<name>' is now complete."

### 9. Clean up worktrees
List any worktrees for sub-tickets that have already been merged into the epic branch:
```bash
git worktree list
```

For each sub-ticket worktree whose branch is fully merged (confirm with `git branch --merged epic/<epic-slug>`):
```bash
git worktree remove <worktree-path>
git branch -d <sub-ticket-branch>
```

Do not remove the epic branch worktree yet — wait until the epic PR merges to main.

### 10. Update the project page
Call `save_project(id: "87ca9685-f2e6-493f-a022-03ef2425d2ab")` with an updated `summary` (max 255 chars):
`WOR-NNN epic in review | <N> sub-tickets shipped | Next: <next epic or milestone>`
