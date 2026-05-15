Audit all `.claude/commands/` skill files for stale WOR-NNN references, cross-checking each ticket against Linear state and flagged file paths — producing a READ-ONLY report so skill docs stop drifting silently after many epics.

## Steps

1. Enumerate all skill files:

   ```bash
   ls .claude/commands/*.md
   ```

   Record the list of every `.md` file in `.claude/commands/`.

2. Extract all `WOR-NNN` tokens from each file using regex:

   ```bash
   grep -rho 'WOR-[0-9]\+' .claude/commands/*.md | sort -u
   ```

   This produces a deduplicated list of every WOR-NNN reference found across all skill files.

3. For each unique `WOR-NNN` token, query its current Linear state using the Linear MCP:

   ```
   mcp__linear-server__get_issue(id: "WOR-NNN")
   ```

   Classify each result:

   - **open** — `state.name` is `Backlog`, `Todo`, `Groomed`, or any active state.
   - **closed-merged** — `state.name` is `Done`, `MergedToEpic`, `Blocked`, or `Duplicate`. Flag as stale.
   - **unknown** — issue not found or MCP call fails (wrong project, deleted ticket). Flag as stale.

4. Flag any `WOR-NNN` references that resolve to closed/merged or unknown state. For each stale reference:

   - Note which skill file(s) contain the reference.
   - Note the current Linear state and why it is stale (e.g. "MergedToEpic — sub-ticket finished in an old epic", "Done — ticket closed without being revisited").
   - Recommend whether to annotate with `→ CLOSED` or remove the reference entirely.

5. Scan the body of each skill file for file-path references and verify they still exist:

   ```bash
   # Extract file paths from command blocks and prose (patterns like .claude/..., app/..., tests/..., docs/..., scripts/...)
   grep -rhoE '\.claude/[A-Za-z0-9_./-]+' .claude/commands/*.md
   grep -rhoE 'app/[A-Za-z0-9_./-]+' .claude/commands/*.md
   grep -rhoE 'tests/[A-Za-z0-9_./-]+' .claude/commands/*.md
   grep -rhoE 'docs/[A-Za-z0-9_./-]+' .claude/commands/*.md
   grep -rhoE 'scripts/[A-Za-z0-9_./-]+' .claude/commands/*.md
   grep -rhoE 'config/[A-Za-z0-9_./-]+' .claude/commands/*.md
   ```

   For each extracted path, check with `test -f <path>`:

   - Flag any referenced file that no longer exists as a stale path reference.
   - Note which skill file contains the stale path and what the likely replacement or removal action should be.

6. Compile the READ-ONLY report. Structure:

   ```
   ## Audit Report — .claude/commands/ (YYYY-MM-DD)

   ### Stale WOR-NNN References

   | WOR-NNN | Status | Referenced In | Recommendation |
   |---------|--------|---------------|----------------|
   | WOR-123 | closed-merged | start-ticket.md:82 | Remove — sub-ticket finished in epic/WOR-493 |
   | WOR-456 | unknown | implement-ticket.md | Investigate — ticket may have been deleted or moved |

   ### Stale File Paths

   | Path | Referenced In | Recommendation |
   |------|---------------|----------------|
   | app/old_module.py | implement-ticket.md:42 | Remove — module was deleted in epic/WOR-480 |
   | ... | ... | ... |

   ### Summary

   - Total skill files scanned: N
   - Unique WOR-NNN references found: N
   - Stale references (closed/merged/unknown): N
   - Stale file paths: N
   - Clean references: N

   No auto-edits were made. All findings are advisory — the operator decides which references to annotate or remove.
   ```

7. Smoke-test the skill against the current `.claude/commands/` directory:

   - Run steps 1–3 against the actual files and confirm the report is generated.
   - Verify that the output contains at least some stale references (the codebase has been iterated across many epics; expect to see stale WOR-NNN refs).
   - Verify that no files were modified — only the report was printed.
   - If the report is empty or contains zero stale references, review the extraction logic in steps 2 and 3 for false negatives (e.g. regex too narrow, MCP query failing silently).

## Constraints

- **READ-ONLY.** This skill MUST NOT modify any `.claude/commands/*.md` file, `app/**`, or any file outside the report output. It emits findings only.
- Do NOT shell out via Bash to generate the report file — write the report content directly (prose or fenced block) as the skill output.
- Do NOT include literal email addresses in any output (PreToolUse email-detection hook blocks writes containing email-shaped strings).
- When a `WOR-NNN` reference cannot be found via Linear, cross-check the GitHub repository mirror as a fallback before marking it unknown.

## Notes

- The `.claude/commands/` directory has accumulated references across many epics. It is expected that some WOR-NNN tickets are closed, merged, or deleted by now. This skill surfaces that drift so the operator can clean it up.
- Stale references to file paths that were deleted during refactors (e.g. old module names, retired scripts) are equally important to flag — they cause confusion when a human or automated agent follows the reference.
- This skill is a starting point. Future iterations could automate the fix (annotate with `→ CLOSED` inline) but this version stays strictly read-only to avoid accidental damage.
