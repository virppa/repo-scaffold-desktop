Before anything else, run a non-blocking skills staleness check:

1. Read `.claude/settings.json` in the current working directory. If the file does not exist, or it has no `skills_source` or `skills_version` key, skip this check entirely and proceed to the grooming steps below.
2. Parse `skills_source` (format: `github:<owner>/<repo>`) to extract owner and repo name.
3. Use WebFetch to call `https://api.github.com/repos/<owner>/<repo>/releases?per_page=50`. If the request fails or returns an error (network unreachable, rate limited, etc.), skip silently and proceed.
4. From the response array, take the first element's `tag_name` as the latest release. Compare it to `skills_version` from settings:
   - If equal: no output, proceed.
   - If `skills_version` is behind: count how many releases in the array have a `tag_name` semantically greater than `skills_version` (treat tags as semver; ignore non-semver tags). Print exactly one line:
     ```
     Skills are N version(s) behind (<skills_version> → <latest tag_name>). Run /update-skills to upgrade.
     ```
     Then proceed — this notice is non-blocking.

---

Look up the Linear issue with identifier $ARGUMENTS in the repo-scaffold-desktop project using the Linear MCP server. Run these in parallel:
- `get_issue($ARGUMENTS, includeRelations: true)` — see existing labels, milestone, parent epic, priority, blocking relations
- `list_milestones(project: "repo-scaffold-desktop")` — check milestone progress before suggesting assignment
- `list_issues(project: "repo-scaffold-desktop", state: "In Progress")` combined with issues that have no parentId — to get current active epics

Then spawn the **repo-investigator** subagent, passing the ticket title and description as the prompt. Use its returned summary as context for the analysis below — do not read any source files yourself.

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
   - **Epic (parent issue)** — which thematic epic this belongs under. Use the live epic list fetched above (issues with no parent that are not Done/Cancelled). If none fit, propose a new epic title.
   - **Milestone** — which project milestone to assign. **First check `list_milestones` progress — do not assign to any milestone at 100%; it is complete and closed to new work.** Suggest the next appropriate open milestone instead. If no existing milestone fits (e.g. clear post-V1 work), flag this and suggest creating a new one with name and description.
   - **Priority** — 1=Urgent / 2=High / 3=Normal / 4=Low
   - **Blockers** — any issues that must ship first (by WOR-NNN identifier)
   - **local-ready label** — assess whether this ticket is safe for local LLM execution. Recommend adding the `local-ready` label if ALL of the following are true: (a) scope touches ≤3 files, none of which are large/complex orchestration modules (e.g. watcher.py, generator.py); (b) the task is straightforward wiring or additive changes requiring minimal cross-file reasoning; (c) no cloud-only dependencies or sensitive credentials needed. If any condition fails, recommend withholding the label — the watcher will route it to cloud. State your reasoning explicitly.

**STOP HERE.** Present your analysis and wait for human approval before making any changes.

---

After the human approves, take all of the following actions in Linear:

1. **Labels** — set the Type and Stream labels on the issue using `save_issue` (use label names, not IDs). If `local-ready` was recommended, add it to the labels list too.
2. **Epic** — set `parentId` to the approved epic identifier using `save_issue`. If a new epic was proposed and approved, create it first with `save_issue` (no parentId, with Type+Stream labels), then set it as parent on this issue
3. **Milestone** — assign with `save_issue`. If a new milestone was approved, create it first with `save_milestone(project: "repo-scaffold-desktop", name: "...", description: "...")`, then assign
4. **Priority** — update if the current value is wrong or missing
5. **Blockers** — add any missing `blockedBy` relations (append-only; existing relations are never removed)
6. **Sub-issues** — if splitting was recommended and approved, create each sub-issue with `save_issue` using `parentId: "$ARGUMENTS"`, then set the same labels, epic, and milestone on each

Report a summary of every change made in Linear.
