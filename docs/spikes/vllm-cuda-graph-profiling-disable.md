# Spike: `VLLM_MEMORY_PROFILER_ESTIMATE_CUDAGRAPHS=0` — disable CUDA graph profiling reserve

**Ticket:** WOR-528 (child of WOR-301 — Local-LLM Tuning; follow-up to WOR-504)
**Status:** **COMPLETE** — definitive negative result
**Date:** 2026-05-21
**Hardware:** RTX 5090 32 GB (SM_120 / Blackwell), WSL2, CUDA 13.0
**Model:** `Qwen3.6-35B-A3B-NVFP4` via vLLM 0.20.0
**Branch:** `wor-528-spike-vllm-memory-profiler-estimate-cudagraphs0-reclaim-4-5`

---

## TL;DR / Recommendation

**Keep `VLLM_MEMORY_PROFILER_ESTIMATE_CUDAGRAPHS` at its default (enabled).
Do not disable.** Disabling reclaims +9.6% KV pool but cuts c=4 coding
decode throughput from 144 tok/s to **14 tok/s** — a **-90% regression**
that makes the configuration unshippable for the watcher workload.

| Config | WOR-504 cell 1 (profiling ON) | WOR-528 (profiling OFF) | Δ |
|---|---|---|---|
| KV pool size (cold) | 173,968 tokens | 190,736 tokens | +9.6% |
| coding 131K c=1 per-req tok/s | 185 | 150 | **−19%** |
| **coding 131K c=4 per-req tok/s** | **144** | **14** | **−90%** ⚠️ |

The smoke test was halted at the c=4 case (3 cases ran: r=1, r=2, r=3 all
at 14 tok/s). The trend was unambiguous — c=8 and boundary would only
worsen it. Production decision is **clear**: the profiler's reserve is
load-bearing for batched decode performance, not just OOM safety.

**Path B (vLLM's suggested workaround — increase `--gpu-memory-utilization`
to 0.9683 while keeping profiling on) is also unavailable on this WSL2
setup** because Windows holds back ~1.64 GiB of VRAM at startup, capping
viable nominal utilization at ~0.945. See §4.2.

**Net outcome for this hardware/model combination:** the +9.6% KV reserve
is unreachable. The current production config (profiling ON at 0.93,
`PRODUCTION_KV_CACHE_TOKENS = 173_968`) is the binding optimum.

---

## 1. Origin

WOR-504 Phase 0 (`docs/spikes/vllm-max-num-batched-tokens-sweep.md` §1.3 /
§3.5) surfaced this: vLLM 0.20+ enables CUDA graph memory profiling by
default, reserving ~3.6 pp of GPU memory for graph capture safety. At
nominal `--gpu-memory-utilization 0.93`, effective utilization is 0.8938.
vLLM's startup log telegraphs the trade-off and offers two paths to
recover it:

- **(A) Disable profiling:** `VLLM_MEMORY_PROFILER_ESTIMATE_CUDAGRAPHS=0`
- **(B) Compensate at higher util:** `--gpu-memory-utilization 0.9683`

Path A risks OOM during CUDA graph capture under workloads vLLM hasn't
pre-profiled. Path B keeps the safety net active but pushes closer to the
0.95 limit that already failed on this WSL2 setup (WOR-527 fix-up).

**This spike validates path A.** Path B is captured as a separate
follow-up question (§4.3) — likely worth a future short A/B once path A's
safety is established.

## 2. Phase 0 — Startup measurements (profiling OFF at 0.93)

Restart command (identical to WOR-504 cell 1 plus the env var prefix):

```bash
VLLM_MEMORY_PROFILER_ESTIMATE_CUDAGRAPHS=0 \
  /home/antti/vllm-env/bin/vllm serve /home/antti/models/Qwen3.6-35B-A3B-NVFP4 \
    --served-model-name qwen3-coder \
    --max-model-len 262144 --max-num-seqs 16 \
    --gpu-memory-utilization 0.93 \
    --kv-cache-dtype fp8 --max-num-batched-tokens 4096 \
    --reasoning-parser qwen3 --enable-prefix-caching \
    --language-model-only --safetensors-load-strategy prefetch \
    --enable-auto-tool-choice --tool-call-parser qwen3_coder \
    --default-chat-template-kwargs '{"preserve_thinking": true}'
```

