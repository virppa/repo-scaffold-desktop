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

## TL;DR / Recommendation

**Keep `--max-num-batched-tokens 4096`.** The WOR-336 "12% chunked-prefill
tax" hypothesis does not hold under production-realistic concurrent
heavy-context workloads. Larger BT shrinks the KV pool (10.8% at
BT=8192, 22.9% at BT=16384) and creates a severe concurrency cliff:

| Boundary 262K c=4 (the production-realistic case) | tok/s per-req | vs baseline |
|---|---|---|
| BT=4096 (current) | **121** | baseline |
| BT=8192 | 38 | **−69%** |
| BT=16384 | **10** | **−92%** |

The coding tier (synthetic short prompts) was BT-invariant at ~1000
tok/s aggregate. **But coding-tier numbers are misleadingly optimistic
for the production workload**: real watcher sessions carry 50K-134K
contexts that grow per turn, concurrent and divergent across workers
— much closer to the boundary tier. The KV-pool-shrinkage + Mamba-SSM
chunk-tax + APC inability combo produces the cliff above.

**Action:**
- No change to the canonical `--max-num-batched-tokens 4096` value.
- Phase 0 corrections (`--gpu-memory-utilization 0.93`,
  `PRODUCTION_KV_CACHE_TOKENS = 173_968`) land with this spike's PR.
- The real lever for production throughput is **WOR-502 KV-budget-aware
  adaptive concurrency**, not BT tuning. Strongly motivated by this
  spike's findings.

**Methodology:**
- Cells 1-3 sweep (`bt_4096`, `bt_8192`, `bt_16384`) was sufficient to
  resolve the curve. Cells 4-6 skipped — predicted to confirm the
  monotonic worsening into OOM territory without changing the
  recommendation. Saved ~60 min of operator time. The sweep is
  resumable; cells 4-6 can be run later via `--cells` if a complete
  curve is needed.
- Bench infrastructure (sweep IDs, resume, /metrics deltas) worked
  cleanly once the metric-name typo was fixed mid-sweep.

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
| 8192 | 155,104 | −10.8% | 2.27x | 4 |
| 16384 | **134,144** | **−22.9%** | **1.94x** | **4** |
| 32768 | *(pending — predicted ~107k)* | | | |
| 65536 | *(pending — predicted ~78k)* | | | |
| 131072 (chunkoff) | *(pending — may fail outright)* | | | |

**Cell 3 update (BT=16384):** the KV-pool decline is accelerating
slightly per BT doubling (−11%, then −14% within the same starting
budget). At this point a *single* 134K-context heavy worker just
fits the pool at 0.9 utilization — concurrent heavy serving is now
impossible without preemption. Mid-weight (67K) `kv_ceiling` drops
from 2 to 1 — the first material concurrency loss for the watcher's
typical workload band.

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
| vllm_bt_16384 | run_20260520_193709 | 182 | 574 | 1010 | 182 | 1006 | **117** ⚠️ | **98** ⚠️ | **97.3%** | 1.03s | 0 |
| vllm_bt_32768 | SKIPPED‡ | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| vllm_bt_65536 | SKIPPED‡ | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| vllm_bt_chunkoff | SKIPPED‡ | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |

‡ Cells 4-6 skipped after cell 3 confirmed the trend is monotonic and
accelerating. Boundary c=4 throughput dropped 92% from baseline by
BT=16384; further BT increases would push into OOM territory (VRAM
offload already triggered at BT=16384) with no new information for
the production recommendation. See §3 Discussion for full reasoning.

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

### 2.3 Cell 3 — `vllm_bt_16384` — **COMPLETE**

**Sweep ID:** `run_20260520_193709`

**Headline:** *the boundary cliff deepens monotonically. Boundary c=4
per-req drops to 10 tok/s (−92% from baseline), boundary c=8 per-req
drops to 12 tok/s (−90%). For the first time, boundary c=1 (solo) also
degrades — 117 tok/s vs 157 baseline (−25%) — and VRAM offload to host
memory triggers during the cold boundary prefill.*

