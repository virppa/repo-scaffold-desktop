Look up the Linear issue with identifier $ARGUMENTS in the {{ linear_project }} project using the Linear MCP server. Also fetch `get_issue($ARGUMENTS, includeRelations: true)` to see its milestone, labels, priority, parent epic, and any blocking relations.

Work through these phases in order:

### Spike gate
Check whether the issue carries a label whose name matches **Spike** (case-insensitive).

If the Spike label is present:
1. Set state to In Progress: `save_issue(id: "$ARGUMENTS", state: "In Progress")`
2. Post a comment: `save_comment(issueId: "$ARGUMENTS", body: "Spike ticket — implementing interactively (no watcher manifest). See CLAUDE.md spike workflow.")`
3. Print the following and **STOP** — do not create a branch, do not write a manifest:

```
This ticket is labelled Spike — interactive implementation required.

Spike tickets bypass the watcher. Implement them interactively:
  1. Create a branch: git checkout -b <branch-name>
  2. Investigate and document findings in docs/spikes/<name>.md
  3. Commit findings with: git commit -m "Part of $ARGUMENTS: ..."
  4. Run /finalize-ticket to open a PR (review_mode: human — no auto-merge)
  5. Human reviews before merge; close the Linear ticket manually after merge
```

**Do not write a ReadyForLocal manifest for Spike tickets.**

### Watcher status check
Check whether the watcher daemon is running by reading `.claude/watcher.pid`:
```bash
cat .claude/watcher.pid 2>/dev/null && echo "Watcher: running (PID $(cat .claude/watcher.pid))" || echo "Watcher: not running"
```
If not running, print this advisory (do not block or prompt):
```
Watcher: not running

  Cloud mode (Anthropic API):
    python -m app.cli watcher --worker-mode cloud

  Local mode (RTX 5090 + Ollama — pre-warm GPU first):
    set OLLAMA_KEEP_ALIVE=-1 && ollama run qwen3-coder:30b ""      # loads model into VRAM indefinitely; exit immediately after
    python -m app.cli watcher --worker-mode local

  Auto mode (uses each manifest's implementation_mode):
    python -m app.cli watcher
```

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
- Derive the epic branch name using the `epic/wor-NNN-slug` prefix (e.g. `epic/wor-49-template-system`). The `epic/` prefix keeps epic branches out of Linear's `wor-*` branch automation, preventing the epic issue from being moved to InProgressLocal on every push.
- Check whether that branch exists on the remote:
  ```bash
  git fetch origin
  git branch -a | grep epic/wor-NN
  ```
- If the epic branch does **not** exist yet — create it from main and push it:
  ```bash
  git checkout -b epic/<epic-slug>
  git push -u origin epic/<epic-slug>
  git checkout main
  ```
- If it already exists — confirm it is present on origin (no further action needed)

**If no parent epic exists:**
- Warn: "This ticket has no parent epic — branch will target main instead of an epic branch."
- Continue with the normal main-targeting flow (step 3 will branch off main)

### 0.55. Epic-size charter check (run when parent epic exists)

When the ticket has a parent epic, count the parent's sub-tickets via `list_issues(parentId: <epicId>)` and inspect the parent description for a `**Charter:** <sentence>` line and a `**Sub-ticket budget:** <N>` line.

Skip this check entirely if the parent epic carries the `meta-epic` label (these are long-lived umbrella issues like "Watcher Reliability" that accumulate independent reliability fixes by design).

Exclude sub-tickets in `Done`, `Cancelled`, `Duplicate`, or `MergedToEpic` states from the count — only count open work.

If `count >= 6` (or `count >= budget` if budget is set), surface this prompt to the human and wait for confirmation before proceeding with the rest of `/start-ticket`:

```
Parent epic <PARENT> "<parent title>" already has <N> open sub-tickets.
Charter: "<charter line from parent description, or '(no charter set)'>"

Does WOR-NNN "<this ticket title>" directly produce that charter outcome?

  yes — proceed (the ticket aligns with charter; budget pressure noted)
  no  — re-parent before proceeding:
        1. Move to standalone (clear parentId)
        2. Open a Wave 2 epic (sibling to current parent) and re-parent
        3. Find a more-fit existing parent

Which?
```

If the human picks `yes`, continue with the rest of `/start-ticket` unchanged.

If the human picks a re-parent option:
- **(1) Standalone**: `save_issue(id: "$ARGUMENTS", parentId: null)` and continue.
- **(2) Wave 2**: create the new epic with `save_issue(team: ..., title: "Wave 2 — <theme>", labels: ["Refactor", ...])`, then `save_issue(id: "$ARGUMENTS", parentId: "<new-epic-id>")` and continue.
- **(3) Different parent**: ask which one, then `save_issue(id: "$ARGUMENTS", parentId: "<new-parent>")` and continue.

This check is the runtime enforcement of the budget that `/groom-ticket` set; it ensures the budget pressure surfaces at start time even if a sub-ticket was filed without re-grooming the parent.

