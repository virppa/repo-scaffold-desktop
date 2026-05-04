#!/usr/bin/env bash
# Post-run summary for a worker session that ran while poll_vllm_metrics.sh
# was capturing /metrics. Reports prefix-cache hit ratio, mean TTFT, total
# generation, and worker-log signals (thinking-block count, input_tokens
# growth) for the named ticket.
#
# Usage:
#   scripts/telemetry/summarize_run.sh <ticket_id_lower>
#   scripts/telemetry/summarize_run.sh wor_359
#
# Reads:
#   .claude/artifacts/<ticket>/result.json            (worker outcome)
#   .claude/artifacts/<ticket>/worker_<ticket>.log    (Claude Code stream-json)
#
# Pulls live cumulative counters from vLLM /metrics for the cache-hit headline.
# Numbers are session-wide, so the summary is only meaningful when this was
# the only worker active during the captured window.

set -eu

TICKET_LOWER="${1:?ticket_id_lower required, e.g. wor_359}"
ART_DIR=".claude/artifacts/${TICKET_LOWER}"
RESULT_JSON="${ART_DIR}/result.json"
WORKER_LOG="${ART_DIR}/worker_${TICKET_LOWER//_/-}.log"
VLLM_URL="${VLLM_URL:-http://localhost:8000}"

echo "=== ${TICKET_LOWER} ==="

if [ -f "$RESULT_JSON" ]; then
  echo "--- result.json ---"
  cat "$RESULT_JSON"
  echo
else
  echo "(no result.json — worker not yet finished?)"
fi

echo "--- vLLM cumulative counters ---"
METRICS=$(curl -s --max-time 5 "$VLLM_URL/metrics" 2>/dev/null)
HITS=$(printf '%s' "$METRICS" | awk '/^vllm:prefix_cache_hits_total\{/  {print $2; exit}')
QUERIES=$(printf '%s' "$METRICS" | awk '/^vllm:prefix_cache_queries_total\{/ {print $2; exit}')
TTFT_SUM=$(printf '%s' "$METRICS" | awk '/^vllm:time_to_first_token_seconds_sum\{/ {print $2; exit}')
TTFT_CNT=$(printf '%s' "$METRICS" | awk '/^vllm:time_to_first_token_seconds_count\{/ {print $2; exit}')
PROMPT_TOK=$(printf '%s' "$METRICS" | awk '/^vllm:prompt_tokens_total\{/ {print $2; exit}')
GEN_TOK=$(printf '%s' "$METRICS" | awk '/^vllm:generation_tokens_total\{/ {print $2; exit}')
PREEMPT=$(printf '%s' "$METRICS" | awk '/^vllm:num_preemptions_total\{/ {print $2; exit}')

if [ -n "${QUERIES:-}" ] && [ "${QUERIES%.*}" != "0" ]; then
  HIT_RATIO=$(awk -v h="$HITS" -v q="$QUERIES" 'BEGIN { printf "%.2f%%", (h/q)*100 }')
else
  HIT_RATIO="(no queries)"
fi
if [ -n "${TTFT_CNT:-}" ] && [ "${TTFT_CNT%.*}" != "0" ]; then
  TTFT_MEAN=$(awk -v s="$TTFT_SUM" -v n="$TTFT_CNT" 'BEGIN { printf "%.3fs", s/n }')
else
  TTFT_MEAN="(no requests)"
fi

printf '  prefix_cache_hits_total    : %s\n' "${HITS:-?}"
printf '  prefix_cache_queries_total : %s\n' "${QUERIES:-?}"
printf '  prefix_cache_hit_ratio     : %s\n' "$HIT_RATIO"
printf '  prompt_tokens_total        : %s\n' "${PROMPT_TOK:-?}"
printf '  generation_tokens_total    : %s\n' "${GEN_TOK:-?}"
printf '  ttft_mean                  : %s (n=%s)\n' "$TTFT_MEAN" "${TTFT_CNT:-0}"
printf '  num_preemptions_total      : %s\n' "${PREEMPT:-?}"
echo

if [ -f "$WORKER_LOG" ]; then
  echo "--- worker log signals ---"
  THINK_BLOCKS=$(grep -c '"thinking":"' "$WORKER_LOG" || true)
  THINK_CHARS=$(grep -o '"thinking":"[^"]*"' "$WORKER_LOG" | awk '{sum+=length($0)} END {print sum+0}')
  INPUT_FIRST=$(grep -oE '"input_tokens":[0-9]+' "$WORKER_LOG" | head -1 | cut -d: -f2)
  INPUT_LAST=$(grep -oE '"input_tokens":[0-9]+' "$WORKER_LOG" | tail -1 | cut -d: -f2)
  OUTPUT_TOTAL=$(grep -oE '"output_tokens":[0-9]+' "$WORKER_LOG" | awk -F: '{sum+=$2} END {print sum+0}')
  printf '  thinking_blocks      : %s\n' "$THINK_BLOCKS"
  printf '  thinking_chars_total : %s\n' "$THINK_CHARS"
  printf '  input_tokens_first   : %s\n' "${INPUT_FIRST:-?}"
  printf '  input_tokens_last    : %s\n' "${INPUT_LAST:-?}"
  printf '  output_tokens_sum    : %s\n' "$OUTPUT_TOTAL"
else
  echo "(no worker log at ${WORKER_LOG})"
fi
