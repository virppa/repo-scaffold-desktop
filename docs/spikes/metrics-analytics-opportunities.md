# WOR-105 Spike: Metrics & Analytics Opportunities

**Milestone:** Metrics & Observability
**Status:** Done
**Epic:** WOR-115

---

## Context

The existing `ticket_metrics` table captures per-ticket aggregate data well: token usage, wall time, outcome, retry count, diff size, Sonar findings. The watcher is the sole writer; workers emit JSON result files only.

This spike surveys the current schema, the watcher execution data available at runtime, and the generator pipeline to identify 3–5 additional tracking opportunities. Each opportunity includes a concrete schema proposal. The single highest-value opportunity is then prototyped.

---

## Existing schema (summary)

```sql
ticket_metrics (
    ticket_id, project_id, epic_id,
    implementation_mode, cloud_used, cloud_model, cloud_tokens, cloud_cost_estimate,
    local_used, local_model, local_tokens, local_wall_time,
    escalated_to_cloud, outcome, retry_count, check_failures_json,
    lines_changed, files_changed, sonar_findings_count, context_compactions,
    recorded_at
)
```

Key gaps:
- `check_failures_json` stores **aggregate counts** only — no timing, no individual-run history.
- No tracking of **which checks are slowest** or **how failure rates differ by check**.
- No record of **when** each phase of the pipeline started/ended.
- Generator/preset usage is completely invisible.
- Skill invocations (the command surface) are not recorded.

---

## Opportunity 1: Per-check execution log ⭐ (prototyped)

### Problem

`check_failures_json` tells you `{"mypy": 2, "pytest": 1}` across a whole ticket run, but you cannot answer:
- Which check runs slowest?
- Does `ruff` fail on first run and pass on retry, or does it consistently pass?
- Are certain tickets consistently slow on `mypy` (large PR diff)?

### Proposed schema

```sql
CREATE TABLE IF NOT EXISTS check_run_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   TEXT NOT NULL,
    project_id  TEXT NOT NULL,
    check_cmd   TEXT NOT NULL,          -- e.g. "ruff check .", "mypy app/", "pytest"
    outcome     TEXT NOT NULL,          -- 'passed' | 'failed'
    duration_s  REAL,                   -- wall time in seconds, NULL if not timed
    recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
)
```

### Why this is highest value

1. **Data is already available** — the watcher runs `required_checks` sequentially; adding `time.monotonic()` around each call is zero-risk.
2. **Immediately actionable** — slow checks block the local worker. Seeing p95 duration per check tells us which one to optimise first.
3. **Failure pattern analysis** — a check that fails 80 % of first attempts but 0 % of second attempts signals a flaky test, not a code problem.
4. **No impact on the existing table** — it is an additive new table; no migration needed.

### Example queries

```sql
-- Average duration per check, ordered slowest first
SELECT check_cmd,
       COUNT(*)              AS total_runs,
       ROUND(AVG(duration_s), 2) AS avg_s,
       ROUND(MAX(duration_s), 2) AS max_s,
       ROUND(100.0 * SUM(CASE WHEN outcome = 'passed' THEN 1 ELSE 0 END) / COUNT(*), 1) AS pass_pct
FROM check_run_log
GROUP BY check_cmd
ORDER BY avg_s DESC;

-- Tickets whose pytest run was the slowest
SELECT ticket_id, duration_s
FROM check_run_log
WHERE check_cmd = 'pytest'
ORDER BY duration_s DESC
LIMIT 10;
```

---

## Opportunity 2: Skill / command invocation log

### Problem

Every ticket passes through `/groom-ticket` → `/start-ticket` → `/implement-ticket` → `/security-check` → `/finalize-ticket`. Currently there is no record of **which commands were actually called**, **in what order**, or **how long each phase took**.

### Proposed schema

```sql
CREATE TABLE IF NOT EXISTS skill_invocation_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   TEXT,                   -- NULL for non-ticket commands (e.g. /prioritize)
    project_id  TEXT NOT NULL,
    skill_name  TEXT NOT NULL,          -- 'groom-ticket', 'start-ticket', 'implement-ticket', etc.
    phase       TEXT,                   -- 'start' | 'end'
    outcome     TEXT,                   -- 'success' | 'aborted' | 'escalated' (on 'end' phase only)
    duration_s  REAL,                   -- populated on 'end' phase
    recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
)
```

### Why useful

