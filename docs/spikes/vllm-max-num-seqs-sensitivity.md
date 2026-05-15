# Spike: vLLM server-config sweep — `max-num-seqs` sensitivity & the KV ceiling

**Ticket:** WOR-336 (child of WOR-301 — Local-LLM tuning spikes)
**Status:** Analytical portion **complete** — answered from existing
telemetry + a live in-session A/B. No new GPU campaign was required for
the headline decision. One narrowly-scoped GPU follow-up remains (large
`--max-num-batched-tokens` values) and is split out below.
**Date:** 2026-05-16
**Method:** read-only forensic on the production metrics DB
(`app.db` — `ticket_metrics` n=101, `ticket_run_log` n=133,
`bench_run` n=1889) plus a live 6→2 concurrency A/B observed during the
WOR-493 overnight run. Reproduce with:

```bash
python scripts/spikes/wor336_throughput_forensic.py        # human-readable
python scripts/spikes/wor336_throughput_forensic.py --json # machine-readable
```

---

## TL;DR / Recommendation

**Keep `--max-num-seqs 16`. It is not the bottleneck and never was.**

1. Existing controlled-bench data already contains the seqs sweep the
   ticket asked for. `seqs=8` and `seqs=16` are throughput-identical at
   c=1–2; `seqs=16` sustains **8 concurrent streams at ~982 tok/s
   aggregate with VRAM flat at 29.2 GB** (3 GB headroom on the 32 GB
   5090). `seqs=200` is *slower* (113.8 vs 171.9 tok/s at c=1) and runs
   the card to **31.3 GB (near-OOM)** — re-confirming the standing
   "never seqs=200" finding.
2. The "8× unexplained solo-throughput variance" that bumped this spike
   to P2 **was largely an artifact of the pre-WOR-285 token
   under-count** (~3×) compounded with raw turn-count differences. After
   the corrected counts there is no GPU mystery to explain.
3. The real production limit is **KV-cache bytes, not sequence slots**.
   Concurrent real workers each carry a large, divergent, monotonically
   growing context (~134 K tokens at the compaction ceiling). Their
   aggregate KV working set oversubscribes the ~148,816-token pool, vLLM
   evicts prefix-cache blocks between turns, and every turn re-prefills
   the full context. The lever is **KV-budget-aware concurrency**
   (WOR-502), for which this spike ships a validated pure helper
   (`kv_concurrency_ceiling`).
4. Blocking gap surfaced: **every `vllm_*` telemetry column is NULL for
   all 101 tickets** (the WOR-439 capture bug). The prefix-cache-vs-
   concurrency hypothesis is therefore *unanswerable from telemetry*
   until WOR-439 lands; the only direct evidence is the live A/B below.

Production config is **already correct**. No `CLAUDE.md` change needed.

---

## H1 — Solo-throughput variance: the keystone correction

The ticket cited four "structurally identical SOLO" runs varying 8×.
Re-pulled from `ticket_metrics` with the post-WOR-285 corrected token
counts:

| Ticket | In-ticket (pre-WOR-285) | **Corrected** | Turns | Tools | Wall |
|--------|-------------------------|---------------|-------|-------|------|
| WOR-305 | 68 K out, 39 tok/s | **211 K out, 120.3 tok/s** | 294 | 121 | 29 m |
| WOR-338 | 45 K out, 9.7 tok/s | **132 K out, 28.6 tok/s** | 307 | 119 | 77 m |
| WOR-332 | 58 K out, 7.3 tok/s | **162 K out, 20.2 tok/s** | 399 | 146 | 134 m |
| WOR-277 | 49 K out, 5 tok/s | **134 K out, 13.8 tok/s** | 266 | 109 | 163 m |

Output tokens were under-counted ~2.7–3.1× across the board (exactly the
WOR-285 bug). After correction these runs are **not** structurally
identical: they differ 1.5× in turn count and 5.6× in wall time at
similar output volume, all pinned at the ~134 K compaction ceiling for
max input. `output_tokens_per_wall_second` is **not a GPU-speed metric**
— it is `tokens_generated / (turns × (tool_roundtrip + prefill_of_~134K
+ decode))`. The spread is dominated by turns and per-turn prefill, not
decode rate.

Full solo distribution (n=85): min 0.38, p25 4.7, median 8.0, p75 26.0,
max 120.3 tok/s. The extreme low tail is **not throughput data**:

- **WOR-319** — 12 turns, 6 tools, 1,968 output tokens, **87 min wall**
- **WOR-314** — 12 turns, 6 tools, 2,771 output tokens, **74 min wall**

12 turns cannot consume 80 minutes of GPU. These are hung/idle sessions
(external wait or a stuck tool), an outlier class to investigate
separately — **not** a tuning signal. They are excluded from all
throughput conclusions here.