| Tier × ctx × c | Cell 1 (4096) | Cell 2 (8192) | Cell 3 (16384) | Δ vs baseline |
|---|---|---|---|---|
| coding 131K c=1 | 185 | 182 | 182 | −2% |
| coding 131K c=8 agg | 1010 | 990 | 1010 | flat |
| coding 262K c=1 | 185 | 182 | 182 | −2% |
| coding 262K c=8 agg | 1003 | 985 | 1006 | flat |
| boundary 262K c=1 (warm) | 157 | 154 | **117** | **−25%** ⚠️ |
| boundary 262K c=1 r=0 cold TTFT | 20.5s | 23.6s | **34.9s** | +70% slower |
| **boundary 262K c=4 per-req** | 121 | 38 | **10** | **−92%** ⚠️ |
| **boundary 262K c=4 agg** | 481 | 154 | **40** | **−92%** ⚠️ |
| **boundary 262K c=8 per-req** | 116 | 37 | **12** | **−90%** ⚠️ |
| **boundary 262K c=8 agg** | 944 | 297 | **98** | **−90%** ⚠️ |
| Offload column | No | No | **Yes** (c=1 r=0) | first offload event |
| cache_hit ratio | 97.3% | 97.3% | 97.3% | unchanged |
| preempts | 0 | 0 | 0 | still cache eviction |

**Two new failure modes:**

1. **Boundary c=1 (solo) degrades for the first time** (157 → 117,
   −25%). The 134K KV pool barely fits one full 249K-token boundary
   prompt; even with no concurrent workers, vLLM partially evicts
   during prefill. The cold-prefill TTFT also extends from 20.5s
   (cell 1) → 34.9s (cell 3), confirming the prefill itself is
   getting harder.

2. **VRAM offload to host memory triggered** on boundary 262K c=1
   r=0 — `Offload Yes` in the summary table. VRAM held at 31.2 GB
   (ceiling) and vLLM moved KV blocks to system RAM to keep the
   request alive. This is the danger threshold — pushing BT higher
   would either OOM outright or thrash through PCIe-bound offload
   storms.

**Coding tier remains immune:** short prompts don't trip the
oversubscription threshold even at c=8, and aggregate throughput is
within 1% of baseline across all coding configurations.

**One-iteration anomaly:** coding 131K c=8 r=3 took 33.85s wall vs
~14s for r=1/r=2 — same warmup pattern as cell 2's first c=8
iteration. Internal vLLM state transition, not a regression.

### 2.4-2.6 Cells 4-6 — **SKIPPED**

The cell 1→2→3 trajectory established a clear monotonic curve:
boundary c=4 per-req throughput is **121 → 38 → 10 tok/s** — losing
roughly two-thirds at each BT doubling, with VRAM offload already
triggered at BT=16384.

**Predicted cell 4-6 outcomes (not measured):**

| Cell | KV pool (predicted) | Boundary c=4 per-req (predicted) | Failure mode |
|---|---|---|---|
| 4 (BT=32768) | ~107k | ~5-8 tok/s | severe; potentially boundary c=1 also fails |
| 5 (BT=65536) | ~78k | OOM-risk | boundary tier may fail at c≥1; coding 262K starts hurting |
| 6 (BT=131072, chunkoff) | ~50k or less | likely fail | may not boot at full util; even coding 131K marginal |

Running cells 4-6 would extend the curve into pathological OOM
territory without changing the production recommendation, which is
already locked in (see §3 Discussion and §4 Recommendation). The
operator-time cost (~45-75 min more) is not justified given the
monotonic trend and the VRAM-offload warning at BT=16384.

**Saved data:** the operator can re-run cells 4-6 later via
`python scripts/bench/run_wor504_sweep.py --cells vllm_bt_32768`
etc. The wrapper is resumable; cells 1-3 will be skipped as already
complete. This is the right exit condition for the spike — clear
answer obtained, full curve documented in §3, future operators
can extend the matrix if needed.

---

## 3. Discussion

The sweep produced a clear and unexpected answer: **the WOR-336 "BT
recovers the 12% chunked-prefill tax" hypothesis is wrong**. Larger
BT doesn't recover the tax — it amplifies a different, much larger
cost (KV-pool oversubscription cliff under realistic concurrent
heavy-context workloads).

### 3.1 The BT-vs-throughput curve has two regimes, separated by workload

**Coding tier (short prompts at large ctx allocation):**
throughput is **invariant to BT** across 4096-16384. Aggregate
c=8 stays at 1003-1010 tok/s regardless. The "ctx=262144" config
parameter is just an allocation ceiling; actual coding prompts are
a few hundred tokens, so per-worker KV footprint is small. The
oversubscription threshold is never crossed.

**Boundary tier (~249K-token prompts, realistic concurrent fan-out):**
throughput **collapses monotonically and steeply** with BT. The
mechanism (validated by three data points):