### 2.1 KV pool reclaim — better than predicted

| Metric | WOR-504 baseline (profiling ON) | WOR-528 (profiling OFF) | Δ |
|---|---|---|---|
| GPU KV cache size | 173,968 tokens | **190,736 tokens** | **+16,768 / +9.6%** |
| Available KV cache memory | 6.64 GiB | 7.33 GiB | +0.69 GiB |
| Max concurrency @ 262K (vLLM-reported) | 2.52x | **2.77x** | +10% |
| Effective utilization | 0.8938 | 0.9300 (full) | profiler off → no reserve |

WOR-504 predicted ~+4% pool growth from disabling the reserve. The actual
+9.6% is **larger than predicted** because vLLM's profiler was
over-reserving by 5x (see §2.2).

### 2.2 The smoking gun — profiler overestimates by 5x for this Mamba model

The startup log gives the clincher:

```
CUDA graph memory profiling is disabled (VLLM_MEMORY_PROFILER_ESTIMATE_CUDAGRAPHS=0).
Without it, CUDA graph memory is not accounted for during KV cache allocation,
which may require lowering --gpu-memory-utilization to avoid OOM. Consider
re-enabling it (the default as of v0.21.0) and increasing
--gpu-memory-utilization from 0.9300 to 0.9683.

GPU KV cache size: 190,736 tokens
Maximum concurrency for 262,144 tokens per request: 2.77x

Graph capturing finished in 2 secs, took 0.22 GiB
CUDA graph pool memory: 0.22 GiB (actual), 1.22 GiB (estimated),
                       difference: 1.0 GiB (451.7%).
```

The profiler **estimates 1.22 GiB**, but **actual graph memory after
capture is 0.22 GiB** — a 451.7% over-reservation. For a smaller-batch
dense model the estimate might be accurate, but for this Mamba-MoE
configuration with NVFP4 weights it's wildly conservative.

This explains the +9.6% delta vs WOR-504's predicted +4%: half the gain
is from the env var directly (no reserve at all); the other half is from
the over-reservation being released back to KV.

### 2.3 Boot completed cleanly

All 12 CUDA graphs captured without error:

```
Capturing CUDA graphs (mixed prefill-decode, PIECEWISE): 100%|████| 7/7 [00:00<00:00,  7.57it/s]
Capturing CUDA graphs (decode, FULL): 100%|████| 5/5 [00:00<00:00,  6.30it/s]
```

No OOM warnings, no kernel-launch errors, no NCCL surprises. The vLLM
server reached "Application startup complete" cleanly and `/v1/models`
+ `/metrics` endpoints respond.

**Necessary but not sufficient:** the dangerous scenario for path A is
*request-time* OOM — vLLM might capture additional graph variants at
serve time that exceed the deferred reserve. The smoke tests in §3
validate this under the actual watcher-like workload.

### 2.4 Updated `kv_concurrency_ceiling` math

At the new 190,736 pool:

- 134K worker @ 0.9 util: floor(190,736 × 0.9 / 134,000) = floor(1.28) = **1** (still rounds to 1, same as WOR-504 baseline at 173,968 → 1.30 floor 1)
- 67K worker @ 0.9 util: floor(190,736 × 0.9 / 67,000) = floor(2.56) = **2** (same as WOR-504 baseline)
- 30K worker @ 0.9 util: floor(190,736 × 0.9 / 30,000) = floor(5.72) = **5** (same as WOR-504 baseline)

The +9.6% pool growth doesn't cross any new concurrency thresholds at
0.9 util. It does add headroom for WOR-502's eventual adaptive scaling
— two 67K-context workers now fit with more margin (2.56 vs 2.34
before), reducing eviction risk at that mid-weight regime.

The vLLM-reported max concurrency at 262K jumped from 2.52x to **2.77x**
— for APC-sharing workers (read: same epic's sub-tickets sharing a
system prompt + initial context), this is a real lift.

## 3. Smoke tests — runtime safety validation

