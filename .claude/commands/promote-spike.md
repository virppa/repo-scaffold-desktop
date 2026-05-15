A skill that turns a spike investigation doc into draft Linear tickets — one per recommendation row — stopping for architect approval before creating anything.

## Steps

1. Take the path argument: `$ARGUMENTS` is the path to a `docs/spikes/*.md` file.

2. Read the markdown file at that path. If the file does not exist:
   ```
   ERROR: File not found at '$ARGUMENTS'.
   Ensure the path is relative to the repo root (e.g. 'docs/spikes/wor-123-example.md').
   ```
   STOP.

3. Locate the recommendation table:
   - Look for a section whose heading matches `## Recommendation` or `## Recommendations` (one or two hash marks; case-sensitive).
   - If found: the recommendation table is the **first markdown table** under that heading.
   - If no such heading exists: the recommendation table is the **last markdown table** in the entire file.
   - If no markdown table is found at all:
     ```
     No recommendation table found in '$ARGUMENTS'.
     This file does not contain a structured recommendation section or any markdown tables.
     Cannot proceed — the skill requires a table with columns like Priority | Action | Ticket.
     ```
     STOP cleanly (no error stack).

4. Parse each row of the recommendation table. A valid row must have at least 2 columns.
   - Expected columns (in order): `Priority` / `Action` / `Ticket` (or similar — map by position: col 0 = priority, col 1 = action description, col 2+ = ticket reference).
   - A row is a **no-op** if the Priority is `WATCH`, `MONITOR`, or `SKIP` (case-insensitive). Skip it silently with a log line.
   - For other priorities, the row produces one draft ticket.

5. For each actionable row, **draft** the ticket data as a structured plan (do NOT call `save_issue` yet). Present the draft to the architect:

   ```
   Drafting N ticket(s) from '$ARGUMENTS' — spike parent: <spike_ticket_id>
   =========================================

   Row <N>:
     Title:       <short imperative title from Action>
     Description: <full Action text + context from the spike doc>
     Priority:    <Priority>
     Ticket Ref:  <Ticket column if present, else "new ticket">

   ----------------------------------------

   Total rows in table: <total>
   Watch/Monitor/Skip rows skipped: <skipped>
   Drafted tickets: <draft_count>
   Spike ticket (parentId): <spike_ticket_id>

   STOP — architect approval required before any save_issue calls.
   Reply with "APPROVED" to proceed, or provide corrections.
   ```

6. **STOP and wait for architect approval.** Do NOT call any Linear MCP tools to create issues. The skill halts here until the human replies.

   If the human says "APPROVED" (or equivalent affirmative):
   - Proceed to step 7.

   If the human provides corrections:
   - Update the drafts per the feedback.
   - Re-print the updated drafts.
   - STOP again and wait for approval.

7. Once approved, look up the spike ticket's Linear ID using the MCP server:

   ```
   get_issue(<spike_ticket_id>) — verify the issue exists and has the Spike label
   ```

   Then for each approved row, create the ticket:

   ```
   save_issue(
       title: "<short title>",
       description: "<body with spike context, table row content, and \"Recommended by spike: <spike doc filename>\" at the end>",
       parentId: "<spike_ticket_id>",
       labels: ["Fix"]                    // default; adjust if the human specifies a different Type
   )
   ```

   For each call, capture the returned Linear issue ID.

8. Report a summary:

   ```
   Promoted <draft_count> recommendation(s) from '<ARGUMENTS>'

   Created:
     - <created_ticket_id_1> — <title_1>
     - <created_ticket_id_2> — <title_2>
     ...

   Skipped (WATCH/MONITOR/Skip):
     - <skipped_description_1>
     ...

   All child tickets have <spike_ticket_id> as parent.
   ```

## Notes

- The skill targets the manual gap between spike findings and Linear tracking. Spike docs live at `docs/spikes/*.md`; the skill reads them, extracts the recommendation table, and drafts tickets with the spike ticket as parent.
- The architect-approval stop is hard-coded — no `save_issue` fires until the human explicitly approves.
- Spike ticket IDs typically look like `WOR-XXX` in this project. If the path filename or the doc header contains a `WOR-` pattern, use that as the `spike_ticket_id`. If none is found, prompt the human: "No spike ticket ID found in the doc header. What is the parent ticket ID?" before proceeding.
- The `labels` field can be a single string or a list — `save_issue` accepts both.
