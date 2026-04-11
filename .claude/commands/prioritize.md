Pull all open issues from the **repo-scaffold-desktop** project using the Linear MCP server (`list_issues` with `project: "repo-scaffold-desktop"`, excluding Done/Cancelled). Also fetch each issue's relations with `get_issue` (includeRelations: true) for the ones that look like they may block others.

Then produce a prioritized backlog overview in four sections:

---

### 1. Current state
List every open issue grouped by status (In Progress → In Review → Backlog → other). For each issue show: identifier, title, status, and one-line description of what it delivers.

### 2. Dependency map
Identify any blocking relationships (A must ship before B). If no explicit Linear relations exist, infer logical dependencies from the issue titles and descriptions (e.g., "generator core" clearly unblocks UI tickets). Show as a simple list:
```
WOR-X blocks WOR-Y, WOR-Z
WOR-A blocks WOR-B
(no dependencies) WOR-C, WOR-D
```

### 3. Recommended order
Rank all Backlog issues by priority. Use this scoring logic — apply in order, stop when a ticket is differentiated:
1. **Unblocks the most other tickets** — do first
2. **Currently blocked** — defer until its blocker ships
3. **Smallest scope** (single file / doc-only) — prefer over large multi-file tickets at equal value
4. **Closer to the immediate milestone** (per CLAUDE.md: "Generate a local repository skeleton from a selected preset and write all files to disk") — prefer
5. **Release gate** (e.g., release checklist) — do last

Output as a numbered list with a one-line rationale per ticket.

### 4. Suggested next ticket
State the single best ticket to pick up right now and why. If something is already In Progress, say so and recommend finishing it first.

---

**Do not update Linear, create issues, or begin implementation.** Present the analysis and wait for the human to decide.