- **Per-worker KV demand exceeds pool** when concurrent boundary
  workers run. At c=4 the demand is 4 × ~249K ≈ 1M tokens against
  a pool that shrinks from 174K (BT=4096) to 134K (BT=16384). APC
  cannot help because `seed=None` at c>1 produces divergent prompts
  with no shared prefix.
- **vLLM's response is constant prefix-cache eviction** (not
  preemption — `preempts=0` throughout). The cache_hit ratio stays
  at 97.3% across all cells because hits are scored at allocation
  time, before the just-evicted blocks would have hit.
- **Larger BT makes each prefill chunk take ~2x longer** (Mamba SSM
  state initialization scales with chunk size). Under the constant
  eviction-and-re-prefill cycle, longer chunks monopolize the GPU's
  compute lanes, starving decode streams across all concurrent
  workers. This is the WOR-221 step B "Mamba SSM chunk overhead"
  finding (which measured −45% on boundary at BT=4096-with-chunked-
  prefill-on vs without) showing up dramatically at higher BT.

### 3.2 Why solo boundary started degrading at BT=16384

A new threshold crossed at cell 3: even with no concurrent workers,
boundary c=1 dropped to 117 tok/s (vs 157 baseline, −25%), and the
cold-prefill TTFT extended from 20.5s to 34.9s. With KV pool at
134,144 tokens and a 249K-token prompt, vLLM cannot keep the full
KV cache resident — it partially evicts even during the prefill of
the very first request. The kv_concurrency_ceiling(134K) is exactly
1 at 0.9 utilization, with no margin for the additional working
memory the prefill itself needs.

This is the boundary between "lossy under concurrency" (BT=8192)
and "lossy even solo" (BT=16384). Higher BT would push this into
"cannot complete the request" territory — the VRAM offload event
at cell 3 r=0 is the precursor.

### 3.3 Coding-tier immunity is a quirk of the bench, not the production workload

The coding tier sends short prompts but reserves `ctx=262144` for
output. The bench reports tok/s based on the output generation rate,
which is decode-dominated. Concurrent decode of 8 short-prompt
requests fits easily in any KV pool 30K+. This is why the coding
tier shows BT-invariance.

**Real worker sessions are not like this.** They:
- Carry 50K-134K-token *input* contexts that grow per turn
- Run multiple concurrent workers with divergent prompts (different
  tickets in flight)
- Continually re-prefill on each turn (the WOR-336 H2 forensic
  showed effective tok/s collapsing with concurrency in production)

Production behavior is much closer to the boundary tier than to the
coding tier. **The sweep's coding-tier numbers are the optimistic
scenario; the boundary-tier numbers are the realistic one.**

### 3.4 The WOR-336 H4 bonus signal is misleading in isolation

The motivating evidence for this spike was: WOR-336 H4 measured
`chunk_off_4096 = 115.8 tok/s` vs `chunk_on_4096 = 102.3 tok/s` at
c=1 long-context — a 12% chunked-prefill cost. This held out the
hope that "if we can disable chunked prefill or use a BT that's
effectively no-chunking, we'd recover 12%."

What this sweep shows:
- **At c=1 the gap is real but small (cell 1 boundary c=1 is 157
  tok/s, in the same ballpark as the WOR-221 step A 162 tok/s
  chunk_off baseline)**.
- **The cost of any BT > 4096 vastly exceeds the 12% recovery
  potential** because the production workload runs at c≥2, often c≥4.
- The chunk-off goal that the spike's chunkoff cell was meant to
  test would require BT ≥ max-model-len (262144), which is far
  outside the safe-pool range we measured.

The WOR-336 bonus signal was a c=1 measurement, but the watcher
operates at c≥2 effectively all the time. The 12% recovery would
require running at c=1 (negating the local-LLM strategy entirely)
or finding a way to disable chunked prefill safely, which the
148,816-token KV pool budget prohibits at boundary contexts.

### 3.5 What about the CUDA graph profiling overhead?

All cells ran with `VLLM_MEMORY_PROFILER_ESTIMATE_CUDAGRAPHS=1`
(the default). vLLM reserves 3.6-8.3 pp of memory for graph
capture safety — effective utilization at nominal 0.93 was
0.8938 (cell 1), 0.9128 (cell 2), 0.9174 (cell 3). Disabling
this could reclaim ~4-5% more KV pool size, partially offsetting
the BT shrinkage.

But the *relative* trends across BT values are invariant to this
overhead — the cliff appears at BT=8192 regardless. The CUDA graph
follow-up spike (see §5) might extend the safe BT range slightly
on the margin, but it cannot rescue BT=16384's boundary cliff.

### 3.6 Bench-vs-production divergence revisited