### 0.6. Coordination check
Query Linear for sibling tickets in the same epic that are currently In Progress:
```
list_issues(project: "{{ linear_project }}", state: "In Progress", parentId: <epicId>)
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
- List what new files will be created (not just edited) — for each one, add to `risk_flags` in the manifest: `"<filename>.py is a new file — worker must read source type signatures before writing and run mypy on the file immediately after creation"`
- List what new test files will be created — for each one, add to `risk_flags`: `"<test_file>.py is a new test file — worker must read a sibling test file first for fixture/mock patterns, then run pytest <file> -x immediately after creation"`
- If any instance methods are being extracted from a class into module-level functions, add to `risk_flags`: `"methods extracted from <ClassName> — grep for patch.object(instance, '<method>') in tests/ and convert to patch('new.module.path.<method>') — patch.object silently does nothing once the method is no longer on the class"`
- If the ticket involves moving files into a new subpackage, add to `risk_flags`: `"package reorganization — move ALL source files into the subpackage first, update ALL imports in consumers, then write __init__.py LAST — do not run pytest until the move is complete or ModuleNotFoundError will appear on every intermediate check"` — and add a second risk_flag with the explicit old→new module path mapping table for every moved module (e.g. `"patch path migration: app.core.watcher_subprocess → app.core.watcher.watcher_subprocess, app.core.watcher_worktrees → app.core.watcher.watcher_worktrees, ..."`) so the worker can use replace_all=True bulk substitution per file rather than discovering stale paths from test failures
- List what new tests are needed (file, test name, what it verifies)
- Flag any security surface introduced: new I/O, user input handling, file operations, subprocess calls
- Note edge cases and overwrite behavior to consider
- Assess local-model suitability: is the scope bounded (≤3 small/medium files, straightforward wiring)? Or does it touch large/complex modules (e.g. watcher.py, generator.py) requiring multi-step reasoning across many dependencies? Record your conclusion — it determines `implementation_mode` in the manifest.
- Classify the ticket's effort level using this rule: simple/additive work touching ≤2 files → 'high'; bounded multi-file work → 'xhigh'; complex/large modules or deep cross-file reasoning → 'max'. Record your classification in the manifest.
- **Taxonomy classification** (WOR-262) — record these 7 dimensions in the manifest. All optional but populate when you can:
  - `change_type` — one of `additive` (new feature/file), `modification` (existing behavior changes), `refactor` (no behavior change), `removal` (deletion/cleanup), `docs` (markdown / comments only)
  - `reasoning_demand` 1-5 — how much cross-file reasoning is needed (1 = local change in one function, 5 = touches many modules with non-obvious invariants)
  - `scope_clarity` 1-5 — how explicit the AC is (1 = vague "improve X", 5 = exact file/line targets and expected behaviour)
  - `constraint_density` 1-5 — number of hard rules in `implementation_constraints` (1 = none, 5 = many strict gates)
  - `ac_specificity` 1-5 — how testable the AC is (1 = subjective only, 5 = each bullet maps to an assertion)
  - `tech_stack` — comma-separated tags of the technologies involved, e.g. `python,sqlite,pydantic` or `markdown,yaml`
  - `raw_extensions` — JSON array string of file extensions touched, e.g. `[".py",".md"]`

### 3. Create the branch and update Linear
Using the branch name from Linear's "Copy branch name" format (usually `WOR-NNN-short-description`):

**If this ticket has a parent epic with an epic branch:**
```bash
git checkout epic/<epic-slug>
git pull origin epic/<epic-slug>
git checkout -b <sub-ticket-branch>
git push -u origin <sub-ticket-branch>
git checkout main
```
The final `git checkout main` is required — the watcher uses `git worktree add` to check out the branch in an isolated directory, and git refuses to do that if the branch is already checked out in the main working tree.

**If no parent epic (targeting main):**
```bash
git checkout -b <branch-name>
git push -u origin <branch-name>
git checkout main
```
Same reason — leave main checked out so the watcher can worktree the sub-ticket branch.

**If the parent epic was previously Backlog** (i.e., this is the first sub-ticket being started in this epic), also promote all other Backlog children to **Todo**:
```
list_issues(project: "{{ linear_project }}", parentId: <epicId>, state: "Backlog")
→ for each result (excluding the current ticket): save_issue(id: "WOR-X", state: "Todo")
```
"Todo" signals "actively queued in this epic, not yet started" — distinguishes from Backlog items that aren't in scope yet. Skip this step if the epic was already In Progress.

### 4. Present the plan
Summarize as:
```
Branch: <branch-name> (off <epic-branch | main>)
Milestone: <milestone name> (<progress>%)
Epic: <parent issue title or "none">
Implementation mode: <local|cloud> — local-ready label <present → local / absent → cloud>
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

