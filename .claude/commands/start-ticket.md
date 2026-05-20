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

  Local mode (RTX 5090 + vLLM — start the server in WSL2 first if not already up):
    /home/antti/vllm-env/bin/vllm serve /home/antti/models/Qwen3.6-35B-A3B-NVFP4 \
      --served-model-name qwen3-coder --max-model-len 262144 --max-num-seqs 16 \
      --gpu-memory-utilization 0.95 \
      --kv-cache-dtype fp8 --max-num-batched-tokens 4096 --reasoning-parser qwen3 \
      --enable-prefix-caching --language-model-only --safetensors-load-strategy prefetch \
      --enable-auto-tool-choice --tool-call-parser qwen3_coder \
      --default-chat-template-kwargs '{"preserve_thinking": true}'
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

### 0.56. Cross-epic branch detection (WOR-419)

The principle: **Linear parentId describes the ticket; git base_branch describes
the shipping unit. They can diverge.** (WOR-419)

Detect the active epic branch in flight and prefer it for the current ticket.
This lets sub-tickets target whichever epic branch is active rather than always
using their own Linear-parent epic branch — which may be a different epic in
flight.

Step — list all epic branches and check their Linear-parent status:
```bash
# 1. List epic branches on origin
epic_branches=$(git ls-remote --heads origin 'epic/*' | sed 's|.*refs/heads/||' | sort)

# 2. Determine which epic branches are "active"
active_epics=""
for branch in $epic_branches; do
  # Extract the epic ticket ID from the branch (epic/wor-NNN-slug → WOR-NNN)
  epic_id=$(echo "$branch" | sed 's|epic/wor-\([0-9]*\)-.*|\WOR-\1|')
  # Fetch the parent epic issue via Linear MCP
  parent_issue=$(get_issue "$epic_id")
  # Check if parent is "In Review" → if so, this epic is NOT active
  parent_state=$(echo "$parent_issue" | jq -r '.state.name')
  if [ "$parent_state" = "In Review" ]; then
    continue
  fi
  # Check if any direct child is InProgressLocal or MergedToEpic
  children_count=$(list_issues(parentId: "$epic_id") | jq '[.[] | select(.state.type == "InProgressLocal" or .state.type == "MergedToEpic")] | length')
  if [ "$children_count" -gt 0 ]; then
    active_epics="$active_epics $branch"
  fi
done

# 3. Apply the rule:
#    - If exactly ONE active epic exists AND current ticket's Linear-parent
#      epic is NOT itself active → default base_branch to the active epic
#    - If MULTIPLE active epics exist → fall back to current behavior (use
#      Linear-parent epic branch), surface note to architect
#    - If NO active epic exists → use default branch resolution

# Check if the current ticket's Linear-parent epic is in the active list
parent_epic_base="${manifest.base_branch:-}"
is_parent_active=false
for active in $active_epics; do
  if [ "$active" = "$parent_epic_base" ]; then
    is_parent_active=true
    break
  fi
done

if [ "$is_parent_active" = "false" ] && [ -n "$active_epics" ]; then
  active_count=$(echo "$active_epics" | wc -w)
  if [ "$active_count" -eq 1 ]; then
    echo "Preferring active epic branch $active_epics (parent epic $parent_epic_base is not in-flight)"
    # Use this branch as base_branch instead of the Linear-parent epic
    base_branch="$active_epics"
  elif [ "$active_count" -gt 1 ]; then
    echo "WARNING: Multiple active epic branches detected ($active_epics). Using Linear-parent epic branch. Manual intervention may be needed."
  fi
fi
```

Architect-facing: if `is_parent_active` is false and `active_count == 1`, explain:
*"This ticket's Linear parent is epic <parent> but the active epic branch in-flight
is <active>. Defaulting base_branch to <active> — the shipping unit diverges from
the Linear-parent (WOR-419 principle)."*

**Edge cases:**
- If MULTIPLE active epic branches exist → fall back to current behavior; surface note
- If NO active epic branch exists → use default branch resolution (Linear-parent)
- If the ticket has no parent epic → skip this check entirely

### 0.57. Epic-size charter check (run when parent epic exists)

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

### 2. Split-on-multi-feature check (WOR-443)

Before the architect plans the implementation, inspect the acceptance criteria from step 1.

**Split criteria — all three must hold to recommend a split:**

1. The AC enumerates **3 or more** bullets that each describe a *separately-scoped feature* (not 3+ bullets that together describe edge cases of one feature)
2. The features touch **distinct files or surfaces** (not just different methods of the same class)
3. Each feature could be **independently tested and shipped**

If all three hold, print this and wait for the operator's answer before continuing to step 2.5:

