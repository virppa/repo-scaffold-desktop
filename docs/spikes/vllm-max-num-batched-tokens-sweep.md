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

**Matrix:**

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
| vllm_bt_4096 | — | — | — | — | — | — | — | — | — | — | — |
| vllm_bt_8192 | — | — | — | — | — | — | — | — | — | — | — |
| vllm_bt_16384 | — | — | — | — | — | — | — | — | — | — | — |
| vllm_bt_32768 | — | — | — | — | — | — | — | — | — | — | — |
| vllm_bt_65536 | — | — | — | — | — | — | — | — | — | — | — |
| vllm_bt_chunkoff | — | — | — | — | n/a | n/a | n/a | n/a | — | — | — |

### 2.1 Cell 1 — `vllm_bt_4096` (baseline)

*(To be appended when cell 1 completes.)*

Expected:
- coding 131K c=1: ~187 tok/s (per WOR-221 step I baseline)
- coding 131K c=8: ~982 tok/s aggregate
- boundary 262K c=1: ~155 tok/s (per WOR-221 step I)
- prefix_cache_hit_ratio: high (>0.9) — small steady-state repeats

If these reproduce within ~5%, it confirms the harness is healthy and the
0.93 KV regime doesn't degrade the baseline.

### 2.2 Cell 2 — `vllm_bt_8192`
*(Pending)*

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
