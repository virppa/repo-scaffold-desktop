# vLLM telemetry helpers

Ad-hoc scripts for capturing vLLM `/metrics` data around a worker run, used
to diagnose run cost / cache effectiveness without baking anything into the
watcher itself.

Born from the WOR-368 E2E smoke test (WOR-359 dispatched in 154s with a
94% prefix-cache hit ratio). Kept here as reference material — if the
data turns out actionable, fold it into the watcher proper as a gated
capture (see "Attribution caveat" below).

## Files

- `poll_vllm_metrics.sh` — append timestamped `/metrics` snapshots to a log every N seconds
- `summarize_run.sh` — post-run summary: hit ratio, TTFT, generation totals, worker-log signals

## Typical usage

```bash
# Before /start-ticket flips the ticket to ReadyForLocal:
scripts/telemetry/poll_vllm_metrics.sh \
    .claude/artifacts/wor_359/vllm_metrics_timeline.txt 10 &
POLL_PID=$!

# ... watcher dispatches → worker finishes → result.json appears ...

kill "$POLL_PID"
scripts/telemetry/summarize_run.sh wor_359
```

## Attribution caveat

vLLM `/metrics` are **server-wide cumulative counters**, not per-request.
The summary is only meaningful for solo runs (`dispatch_concurrency == 0`
in `ActiveWorker`). With ≥2 concurrent workers the deltas mix everyone's
traffic together and per-ticket attribution breaks.

If we ever wire this into the watcher, gate the capture on
`dispatch_concurrency == 0` and skip with a note when concurrent workers
were active.
