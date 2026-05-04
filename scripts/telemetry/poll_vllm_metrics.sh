#!/usr/bin/env bash
# Poll vLLM /metrics every N seconds and append timestamped snapshots to a log.
# Designed for solo-bound smoke tests where dispatch_concurrency == 0 and the
# server-wide counters can be cleanly attributed to one worker session.
#
# Usage:
#   scripts/telemetry/poll_vllm_metrics.sh <out_path> [interval_seconds] [vllm_url]
#
# Example (paired with a /start-ticket → ReadyForLocal flow):
#   scripts/telemetry/poll_vllm_metrics.sh \
#       .claude/artifacts/wor_359/vllm_metrics_timeline.txt 10 &
#   POLL_PID=$!
#   # ... watcher dispatches worker, worker finishes, result.json appears ...
#   kill "$POLL_PID"
#
# The script intentionally targets a *narrow* metric set — the high-signal
# ones for diagnosing slow runs (cache, throughput, queue, KV pressure).
# If you need other vLLM metrics, edit the grep pattern below.

set -u

OUT_PATH="${1:?out_path required}"
INTERVAL="${2:-10}"
VLLM_URL="${3:-http://localhost:8000}"

mkdir -p "$(dirname "$OUT_PATH")"

while true; do
  TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  printf '===== %s =====\n' "$TS" >> "$OUT_PATH"
  curl -s --max-time 5 "$VLLM_URL/metrics" 2>/dev/null \
    | grep -E '^(vllm:prefix_cache_(hits|queries)_total|vllm:gpu_cache_usage_perc|vllm:num_requests_(running|waiting)|vllm:time_to_first_token_seconds_(sum|count)|vllm:time_per_output_token_seconds_(sum|count)|vllm:prompt_tokens_total|vllm:generation_tokens_total|vllm:num_preemptions_total)' \
    >> "$OUT_PATH"
  sleep "$INTERVAL"
done