```
Heads-up: this ticket enumerates <N> separable improvements (<X>, <Y>, <Z>, ...).
Consider splitting before /start-ticket — each could be its own ticket:
  - smaller per-session context (reduces compaction risk)
  - <N>x parallel-safe by file
  - independent rollback if one feature regresses

Continue with single ticket? [y/N]
```

Default to **N** (do not proceed) — the operator must explicitly answer `y` to continue. The split-or-not decision is the operator's, but the recommendation must be surfaced, never silently skipped.

- Operator answers **N** (or presses enter): stop here. Recommend they close-and-re-file the ticket as `<N>` sub-tickets (or split it in Linear), then re-run `/start-ticket` on the smaller pieces. Do not write a manifest.
- Operator answers **y**: continue with the single ticket through step 2.5 onward, unchanged.

If any of the three criteria do **not** hold (the AC is one cohesive feature, or the bullets are edge cases / sub-steps of the same change), skip this block silently — do not print the prompt.

**Why (WOR-306 retro, 2026-05-11):** a ticket whose AC enumerated 4 separable TUI improvements ran 77 min wall (5x the smallest bundle peer), consumed 25.3M tokens, hit `input_tokens_max` → mid-session compaction (5.3 min lost), and locked `watcher.py` for the duration — gating two sibling tickets ~60 min. Split into 4 tickets it would have been ~15-20 min each and 4x parallel-safe by file. This check is the systematic fix; WOR-306 was the canary.

### 2.5. Routing assessment

Before computing implementation_mode, determine the routing for this ticket.
Routing answers: **"Where should this ticket run?"** — local worker vs cloud API.

The manifest's `routing` field defaults to `"local"`. Override to `"cloud_preferred"`
or `"cloud_only"` when justified. The local worker is the default because the
hybrid engine is optimised for local execution — cloud is the fallback, not the
default.

**Routing values:**

- **`local`** (default) — run on the local worker. Suitable when:
  - Scope is bounded (≤3 small/medium files, or clearly defined multi-file scope)
  - No external API keys / credentials are required
  - No cloud-only dependencies (e.g. Anthropic API, GitHub API)
  - Examples: bug fixes, config changes, new presets, template edits, docs-only tickets

- **`cloud_preferred`** — local worker will attempt, but cloud is a natural fallback.
  Suitable when the task has meaningful cross-file reasoning or touches complex
  modules. The local worker will try first (per failure_policy), but a 1st-failure
  escalation to cloud is expected.
  - Examples: watcher lifecycle changes, metrics DB schema evolution, benchmark
    runner improvements, multi-module refactors

- **`cloud_only`** — must run in the cloud. Required when the ticket fundamentally
  depends on services unavailable locally, or the scope exceeds the local worker's
  practical limits. Justification is required.
  - Examples: full epic integration review, cross-repo dependency analysis,
    production model fine-tuning, security audit of external integrations

**Classification rule:** assess scope, complexity, and dependencies. If in doubt,
default to `local`. The failure_policy escalation paths handle the cloud fallback
without needing to pre-declare every ticket as cloud.

Record your routing choice in the manifest at `routing` (string: `"local"` |
`"cloud_preferred"` | `"cloud_only"`).

### 3. As Architect — plan the implementation