---

## H2 — Concurrency → throughput (production regime, `ticket_metrics`)

Effective tok/s by `dispatch_concurrency` (local workers only):

| concurrency | n | mean tok/s |
|-------------|---|------------|
| 0 (solo)    | 49 | 21.3 |
| 1           | 5  | 9.2  |
| 2           | 6  | 6.1  |
| 3           | 4  | 5.2  |
| 4           | 4  | 5.8  |
| 5           | 1  | 4.7  |
| 6           | 1  | 2.3  |
| (pre-column)| 31 | 16.5 |

Monotonic collapse with concurrency **in the production regime**. Note
the low absolute numbers reflect the H1 wall-time confound (tool-bound
tickets), but the *relative* shape is real and is the opposite of the
controlled bench (H4) — see the reconciliation.

---

## H3 — Prefix-cache vs concurrency: telemetry is BLIND (WOR-439)

`vllm_metrics_attributable`, `vllm_prefix_cache_hit_ratio`,
`vllm_preemptions`, `vllm_prompt_tokens`, `vllm_ttft_mean_seconds`:
**0 / 101 non-null.** The robust parser exists
(`watcher_helpers.capture_vllm_metrics` / `compute_vllm_metrics_delta`)
— the gap is the **WOR-439 capture/attribution bug**, not missing code.
**WOR-439 is a hard blocker for answering this spike's headline
hypothesis from telemetry.** Until it lands, the only direct evidence is
the live A/B:

> **Live 6→2 A/B (WOR-493 overnight, this session):** at 6 concurrent
> real workers the vLLM prefix-cache hit-rate collapsed to ~15% and
> effective throughput to ~4 tok/s; dropping the pool to 2 recovered the
> hit-rate to ~67% and throughput to ~40 tok/s. This is the
> KV-oversubscription signature.

---

## H4 — Controlled bench sweep (the seqs answer, already in `bench_run`)

The WOR-221 campaign (2026-04-26 → 29) already captured the seqs sweep.
Per-stream tok/s, NVFP4 production model:

**`seqs=8` vs `seqs=16` (apples-to-apples):**

| concurrency | seqs=8 tok/s | seqs=16 tok/s | seqs=16 aggregate | VRAM |
|-------------|--------------|---------------|-------------------|------|
| 1 | 168.6 | 171.9 | 171.9 | 29.2 GB |
| 2 | 144.3 | 148.2 | 296.4 | 29.2 GB |
| 3 | — | 134.1 | 402.3 | 29.2 GB |
| 4 | — | 136.9 | 547.6 | 29.2 GB |
| 5 | — | 126.9 | 634.5 | 29.2 GB |
| 6 | — | 128.8 | 772.8 | 29.3 GB |
| 7 | — | 124.8 | 873.6 | 29.2 GB |
| 8 | — | 122.8 | **982.4** | 29.2 GB |

**`seqs=200` check:** c=1 113.8 tok/s (−34% vs seqs=16), VRAM **31.3 GB**.
Slower *and* near-OOM. Decisively rejected.

Per-stream throughput is **flat (~120–172 tok/s)** across c=1→8;
aggregate scales **near-linearly to ~982 tok/s**; VRAM is **rock-stable
at ~29 GB** even at c=8 / ~218 K context. There is **no `max-num-seqs`
ceiling and no KV collapse in the controlled bench.** Lowering seqs to 8
buys nothing and caps concurrency; 16 is correct.

**Bonus signal for the batched-tokens arm:** `chunk_off_4096` 115.8 vs
`chunk_on_4096` 102.3 tok/s at c=1 — the current 4096 chunked-prefill
cap costs ~12% on long-context traffic. This *motivates* (does not yet
answer) the larger-`max-num-batched-tokens` sweep.

---

## H5 — Compaction interaction (hypothesis killed)

`with_compaction` mean 32.1 tok/s (n=11) vs `no_compaction` 14.9 tok/s
(n=90). Compaction correlates with the **fastest** runs because it only
fires on long, high-output sessions where decode dominates wall time
(WOR-305: 211 K output, 1 compaction, 120 tok/s). Compaction is a
*symptom* of high-throughput sessions, not a cause of slow ones. The
"compaction makes runs slow" hypothesis is **false**.

---

## Reconciliation — why the bench scales but production collapses

Two real datasets, opposite shapes, both correct:

| | Controlled bench (H4) | Production workers (H2 + live A/B) |
|---|---|---|
| Prompt shape | fixed, low-divergence | growing, divergent per turn |
| Per-stream tok/s vs concurrency | **flat** to c=8 | **collapses** |
| Aggregate @ c=8 | ~982 tok/s | thrashes |
| KV working set | bounded by fixed prompt | ~134 K × N workers |

