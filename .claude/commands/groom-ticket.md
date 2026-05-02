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

Look up the Linear issue with identifier $ARGUMENTS in the {{ linear_project }} project using the Linear MCP server. Run these in parallel:
- `get_issue($ARGUMENTS, includeRelations: true)` — see existing labels, milestone, parent epic, priority, blocking relations
- `list_milestones(project: "{{ linear_project }}")` — check milestone progress before suggesting assignment
- `list_issues(project: "{{ linear_project }}", state: "In Progress")` combined with issues that have no parentId — to get current active epics
- `list_issues(parentId: "$ARGUMENTS")` — **check for existing child issues before proposing a split**

Then spawn the **repo-investigator** subagent, passing the ticket title and description as the prompt. Use its returned summary as context for the analysis below — do not read any source files yourself.

### Epic charter check (run FIRST when grooming an epic)

If the issue has **no `parentId`** (it is itself an epic / umbrella ticket), enforce these gates before anything else:

1. **Charter line**: the epic description must contain a single sentence describing the **user-visible outcome that ships at epic closure**. Look for a line of the form `**Charter:** <one sentence>` near the top of the description.
   - If missing: prompt the human — "This epic has no Charter line. Add one now? (one sentence describing what ships when the epic closes)" — and propose a draft based on the epic title + description for the human to refine.
   - If present but vague (e.g. starts with "improve" / "enhance" / "various"): warn that the charter risks drift, suggest a sharper rewrite, and ask the human to confirm or replace.

2. **Sub-ticket budget**: the epic description must contain `**Sub-ticket budget:** <N>` where N is an integer 3–6 (default 5).
   - If missing: prompt the human to set one (default 5 unless the human overrides).

3. **Persistence**: once charter + budget are agreed, ensure they appear at the top of the epic description (before the "## Problem" or other sections). Update the description via `save_issue(id: "<EPIC>", description: "...")` if needed.

4. **Refusal**: do NOT mark the epic state=Groomed until both charter and budget are persisted in the description.

5. **Backward compatibility**: existing epics may not have a charter or budget. The skill must NOT block existing epics from grooming — it should prompt the human to add them and proceed once filled in. Never silently set state=Groomed on an epic with a missing charter.

6. **Meta-epic exemption**: if the epic carries a `meta-epic` label, skip the budget check entirely (charter is still required). The label is for long-lived umbrella issues like "Watcher Reliability" that accumulate independent reliability fixes by design.

### Sub-ticket budget check (run when grooming a non-epic ticket with a parent)

If the issue has a `parentId`, fetch the parent epic with `get_issue(<parentId>)`:

1. Read the **Sub-ticket budget** from the parent's description (parse the `**Sub-ticket budget:** <N>` line). If missing, default to 5 and warn the human.

2. Skip this check entirely if the parent epic carries the `meta-epic` label.

3. Count the parent's open sub-tickets via `list_issues(parentId: <parentId>)`, excluding any in state `Done`, `Cancelled`, `Duplicate`, `MergedToEpic`. **Note: count includes the ticket being groomed if it already has the parent set.**

4. If `count >= budget`, surface this prompt to the human and require an explicit choice before proceeding:

   ```
   Parent epic <PARENT> "<parent title>" has <N> open sub-tickets (budget: <budget>).
   Charter: "<charter line from parent description>"

   Adding this ticket exceeds the budget. Choose:
     1. Bump the budget (justify in one sentence — added to epic description)
     2. Open a Wave 2 epic — create new sibling epic, move this ticket under it
     3. Move to standalone — clear parentId on this ticket
     4. The new ticket directly produces the charter outcome (proceed; budget assumed soft)

   Which option?
   ```

5. After the human chooses:
   - **(1) Bump**: update the parent epic description's budget line; append a one-sentence justification.
   - **(2) Wave 2**: create a new epic with a draft charter (the human will edit), set this ticket's parentId to the new epic.
   - **(3) Standalone**: call `save_issue(id: "<TICKET>", parentId: null)`.
   - **(4) Proceed**: continue with grooming as planned; no Linear changes for budget.

---

As a Product Owner, evaluate the issue before development begins:

1. **Restate** the requirement in one sentence in plain terms.

2. **Scope check** — is this one coherent unit of work, or does it span multiple concerns?
   - Flag if the ticket mixes UI + core logic + infrastructure
   - Flag if it seems too large for a single PR (rule of thumb: >3 files changed, or >1 day of work)

3. **Acceptance criteria** — if missing or vague, propose 3–5 bullet points that define "done".

4. **Splitting** — check the `list_issues(parentId: ...)` result first:
   - If child issues **already exist**: present them, assess whether they adequately cover the scope, and flag any gaps or duplicates. Do not draft new sub-issues for coverage that already exists.
   - If no children exist and the ticket should be split: draft sub-issue titles and brief descriptions for each.

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
3. **Milestone** — assign with `save_issue`. If a new milestone was approved, create it first with `save_milestone(project: "{{ linear_project }}", name: "...", description: "...")`, then assign
4. **Priority** — update if the current value is wrong or missing
5. **Blockers** — add any missing `blockedBy` relations (append-only; existing relations are never removed)
6. **Sub-issues** — if splitting was recommended and approved:
   - **If child issues already existed**: update them with the approved labels, state, milestone, and blockers using `save_issue`. Do not create new issues for existing children.
   - **If no children existed**: create each sub-issue with `save_issue` using `parentId: "$ARGUMENTS"`, then set the same labels, epic, and milestone on each.

Report a summary of every change made in Linear.
