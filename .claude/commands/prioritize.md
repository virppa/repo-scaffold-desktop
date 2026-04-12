Pull all open issues from the **repo-scaffold-desktop** project using the Linear MCP server. Run these fetches in parallel:

- `list_issues` with `project: "repo-scaffold-desktop"` — exclude Done/Cancelled
- `list_milestones` with `project: "repo-scaffold-desktop"` — get progress % on each

Then for every issue that has or appears to have blocking relations, fetch `get_issue(id, includeRelations: true)` to get the **actual Linear blocker chain** (do not infer from titles).

Produce the following five sections:

---

### 1. Milestone overview

List each milestone with its progress % and status (complete / active / upcoming). Identify the **current active milestone** — the earliest incomplete one. Flag any milestone that is behind expectations.

### 2. Current state

List every open issue grouped first by milestone, then by status within each milestone (In Progress → In Review → Backlog). For each issue show:

```
WOR-NNN [Type/Stream labels] Title — one-line description of what it delivers
         Status: <status>   Priority: <priority>
```

Issues not assigned to any milestone are listed last under **Unassigned (needs triage)**.

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

Output as a numbered list: `WOR-NNN [milestone] — rationale`.

### 5. Suggested next ticket

State the single best ticket to pick up right now and why. If something is already In Progress, say so and recommend finishing it first.

---

### 6. Project health summary

Write a 5–8 line project status update in this format (ready to paste into Linear's project Updates tab or a team channel):

```
**Health:** On Track / At Risk / Off Track
**Active milestone:** <name> (<progress>% complete)
**In flight:** <list of In Progress / In Review issues>
**Completed since last update:** <recently closed issues if visible>
**Blockers / risks:** <any blocked or at-risk items>
**Next up:** <top 2-3 items from recommended order>
```

Set health as: **On Track** if active milestone ≥ its expected progress and no High/Urgent issues are blocked; **At Risk** if milestone is behind or a High issue is blocked; **Off Track** if multiple milestones are slipping or blockers are unresolved for >1 cycle.

---

### 6. Update the Linear project page

After presenting sections 1–5, update the **repo-scaffold-desktop** project in Linear to reflect current state. Do both of these:

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

**Do not create issues or begin implementation.** Present the analysis first (sections 1–5), then update the project page.
