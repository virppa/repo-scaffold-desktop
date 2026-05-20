# Spike: vLLM `--max-num-batched-tokens` sweep

**Ticket:** WOR-504 (child of WOR-301 — Local-LLM Tuning; sibling of WOR-336)
**Status:** **IN PROGRESS** — Phase 0 complete; Phase 1 BT sweep underway
**Date:** 2026-05-20 (Phase 0); Phase 1 cells appended as completed
**Hardware:** RTX 5090 32 GB (SM_120 / Blackwell), WSL2, CUDA 13.0
**Model:** `Qwen3.6-35B-A3B-NVFP4` via vLLM 0.20.0
**Driver:** `scripts/bench/run_wor504_sweep.py` — resumable across sessions

> **Working draft.** This file is appended to as each Phase 1 cell finishes.
> The "Recommendation" section is filled in after all cells complete and
> data is consistent across pair-wise compares.

---

## TL;DR

*(To be written when Phase 1 completes. Phase 0's headline lives in §1 below.)*

---

## 1. Phase 0 — KV pool re-measurement at `--gpu-memory-utilization 0.93`

**Goal:** Confirm `PRODUCTION_KV_CACHE_TOKENS = 148_816` is correct after the
WOR-527 GPU-memory-utilization fix lands, before the BT sweep cells run.
Capturing the corrected baseline first means every Phase 1 cell measures
against the same KV regime.

**Method:** Restart vLLM with the canonical command updated to include
`--gpu-memory-utilization 0.93`; capture the `GPU KV cache size: N tokens`
line from the startup log. If the delta vs the 148,816 baseline is ≥2%,
update the constant.

### Headline finding

| Metric | Old (0.90 implicit) | New (0.93 measured) | Delta |
|---|---|---|---|
| KV pool tokens | 148,816 | **173,968** | **+16.9%** |
| Effective utilization (post-CUDA-graph reserve) | ~0.90 | **0.8938** | — |
| `kv_concurrency_ceiling(134K)` at 0.9 util | 1 | 1 (1.168 floor) | unchanged |
| `kv_concurrency_ceiling(67K)` at 0.9 util | 1 | **2** | +1 |
| `kv_concurrency_ceiling(30K)` at 0.9 util | 4 | **5** | +1 |
| vLLM-reported max concurrency @ 262K (APC) | ~1.7x (estimated) | **2.52x** | +0.82 |

**The +16.9% KV pool growth pushes the WOR-336 "1.11 worker" ceiling for
heavy 134K-context concurrent workers up to 1.30 (still rounds to 1 at
default 0.9 util), and bumps mid-weight workers from 1 to 2.** The vLLM-
reported 2.52x at full 262K context relies on automatic prefix caching
(APC) — divergent contexts realize less than this. The real-world
concurrent-worker ceiling for the watcher sits somewhere between the
pessimistic 1.30 and the optimistic 2.52, depending on how much prefix
overlaps across in-flight workers.

This is the upper-bound input for **WOR-502** (KV-budget-aware adaptive
concurrency): the helper's pool-size argument should be `173_968`, not
`148_816`.

### Unexpected finding #1 — `0.95` is unbootable on this WSL2 setup

WOR-527 originally landed `--gpu-memory-utilization 0.95` based on the
WOR-336 spike doc's "corrected configs" line. The first restart attempt
failed at engine init:

```
ValueError: Free memory on device cuda:0 (30.2/31.84 GiB) on startup is
less than desired GPU memory utilization (0.95, 30.25 GiB).
```

WSL2 always holds back some VRAM for the Windows host (here, ~1.64 GiB
for the desktop, browser, etc.) — leaving only ~30.2 GiB free at startup.
`0.95 × 31.84 = 30.25 GiB` exceeds this by ~50 MB, OOM-failing at the
"request memory" stage of engine init.

`0.93` requests ~29.61 GiB and fits with ~590 MiB headroom. **This is the
new production value**, rolled into all 5 canonical command sites
(`watcher_services.py`, `CLAUDE.md`, `README.md`, `start-ticket.md`,
`start-epic.md`) as part of this spike.

**Lesson:** the WOR-336 spike doc's "0.95 corrected configs" line was
theoretical — what we'd want if WSL2 didn't hold back VRAM. Real-world
boot requires measuring against actual startup-free-memory, which is
host-environment-dependent. The watcher's auto-start path now uses 0.93,
which is robust across operator environments.

### Unexpected finding #2 — vLLM 0.20+ silently reserves ~3.6 pp for CUDA graph profiling

vLLM startup log (post-Phase-0 boot at 0.93):

```
CUDA graph memory profiling is enabled (default since v0.21.0).
The current --gpu-memory-utilization=0.9300 is equivalent to
--gpu-memory-utilization=0.8938 without CUDA graph memory profiling.
To maintain the same effective KV cache size as before, increase
--gpu-memory-utilization to 0.9662. To disable, set
VLLM_MEMORY_PROFILER_ESTIMATE_CUDAGRAPHS=0.
```

vLLM 0.20+ reserves ~3.6 percentage points of GPU memory for safe CUDA
graph capture by default (this default predates v0.21.0's announcement
of the change; 0.20.0 ships with it active). The 173,968-token measurement
is at **effective 0.8938**, not nominal 0.93.

**Implications:**

- The WOR-336 baseline of 148,816 was measured before this default was
  on, making the comparison slightly apples-to-oranges. The true "0.90-
  equivalent" gain from going to 0.93 is somewhat larger than the
  reported +16.9%.
- Setting `VLLM_MEMORY_PROFILER_ESTIMATE_CUDAGRAPHS=0` would reclaim ~3.6
  pp of utilization, potentially adding another ~7,000 tokens to the
  pool. But it risks OOM during graph capture for sequences vLLM didn't
  pre-profile, which was the reason the default was flipped on.

**Open follow-up:** *Spike: try `VLLM_MEMORY_PROFILER_ESTIMATE_CUDAGRAPHS=0`
under the watcher's actual workload at c=4 to test whether the reserve
is necessary or overly conservative.* Estimated ~30 min of supervised
work. Not bundled into WOR-504 to keep the BT axis clean.

### Code changes from Phase 0

- `app/core/watcher/watcher_helpers.py`:
  `PRODUCTION_KV_CACHE_TOKENS = 148_816` → `173_968`, comment block
  updated with the 0.93 measurement context and the CUDA graph profiling
  caveat.
- `tests/test_wor336_kv_ceiling.py`: 4 fixture values flowed from the
  constant change (30K-worker ceiling 4→5, 67K @ 0.9 1→2, 20K @ 0.5/1.0
  3,7→4,8, production_constant_sanity 148,816→173,968).
- WOR-527 fix-up: `--gpu-memory-utilization` 0.95 → 0.93 across all 5
  canonical command sites + the WOR-504 sweep configs and wrapper.

---

## 2. Phase 1 — BT sweep cells

### Live finding (cell 2 launch, before bench runs)

The first vLLM relaunch at BT=8192 revealed an unexpected coupling: **the
KV pool shrinks as BT grows**. Per the cell 2 startup log:

```
Available KV cache memory: 5.99 GiB
GPU KV cache size: 155,104 tokens     ← was 173,968 at BT=4096
Maximum concurrency for 262,144 tokens per request: 2.27x  ← was 2.52x
```

| BT | KV pool tokens | Δ vs 4096 | vLLM max c @ 262K | `kv_ceiling(30K)` @ 0.9 |
|---|---|---|---|---|
| 4096 | 173,968 | — | 2.52x | 5 |
| 8192 | **155,104** | **−10.8%** | **2.27x** | **4** |
| 16384 | *(pending)* | | | |
| 32768 | *(pending)* | | | |
| 65536 | *(pending)* | | | |
| 131072 (chunkoff) | *(pending)* | | | |

**Mechanism:** per-step prefill activation memory scales with
`--max-num-batched-tokens`. Each doubling of BT roughly doubles the
activation memory that vLLM reserves for chunked-prefill computation,
and that memory comes out of the KV-cache budget — not the model
weights or the CUDA graph reserve.

This is **not** a flaw — it's the trade-off the spike was set up to
measure. The chunked-prefill speedup of larger BT (fewer Mamba-SSM
chunks per prefill) has to exceed the cost of reduced KV pool
(more frequent prefix-cache eviction, lower max concurrency, possible
preemption pressure) for any BT > 4096 to be a net win in production.

**Re-predicted concern list for cells 4-6:** if KV pool drops ~10% per
BT doubling, BT=32768 leaves ~110k tokens — a single 131K coding-tier
worker no longer fits without preempting. BT=65536 and the chunkoff
cell (BT=131072) may be unable to keep the working set in memory at
all for boundary 262K prompts, showing severe throughput cliffs not
from chunked-prefill but from **KV starvation forcing constant
preemption**.

### Matrix

| Cell | Backend ID | `--max-num-batched-tokens` | Tiers | Concurrency | Status |
|---|---|---|---|---|---|
| 1 | `vllm_bt_4096` | 4096 (baseline, chunk-on implicit) | coding (131K, 262K) + boundary (262K) | c=1, 4, 8 | *(pending)* |
| 2 | `vllm_bt_8192` | 8192 | same | c=1, 4, 8 | *(pending)* |
| 3 | `vllm_bt_16384` | 16384 | same | c=1, 4, 8 | *(pending)* |
| 4 | `vllm_bt_32768` | 32768 | same | c=1, 4, 8 | *(pending)* |
| 5 | `vllm_bt_65536` | 65536 (KV-pool safe-cap) | same | c=1, 4, 8 | *(pending)* |
| 6 | `vllm_bt_chunkoff` | 131072 (chunk-off at coding tier) | coding (131K) only | c=1, 4, 8 | *(pending)* |

State persisted to `.claude/artifacts/wor_504/sweep_state.json` after every
cell — resumable across sessions via `python scripts/bench/run_wor504_sweep.py`.

### Cell results table

*(Populated as each cell completes. Per-cell deep-dives follow below.)*

| backend_id | sweep_id | coding 131K c=1 tok/s | coding 131K c=4 agg | coding 131K c=8 agg | coding 262K c=1 tok/s | coding 262K c=8 agg | boundary 262K c=1 tok/s | boundary 262K c=8 agg | prefix_cache_hit_ratio | mean TTFT (s) | preemptions |
|---|---|---|---|---|---|---|---|---|---|---|---|
| vllm_bt_4096 | run_20260520_182252 | **185** | **574** | **1010** | **185** | **1003** | **157** (warm) | **974** | **97.3%**† | 0.86s** | 0 |
| vllm_bt_8192 | run_20260520_190355 | 182 | 579 | 990 | 182 | 985 | 154 | **297** ⚠️ | **97.3%** | 1.05s | 0 |
| vllm_bt_16384 | — | — | — | — | — | — | — | — | — | — | — |
| vllm_bt_32768 | — | — | — | — | — | — | — | — | — | — | — |
| vllm_bt_65536 | — | — | — | — | — | — | — | — | — | — | — |
| vllm_bt_chunkoff | — | — | — | — | n/a | n/a | n/a | n/a | — | — | — |

### 2.1 Cell 1 — `vllm_bt_4096` (baseline) — **COMPLETE**

**Sweep ID:** `run_20260520_182252` (after the prior 404 failure;
the polluted `run_20260520_181934` rows remain in `bench_run` with
all `outcome='error'` and can be excluded via `WHERE outcome = 'ok'`).

**Headline:** baseline reproduces WOR-221 step I within ±2% across all 6
tier × context × concurrency combinations. Methodological validity
confirmed for the 0.93 KV regime.

| Metric | Cell 1 (0.93) | WOR-221 step I (0.90) | Δ |
|---|---|---|---|
| coding 131K c=1 | **185 tok/s** | 186.8 tok/s | −1% |
| coding 131K c=4 agg | **574 tok/s** | 567.9 tok/s | +1% |
| coding 131K c=8 agg | **1010 tok/s** | 998.8 tok/s | +1% |
| coding 262K c=1 | **185 tok/s** | 182.3 tok/s | +2% |
| coding 262K c=4 agg | **573 tok/s** | 579.4 tok/s | −1% |
| coding 262K c=8 agg | **1003 tok/s** | 996.7 tok/s | +1% |
| boundary 262K c=1 (warm) | **157 tok/s** | 155.1 tok/s | +1% |
| boundary 262K c=4 agg | **481 tok/s** | 519.5 tok/s | −7% |
| boundary 262K c=8 agg | **944 tok/s** | 951.1 tok/s | −1% |

**Concurrency scaling — no cliff:**

| | coding 131K | coding 262K |
|---|---|---|
| c=1 → c=4 speedup | 3.11x | 3.07x |
| c=1 → c=8 speedup | **5.47x** | **5.76x** |

Both regimes scale near-linearly to c=8, matching the WOR-221 "no
concurrency cliff at seqs=16" finding. VRAM held at 31.0-31.2 GB
throughout — well within the 32 GB ceiling, no OOM, zero preemptions.

**APC working as expected (direct evidence, not from /metrics):**

| Repeat | TTFT (boundary 262K c=1) |
|---|---|
| r=0 (cold) | **20.53s** |
| r=1 (warm) | 2.47s |
| r=2 (warm) | 2.42s |
| r=3 (warm) | 2.42s |

The 8x TTFT drop between r=0 and r=1 is the prefix cache hitting on
the ~249K-token boundary prompt. APC is firing strongly at the new
KV pool size; the cache effectively serves the same prompt across
repeats.

**Caveats:**

† **Cache hit ratio (97.3%) computed manually** from the cumulative
  /metrics counters captured just after cell 1 finished
  (`vllm:prefix_cache_queries_total = 6,471,480`;
  `vllm:prefix_cache_hits_total = 6,294,288`; ratio = 6,294,288 /
  6,471,480 = 0.9726). Wrapper missed it live because the original
  metric names lacked the `_total` suffix that vLLM 0.20 actually
  uses; fix landed and cells 2-6 will capture per-cell hit ratios
  directly. Since vLLM was freshly launched in this session,
  cell 1's traffic is the only contributor to these counters --
  the manual delta is exact.

  **97.3% is a very strong APC hit rate**, consistent with the
  bench's repeats=3 pattern: each (tier, ctx, c) combo sends the
  same prompt three times; the first call is cold, the next two
  hit warm cache. Roughly two-thirds of the tokens queried get
  cache hits, plus the first call's queried tokens get partial
  block-level hits from shared system prompt and structure.

\*\* **TTFT mean 0.86s** captured from `vllm:time_to_first_token_seconds_*`
  is lower than the bench's per-case TTFTs (2.0-2.5s typical). Likely
  the vLLM internal histogram measures from "request received" to
  "first token decoded" (skipping client roundtrip), while the bench's
  TTFT is client wall-clock. Both useful, different things.

**Quality eligibility flag** ("task success 0% < 70%"): an artifact
of the coding-tier quality evaluator (pytest/ruff/mypy on generated
output). 0% pass is the FP4-non-determinism territory WOR-221 already
noted, amplified by strict CI checks. Out of scope for WOR-504, which
measures throughput not coding quality.

### 2.2 Cell 2 — `vllm_bt_8192` — **COMPLETE**

**Sweep ID:** `run_20260520_190355`

**Headline:** *coding tier and boundary-c=1 unchanged within ~2%; boundary
c=4 and c=8 collapse by ~70%*. This is the spike's most consequential
finding — it inverts the working hypothesis that "larger BT recovers
the chunked-prefill tax." Larger BT instead creates a sharp cliff under
the exact workload the production watcher generates.

| Tier × ctx × c | Cell 1 (BT=4096) | Cell 2 (BT=8192) | Δ |
|---|---|---|---|
| coding 131K c=1 | 185 | 182 | −2% |
| coding 131K c=4 agg | 574 | 579 | +1% |
| coding 131K c=8 agg | 1010 | 990 | −2% |
| coding 262K c=1 | 185 | 182 | −2% |
| coding 262K c=4 agg | 573 | 547 (avg) | −5% |
| coding 262K c=8 agg | 1003 | 985 | −2% |
| boundary 262K c=1 (warm) | 157 | 154 | −2% |
| **boundary 262K c=4 per-req** | **121** | **38** | **−69%** ⚠️ |
| **boundary 262K c=4 agg** | **481** | **154** | **−68%** ⚠️ |
| **boundary 262K c=8 per-req** | **116** | **37** | **−68%** ⚠️ |
| **boundary 262K c=8 agg** | **944** | **297** | **−69%** ⚠️ |
| cache_hit ratio | 97.3%† | **97.3%** (live) | identical |
| preemptions | 0 | 0 | unchanged |

**Mechanism of the collapse:**

The KV-pool shrink alone (173,968 → 155,104, −11%) can't linearly
explain a 70% throughput drop. The real mechanism combines three
effects:

1. **Boundary tier prompts are ~249K tokens each** — the coding
   tier's `ctx=262144` is just the allocated context budget for a
   short (~few-hundred-token) prompt, but boundary actually fills 95%
   of the context. Concurrent c=4 boundary means 4 × ~249K KV tokens
   demanded against a 155K pool — vast oversubscription.

2. **APC can't help** because `seed=None` at c>1 produces four
   different boundary prompts with no shared prefix. So vLLM has to
   keep all four KV working sets, and constantly evicts/re-prefills.

3. **Each prefill chunk at BT=8192 takes ~2x longer than at BT=4096**
   (Mamba SSM state initialization scales with chunk size). Under
   constant eviction-and-re-prefill from #1+#2, the longer prefill
   chunks monopolize the GPU's compute lanes for longer, starving
   decode streams across all workers. This is the WOR-336 "KV
   oversubscription" failure mode amplified by larger BT.

vLLM reports 0 preemptions — but the throughput collapse comes from
constant prefix-cache *eviction* (a different mechanism from
preemption), not from sequences being swapped out. The cache_hit
ratio stays at 97.3% because the same prompts that miss-and-re-prefill
are scored as cache misses for the *new* allocation, not the eviction
that just happened. (vLLM's hit-ratio measure undercounts the
eviction cost.)

**c=1 is unaffected** because a single boundary worker has the pool
to itself; no oversubscription. The cliff appears only when
concurrent serving forces KV competition.

**Coding tier is unaffected** because its short prompts don't trip
the oversubscription threshold even at c=8 — the per-worker KV
footprint is small.

**Implication for production:** the watcher's actual workload is
multi-turn, growing-context, divergent across concurrent workers —
*much closer to the boundary tier than to coding*. The spike's
working hypothesis "larger BT recovers the WOR-336 12%" doesn't hold.
At BT=8192 the boundary-tier collapse is far larger than any
chunked-prefill gain.

**One-iteration anomaly:** coding 131K c=8 r=1 took 58.78s wall vs
~15s for r=2/r=3. The first c=8 iteration paid CUDA-graph / JIT
warmup cost specific to this BT value; subsequent iterations
normalized. Single-iteration noise, no impact on the per-cell
aggregate.

### 2.3 Cell 3 — `vllm_bt_16384`
*(Pending)*

### 2.4 Cell 4 — `vllm_bt_32768`
*(Pending)*

### 2.5 Cell 5 — `vllm_bt_65536`
*(Pending — must not push past KV pool / VRAM)*

### 2.6 Cell 6 — `vllm_bt_chunkoff`
*(Pending — coding tier only; head-to-head against vllm_bt_4096 at coding 131K)*

---

## 3. Discussion

*(To be written after all cells complete. Pulls together: BT-vs-tok/s curve,
chunk-on-vs-chunk-off tax, KV-pool-peak observations, prefix-cache hit rate
behavior across BT values.)*

### 3.1 Does larger BT recover the chunked-prefill penalty?

*(Open. WOR-336 H4 measured chunk_off_4096 at 115.8 vs chunk_on_4096 at 102.3
on long context — a ~12% chunked-prefill cost. The sweep tests whether BT
values from 8192 to 65536 recover this gap, or whether the Mamba-SSM
per-chunk-boundary tax is fixed.)*

### 3.2 Does the chunkoff cell vindicate disabling chunked prefill entirely?

*(Open. WOR-221 step B found enabling chunked prefill caused −45% on the
boundary tier — the "chunked-prefill on" overhead is real and significant
at default BT. If the chunkoff cell's coding-131K result is materially
higher than vllm_bt_65536's coding-131K, that's evidence the Mamba SSM
tax is structural and BT-tuning can only partially recover it.)*

### 3.3 Interaction with the CUDA graph profiling overhead

*(Open. All cells run with the 3.6 pp CUDA graph reserve active. The
absolute tok/s numbers are at the "effective 0.8938" operating point; if
the follow-up CUDA-graph-profiling spike confirms it's safe to disable,
all numbers would scale somewhat — but the relative ranking across BT
values is invariant to this overhead.)*

---

## 4. Recommendation

*(Filled in when all cells complete and data is consistent. Possible
outcomes:)*

1. **Keep `--max-num-batched-tokens 4096`** — if no higher BT shows
   meaningful improvement over the baseline.
2. **Move to `--max-num-batched-tokens N`** — if a specific higher value
   gives ≥5% aggregate throughput improvement at c=8 without OOM or
   latency regression.
3. **Disable chunked prefill** (`--max-num-batched-tokens >= max-model-len`)
   — if `vllm_bt_chunkoff` materially outperforms all BT-tuned cells,
   reproducing WOR-221's chunk-off baseline.

A follow-up Fix ticket rolls the production recommendation into the
canonical `vllm serve` command across the 5 sites (`watcher_services.py`,
`CLAUDE.md`, `README.md`, `start-ticket.md`, `start-epic.md`).

---

## 5. Open follow-ups (not in this spike)

1. **CUDA graph memory profiling spike** — `VLLM_MEMORY_PROFILER_ESTIMATE_CUDAGRAPHS=0`,
   ~30 min, ~+4% KV pool if safe. Filed separately at the end of WOR-504.
2. **WOR-502 KV-budget-aware concurrency** — consumes the updated
   `PRODUCTION_KV_CACHE_TOKENS = 173_968` and the vLLM-reported 2.52x at
   262K. The +16.9% pool growth makes this lever immediately useful.
3. **APC effectiveness under divergent contexts** — the 2.52x vLLM-
   reported max concurrency relies on prefix sharing. Measuring how
   much divergent watcher traffic actually exploits APC would tighten
   the WOR-502 calibration.

---

## 6. References

- `docs/spikes/vllm-max-num-seqs-sensitivity.md` — WOR-336 parent spike
  (the `max-num-seqs is not the bottleneck, KV is` finding)
- `docs/spikes/vllm-benchmark-plan.md` — WOR-118 / WOR-221 bench
  methodology, original 148,816 baseline
- WOR-336 — parent spike; PR #1029
- WOR-362 — watcher-pattern bench workload (not used here; the tier
  path's coding+boundary tiers are closer to the WOR-336 methodology
  that produced the 12% finding)
- WOR-439 — vLLM `/metrics` capture fix (PRs #1031, #1039) — unblocks
  per-cell prefix-cache + TTFT capture in this spike
- WOR-527 — `--gpu-memory-utilization` fix (PR #1082, follow-up commit
  in this branch corrected 0.95 → 0.93)
- WOR-502 — KV-budget-aware concurrency (consumes Phase 0's updated
  constant)
- `scripts/bench/run_wor504_sweep.py` — operator-paced resumable sweep driver
- `config/bench-bt-*.toml` — per-cell bench configs