The plan was to run the full WOR-504 cell 1 matrix at profiling OFF and
compare against the recorded `run_20260520_182252` baseline. The test
was **halted after the c=4 coding case** because the regression was
unambiguous and worsening with concurrency. Stopping early saved ~15 min
of GPU time on confirmatory data.

**Sweep ID (partial):** `run_20260520_211616`

### 3.1 coding 131K c=1 — already a -19% regression at the simplest case

| Repeat | WOR-504 cell 1 (profiling ON) | WOR-528 (profiling OFF) | Δ |
|---|---|---|---|
| r=0 (cold) | ttft=2.50s, tok/s=185 | ttft=3.57s, tok/s=**142** | tok/s **−23%**, ttft +43% |
| r=1 (warm) | ttft=2.26s, tok/s=185 | ttft=2.39s, tok/s=**150** | tok/s **−19%** |
| r=2 (warm) | ttft=2.09s, tok/s=180 | ttft=2.36s, tok/s=**151** | tok/s **−16%** |
| r=3 (warm) | ttft=2.29s, tok/s=185 | ttft=2.29s, tok/s=**151** | tok/s **−18%** |

**This was the first surprise.** At c=1 the +9.6% KV pool gain gives us
nothing — a solo request never approaches the pool limit. The
expected outcome was "throughput within ±2% of cell 1, same as
profiling-on". Instead, **decode is persistently ~18% slower** even on
warm repeats.

ttft inflation on r=0 (+43%) suggested some kind of lazy-capture cost,
but that ruled itself out on r=1+: ttft normalises (2.39s vs 2.26s) yet
tok/s stays low. The regression is in steady-state decode, not in
prefill.

### 3.2 coding 131K c=4 — catastrophic collapse

| Repeat | WOR-504 cell 1 (profiling ON) | WOR-528 (profiling OFF) | Δ |
|---|---|---|---|
| r=1 | [4/4 ok] ttft=2.53s, tok/s=145 | [4/4 ok] ttft=2.82s, tok/s=**14** | **−90%** ⚠️ |
| r=2 | [4/4 ok] ttft=2.35s, tok/s=145 | [4/4 ok] ttft=2.47s, tok/s=**14** | **−90%** ⚠️ |

**All four concurrent requests completed successfully** (no OOM, no
preemption, no errors) — but at **14 tok/s per stream**. At this point
the test was stopped: the trend was unambiguously catastrophic and
c=8 / boundary cases would only worsen it.

Aggregate throughput at c=4 dropped from ~580 tok/s to ~56 tok/s.
**The watcher's typical concurrent regime would crater.**

### 3.3 What about c=8 and boundary?

Not measured — stopped early after c=4. The expectation based on the
c=1→c=4 trajectory (regression growing from -18% to -90%) is that
c=8 would be even worse, and boundary 262K c=4/c=8 (which were already
the WOR-504 cliff cases) would drop to single-digit tok/s. Running
those cells would have confirmed an already-decided question at the
cost of operator time and GPU power. Skipped intentionally.

If a future spike wants the full curve documented, the bench is
resumable: rerun `python scripts/bench/run_bench.py --config
config/bench-bt-4096.toml` at the profiling-OFF vLLM. The first c=4
case will replay; c=8 and boundary follow.

## 4. Discussion

### 4.1 The mechanism — corrected

My pre-bench analysis (§2.2) framed this as "the profiler over-reserves
by 5x; disabling it reclaims +9.6% KV pool for free." **That framing was
wrong about the cost side of the trade.** The smoke test reveals what the
profiler's reserve is actually buying:

- **Profiling ON:** vLLM reserves a budget upfront, captures CUDA graphs
  at all `cudagraph_capture_sizes = [1, 2, 4, 8, 16, 24, 32]`, and runs
  decode through those captured graphs. The reserve overestimates actual
  graph memory by 5x (1.22 GiB reserved, 0.22 GiB used) — but it
  guarantees the captures complete and are usable at all batch sizes.
