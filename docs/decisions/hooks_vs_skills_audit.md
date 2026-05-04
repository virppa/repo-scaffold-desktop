# Hooks vs Skills Audit (WOR-374)

**Status:** Living document — first pass 2026-05-04.

## Architectural principle

> **Skills are for judgment. Hooks are for invariants.**

A skill (markdown in `.claude/commands/` or prose in `CLAUDE.md`) tells the
model what to do — it's a *hint* the model can follow, drift past, or
reinterpret. A hook (script wired in `.claude/settings.json`) checks whether
something happened — it's a *guarantee*. When a rule is binary and
mechanically checkable ("X must have happened" / "X must not happen"), the
hook is cheaper, deterministic, and immune to the model's planning loop
quirks. When a rule is judgment-laden ("decide whether to relax the gate"),
prose is the only option.

The repo already had this pattern in operation when this audit started —
PostToolUse hooks for ruff/mypy/bandit/lint-imports/pytest, and PreToolUse
blocks on destructive shell commands and writes to sensitive files. The
audit below extends the pattern to workflow-completion gates (Stop hook)
and to per-tool invariants the model has been historically poor at
following (Read cap).

## Cost argument

Each prose rule in CLAUDE.md or skill markdown is paid for in two ways:
1. **Tokens.** Every worker session loads the full skill + CLAUDE.md into
   context. A 200-token "MANDATORY FINAL ACTIONS" paragraph is ~200 tokens
   per session, every session.
2. **Variance.** Even when the model loads the rule, compliance is
   stochastic. WOR-282 attempt 1 had every relevant rule in its prompt
   and still skipped the commit step.

A hook script is ~10ms of subprocess work per qualifying tool call, costs
zero tokens, and has 0% variance — it always fires when it should and
never when it shouldn't.

**Heuristic:** if a rule's compliance can be measured by `git status`,
file existence, a regex on tool input, or a process-state check, it
belongs in a hook.

## Existing hook precedent

From `.claude/settings.json` before this audit:

| Event | Matcher | Purpose |
|---|---|---|
| PreToolUse | Bash | Block destructive shell (`rm -rf`, `git push --force`, `git reset --hard`, `git checkout --`, `git clean -f`) |
| PreToolUse | Edit\|Write | Block writes to sensitive files (`.env`, `.mcp.json`, `.claude/settings*`) |
| PreToolUse | Edit\|Write | Detect emails in non-test/non-Python content |
| PostToolUse | Edit\|Write | `ruff check --fix` + `ruff format` on `.py` files |
| PostToolUse | Edit\|Write | `mypy <file>` on `.py` files |
| PostToolUse | Edit\|Write | `bandit -q <file>` on `.py` files |
| PostToolUse | Edit\|Write | `lint-imports` on `.py` files |
| PostToolUse | Edit\|Write | `pytest <file>` on test files |

## Hooks added by this audit (Q2 2026)

| Hook | Replaces prose rule | PR | Status |
|---|---|---|---|
| `Stop` → `check_session_complete.py` | "MANDATORY FINAL ACTIONS" / "commit before declaring done" | #740 | Merged |
| `PreToolUse[Read]` → `check_read_cap.py` | "Per-file 2-read cap (WOR-355)" / "Trust the Edit tool — do not re-read" | #741 | Open |

## Audit table — `CLAUDE.md` "Worker efficiency"

Every rule in the section, with disposition. Disposition values:

- **already-hooked** — already enforced by an existing hook
- **hook (worth it)** — hook-amenable and high-value, file a sub-ticket
- **hook (low priority)** — hook-amenable but small impact; later
- **prose (judgment)** — needs LLM judgment; cannot be deterministic
- **prose (hard to detect)** — could in principle be a hook, but the detection regex/check is fragile or has too many false positives

