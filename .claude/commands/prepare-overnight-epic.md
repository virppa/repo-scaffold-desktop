Prepare a fire-and-forget "overnight cleanup" mega-epic of 20-30 single-bound, parallel-safe sub-tickets that the watcher daemon can chew through unattended overnight.

**Design philosophy:** maximize independent throughput, accept losses. Single-ticket failures are expected; the morning workflow is `/close-epic` → epic→main PR with whatever subset succeeded. There is no rescue path. Tickets are independent by construction (no `blocked_by` chains; one file per ticket).

**Two operator gates:**
- **Gate 1 (Phase 5)** — review candidate list before any Linear or git writes
- **Gate 2 (Phase 8)** — final launch confirmation before the watcher dispatches

Either gate can abort cleanly.

Arguments: `$ARGUMENTS` may include `--target-count N` (default 25), `--epic-title "..."`, `--dry-run` (print everything but skip Linear writes + manifest writes).

---

### Watcher status check (advisory)

Check whether the watcher daemon is running:
```bash
cat .claude/watcher.pid 2>/dev/null && echo "Watcher: running (PID $(cat .claude/watcher.pid))" || echo "Watcher: NOT RUNNING"
```

The skill does NOT block on this — Gate 2 will refuse `launch` if the watcher is not running, but the operator can start it then. Sub-tickets stay in `Groomed` state safely until the operator sets them `ReadyForLocal` at Gate 2.

If watcher is not running, print:
```
Watcher: not running

To start before launch (Gate 2):
  python -m app.cli watcher              # auto mode (per-manifest implementation_mode)
  python -m app.cli watcher --worker-mode local --max-local-workers 8
```

---

### 0. Clean up local branches
```bash
git fetch --prune
git checkout main
git pull
git branch --merged main | grep -v '^\*\? *main$' | xargs -r git branch -d
```

---

### Phase 1 — Mining (two sources, prefer backlog over Sonar)

#### Source A: existing Linear backlog (preferred)

```
mcp__linear-server__list_issues(
    project: "{{ linear_project }}",
    state: "Groomed",
    labels: "Local-ready",
    limit: 100,
)
```

Also include `state: "Todo"` results (run a second query) — Todo tickets are queued in an active epic but not started, equally valid candidates.

For each result, fetch `get_issue(id, includeRelations: true)` to read its parent epic + labels.

**Filter to overnight-safe**:
- Description contains a `## Files` or `allowed_paths` section with **1-3 files** total. Skip if larger.
- Parent epic is NOT in the active high-priority list:
  - `WOR-335` (Watcher resilience)
  - `WOR-300` (Watcher metrics correctness)
  - `WOR-298` (Routing redesign)
  - `WOR-365` (Metrics enrichment Wave 2 — closing)
  - or any epic whose title contains "Wave 1" or "Critical"
- No open PR or branch already exists for the ticket (check via `gh pr list --search <WOR-NNN>` — skip if any).
- Priority is 3 (Normal) or 4 (Low). Never include Urgent (1) or High (2) — those need supervised execution.

For each surviving backlog candidate, record:
- Existing parent epic (will be replaced by the mega epic — operator can opt out per-ticket)
- Inferred file list from the description's `allowed_paths` block

#### Source B: SonarQube findings (top up to target count)

If backlog yielded ≥ target_count candidates, skip this step. Otherwise query Sonar for `target_count - len(backlog_candidates)` additional candidates:

```
mcp__sonarqube__issues(
    project_key: "virppa_repo-scaffold-desktop",
    severities: ["MAJOR", "MINOR"],
    statuses: ["OPEN", "CONFIRMED"],
    resolved: false,
    rules: [
        "python:S1172",   # unused parameter
        "python:S1481",   # unused local variable
        "python:S1854",   # dead store
        "python:S1143",   # unnecessary if/else
        "python:S125",    # commented-out code
        "python:S100",    # docstring naming convention
        "python:S5754",   # bare except clauses
    ],
    limit: 200,
    facets: ["files", "rules", "severities"],
)
```

**This rule list is the allow-list, not a starting point.** Add new rules ONLY after observing one overnight where they succeed without manual rescue.

For each Sonar finding, record:
- File path (`component` field)
- Rule ID + count
- Severity

---

### Phase 2 — Filter

Drop candidates where:

**Forbidden orchestration files** (large, slow per WOR-322 forensic):
- `app/core/watcher/watcher.py`
- `app/core/watcher/watcher_finalize.py`
- `app/core/watcher/dispatch.py`
- `app/core/generator.py`
- `app/core/metrics.py`
- `app/core/manifest.py`