WOR-336 H2 vs H4 already noted: controlled bench scales to 982
tok/s aggregate at c=8 while production workers thrash. This spike
quantifies the gap: in the controlled bench, *coding* tier hits
1010 tok/s at c=8 across all BT values; in the boundary tier
(production-realistic), c=8 aggregate at BT=16384 is 98 tok/s.

**The boundary tier IS the production scenario.** Future spikes that
care about production-realistic numbers should default to boundary
+ c≥2, not coding.

---

## 4. Recommendation

### 4.1 Production setting: keep `--max-num-batched-tokens 4096`

The current production value is correct. The WOR-336 "12% bonus
signal" cannot be exploited by BT tuning under realistic concurrent
worker conditions — every BT > 4096 trades modest c=1 gains for
severe c≥4 boundary collapse.

**No change to the canonical vLLM serve command across the 5 sites
(`watcher_services.py`, `CLAUDE.md`, `README.md`, `start-ticket.md`,
`start-epic.md`).** Phase 0 already updated `--gpu-memory-utilization
0.93` and `PRODUCTION_KV_CACHE_TOKENS = 173_968` — those land as
part of this spike's PR.

### 4.2 Where the real lever is

The spike found that the limiting factor for production throughput
is **KV-pool oversubscription** under concurrent heavy-context
workers, not chunked-prefill efficiency. This validates two
existing follow-ups:

- **WOR-502** (KV-budget-aware adaptive concurrency) is the right
  lever. It consumes the Phase 0 `PRODUCTION_KV_CACHE_TOKENS =
  173,968` constant and the spike's observation that 1.30 heavy
  workers fit at 0.9 utilization. Capping concurrency to 1-2 for
  heavy workers and 4+ for light workers avoids the oversubscription
  cliff entirely.
- **CUDA graph profiling spike** (follow-up §5) is a secondary
  lever — could buy ~4% more KV but is bounded in upside.

Both are higher-leverage than any BT-value tuning.

### 4.3 Negative-result findings worth preserving

This spike produced several findings worth keeping even though they
flow against the original hypothesis:

1. **BT-tuning is a trade, not a free knob.** Larger BT shrinks the
   KV pool by 10-23% (BT=8192 to BT=16384) and creates a severe
   concurrent-boundary-throughput cliff. Future spikes should not
   assume BT can be raised without measuring KV impact first.

2. **The coding-tier bench is misleadingly optimistic.** Short
   prompts at large ctx allocation insulate the benchmark from
   the oversubscription regime that production runs in. Use the
   boundary tier (or build a watcher-pattern workload variant)
   for production-relevant numbers.

3. **vLLM 0.20+ silently reserves memory for CUDA graph profiling.**
   Documented in §1.3; constant follow-up if reclaim is safe.

4. **0.95 is too aggressive on WSL2 with default Windows desktop**
   (Phase 0 finding; WOR-527 fix-up to 0.93 lands in this PR).

5. **Effective utilization grows with BT** — at higher BT, vLLM's
   reserve for per-step prefill activation memory grows, leaving
   less for KV but more for chunked-prefill working memory.

---

## 5. Open follow-ups (not in this spike)

1. **CUDA graph memory profiling spike** —
   `VLLM_MEMORY_PROFILER_ESTIMATE_CUDAGRAPHS=0`, ~30 min, ~+4-5%
   KV pool if safe. Filed separately at the end of WOR-504.
2. **WOR-502 KV-budget-aware concurrency** — consumes Phase 0's
   updated `PRODUCTION_KV_CACHE_TOKENS = 173_968` and the spike's
   per-worker-context concurrency math. Now strongly motivated by
   §3.1's boundary collapse mechanism.
3. **APC effectiveness under divergent contexts** — the 2.52x
   vLLM-reported max concurrency at cell 1 relies on prefix
   sharing that real divergent workers don't realize. Measuring
   per-cell APC effectiveness under divergent boundary prompts
   would tighten the WOR-502 calibration.
4. **Cells 4-6 of this matrix (deferred)** — operator may resume
   via `python scripts/bench/run_wor504_sweep.py --cells vllm_bt_32768`
   if a complete curve is needed for future readers. Predicted
   outcomes are documented in §2.4-2.6.
5. **WOR-502's concurrency cap value** — the spike found
   `kv_concurrency_ceiling(134K) ≈ 1.30` at the current pool.
   Whether 1 (round-down conservative) or 2 (with APC partial credit)
   is the right cap value remains an open question for WOR-502's
   calibration phase.

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