<!-- WOR-276: Successor to WOR-214's effort field — moved from a raw enum into a strict 3-tier gate with a verb-default override. -->
- List which files need to change and what changes are needed
- List what new files will be created (not just edited) — for each one, add to `risk_flags` in the manifest: `"<filename>.py is a new file — worker must read source type signatures before writing and run mypy on the file immediately after creation"`
- List what new test files will be created — for each one, add to `risk_flags`: `"<test_file>.py is a new test file — worker must read a sibling test file first for fixture/mock patterns, then run pytest <file> -x immediately after creation"`
- If any instance methods are being extracted from a class into module-level functions, add to `risk_flags`: `"methods extracted from <ClassName> — grep for patch.object(instance, '<method>') in tests/ and convert to patch('new.module.path.<method>') — patch.object silently does nothing once the method is no longer on the class"`
- If `related_files_hint` has more than 5 files, add to `risk_flags`: `"related_files_hint has >5 files ({N}) — worker will read many files; enforce 2-read cap per file to prevent context bloat"`
- If `context_snippets` is populated (non-null, non-empty), add to `risk_flags`: `"context_snippets populated ({N} snippets) — worker reads from manifest; enforce 2-read cap per file"`
- If **all** entries in `allowed_paths` are under `tests/` (test-heavy ticket) OR all entries are under `.claude/commands/` (lint-only ticket), add to `risk_flags`: `"hook-trust-violation risk: {N} allowed_path(s) are all test-only / lint-only — worker has a strong incentive to manually re-run checks outside hooks; enforce the hard-rule in implement-ticket.md step 3 that bans Bash invocations of ruff/mypy/pytest/bandit/lint-imports outside required_checks"`
- If the ticket involves moving files into a new subpackage, add to `risk_flags`: `"package reorganization — move ALL source files into the subpackage first, update ALL imports in consumers, then write __init__.py LAST — do not run pytest until the move is complete or ModuleNotFoundError will appear on every intermediate check"` — and add a second risk_flag with the explicit old→new module path mapping table for every moved module (e.g. `"patch path migration: app.core.watcher_subprocess → app.core.watcher.watcher_subprocess, app.core.watcher_worktrees → app.core.watcher.watcher_worktrees, ..."`) so the worker can use replace_all=True bulk substitution per file rather than discovering stale paths from test failures
- List what new tests are needed (file, test name, what it verifies)
- Flag any security surface introduced: new I/O, user input handling, file operations, subprocess calls
- Note edge cases and overwrite behavior to consider
- Assess local-model suitability: is the scope bounded (≤3 small/medium files, straightforward wiring)? Or does it touch large/complex modules (e.g. watcher.py, generator.py) requiring multi-step reasoning across many dependencies? Record your conclusion — it determines `implementation_mode` in the manifest.
- Classify the ticket's effort level using this 3-tier gate (WOR-276 successor to WOR-214's effort field). Record your classification in the manifest along with a one-sentence justification:
  - **high** — single source file (<200 LOC) with only test files in allowed_paths; OR test-only allowed_paths; OR a single-line / single-block fix (e.g. typo, error message, return value).
  - **xhigh** — bounded multi-file work (≤4 files), no new abstractions introduced.
  - **max** — new core module; multi-file refactor; package reorganization; touches `watcher.py` or `generator.py`.
  - **Verb override:** If the manifest's `objective` contains any of the words `extract`, `split`, `reorganize`, `migrate`, or `rewrite`, the default is `max` regardless of other signals.
- **Taxonomy classification** (WOR-262) — record these 7 dimensions in the manifest. All optional but populate when you can:
  - `change_type` — one of `additive` (new feature/file), `modification` (existing behavior changes), `refactor` (no behavior change), `removal` (deletion/cleanup), `docs` (markdown / comments only)
  - `reasoning_demand` 1-5 — how much cross-file reasoning is needed (1 = local change in one function, 5 = touches many modules with non-obvious invariants)
  - `scope_clarity` 1-5 — how explicit the AC is (1 = vague "improve X", 5 = exact file/line targets and expected behaviour)
  - `constraint_density` 1-5 — number of hard rules in `implementation_constraints` (1 = none, 5 = many strict gates)
  - `ac_specificity` 1-5 — how testable the AC is (1 = subjective only, 5 = each bullet maps to an assertion)
  - `tech_stack` — comma-separated tags of the technologies involved, e.g. `python,sqlite,pydantic` or `markdown,yaml`
  - `raw_extensions` — JSON array string of file extensions touched, e.g. `[".py",".md"]`

### 4. Create the branch and update Linear
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