The GPU sustains 8 full-speed concurrent decode streams (proven). Real
concurrent workers each hold a ~134 K-token growing context; at the
~148,816-token KV pool, **two heavy workers already oversubscribe it**.
vLLM then evicts prefix-cache blocks between turns → every turn
re-prefills ~134 K tokens → effective tok/s craters even though raw
decode capacity is ~120 tok/s/stream. The 2.17× "ceiling" is this
KV-bytes-vs-context ratio, not a sequence-slot limit.

**Therefore the lever is KV-budget-aware concurrency, not `seqs`.**

---

## Decision matrix

| Knob | Verdict | Evidence |
|------|---------|----------|
| `--max-num-seqs` 8 | Reject | identical to 16 at c≤2, caps concurrency (H4) |
| `--max-num-seqs` 16 | **Keep** | 982 tok/s @ c=8, VRAM-safe (H4) |
| `--max-num-seqs` 24/32/48 | Not worth GPU time | 16 already non-binding; no headroom problem |
| `--max-num-seqs` 200 | Reject | −34% tok/s, 31.3 GB near-OOM (H4) |
| KV-budget concurrency cap | **Adopt (WOR-502)** | H2 + live A/B + reconciliation |
| `--max-num-batched-tokens` >4096 | Worth one GPU sweep | chunk_off>chunk_on +12% (H4 bonus) |

---

## What shipped in this spike PR (automatable, no GPU)

1. `docs/spikes/vllm-max-num-seqs-sensitivity.md` — this report.
2. `scripts/spikes/wor336_throughput_forensic.py` — reproducible
   read-only forensic over `app.db`.
3. `app/core/watcher/watcher_helpers.kv_concurrency_ceiling()` — pure,
   tested helper that computes max safe concurrent workers from KV
   capacity and per-worker context. **This is the function WOR-502
   (effort-aware adaptive concurrency) consumes.** Constants
   `PRODUCTION_KV_CACHE_TOKENS = 148_816`,
   `COMPACTION_CONTEXT_CEILING = 134_000`.
4. `tests/test_wor336_kv_ceiling.py` — unit tests.

---

## Split out — remaining work (not in this PR)

The headline seqs decision is **made**. Deferred items:

### A. (GPU) `--max-num-batched-tokens` sweep — *the one real GPU arm*

Motivated by the H4 bonus signal (4096 chunked-prefill costs ~12% on
long context). This is the absorbed ex-WOR-385 scope. Filed as a
focused follow-up child of WOR-301 (see Linear). Bench commands, ready
to run when a GPU window is free (do **not** auto-run):

```bash
# corrected configs (gpu-mem-util 0.95; cap batched-tokens at 65536
# given KV=148,816; probe --scheduler-reserve-full-isl first)
python scripts/bench/run_bench.py --tier speed --config config/bench-bt-4096.toml
python scripts/bench/run_bench.py --tier speed --config config/bench-bt-8192.toml
python scripts/bench/run_bench.py --tier speed --config config/bench-bt-16384.toml
python scripts/bench/run_bench.py --tier speed --config config/bench-bt-32768.toml
python scripts/bench/run_bench.py --tier speed --config config/bench-bt-65536.toml
python scripts/bench/run_bench.py --compare run_<a> run_<b> ...
```

### B. (Blocked on WOR-439) prefix-cache-vs-concurrency telemetry

Cannot be answered from `app.db` until WOR-439 fixes vLLM `/metrics`
capture (currently 0/101). The live A/B is the only direct evidence
until then. WOR-439 should be prioritised — it gates *quantitative*
confirmation of the central finding.

### C. (Implementation, not spike) KV-budget adaptive concurrency

WOR-502 consumes `kv_concurrency_ceiling()`. Out of spike scope.

### Dropped (answered, no ticket)

- seqs 24/32/48 sweep — 16 is provably non-binding; new GPU runs would
  only re-confirm. Not worth the campaign.
- 3-scenario solo-degradation protocol — the "solo variance" it was
  designed to chase was the WOR-285 artifact (H1). Moot.

---

## References

- `scripts/spikes/wor336_throughput_forensic.py` — reproducible forensic
- WOR-285 — token-aggregation fix (the correction behind H1)
- WOR-439 — vLLM `/metrics` capture (blocks H3 telemetry)
- WOR-502 — effort-aware adaptive concurrency (consumes the helper)
- WOR-287 — prefix-cache verification (~87% solo; extended by the live A/B)
- `docs/spikes/vllm-benchmark-plan.md` — production config + bench harness
- memory `project_vllm_concurrent_throughput.md`,
  `project_vllm_production_config.md`