| Rule | Disposition | Notes |
|---|---|---|
| "No standalone `cd` commands" | hook (worth it) | PreToolUse on Bash, regex `^\s*cd\s+\S+\s*$` (no `&&`/`;`/`|`) → warn-don't-block. Wasteful but not destructive. |
| "Batch file reads (one shell one-liner)" | prose (hard to detect) | Detecting "should have batched" requires comparing across multiple Read calls — out of scope for a single PreToolUse hook. The Read-cap hook (WOR-371) addresses the symptom indirectly. |
| "Trust the Edit tool — do not re-read after editing" + "Per-file 2-read cap (WOR-355)" | already-hooked | WOR-371 (PR #741) |
| "Update mock patch paths after any module move" | hook (worth it) | Already partially enforced by `scripts/check_patch_paths.py` (pre-commit). Could move earlier: PostToolUse on Bash matching `git mv` or any rename → run check_patch_paths and surface output. Catches it within the session, not at commit time. |
| "Convert `patch.object` when extracting instance methods" | prose (hard to detect) | Detecting class→module-level extraction requires AST diff across two Edits — too brittle for a hook. The pre-commit `check_patch_paths` partially helps. |
| "Edit existing files with the Edit tool, not `python3 -c`/sed/Bash one-liners" | hook (worth it) | PreToolUse on Bash, regex matching `python3?\s+-c\s+.*(open\\(|with\\s+open\\()`, `sed -i`, `tee\s+\S+\.py` → block with "use Edit". Lots of past tickets hit this. |
| "Create new files with the Write tool, not Bash heredocs" | hook (worth it) | PreToolUse on Bash, regex matching `cat\s*>\s*\S+\.py\s*<<` or `cat\s*>>\s*\S+\.py\s*<<` → block with "use Write". |
| "Run mypy on each new Python file immediately after creating it" | already-hooked (partial) | Existing PostToolUse hook runs `mypy <file>` after every Edit/Write to `.py`. The "immediately" part is enforced by the hook firing automatically. The "before writing" half (read source first) is judgment. |
| "Package reorganizations: __init__.py last + WIP commit between" | prose (judgment) | Detecting "this is a package reorg" mid-session requires tracking intent across many tool calls. Prose is the right call. |
| "Use `replace_all=True` for bulk patch string migrations" | prose (low priority) | Detection would require seeing repeated Edits with the same `old_string` in the same session — could be a PreToolUse hook on Edit but cost/benefit is low. Skip. |
| "Run unscoped `pytest` from `required_checks` before declaring success" | already-hooked | Indirectly enforced by WOR-372's Stop hook — `git status --porcelain` clean implies the commit happened, and the worker can only commit after the manifest's `required_checks` (which includes `pytest`) passed. |
| "Test allowed_paths are auto-globbed at /start-ticket time" | not applicable | This is `/start-ticket` skill behavior, not worker-side discipline. Already implemented in the skill. |

## Audit table — skill files in `.claude/commands/`

Reviewed each skill for prose-rules that could be hooked:

| Skill | Notable invariant | Disposition |
|---|---|---|
| `start-ticket` | "leave main checked out so the watcher can worktree the sub-branch" | prose (judgment) — operator behavior, not worker. |
| `start-ticket` | Manifest validation (allowed_paths populated, required_checks set) | hook (worth it) — see net-new candidates below. |
| `start-ticket` | Charter/budget check on parent epic | prose (judgment) — needs human prompt with re-parent options. |
| `groom-ticket` | "set state=Groomed on epic + open sub-tickets after human approves" | prose (judgment) — operator workflow. |
| `finalize-ticket` | File-size threshold check (800/1200/2000 for tests; 500/700/1200 for prod) | hook (worth it) — pre-commit hook `check-file-sizes.py` could enforce on every commit, not just at finalize. |
| `finalize-ticket` | "PR title format: WOR-NNN ..." | hook (low priority) — pre-push or commit-msg hook could check. |
| `close-epic` | Test-name diff between epic-baseline and epic-final (per WOR-373) | hook (worth it) — see net-new candidates below. WOR-373 already covers this. |
| `implement-ticket` | "Allowed paths only / forbidden paths never" | already-hooked (existing PreToolUse on Edit\|Write blocks `.env`, `.mcp.json`, `.claude/settings*`). Manifest's `allowed_paths` is harder — could be a hook but the regex against glob patterns gets fragile. Prose for now; revisit if drift observed. |
| `implement-ticket` | "FINAL ACTIONS: commit + result.json" | already-hooked | WOR-372 (PR #740) |
| `implement-ticket` | "No re-planning" | prose (judgment) |
| `prepare-overnight-epic` | "two operator gates before dispatch" | prose (judgment) |
| `security-check` | bandit + diff review | prose (judgment) — already partly hooked via PostToolUse bandit. |

## Net-new hook candidates (not currently in any prose rule)

These were spotted while writing the audit. Each is a small reliability
gap that the existing prose rules don't address but a hook could:

| Candidate | Mechanism | Why |
|---|---|---|
| **Manifest validation at dispatch** — `allowed_paths` non-empty AND `required_checks` non-empty | Watcher-side check in `dispatch.start_ticket` (not a hook per se, same principle) | Empty AC fields are likely authoring mistakes that today silently pass `ExecutionManifest.model_validate` |
| **`base_branch` exists on remote at dispatch time** | Watcher fetch + 404 detection in `dispatch.start_ticket` | If the epic branch was deleted/renamed, dispatch silently fails downstream — refuse early |
| **Stale-epic refusal** | Watcher-side: `git rev-list --count <epic>..main` > N → refuse dispatch | Already filed as WOR-373; this is the audit's blessing of the design |
| **Heredoc-write detection** | PreToolUse on Bash, regex `cat\s*>+\s*\S+\.(py|md|json)\s*<<` | "Use Write tool, not heredocs" rule from CLAUDE.md, hook-amenable |
| **`python3 -c` file-edit detection** | PreToolUse on Bash, regex `python3?\s+-c.*(open\\(|with\\s+open\\()` writing to a tracked file | "Use Edit tool, not python -c" rule from CLAUDE.md, hook-amenable |

## Recommended next sub-tickets

Filed as follow-ups under WOR-374:

1. **WOR-376** (proposed) — combined "Bash discipline" PreToolUse hook
   - Detect bare `cd <path>` (warn)
   - Detect `cat > file.py <<` heredocs writing tracked files (block, redirect to Write)
   - Detect `python3 -c "...open(...)..."` patterns (block, redirect to Edit)
   - Single hook script, three regex checks, ~80 LOC + tests

2. **WOR-377** (proposed) — file-size pre-commit hook (move from finalize-ticket skill)
   - Read thresholds from a config file or constant
   - Run on every commit, not just at `/finalize-ticket` time
   - Surfaces drift earlier than the close-epic gate

3. **WOR-378** (proposed) — manifest-quality check at dispatch
   - Watcher refuses to dispatch when `allowed_paths` is empty AND `manifest.implementation_mode == "local"`
   - Watcher refuses to dispatch when `required_checks` is empty
   - Refuse with a clear Linear comment so the operator knows to re-author

4. **WOR-379** (proposed) — earlier `check_patch_paths.py` invocation
   - PostToolUse on Bash matching `git mv|git rename` runs the existing pre-commit script immediately
   - Catches stale patch paths within the session, not at commit time

These four are sized to be ~30-80 LOC each, all hook-amenable, all
following the WOR-371/WOR-372 pattern. None are urgent (P3-P4) — file
them for future workers and pick up incrementally.

## What this audit explicitly does NOT change

- Does not rewrite any prose rule in `CLAUDE.md` or the skills. Even
  rules that are now hooked stay in prose as documentation — the hook
  is the enforcement, the prose is the explanation.
- Does not implement any of the proposed sub-tickets. Implementations
  ship under their own sub-tickets, with their own tests.
- Does not change watcher dispatch logic. WOR-373 and the WOR-378
  proposed ticket cover those changes.

## References

- WOR-374 (this audit ticket)
- WOR-371 (Read-cap PreToolUse hook, PR #741)
- WOR-372 (Stop hook, PR #740)
- WOR-373 (process: stale-epic dispatch refusal, P2 High)
- WOR-355 (established the 2-read cap)
- `.claude/settings.json` — existing hook precedent
- `CLAUDE.md` "Worker efficiency" section — prose-rule source
- `.claude/commands/*.md` — skill prose-rule source