- Cycle time breakdown: how long does grooming take vs. implementation vs. finalization?
- Escalation funnel: how many tickets make it from `start-ticket` to `finalize-ticket` without interruption?
- Skill failure rate: which skills abort most often?

### Integration point

Skills run as Claude Code slash commands. The simplest integration is a thin wrapper in the skill entrypoint that records start/end rows. The Stop hook already fires at end-of-turn and could be extended to write a row.

---

## Opportunity 3: Scaffold generation log

### Problem

The generator (`app/core/generator.py`) produces files from presets, but we have no telemetry on **which presets are used**, **which option toggles are enabled**, or **how many files are generated**. Preset usage data would inform which presets to invest in and which to prune.

### Proposed schema

```sql
CREATE TABLE IF NOT EXISTS scaffold_generation_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id       TEXT NOT NULL,
    preset_name      TEXT NOT NULL,          -- 'python_basic', 'typescript_lib', etc.
    options_json     TEXT,                   -- JSON object of enabled toggles
    files_generated  INTEGER NOT NULL,
    output_path      TEXT,                   -- anonymised (basename only, no user home)
    recorded_at      TEXT NOT NULL DEFAULT (datetime('now'))
)
```

### Why useful

- Tells us the most-used presets so we can prioritise template improvements.
- Option co-occurrence analysis: do users who enable `--pre-commit` also always enable `--ci`?
- File count growth: does adding a new option bloat the output?

### Integration point

`generator.py:Generator.generate()` already returns a list of written paths. A single `store.record_generation(...)` call at the end of generation is sufficient.

---

## Opportunity 4: PR cycle time tracking

### Problem

We know each ticket's `local_wall_time` (worker session duration) but not the end-to-end cycle time from ticket start to PR merge. A ticket could sit in `In Review` for days without any record of that wait.

### Proposed schema

Add three timestamp columns to `ticket_metrics`:

```sql
ALTER TABLE ticket_metrics ADD COLUMN worker_started_at TEXT;  -- watcher launches worker
ALTER TABLE ticket_metrics ADD COLUMN pr_opened_at      TEXT;  -- watcher calls gh pr create
ALTER TABLE ticket_metrics ADD COLUMN pr_merged_at      TEXT;  -- CI merge event / watcher poll
```

### Why useful

- End-to-end cycle time = `pr_merged_at - worker_started_at`.
- Review wait = `pr_merged_at - pr_opened_at`. If review wait dominates, the bottleneck is human review, not the worker.
- Trend over time: is the pipeline getting faster as we tune it?

### Integration point

The watcher already calls `metrics_store.record()` at worker finish. Extending that call to include `worker_started_at` (from `ActiveWorker.start_time`) and `pr_opened_at` (after `gh pr create`) is low effort. `pr_merged_at` requires a poll on the PR state or a webhook.

---

## Opportunity 5: Escalation cause log

### Problem

`escalated_to_cloud` is a boolean. When a ticket escalates, we lose the reason: was it `scope_drift`? `forbidden_path_touched`? A Sonar `blocker` finding? The escalation policy classifies these but currently only writes a Linear comment — it writes nothing to the metrics DB.

### Proposed schema

```sql
CREATE TABLE IF NOT EXISTS escalation_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id     TEXT NOT NULL,
    project_id    TEXT NOT NULL,
    trigger       TEXT NOT NULL,   -- 'scope_drift' | 'sonar_blocker' | 'max_retries' | ...
    detail        TEXT,            -- free text from escalation artifact
    retry_count   INTEGER NOT NULL DEFAULT 0,
    recorded_at   TEXT NOT NULL DEFAULT (datetime('now'))
)
```

### Why useful

- Which escalation trigger is most common? If `max_retries` dominates, raise the retry budget or fix the check. If `sonar_blocker` dominates, tune the escalation policy.
- Are certain epics or presets escalation-prone?
- Feeds back into `escalation_policy.toml` tuning: data-driven policy changes.

---

## Decision

**Prototype Opportunity 1 (per-check execution log).** It is the highest value because:

- The raw data (check timing and outcome) is already produced by the watcher's `required_checks` loop but immediately discarded.
- The schema is purely additive — no existing table is modified.
- It directly answers the most operationally urgent question: *which check is my bottleneck?*
- The prototype is complete: new table, new Pydantic model, new store methods, and tests.

The remaining opportunities are recorded here as a backlog for future epics.
