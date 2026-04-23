Pull all open issues from the **{{ linear_project }}** project using the Linear MCP server. Run these fetches in parallel:

- `list_issues` with `project: "{{ linear_project }}"` — exclude Done/Cancelled
- `list_milestones` with `project: "{{ linear_project }}"` — get progress % on each

Then for every issue that has or appears to have blocking relations, fetch `get_issue(id, includeRelations: true)` to get the **actual Linear blocker chain** (do not infer from titles).

For each Backlog or Todo issue in the current active milestone, spawn the **repo-investigator** subagent (passing the ticket title + description) to get the file-impact summary. Use the returned `Estimated change surface` and `Hidden coupling risks` fields to inform the dependency map and recommended order below. Do not read source files yourself.

Produce the following five sections:

---

### 1. Milestone overview

List each milestone with its progress % and status (complete / active / upcoming). Identify the **current active milestone** — the earliest incomplete one. Flag any milestone that is behind expectations.

### 2. Current state

List every open issue grouped first by epic (parent issue), then by status within each epic (In Progress → In Review → Backlog). For each issue show:

```
WOR-NNN [Type/Stream labels] Title — one-line description of what it delivers
         Status: <status>   Milestone: <name>   Priority: <priority>
```

Issues with no epic (parent) are listed last under **Ungrouped (needs triage)**.

### 3. Dependency map

Use **actual blocking relations from Linear** (from `get_issue(includeRelations: true)`). Show as:

```
WOR-X blocks WOR-Y, WOR-Z
WOR-A blocks WOR-B
(unblocked) WOR-C, WOR-D
```

Flag any cycles or issues blocked by something that is already Done/Cancelled (stale blockers).

### 4. Recommended order

Rank all Backlog issues by priority using this scoring — apply in order, stop when a ticket is differentiated:

1. **Unblocks the most other tickets** — do first
2. **Currently blocked** — defer until blocker ships
3. **Fits current active milestone** — prefer over future milestone work
4. **Smallest scope** (single file / doc-only) — prefer at equal value
5. **Release gate** — do last

Output as a numbered list: `WOR-NNN [epic] [milestone] — rationale`.

### 5. Suggested next ticket

State the single best ticket to pick up right now and why. If something is already In Progress, say so and recommend finishing it first.

---

### 6. Milestone lifecycle recommendations

Based on the milestone overview and current issue distribution, recommend any of the following if warranted:

- **Create a new milestone** — if there are ungrouped issues that belong to a phase not yet defined (e.g. a "V2" or "Post-MVP" milestone). Propose: name, description, and which issues should move into it.
- **Retire a completed milestone** — if a milestone is at 100% but still has open issues incorrectly assigned to it, flag those issues and suggest reassigning them to an appropriate open milestone.
- **Rename or redescribe a milestone** — if the current name no longer matches what the remaining work actually is.

> Note: the MCP server supports `save_milestone` (create/edit) but not delete. To retire a milestone, prefix its name with `ARCHIVED:` or advise the user to delete it manually in Linear.

**Do not act on these recommendations without human approval.** On approval, use `save_milestone` to create or update milestones, and `save_issue` to reassign issues.

---

### 7. Update the Linear project page

After presenting sections 1–5, update the **{{ linear_project }}** project in Linear to reflect current state. Do both of these:

**A. Update `summary`** (max 255 chars) — a single sentence capturing the current milestone and what's in flight. Example format:
`MVP Build 88% → Test/polish 40% | In flight: WOR-X, WOR-Y | Next: WOR-Z`

**B. Update `description`** — read the current project description with `get_project`, then:
1. Strip any existing block that starts with `## 📍 Current state` (everything up to the next `##` heading or end of that section)
2. Prepend a fresh block at the very top:

```markdown
## 📍 Current state — <YYYY-MM-DD>

**Active milestone:** <name> (<progress>%)
**Health:** On Track / At Risk / Off Track
**In flight:** WOR-X Title, WOR-Y Title
**Blocked:** WOR-Z blocked by WOR-A (or "none")
**Next up:** WOR-A, WOR-B, WOR-C

---

```

3. Append the rest of the original description unchanged after the `---` separator.

Call `save_project` with `id: "87ca9685-f2e6-493f-a022-03ef2425d2ab"` and both `summary` and `description` fields.

**Do not create issues or begin implementation.** Present sections 1–5, discuss section 6 recommendations with the human if warranted, then update the project page.