**Interactive implementation recommended** if ALL four conditions hold:
- `implementation_mode: cloud` (watcher would spawn another cloud session — no local-model benefit)
- `allowed_paths` contains only `.claude/commands/`, `CLAUDE.md`, `docs/`, or `schemas/` (no production Python)
- No parallel siblings currently In Progress (no worktree isolation needed)
- Small scope (≤ 3 files, no complex logic)

If all four apply, note it explicitly: *"This ticket is a good candidate for interactive implementation — skip the manifest and run `/implement-ticket $ARGUMENTS` in this session."*

**STOP HERE. Do not write any code until the human approves this plan.**

---

### 2.5. Populate context_snippets from related_files_hint

Before writing the manifest (step 4.6), populate the `context_snippets` field so the
local worker can read file headers without round-trip Read calls.

For each file path listed in `related_files_hint` (the architect populates this in step 2):
1. Read the first ~60 lines of the file using the **Read** tool — one Read call per file.
2. If the file has fewer than 80 lines, read the entire file.
3. Cap each snippet at **min(80 lines, 3000 characters)** — truncate at whichever limit is hit first.
4. Store as a JSON object keyed by file path: `{ "<file_path>": "<snippet_content>" }`.
5. If `related_files_hint` has **more than 10 files**, take only the first 10.

If `related_files_hint` is empty, leave `context_snippets` as null (omit it from the manifest).

Write the populated `context_snippets` object into the manifest at `context_snippets` key
(see step 4.6 for the full manifest structure).

### 4.6. After human approves the plan — generate the execution manifest

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
  "implementation_mode": "<local if ticket has local-ready label, otherwise cloud>",
  "effort": "<high|xhigh|max — effort classification from architect phase>",
  "change_type": "<additive|modification|refactor|removal|docs — taxonomy>",
  "reasoning_demand": <1-5: cross-file reasoning depth>,
  "scope_clarity": <1-5: how explicit the AC is>,
  "constraint_density": <1-5: number of hard rules>,
  "ac_specificity": <1-5: how testable the AC is>,
  "tech_stack": "<comma-separated tags, e.g. 'python,sqlite,pydantic'>",
  "raw_extensions": "<JSON array string of extensions, e.g. '[\".py\",\".md\"]'>",
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
    "failed": "Blocked"
  },
  "context_snippets": {
    "<file_path>": "<snippet content, capped at 80 lines / 3000 chars>"
  },
  "artifact_paths": {
    "result_json": ".claude/artifacts/<ticket_id_lower>/result.json",
    "manifest_copy": ".claude/artifacts/<ticket_id_lower>/manifest.json"
  }
}
```

Write this JSON to `.claude/artifacts/<ticket_id_lower>/manifest.json` (e.g. `.claude/artifacts/wor_80/manifest.json`). Create parent dirs as needed.

> **Path normalization:** `<ticket_id_lower>` is `ticket_id.lower().replace("-", "_")` — hyphens become underscores (e.g. `WOR-127` → `wor_127`). This matches `ArtifactPaths.from_ticket_id()` in `app/core/manifest.py`. Using `wor-127` (hyphen) will cause a "No such file or directory" error at watcher startup.

**Sync Linear blockedBy with the manifest.** If the manifest's `blocked_by_tickets` field is non-empty (e.g. `"blocked_by_tickets": ["WOR-266"]`), call `save_issue(id: "$ARGUMENTS", blockedBy: [...])` with the same set of ticket identifiers so that Linear's relation matches the manifest. If the list is empty, do not call `save_issue` for blockedBy.

Then:
1. Set the ticket to **ReadyForLocal** in Linear: `save_issue(id: "$ARGUMENTS", state: "ReadyForLocal")`
2. Post a Linear comment with the manifest path: `save_comment(issueId: "$ARGUMENTS", body: "Execution manifest written to .claude/artifacts/<ticket_id_lower>/manifest.json — watcher may now pick up.")`

The cloud preflight is now complete.

**STOP HERE. Do NOT run `/implement-ticket`. The watcher daemon will pick this ticket up automatically once it detects `ReadyForLocal` state. Your job for this session is done.**

To monitor worker progress once the watcher picks up the ticket:
```bash
# Worker log (stdout + stderr from the claude session):
tail -f .claude/worktrees/<worker-branch>/.claude/worker_<ticket_id_lower>.log
# e.g. for WOR-62:
tail -f ".claude/worktrees/wor-62-structured-claudemdj2-for-full_agentic-preset/.claude/worker_wor-62.log"

# Result artifact (written when worker finishes):
cat .claude/artifacts/<ticket_id_lower>/result.json
```

---

### 5. Opportunistic issue capture (after plan is shown — do not delay the plan for this)

While reading the codebase to plan this ticket you may have noticed things outside the current scope. Surface anything that looks like:
- An apparent bug in code you read (not in scope for this ticket)
- A missing feature that pairs naturally with this work
- An unhandled edge case that could cause a real problem

**Rules:**
- Only surface things genuinely encountered while reading — no extra scans
- Check existing Linear issues first (`list_issues` with `project: "{{ linear_project }}"`) to avoid duplicates
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
