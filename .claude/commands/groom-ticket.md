Look up the Linear issue with identifier $ARGUMENTS in the repo-scaffold-desktop project using the Linear MCP server. Also fetch `get_issue($ARGUMENTS, includeRelations: true)` to see existing labels, milestone, and blocking relations.

As a Product Owner, evaluate the issue before development begins:

1. **Restate** the requirement in one sentence in plain terms.

2. **Scope check** — is this one coherent unit of work, or does it span multiple concerns?
   - Flag if the ticket mixes UI + core logic + infrastructure
   - Flag if it seems too large for a single PR (rule of thumb: >3 files changed, or >1 day of work)

3. **Acceptance criteria** — if missing or vague, propose 3–5 bullet points that define "done".

4. **Splitting** — if the ticket should be split, draft sub-issue titles and brief descriptions for each.

5. **Dependencies** — note any other tickets or work that must come first. Check actual Linear relations from `get_issue(includeRelations: true)` before inferring.

6. **Metadata recommendations** — propose values for any of these that are missing or wrong:
   - **Type label** — one of: Feature / Fix / Refactor / Spike / Bug
   - **Stream label** — one of: Product / Infra / AI / Docs
   - **Milestone** — which of the project milestones this belongs to: Discovery / Scope Locked / MVP Build / Test/polish / Release
   - **Priority** — 1=Urgent / 2=High / 3=Normal / 4=Low
   - **Blockers** — any issues that must ship first (by WOR-NNN identifier)

**STOP HERE.** Present your analysis and wait for human approval before making any changes.

---

After the human approves, take all of the following actions in Linear using `save_issue`:

1. **Labels** — set the Type and Stream labels on the issue (use label names, not IDs)
2. **Milestone** — assign the issue to the recommended milestone
3. **Priority** — update if the current value is wrong or missing
4. **Blockers** — add any missing `blockedBy` relations (append-only; existing relations are never removed)
5. **Sub-issues** — if splitting was recommended and the human approved, create each sub-issue with `save_issue` using `parentId: "$ARGUMENTS"`, then set the same milestone and labels on each sub-issue

Report a summary of every change made in Linear.
