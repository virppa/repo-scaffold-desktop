Project queue drain-time ETA from historical metrics.

Given a set of pending (`ReadyForLocal` / `WaitingForDeps`) tickets, project
how long the entire queue will take to drain using historical `local_wall_time`
from `ticket_metrics`. Prints a total ETA (concurrency-adjusted) and a
per-ticket breakdown.

Arguments: `$ARGUMENTS` may include `--max-workers N` (default 8, override
`dispatch_concurrency` per-ticket) and `--quiet` (only print the total ETA,
suppress per-ticket breakdown).

---

### Phase 1 — Gather pending tickets

List all issues in the `{{ linear_project }}` project with state
`ReadyForLocal` or `WaitingForDeps`:

```
mcp__linear-server__list_issues(
    project: "{{ linear_project }}",
    state: "ReadyForLocal",
    includeArchived: false,
    limit: 100,
)
```

Run the same query again with `state: "WaitingForDeps"` and merge the results.

For each issue, read its manifest from
`.claude/artifacts/<ticket_id_lower>/manifest.json` (e.g.
`.claude/artifacts/wor_123/manifest.json`). Skip any ticket whose manifest is
missing or broken.

For each surviving ticket, record:
- `ticket_id` (e.g. `WOR-123`)
- `title`
- Manifest fields: `change_type`, `reasoning_demand`, `effort`
- `linear_id` — the UUID (null for Batch 1 with `linear_id: null` in
  manifest — those are Batch 1 tickets and should be skipped)

Skip any manifest whose `linear_id` is `null` — those are epic-level dispatch
tickets already queued as a batch and should not be individually estimated.

If no valid pending tickets with a `linear_id` are found:
```
No pending tickets found. Nothing to estimate.
```
and exit.

---

### Phase 2 — Resolve metrics DB

The real `app.db` lives in the user's roaming profile, NOT in the repo cwd:

```bash
# Resolve the real DB path (copy from scripts/spikes/wor336_throughput_forensic.py)
python3 -c "
import os, sys
env = os.environ.get('REPO_SCAFFOLD_DB')
if env and os.path.exists(env):
    sys.stdout.write(env)
    sys.exit(0)
import platform
roaming = os.path.join(os.path.expanduser('~'), 'AppData', 'Roaming', 'repo-scaffold', 'app.db')
if os.path.exists(roaming) and os.path.getsize(roaming) > 0:
    sys.stdout.write(roaming)
    sys.exit(0)
posix = os.path.join(os.path.expanduser('~'), '.local', 'share', 'repo-scaffold', 'app.db')
if os.path.exists(posix) and os.path.getsize(posix) > 0:
    sys.stdout.write(posix)
    sys.exit(0)
# Fallback: cwd
sys.stdout.write('app.db')
"
```

If the resolved path points to a 0-byte file or does not exist, the command
prints:
```
WARN: Metrics DB not found or empty. ETA estimates will use fallback defaults.
```
and continues with fallback-only estimates.

Open the DB **read-only** via URI:
```sql
sqlite3 "file:<resolved_path>?mode=ro"
```

---

### Phase 3 — Query historical medians

For each pending ticket, group by `(change_type, reasoning_demand)` to get the
median `local_wall_time` across historical successful runs:

```sql
SELECT change_type,
       reasoning_demand,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY local_wall_time) AS median_seconds
FROM ticket_metrics
WHERE outcome = 'success'
  AND local_wall_time IS NOT NULL
  AND change_type IS NOT NULL
  AND reasoning_demand IS NOT NULL
GROUP BY change_type, reasoning_demand;
```

If the DB has no rows for a ticket's `(change_type, reasoning_demand)` cell,
fall back to `(change_type, NULL)` — i.e. median wall time for all tickets
with that `change_type` regardless of `reasoning_demand`.

If that also has no rows, fall back to `(NULL, NULL)` — overall median wall time.

If the overall median also has no data (DB is empty), use hardcoded fallbacks
per `effort`:
| effort   | fallback_seconds |
|----------|-----------------|
| low      | 600             |
| medium   | 1800            |
| high     | 7200            |
| xhigh    | 28800           |
| max      | 57600           |

---

### Phase 4 — Calculate ETA

For each ticket, the estimated wall time is the median wall time for its
taxonomic cell (or fallback).

The total projected drain time for the queue is:

```
total_seconds = sum(all_ticket_estimates) / max(dispatch_concurrency, 1)
```

where `dispatch_concurrency` comes from the `ticket_metrics.dispatch_concurrency`
column of the most recent historical row for that ticket's `ticket_id` (read the
latest row ordered by `ROWID DESC`, or fall back to 1 if no history).

If `--max-workers N` was passed, use `N` instead of per-ticket concurrency
(assume full serial for simplicity: `total_seconds / N`).

Print in human-readable format:
```
Total ETA: Xh Ym
  (sum of estimates: Zh Wm, concurrency factor: N workers)
```

---

### Phase 5 — Print per-ticket breakdown

For each pending ticket, print:

```
WOR-NNN  <short_title>  ~HHh MMm  (change_type=..., reasoning_demand=N, effort=..., source=hist|fallback)
```

- `source` is `hist` when the estimate came from actual historical median data,
  or `fallback` when it used the effort-level default.
- `~HHh MMm` uses hours when the estimate exceeds 60 minutes, otherwise minutes
  (e.g. `~45m` for anything under an hour, `~3h 15m` for longer).

Sort tickets by estimated wall time descending.

If `--quiet` was passed, skip this section entirely.

---

### Phase 6 — Edge-case notes

When the DB has very little data (fewer than 5 rows), print a footnote:

```
  Note: Based on N historical sample(s). Estimates may be inaccurate.
```

When all tickets are fallback-only (no history at all), print:

```
NOTE: No historical data available — estimates use effort-level defaults.
```

---

## Example output

```
Pending tickets: 5

  WOR-484  dispatch-eta skill spec    ~2h 30m  (change_type=additive, reasoning_demand=1, effort=high, source=hist)
  WOR-485  watcher retry logic        ~1h 15m  (change_type=refactor, reasoning_demand=2, effort=medium, source=fallback)
  WOR-486  TUI color fix             ~0h 15m  (change_type=fix, reasoning_demand=1, effort=low, source=fallback)
  WOR-487  metrics enrichment        ~4h 00m  (change_type=additive, reasoning_demand=3, effort=high, source=hist)
  WOR-488  Sonar dead-store cleanup  ~0h 30m  (change_type=refactor, reasoning_demand=1, effort=medium, source=fallback)

Total ETA: 9h 30m
  (sum of estimates: 8h 30m, concurrency factor: 8 workers)
  Based on 3 historical sample(s). Estimates may be inaccurate.
```

## Design principles

1. **Read-only.** The command reads the DB, never writes. `mode=ro` URI is
   mandatory.
2. **Taxonomy-first grouping.** `change_type` + `reasoning_demand` are the
   primary keys for median lookup. Effort is not used for grouping (it is
   redundant with taxonomy and has too few unique values for reliable median).
3. **Fallback ladder.** Each ticket gets its most specific historical data
   possible. Never leave a ticket with no estimate.
4. **Concurrency-aware.** Per-ticket historical concurrency is used to adjust
   the aggregate. The sum of wall times is divided by the concurrency factor.
5. **Simple output.** One total line, one per-ticket line, one footnote.
   No tables, no markdown formatting beyond bold labels. The output is designed
   to be readable in a Linear comment.