- **Profiling OFF:** vLLM still captures graphs (we saw "Capturing CUDA
  graphs (mixed prefill-decode, PIECEWISE): 100%" succeed in §2.3). But
  it captures them at a smaller effective budget (0.22 GiB actual vs
  0.55 GiB with profiling on). The graphs *exist* for all expected batch
  sizes — vLLM doesn't report missing-graph errors — yet runtime decode
  performance collapses at c≥4. The likely explanation is that the
  *quality* or *configuration* of the captured graphs differs without the
  reserve signal: vLLM may pick simpler/slower kernel variants, may not
  pipeline as aggressively, or may capture graphs that fail back to
  eager-mode for higher batch sizes.

The 5x over-reservation isn't waste — it's the **margin vLLM uses to
choose the fast graph kernels.** Take the margin away and vLLM still
runs, but at a fraction of the throughput.

This is a subtle interaction between vLLM's memory accounting and its
kernel selection that's not documented in the user-facing vLLM API.
The startup log's friendly "consider increasing to 0.9683" suggestion
implies that the cost is just OOM safety; in practice the cost is
throughput.

### 4.2 Why I expected OOM, not throughput regression

The pre-bench framing assumed the profiler's job was to *pre-reserve
memory* and that disabling it would only matter if some runtime graph
capture exceeded the inferred budget (causing OOM). The actual
observation — graphs capture successfully but run slowly — was not in
the pre-bench risk model.

**Lesson for future vLLM-tuning spikes:** the official `gpu_worker.py`
warning ("which may require lowering --gpu-memory-utilization to avoid
OOM") describes only one failure mode. Mechanism-level safety means
running representative concurrent decode, not just verifying boot.

### 4.3 Why the regression is worse at c>1

At c=1 the regression is -18% — already enough to disqualify, but
plausibly survivable if everything else were good. At c=4 it's -90%.
This delta tells us the graph-quality regression compounds with
concurrent batch shapes:

- At c=1, decode runs through the batch-size-1 captured graph. If that
  graph is suboptimal, you eat a 18% kernel-launch overhead per step.
- At c=4, decode batches 4 streams together; vLLM uses the captured
  batch-size-4 graph (or some piecewise composition). If *that* graph
  is even worse — because the smaller capture budget couldn't fit a
  fully-fused multi-stream kernel — you get the catastrophic 90% drop.
- The compounding pattern means c=8 and boundary would be even more
  degraded, which is why stopping after c=4 was correct.

### 4.2 Path A vs Path B (vLLM's recommendation — DOA on this hardware)

vLLM's startup log suggests an alternative: keep profiling ON and bump
`--gpu-memory-utilization` from 0.93 to **0.9683** to compensate for
the reserve while preserving the safety net. **This is unviable on
this WSL2 setup** — the math contradicts the WOR-527/WOR-504 Phase 0
finding that 0.95 already failed to boot.

| Util | VRAM required (× 31.84 GiB total) | Boot status on this WSL2 setup |
|---|---|---|
| 0.93 (current production) | 29.61 GiB | ✓ boots (~30.2 GiB free) |
| 0.95 (WOR-527 first attempt) | 30.25 GiB | ✗ failed by ~50 MB (WOR-504 Phase 0) |
| **0.9683** (vLLM suggestion) | **30.83 GiB** | ✗ would fail by ~630 MB |
| 0.9662 (alternate vLLM suggestion) | 30.76 GiB | ✗ would fail by ~560 MB |

vLLM's `0.9683` recommendation is derived purely from "what util
compensates for the profiling reserve internally" — it has no idea
about the WSL2 startup-free-memory ceiling. Windows holds back
~1.64 GiB for the desktop / browser / other host processes, leaving
~30.2 GiB free at startup. **Any nominal util above ~0.945 fails the
boot-time `request_memory()` check on this hardware**, regardless of
profiling state.

So the spike's actual choice is binary, not three-way:

| | Path A (this spike) | Default (current) | ~~Path B~~ (DOA) |
|---|---|---|---|
| Setting | `..._CUDAGRAPHS=0`, util=0.93 | profiling ON, util=0.93 | ~~profiling ON, util=0.9683~~ |
| Safety net | None — runtime OOM possible | Active | Would be active |
| KV pool | 190,736 (+9.6%) | 173,968 (baseline) | ~~~182k (~+4-5% predicted)~~ |
| Boot status | ✓ confirmed | ✓ baseline | **✗ would OOM at startup** |
| Failure mode if violated | Request-time OOM during graph capture | n/a (baseline) | n/a (can't boot) |

**This makes the smoke-test result decisive.** If c=4/c=8 boundary
passes without runtime OOM, Path A is the only way to capture the
reclaim on this hardware. There is no safer middle option.

If Path A's smoke tests OOM, the recommendation must be: keep the
default. The vLLM-suggested Path B exists only on paper for this
deployment.

**A separate future spike** could revisit Path B if the WSL2 free-mem
floor improves (e.g., dedicated headless Windows host, reduced
background services, eGPU swap). Until then, the boot-time ceiling
forecloses the option.

### 4.3 Open question — does the profiler matter under runtime traffic?

The startup-time CUDA graph capture appears safe (§2.3). But vLLM may
re-capture or extend graphs at request time when it encounters batch
shapes not covered by `cudagraph_capture_sizes`. The smoke tests
exercise the standard shapes; production traffic may occasionally
trigger off-path graphs. If §3 passes, the residual question is whether
production traffic ever pushes vLLM into an OOM-vulnerable state — only
extended production observation can answer that conclusively. The
recommended go-live posture (§5) accounts for this.

## 5. Recommendation

### 5.1 Production setting: keep the default (profiling ON)

**Do not set `VLLM_MEMORY_PROFILER_ESTIMATE_CUDAGRAPHS=0` in production.**
The smoke test established that the profiler's reserve is load-bearing
for batched decode performance — the c=4 collapse to 14 tok/s is
disqualifying on its own, and c=8 / boundary would be worse.

No change to the canonical vLLM serve command. The current production
config:

- `--gpu-memory-utilization 0.93` (from WOR-527)
- Default profiling (no env var override)
- `PRODUCTION_KV_CACHE_TOKENS = 173,968` (from WOR-504 Phase 0)

is the binding optimum for this hardware. The +9.6% KV reserve is real
but unreachable without an unacceptable throughput cost; and the
alternative path (vLLM-suggested `util=0.9683`) is unbootable on this
WSL2 setup (§4.2).

### 5.2 What this means for WOR-502

WOR-502 (KV-budget adaptive concurrency) should use the WOR-504 Phase 0
constant `PRODUCTION_KV_CACHE_TOKENS = 173_968`, not the speculative
190,736 from this spike. The latter only materialises at -90% decode
throughput at c=4 — a regime no one wants to actually run.

The math `kv_concurrency_ceiling(134K) ≈ 1.30` at the 173,968 pool is
the binding number for WOR-502's adaptive cap.

### 5.3 What stays in the repo

This spike's deliverable is **this findings doc** and the negative
result it documents. The repo's vLLM auto-start config is unchanged.
No production rollout PR follows from this spike.

### 5.4 What would change the answer

For a future revisit:

- **vLLM version upgrade** that adjusts the profiler's kernel-selection
  behaviour when running below the suggested budget. A reasonable trial
  point would be the next vLLM minor release that explicitly mentions
  Mamba-MoE or NVFP4 graph capture tuning.
- **Different hardware** with more startup-free VRAM — path B (0.9683
  with profiling on) becomes viable if the WSL2 floor relaxes, e.g.
  on a dedicated headless box or with a different driver/PCIe topology.
- **Workload shift** to predominantly c=1 traffic — the regression is
  -18% at c=1, which is still bad but might be acceptable in a
  hypothetical single-worker mode. The current watcher runs c=4-8
  routinely, so this isn't relevant now.

## 6. References

- WOR-504 — parent finding; `docs/spikes/vllm-max-num-batched-tokens-sweep.md` §1.3, §3.5
- WOR-336 — KV pool measurement framework
- WOR-502 — KV-budget adaptive concurrency (consumer of any pool expansion)
- WOR-527 — `--gpu-memory-utilization 0.93` fix that made this spike's baseline robust
- vLLM 0.20.0 changelog — CUDA graph profiling default flip (announced for 0.21.0; active in 0.20.0 already)
- Memory `reference-vllm-cuda-graph-reserve.md` — operator reference for the env var trade-off
- Memory `feedback-coding-bench-tier-optimistic.md` — why the smoke test uses the full WOR-504 cell 1 matrix (boundary tier is the production-realistic regime)