### 5. Present the plan
Summarize as:
```
Branch: <branch-name> (off <epic-branch | main>)
Milestone: <milestone name> (<progress>%)
Epic: <parent issue title or "none">
Routing: <local|cloud_preferred|cloud_only> — default local; cloud_preferred for high reasoning_demand cross-file work; cloud_only requires routing_reason justification
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
- `routing: cloud_only` (watcher would spawn a cloud session — no local-model benefit)
- `allowed_paths` contains only `.claude/commands/`, `CLAUDE.md`, `docs/`, or `schemas/` (no production Python)
- No parallel siblings currently In Progress (no worktree isolation needed)
- Small scope (≤ 3 files, no complex logic)

If all four apply, note it explicitly: *"This ticket is a good candidate for interactive implementation — skip the manifest and run `/implement-ticket $ARGUMENTS` in this session."*

**STOP HERE. Do not write any code until the human approves this plan.**

---

### 5.7. Populate context_snippets from related_files_hint

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

### 5.8. Expand test allowed_paths to capture sibling test files (WOR-353)

Before writing the manifest, glob-expand every `tests/test_<stem>.py` entry in
`allowed_paths` to `tests/test_<stem>*.py` so the worker has explicit permission
to run sibling test files that import the same source module.

**Why:** Sibling test files (e.g. `tests/test_watcher_finalize_metrics.py`,
`tests/test_watcher_finalize_recovery.py`) import the same module the worker
modifies, but pytest fails on them aren't visible until the watcher runs the
full suite as part of `required_checks`. Without this expansion, the worker
self-reports success while the watcher rejects — operator must rescue.

**Mechanical rule:** for each entry in `allowed_paths` matching the pattern
`tests/test_<stem>.py`, replace it with `tests/test_<stem>*.py`. Examples:

| Architect wrote | Becomes |
|---|---|
| `tests/test_metrics.py` | `tests/test_metrics*.py` |
| `tests/test_watcher_finalize.py` | `tests/test_watcher_finalize*.py` |
| `tests/test_X.py` (single ticket) | `tests/test_X*.py` |

Already-globbed entries (e.g. `tests/test_*.py`, `tests/foo/*.py`) are left
unchanged. Non-test paths (e.g. `app/core/metrics.py`) are left unchanged.

The architect does NOT need to enumerate sibling tests manually — this step
handles it mechanically before the manifest is written.

### 5.9. Auto-scope allowed_paths for shared-model + risk_flag ripple (WOR-500)

After the WOR-353 test-glob expansion and before writing the manifest, expand
`allowed_paths` so a *correct* cross-cutting edit is never Blocked. An
over-broad `allowed_paths` never Blocks correct work; an under-scoped one
Blocks flawless implementations — WOR-290 (a clean 21-file refactor,
ruff/lint-imports/mypy clean, 1911 pytest passing, Blocked solely for this)
and WOR-502 (a correct KV-admission feature Blocked because `parser.py` /
`dispatch.py` were omitted).

Apply all three rules mechanically:

1. **Shared model/fixture ripple.** If any `allowed_paths` entry is
   `app/core/manifest.py` or `tests/conftest.py` (or another widely-imported
   shared model/fixture that many tests construct), also add
   `tests/conftest.py`, `app/core/manifest_builder.py`, and `tests/test_*.py`.
   A model-field change unavoidably ripples to the shared `make_manifest`
   fixture and every manifest-constructing test.

2. **risk_flag meta-rule.** Re-read every `risk_flag` written in step 3. If a
   risk_flag names a module/file as where logic "lives", "belongs", "is
   located", or must be "found" (e.g. "the arg-parser is in another module",
   "admission belongs in the dispatch path"), that file is **required** in
   `allowed_paths`. A risk_flag naming another module is you having already
   spotted the scope hole — close it here, do not merely describe it.

3. **Common implicit pairs.** If the ticket adds or changes a CLI flag,
   include the argparse module (`app/cli/parser.py`), not only the consumer
   (`app/cli/operator.py`). If it adds watcher dispatch/admission logic,
   include `app/core/watcher/dispatch.py`, not only
   `app/core/watcher/watcher.py`.

When uncertain, over-scope. The architect does NOT need to perfectly predict
the worker's file set — these rules close the systematic under-scoping gaps.

### 5.6. After human approves the plan — generate the execution manifest

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
  "routing": "<local|cloud_preferred|cloud_only> — default local; override when justified (see §2.5 Routing assessment)",
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
  "required_checks": ["ruff check .", "mypy app/", "pytest --no-cov"],
  "optional_checks": [],
  "done_definition": "<plain-English done criteria>",
  "failure_policy": {
    "on_check_failure": "abort",
    "max_retries": 1,          // per WOR-223 rule: 2 for refactor, 1 otherwise
    "escalate_to_cloud": false  // per WOR-223 rule: true when (additive AND rd>=2) OR docs
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

### Routing & retry cap — per WOR-223

When filling `max_retries` and `escalate_to_cloud` in the manifest above, follow this rule:

```
Set failure_policy.escalate_to_cloud = true when (change_type == "additive" AND reasoning_demand >= 2) OR (change_type == "docs"); else false.
Set max_retries = 2 when change_type == "refactor"; else 1.
```

Do NOT set `refactor` tickets to `escalate_to_cloud=true`. They have a 0% failure rate locally — the historical data says try harder locally, never escalate.

#### Why this rule (WOR-223)

A 60-ticket retrospective analysis of local worker failure rates by `change_type` × `reasoning_demand` cell:

| Cell | n | Failures | Failure rate |
|------|---|----------|-------------|
| refactor (any rd) | 20 | 0 | 0/20 = 0% |
| additive, rd >= 2 | 21 | 7 | 7/21 = 33.3% |
| additive, rd < 2 | — | — | low |
| docs | 6 | 2 | 2/6 = 33.3% |
| modification | — | — | low |

Key findings:
- **Refactor** tickets: 0/20 = 0% failure rate. These are the safest category for local execution. `max_retries=2` to encourage thorough local work.
- **Additive + high reasoning demand** (rd >= 2): 7/21 = 33.3% failure rate. The broad cluster across all tickets with `additive` and `rd>=2` was 7/21 = 35% when counting the full population. Use 33.3% for the precise per-cell n=21 cluster. Flag for cloud routing.
- **Docs** tickets: 2/6 = 33.3% failure rate. Small sample (n=6) but worth flagging conservatively.
- **Modification** tickets: low failure rate historically — no special handling needed.

The `max_retries` rule follows from the same data: refactor tickets are reliably successful locally so a higher retry budget is safe; all other types default to 1 retry.

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

### 6. Opportunistic issue capture (after plan is shown — do not delay the plan for this)

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