**Forbidden directories**:
- `app/ui/**` (PySide6, hard to test headlessly)
- `tests/**` (don't auto-rewrite test patterns)
- `.claude/**` (don't touch skills/hooks)

**File-size cutoff**: any file > 500 LOC (per CLAUDE.md file-size advisory). Use `wc -l <file>` or skip via Read.

**Already-claimed**: any file that another open Linear ticket already targets (check via `list_issues` filtered to file path mentions). Avoid stomping on in-flight work.

**Hard cap**: if surviving candidates count > target_count × 1.2 (e.g. 30 for target 25), trim to target_count × 1.2 max before Phase 3.

---

### Phase 3 — Dedupe by file

**At most one ticket per file.** This eliminates file conflicts by construction — no two in-flight workers ever touch the same file.

For Sonar candidates, group findings by file. Multiple findings on `app/core/foo.py` become one ticket with title like:
> "Fix Sonar findings in foo.py: S1172 (3), S1481 (1)"

For backlog candidates, the existing ticket title is preserved.

If a backlog candidate AND a Sonar candidate target the same file, prefer the backlog candidate (already human-shaped).

---

### Phase 4 — Rank + cap to target

Sort surviving candidates by:
1. **Source preference**: backlog tickets first, Sonar second.
2. **Within each source, fewer findings/files first** (smaller scope = faster, more reliable).
3. **Within ties, lower severity first** (MINOR before MAJOR).
4. **Within ties, smaller file LOC first**.

Take top `--target-count` (default 25). Discard the rest.

---

### Phase 5 — GATE 1: Candidate list approval

Print the candidate list grouped by source:

```
PROPOSED OVERNIGHT EPIC: 25 tickets (12 backlog + 13 Sonar)

== From backlog (will re-parent under mega epic) ==
WOR-242  Tighten typing in user_prefs.py                  app/core/user_prefs.py
         current parent: WOR-273 (Watcher Intelligence Wave 2)
WOR-243  Replace string-format with f-string in cli.py    app/cli.py
         current parent: (none — standalone)
... <10 more> ...

== From Sonar (will create new sub-tickets) ==
Fix Sonar S1172 in app/core/credentials.py               app/core/credentials.py    1×S1172   MINOR
Fix Sonar S1481 + S1143 in app/core/foo.py               app/core/foo.py            2 total   MINOR
... <11 more> ...

Backlog re-parenting: each will be moved from current parent to the new mega epic.

Operator response options:
  - 'go'                  — accept the list, proceed to Phase 6 (epic creation)
  - 'skip WOR-NNN, WOR-MMM' — exclude listed tickets, re-show list
  - 'abort'               — exit without any writes
```

**Wait for operator response.** No Linear writes, no git branches, no manifests until 'go'.

If `--dry-run` was passed, also print one example manifest for spot-check then exit.

---

### Phase 6 — Create epic + sub-tickets

After 'go':

**6a. Create the mega epic**:
```
save_issue(
    team: "Work",
    title: "Overnight cleanup wave N",
    description: "**Charter:** Mechanical Sonar + backlog cleanup, fire-and-forget. Single-ticket failures are accepted losses. Morning workflow: /close-epic → epic→main PR with whatever shipped.\n\n**Sub-ticket budget:** 30",
    labels: ["Refactor", "Infra"],
    state: "In Progress",
    priority: 3,
    project: "{{ linear_project }}",
    projectMilestone: "Watcher v3 — routing & cost economics",
)
```

Capture the returned WOR-NNN identifier as `<MEGA_EPIC>`.

Set `<EPIC_BRANCH>` = `epic/wor-NNN-overnight-cleanup-N` (replace N with the wave number — increment from any prior `epic/wor-*-overnight-cleanup-*` branches).

**6b. Create the epic branch**:
```bash
git checkout -b <EPIC_BRANCH>
git push -u origin <EPIC_BRANCH>
git checkout main
```

**6c. Re-parent backlog candidates** (one save_issue per backlog candidate):
```
save_issue(id: "WOR-242", parentId: "<MEGA_EPIC>")
```

**6d. Create new Sonar sub-tickets** (one save_issue per Sonar candidate):
```
save_issue(
    team: "Work",
    parentId: "<MEGA_EPIC>",
    title: "Fix Sonar findings in <file>: S1172 (3), S1481 (1)",
    description: "## Goal\n\nResolve the SonarCloud findings listed below. No behavior change. Single-file scope.\n\n## Findings\n\n- python:S1172 (unused parameter) × 3 — lines 12, 45, 89\n- python:S1481 (unused local variable) × 1 — line 67\n\n## Allowed paths\n\n- `<file path>`\n- `tests/test_<stem>*.py`\n\n## Acceptance criteria\n\n- All listed Sonar findings resolved\n- ruff check ., mypy app/, pytest, lint-imports all pass\n- No behavior change\n\n## Why P3\n\nMechanical refactor, single-file scope. Part of overnight cleanup wave.",
    labels: ["Local-ready", "Fix", "Infra"],
    state: "Groomed",
    priority: 3,
    project: "{{ linear_project }}",
    projectMilestone: "Watcher v3 — routing & cost economics",
)
```

Capture each new sub-ticket's WOR-NNN.

---

### Phase 7 — Branch + manifest per sub-ticket

For each sub-ticket (both re-parented backlog and new Sonar):

**7a. Create the worker branch off the epic**:
```bash
git checkout <EPIC_BRANCH>
git pull origin <EPIC_BRANCH>
git checkout -b <sub-ticket-branch>
git push -u origin <sub-ticket-branch>
git checkout main
```

The sub-ticket branch name comes from Linear's `gitBranchName` field (e.g. `wor-242-tighten-typing-in-user_prefs`).

**7b. Pre-load context_snippets**:
For each file in `allowed_paths`, read the first 80 lines (cap at 3000 chars). These become the `context_snippets` field of the manifest — workers don't need to Read the file again.

If `allowed_paths` includes a `tests/test_<stem>*.py` glob, expand it to actual file paths via Glob first, then read each.

**7c. Write the manifest** to `.claude/artifacts/<ticket_id_lower>/manifest.json`:

```json
{
  "manifest_version": "1.0",
  "ticket_id": "<TICKET_ID>",
  "epic_id": "<MEGA_EPIC>",
  "title": "<ticket title>",
  "priority": 3,
  "status": "ReadyForLocal",
  "linear_id": null,
  "blocked_by_tickets": [],
  "parallel_safe": true,
  "risk_level": "low",
  "risk_flags": [],
  "implementation_mode": "local",
  "effort": "high",
  "change_type": "refactor",
  "reasoning_demand": 1,
  "scope_clarity": 5,
  "constraint_density": 2,
  "ac_specificity": 5,
  "tech_stack": "python",
  "raw_extensions": "[\".py\"]",
  "review_mode": "auto",
  "base_branch": "<EPIC_BRANCH>",
  "worker_branch": "<sub-ticket-branch>",
  "worktree_name": null,
  "objective": "<one-paragraph from the ticket description>",
  "acceptance_criteria": ["<each AC bullet>"],
  "implementation_constraints": [
    "Only edit <single file path> and matching tests/test_<stem>*.py",
    "Trust the Edit tool — do not re-read after editing (WOR-355)",
    "Run unscoped pytest before declaring success — sibling test files matter (WOR-353)"
  ],
  "allowed_paths": ["<single source file>", "tests/test_<stem>*.py"],
  "forbidden_paths": ["app/ui/**", ".env", ".mcp.json", ".claude/settings*", ".importlinter"],
  "related_files_hint": ["<single source file>"],
  "context_snippets": [<file_path>: <first 80 lines>],
  "required_checks": ["ruff check .", "mypy app/", "pytest", "lint-imports"],
  "optional_checks": [],
  "done_definition": "All listed findings resolved; checks pass; no behavior change.",
  "failure_policy": {
    "on_check_failure": "abort",
    "max_retries": 1,
    "escalate_to_cloud": false
  },
  "ticket_state_map": {
    "in_progress_local": "InProgressLocal",
    "failed": "Blocked"
  },
  "artifact_paths": {
    "result_json": ".claude/artifacts/<ticket_id_lower>/result.json",
    "manifest_copy": ".claude/artifacts/<ticket_id_lower>/manifest.json"
  }
}
```

> **`linear_id` field:** All sub-tickets in this skill are Batch 1 (`status: "ReadyForLocal"`), so `linear_id: null` is correct — the watcher resolves the UUID at dispatch time. We never use `WaitingForDeps` because there are no dependency chains in fire-and-forget mode.

> **`failure_policy.max_retries: 1`** catches transient `api_retry`-storm failures (per WOR-360 backend instability signal). `escalate_to_cloud: false` because there is no morning rescue path.

> **`allowed_paths` test glob**: per WOR-353, `tests/test_<stem>*.py` covers sibling test files automatically. Don't enumerate them — let the glob do it.

> **At this point, sub-tickets are still in `Groomed` Linear state.** The watcher only picks up `ReadyForLocal`, so nothing dispatches yet. Phase 8 is the explicit launch.

If `--dry-run`, skip the actual manifest write — just log the path that would be written.

---

### Phase 8 — GATE 2: Launch confirmation

Print the preflight summary:

```
PREFLIGHT — <MEGA_EPIC> ready to launch

Epic:         <MEGA_EPIC> "Overnight cleanup wave N"
Epic branch:  <EPIC_BRANCH>
Sub-tickets:  25 (12 backlog re-parented + 13 new Sonar tickets)
Manifests:    .claude/artifacts/wor_*/manifest.json (all written)
Watcher:      <running PID NNN | NOT RUNNING — start it before 'launch'>
Concurrency:  watcher max-local-workers=8 — 25 tickets queue ~3 deep

Sub-ticket distribution:
  - max_retries=1 each (transient failure recovery)
  - escalate_to_cloud=false (no cloud rescue)
  - all parallel_safe=true (no file conflicts by selection)
  - effort=high (default for mechanical fixes)

Operator response options:
  - 'launch'              — set all 25 to ReadyForLocal, hand off
  - 'inspect WOR-NNN'     — print one manifest for spot-check, re-show preflight
  - 'abort'               — set all 25 to Cancelled, delete branches + manifests, leave epic in Linear with note
```

**Wait for explicit 'launch'.** This is the point of no return — once tickets flip to `ReadyForLocal` the watcher dispatches them within ~30s.

If watcher is not running, REFUSE 'launch' with:
```
Cannot launch: watcher not running. Start it first:
  python -m app.cli watcher --worker-mode local --max-local-workers 8
Then re-run 'launch'.
```

If 'abort':
1. For each sub-ticket: `save_issue(id, state: "Cancelled")` and post a comment "Aborted before launch — manifest discarded."
2. For each Sonar-derived sub-ticket (created in 6d): leave as Cancelled (they're new; can be re-mined next time).
3. For each backlog candidate: also reset `parentId` to its original parent.
4. Delete all manifests + worker branches: `git push origin --delete <branch>` for each, `rm -rf .claude/artifacts/<ticket_id_lower>` for each.
5. Leave the mega epic in Linear (no point deleting it).

If `inspect WOR-NNN`: read `.claude/artifacts/<ticket_id_lower>/manifest.json` and print it. Re-show preflight.

---

### Phase 9 — Set Linear state to ReadyForLocal + operator handoff

After 'launch':

For each sub-ticket:
1. `save_issue(id: "WOR-XXX", state: "ReadyForLocal")`
2. `save_comment(issueId: "WOR-XXX", body: "Manifest written. Watcher will pick up.")`

Print:

```
✅ Overnight epic launched: <MEGA_EPIC>
   - Epic branch: <EPIC_BRANCH>
   - 25 sub-tickets in ReadyForLocal
   - Watcher should begin dispatching within 30s

Morning workflow:
   1. Run `/close-epic <MEGA_EPIC>` (handles failed children gracefully per WOR-331)
   2. Review epic→main PR
   3. Merge

Sleep tight.
```

---

## Allow-list of safe Sonar rules

| Rule | Description | Why safe |
|---|---|---|
| python:S1172 | Unused parameter | Single-line removal |
| python:S1481 | Unused local variable | Single-line removal |
| python:S1854 | Dead store | Single-line removal |
| python:S1143 | Unnecessary if/else | Local simplification |
| python:S125 | Commented-out code | Single-line removal |
| python:S100 | Docstring naming convention | Additive only |
| python:S5754 | Bare except clause | Single-line replacement |

**Add new rules to the allow-list only after observing one overnight where they succeed without manual rescue.** Conservative growth.

## Forbidden file/directory list

```
FORBIDDEN_FILES = [
    "app/core/watcher/watcher.py",
    "app/core/watcher/watcher_finalize.py",
    "app/core/watcher/dispatch.py",
    "app/core/generator.py",
    "app/core/metrics.py",
    "app/core/manifest.py",
]

FORBIDDEN_DIRS = [
    "app/ui/",
    "tests/",
    ".claude/",
]
```

## First-run guidance

For the first overnight using this skill:
- Use `--target-count 5` (small batch to validate the flow)
- Run on a Friday afternoon — sleep on it; check `/close-epic` flow Saturday
- Morning retro should query the new metrics columns (per WOR-365):
  ```sql
  SELECT effort, outcome, COUNT(*), AVG(local_wall_time/60) AS avg_min
  FROM ticket_metrics WHERE epic_id = '<MEGA_EPIC>'
  GROUP BY effort, outcome;

  SELECT ticket_id, api_retry_count, dispatch_concurrency, tags
  FROM ticket_metrics WHERE epic_id = '<MEGA_EPIC>'
  ORDER BY local_wall_time DESC;
  ```
- Success threshold: ≥60% of attempted tickets land in main = skill is working. <40% = tighten the allow-list.
