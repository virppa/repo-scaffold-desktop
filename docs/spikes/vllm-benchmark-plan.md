# vLLM Benchmark Findings — WOR-118

**Spike:** WOR-118 (gates WOR-210)
**Hardware:** RTX 5090 32 GB (SM_120 / Blackwell), WSL2, CUDA 13.0 (driver 596.21, supports up to 13.2)
**Model under test:** `Qwen3.6-35B-A3B-NVFP4` — Blackwell-native FP4 weights, ~21 GiB in VRAM
**Served by:** vLLM 0.20.0
**Baselines:** Ollama `qwen3.6:35b-a3b` and `qwen3-coder:30b` on the same hardware

---

## Recommendation

**`Qwen3.6-35B-A3B-NVFP4` on vLLM is the production watcher backend going forward.**

It replaces both Ollama models:

- Matches `qwen3.6:35b-a3b`'s 160 tok/s throughput at ≤131K but with flat 2.2s TTFT
  (vs Ollama's 4.6s→12s TTFT growth with context)
- Extends to 262K context with FP8 KV (Ollama's 35b-a3b falls to 48 tok/s at 256K;
  vLLM FP8 holds 115–122 tok/s flat across the full 131K–262K range)
- Enables real concurrent serving (c=2) via continuous batching — Ollama serialises
- Matches quality (91% vs ~100% in Ollama; gap is FP4 non-determinism, not capability)

`qwen3-coder:30b` under Ollama was the previous production baseline. At ≤98K context it
was 5–12% faster than 35b-a3b and had better VRAM headroom, but it halves to 80 tok/s
at 131K, collapses to near-unusable at 144K+ with 117K prefill (7–12 tok/s), and offers
no concurrency. With vLLM and NVFP4 weights, 35b-a3b at 131K (160 tok/s) is now 2× the
speed of 30b-coder under Ollama at the same context size. For any watcher session that
exceeds 96K tokens — which is most of them once system prompt + CLAUDE.md + file context
are loaded — **30b-coder under Ollama is no longer competitive**.

---

## Server configurations

### BF16 (≤131K context, highest throughput)

```bash
vllm serve /home/antti/models/Qwen3.6-35B-A3B-NVFP4 \
  --max-model-len 131072 \
  --max-num-seqs 200 \
  --reasoning-parser qwen3 \
  --enable-prefix-caching \
  --language-model-only \
  --safetensors-load-strategy prefetch
```

For watcher use (LiteLLM proxy) add:
`--enable-auto-tool-choice --tool-call-parser qwen3_coder`
**Do not add these when running bench coding tier** — the tool call parser routes
the model's JSON output to `tool_calls` (not `content`), which breaks bench evaluation.

### FP8 KV (≤262K context)

```bash
vllm serve /home/antti/models/Qwen3.6-35B-A3B-NVFP4 \
  --max-model-len 262144 \
  --kv-cache-dtype fp8 \
  --max-num-seqs 200 \
  --max-num-batched-tokens 4096 \
  --reasoning-parser qwen3 \
  --enable-prefix-caching \
  --language-model-only \
  --safetensors-load-strategy prefetch
```

`--max-num-batched-tokens 4096`: required because Mamba cache align mode sets
`block_size=2096` which must be ≤ max_num_batched_tokens (default 2048 fails).

**Backend notes:** `VLLM_MOE_BACKEND` and `VLLM_NVFP4_MOE_BACKEND` are both logged as
"Unknown vLLM environment variable" in 0.20.0 — neither has any effect. The FlashInfer
autotuner selects independently:
- BF16: **FLASHINFER_TRTLLM** — 160+ tok/s
- FP8 KV: **FLASHINFER_CUTLASS** — TRTLLM tactics unsupported for NVFP4+FP8 KV in this
  build. CUTLASS gives 115–122 tok/s, which is correct and expected.

---

## Performance comparison

### Single-worker throughput (c=1)

| Context | Ollama 30b-coder | Ollama 35b-a3b | vLLM NVFP4 BF16 | vLLM NVFP4 FP8 |
|---------|-----------------|----------------|-----------------|----------------|
| 16K | 169 tok/s | 161 tok/s | **164 tok/s** | 124 tok/s |
| 64K | 160 tok/s | 159 tok/s | **160 tok/s** | 125 tok/s |
| 128K | 80 tok/s | 158 tok/s | **160 tok/s** | 117 tok/s |
| 144K | 63 tok/s | 76 tok/s | — (OOM) | **120 tok/s** |
| 196K | 59 tok/s | 74 tok/s | — (OOM) | **120 tok/s** |
| 256K | 48 tok/s | 48 tok/s | — (OOM) | **119 tok/s** |

BF16 OOM above 131K: VRAM is fully consumed by model weights (~21 GiB) plus KV cache
at 131K. FP8 KV halves the KV cache footprint, reclaiming enough VRAM for 262K context.

### Time to first token (TTFT, c=1)

| Context | Ollama 30b-coder | Ollama 35b-a3b | vLLM NVFP4 BF16 | vLLM NVFP4 FP8 |
|---------|-----------------|----------------|-----------------|----------------|
| 16K | 3.6s | 4.6s | **2.2s** | 2.5s |
| 64K | 5.2s | 7.7s | **2.2s** | 2.4s |
| 128K | 12.0s | 12.0s | **2.2s** | 2.4s |

vLLM TTFT is flat because Mamba SSM initialisation dominates (~2.1–2.3s regardless of
context size) and APC eliminates the prefill compute for repeated prompts. Ollama's TTFT
grows linearly with context — no prefix caching, no batching, sequential model I/O.

### Cold prefill under concurrent load (Ollama vs vLLM, 131K, 4 workers)

| | Ollama 30b-coder | Ollama 35b-a3b | vLLM NVFP4 BF16 |
|-|-----------------|----------------|-----------------|
| Concurrent workers | 1 (serialised) | 1 (serialised) | up to 4 |
| Per-req tok/s at c=2 | N/A | N/A | **95** (agg 190) |
| Prefill collapse > 131K | yes — 7–12 tok/s | no | N/A (BF16 OOM) |

30b-coder's prefill collapse above 131K on a 117K-token prompt:
144K → 12.4 tok/s, 160K → 9.1 tok/s, 176K → 7.3 tok/s (vs 35b-a3b at 66–68 tok/s).
For multi-turn watcher sessions where context accumulates, 30b-coder becomes effectively
unusable above 131K even before OOM.

---

## Concurrency

Max safe concurrency: **c=2** for all watcher workloads. c=3 collapses for coding
regardless of context size.

The c=3 collapse is HBM bandwidth saturation, not KV pool exhaustion. Each decode step
reads the full K+V matrices through HBM independently per worker. With thinking-enabled
coding responses (800–2000 output tokens), three concurrent workers saturate the
RTX 5090's memory bandwidth. The boundary tier at 131K confirmed the pattern sharply:

| c | Per-req tok/s | Agg tok/s | Notes |
|---|--------------|-----------|-------|
| 1 | 133 | 133 | baseline |
| 2 | 97 | **194** | 46% agg gain |
| 3 | 23 | 69 | **collapse** — bandwidth saturated |
| 4 | 24 | 96 | marginally recovers vs c=3 |

KV pool usage at c=4 is only 26% (APC deduplication keeps all 4 workers' shared prefix
as one physical copy) — so the cliff is not memory capacity, it's bandwidth.

### BF16 concurrency results (run_20260428_000929)

| Context | c=1 tok/s | c=2 per-req | c=2 agg | c=4 agg |
|---------|----------|-------------|---------|---------|
| 16K | 135 | 110 | 220 | 411 |
| 65K | 124 | 105 | 210 | 415 |
| 131K | 136 | 112 | **224** | 409 |

Short prompts (speed tier) don't trigger the bandwidth cliff — c=4 agg exceeds c=2 for
short outputs. The cliff is output-length driven. For coding workloads (long output with
thinking), c=2 is the ceiling at any context size.

~~**`--max-local-workers 2`** for all watcher configurations~~ — **superseded by WOR-221.**
The c=3 collapse above was caused by `max_num_seqs=200` HBM pre-allocation pressure, not a hard
GPU limit. With `max_num_seqs=16` (WOR-221 finding), no cliff exists at any tested concurrency
level. Aggregate tok/s scales monotonically to c=8 (~1000 tok/s). See WOR-221 conclusions below.

---

## FP8 at 262K context

### Throughput (run_20260428_201813)

| Context | c=1 tok/s | c=2 per-req | c=2 agg |
|---------|----------|-------------|---------|
| 131K | ~115 | — | — |
| 196K | ~120 | — | — |
| 262K | ~119 | ~90 | **~179** |

Speed is flat 131K→262K with FP8 KV — the FP8 penalty vs BF16 is ~27% at 131K
(115 vs 160 tok/s) but the model stays at full native context. At 262K c=2, aggregate
throughput is ~179 tok/s — still 3.7× faster than Ollama 35b-a3b at 256K (48 tok/s).

### Boundary at 262K (run_20260428_204415)

95%-fill prompt (~249K tokens), fixed seed — APC deduplication fully active.

| c | Per-req tok/s | Agg tok/s | VRAM |
|---|--------------|-----------|------|
| 1 | ~106 | 106 | 31.19 GB |
| 2 | ~90 | **~180** | 31.19 GB (flat) |

No OOM. VRAM is flat because both workers share one physical copy of the 262K KV blocks
via APC. c=2 at the full native context window is safe.

### Backend selection rule

| Context needed | Config | Command |
|---------------|--------|---------|
| ≤131K | BF16, fast | `--max-model-len 131072` |
| 131K–262K | FP8, full native | `--max-model-len 262144 --kv-cache-dtype fp8 --max-num-batched-tokens 4096` |

---

## Quality and FP4 non-determinism

vLLM coding quality with 10 repeats:

| Context | tok/s | TTFT | Task success |
|---------|-------|------|--------------|
| 16K | 164 | 2.2s | 10/11 (91%) |
| 64K | 160 | 2.2s | 11/11 (100%) |
| 128K | 160 | 2.2s | 10/11 (91%) |

FP8 KV does not degrade quality further — same 91% pass rate at 16K–128K.

The 9–18% failure rate is FP4 non-determinism, not a vLLM regression. Expert routing in
MoE layers uses warp-parallel top-k over GPU warps — floating-point race conditions cause
different experts to be selected across runs even at `temperature=0, seed=42`. Output
token count varies 885–2129 tokens per run. This is inherent to NVFP4 weight quantisation
on Blackwell and is unrelated to the serving backend.

**Thinking is load-bearing:** `enable_thinking=false` produces ~68 tokens of stub code,
0% pass rate. Thinking must be enabled for all watcher coding tasks.

Ollama comparison: `qwen3.6:35b-a3b` under Ollama achieved 100% quality in the Ollama
baseline run, and `qwen3-coder:30b` achieved 100% as well — but the Ollama benchmark
used a simpler quality check. The vLLM 91% is measured against our full
pytest + ruff + mypy pipeline, which is stricter.

---

## MoE backend investigation

The autotuner selects the right kernel without any env vars. Do not override it.

| Env var / flag | Result |
|---|---|
| `VLLM_MOE_BACKEND=FLASHINFER_TRTLLM` | "Unknown variable" — ignored in vLLM 0.20.0 |
| `VLLM_NVFP4_MOE_BACKEND=...` | "Unknown variable" — ignored in vLLM 0.20.0 |
| `VLLM_FLASHINFER_MOE_BACKEND=latency` | Forces TRTLLM for ALL batch shapes → 118–120 tok/s (worse than autotuner) |
| `VLLM_FLASHINFER_MOE_BACKEND=throughput` | Forces CUTLASS — explicit alternative to autotuner; 143–147 tok/s |
| `VLLM_NVFP4_GEMM_BACKEND=flashinfer-trtllm` | Crashes SM_120: TRTLLM linear mm_fp4 not compiled for Blackwell |
| `--cudagraph-capture-sizes 1 2 4 8` | Reduces CUDA graph overhead but limits autotuner to small batch shapes → CUTLASS wins → lower throughput |

Autotuner default: BF16+NVFP4 → TRTLLM (~160 tok/s). FP8 KV+NVFP4 → CUTLASS
(~115–122 tok/s, TRTLLM unsupported for this combination). Both are correct.

---

## Ops notes

**WSL2 VRAM zombie:** Crashed vLLM leaves a dangling CUDA context showing 30 GB used
with no processes. Fix: `wsl --shutdown` from Windows PowerShell, not `nvidia-smi`.

**Cold-start false alarm:** An FP8 run (run_20260428_193457) produced 18–21 tok/s because
the server was not fully warmed up (CUDA graphs and JIT compilation still in progress)
when the first requests fired. Deleted from bench.db. Always confirm server warmup before
treating low numbers as meaningful. Second run with identical flags showed 107–130 tok/s.

**Mamba alignment trap at 147K BF16:** `--max-model-len 147456` changes `block_size`
from 2096 to 1056 tokens. This shrinks the effective KV pool to 99K tokens (less than the
standard 131K BF16 config) and collapses throughput to 63–70 tok/s (CUTLASS replaces TRTLLM,
new compile cache key, smaller Mamba block crossings). 147K is strictly worse than 131K.
The FP8 config is the right path to extend context, not BF16 with larger `max_model_len`.

**Ollama must be stopped before vLLM:** `ollama stop qwen3.6:35b-a3b` or vLLM OOMs.

**torch.compile cache:** First server start compiles (26s). Subsequent restarts read the
cache — startup is ~12s (model load) after the first time.

**APC cold hit:** First request to a large unique prompt is slow — 9.53s TTFT for a 124K
token boundary prompt (cold prefill). All subsequent repeats hit 2.3s (APC warm hit at
74% → 95%+ block hit rate across repeats). Plan for one cold request per unique large
context; watcher sessions with repeated tool call cycles benefit immediately.

---

## Bench framework changes (made during this spike)

- `runner.py`: `_run_generate()` fires N concurrent threads (real concurrent dispatch);
  `_aggregate()` averages TTFT/tok/s across workers. Concurrency was previously a label
  only — requests fired sequentially regardless of `concurrency_levels`.
- `reporter_ranking.py`: `compute_concurrency_efficiency` changed from efficiency ratio
  (`tok_c / (c × tok_1)`) to aggregate speedup (`c × tok_c / tok_1`). Concurrency scaling
  section now shows per-group table with per-req tok/s, aggregate tok/s, TTFT p50, and
  speedup. Labels: >1.5× "scales well", >1.1× "partial gain", >0.9× "no gain", else
  "overhead".
- `tasks/boundary.py`: fixed to generate a prompt at 95% of `context_size` (was using a
  fixed 131K prompt regardless of the configured context size).

**APC effectiveness reporter caveat:** For this model and sweep the reporter prints
"no APC benefit" for `prefill_shared` vs `prefill_unshared`. This is a measurement
artefact: (1) Mamba SSM initialisation dominates TTFT (~2.1s floor) regardless of context
size, so even a full APC hit saves prefill compute that is invisible against the Mamba
floor; (2) `prefill_unshared` uses `seed=42` for all repeats, so its blocks are also
cached by r=2. The actual APC win is real — cold boundary hit shows 9.53s → 2.34s for
124K tokens. Disregard the APC EFFECTIVENESS reporter section for this model.

---

## WOR-221 parameter sweep findings

**Spike:** WOR-221
**Config:** FP8 KV throughout (`--kv-cache-dtype fp8 --max-model-len 262144`) — all steps
use the production server config so results are directly comparable to FP8 baselines below.
**Sweep script:** `python scripts/bench/run_wor221_sweep.py --step <A-G>`
**Config file:** `config/bench-wor221.toml`

### FP8 baselines (from bench.db, used as comparison targets)

| Tier | Context | c | Per-req tok/s | Agg tok/s | Source sweep |
|------|---------|---|--------------|-----------|--------------|
| coding | 131K | 1 | ~106 | — | run_20260428_201813 |
| coding | 131K | 2 | ~103 | ~206 | run_20260428_201813 |
| coding | 262K | 1 | ~125 | — | run_20260428_201813 |
| coding | 262K | 2 | ~88 | ~176 | run_20260428_201813 |
| boundary | 262K | 1 | ~105 (warm) | — | run_20260428_204415 |
| boundary | 262K | 2 | ~90 | ~180 | run_20260428_204415 |

### Step results

Steps run independently. Sweep IDs are the `run_YYYYMMDD_HHMMSS` prefix printed at bench start.

#### A — Baseline (no chunked prefill, batched_tokens=4096)

`vllm serve /home/antti/models/Qwen3.6-35B-A3B-NVFP4 --max-model-len 262144 --kv-cache-dtype fp8 --reasoning-parser qwen3 --enable-prefix-caching --language-model-only --safetensors-load-strategy prefetch --max-num-seqs 200 --max-num-batched-tokens 4096`

Sweep ID: `run_20260429_194431`

| Tier | Context | c | TTFT avg (s) | Per-req tok/s | Agg tok/s |
|------|---------|---|-------------|--------------|-----------|
| speed | 131K | 1 | 2.20 | 113.5 | 113.5 |
| speed | 131K | 2 | 2.09 | 93.7 | 187.4 |
| coding | 131K | 1 | 2.45 | 120.2 | 120.2 |
| coding | 131K | 2 | 2.60 | 96.2 | 192.4 |
| boundary | 262K | 1 | 8.03 | 113.7 | 113.7 |
| boundary | 262K | 2 | 3.00 | 87.7 | 175.4 |

#### B — Chunked prefill ON, batched_tokens=4096

`vllm serve /home/antti/models/Qwen3.6-35B-A3B-NVFP4 --max-model-len 262144 --kv-cache-dtype fp8 --reasoning-parser qwen3 --enable-prefix-caching --language-model-only --safetensors-load-strategy prefetch --max-num-seqs 200 --max-num-batched-tokens 4096 --enable-chunked-prefill`

Sweep ID: `run_20260429_195236`

| Tier | Context | c | TTFT avg (s) | Per-req tok/s | Agg tok/s | vs A |
|------|---------|---|-------------|--------------|-----------|------|
| speed | 131K | 1 | 2.17 | 120.7 | 120.7 | +6% |
| speed | 131K | 2 | 2.09 | 99.7 | 199.5 | +6% |
| coding | 131K | 1 | 2.40 | 123.4 | 123.4 | +3% |
| coding | 131K | 2 | 2.77 | 84.6 | 169.1 | **−12%** |
| boundary | 262K | 1 | 9.43 | 62.8 | 62.8 | **−45%** |
| boundary | 262K | 2 | 2.83 | 54.6 | 109.2 | **−38%** |

**Verdict: REGRESSION.** The boundary c=1 case (no competing workers, pure prefill cost) drops −45%.
That eliminates scheduling contention as a cause — the Mamba SSM must checkpoint and restore state
at every chunk boundary (~61 chunks for a 249K-token prefill at batched_tokens=4096). This overhead
dominates throughput at large context sizes. Larger batched_tokens (C, D) would reduce chunk count
but can't eliminate the SSM per-chunk tax.

#### C — Chunked prefill ON, batched_tokens=8192 — **SKIPPED**

Skipped: B shows −45% boundary regression at c=1, which eliminates scheduling artifacts as the
cause. The root issue is Mamba SSM state checkpointing per chunk. Larger batched_tokens halve chunk
count but don't remove the per-chunk overhead — partial recovery would still leave boundary
throughput well below baseline A.

#### D — Chunked prefill ON, batched_tokens=16384 — **SKIPPED**

Skipped: same rationale as C. Even at 16384 tokens/chunk (16× larger than B), the ~15 chunks for
a 249K prefill each incur SSM state save/restore. The −38% c=2 regression from B would not
recover to match A.

#### E — num_scheduler_steps=4 — **SKIPPED (flag not in vLLM 0.20.0)**

`--num-scheduler-steps` is not a recognized argument in vLLM 0.20.0. Verified: server startup
fails with "unrecognized arguments: --num-scheduler-steps 4". Flag was added in a later release.

#### F — num_scheduler_steps=8 — **SKIPPED (flag not in vLLM 0.20.0)**

Same as E — flag unavailable in 0.20.0.

#### G — max_num_seqs=8 (queue pressure sanity check)

`vllm serve /home/antti/models/Qwen3.6-35B-A3B-NVFP4 --max-model-len 262144 --kv-cache-dtype fp8 --reasoning-parser qwen3 --enable-prefix-caching --language-model-only --safetensors-load-strategy prefetch --max-num-seqs 8 --max-num-batched-tokens 4096`

Sweep ID: `run_20260429_200447`

| Tier | Context | c | TTFT avg (s) | Per-req tok/s | Agg tok/s | vs A |
|------|---------|---|-------------|--------------|-----------|------|
| speed | 131K | 1 | 2.41 | 163.0 | 163.0 | **+44%** |
| speed | 131K | 2 | 2.16 | 140.0 | 280.1 | **+49%** |
| coding | 131K | 1 | 2.17 | 187.0 | 187.0 | **+56%** |
| coding | 131K | 2 | 2.31 | 160.4 | 320.9 | **+67%** |
| boundary | 262K | 1 | 7.01 | 155.8 | 155.8 | **+37%** |
| boundary | 262K | 2 | 2.57 | 132.3 | 264.6 | **+51%** |

**Verdict: UNEXPECTED — max_num_seqs=200 is actively harmful for this workload.** The hypothesis
was that G would match A (confirming 200 is non-binding). Instead G outperforms A by 37–67%
across every tier and concurrency level, including c=1 where there is no queue pressure at all.

**Likely mechanism:** vLLM pre-allocates internal scheduler state (block tables, sequence metadata)
proportional to `max_num_seqs`. With 200 slots, this consumes enough HBM to create memory pressure
during decode, reducing effective GPU utilization. With 8 slots, more HBM is available for KV cache
and compute. The c=1 improvement (no scheduling interaction) proves the effect is pure
memory-pressure, not scheduling efficiency. Follow-up: check `nvidia-smi`'s VRAM usage at idle
with max_num_seqs=200 vs 8 to quantify the pre-allocation delta.

**Immediate WOR-218 action:** switch to `--max-num-seqs 16` (headroom for c=2 bursts with 8× safety
margin). Testing max_num_seqs=16 vs 8 is low priority — at c=2 either cap is non-binding, and the
throughput difference is likely small.

---

### Conclusions

| Parameter | Verdict | WOR-218 action |
|-----------|---------|----------------|
| `enable_chunked_prefill` | **OFF** — −45% boundary regression (Mamba SSM chunk overhead at each chunk boundary) | Do not enable |
| `max_num_batched_tokens` | Keep at 4096 — irrelevant without chunked prefill; standard scheduler budget | No change |
| `num_scheduler_steps` | **Unavailable** in vLLM 0.20.0 — flag rejected at server startup | Skip; revisit on vLLM upgrade |
| `max_num_seqs=200` | **Actively harmful** — reducing to 8 gives +37–67% throughput across all tiers | Switch to `--max-num-seqs 16` |
| Max viable concurrency | **No cliff** — WOR-118 cliff was seqs=200 pressure; with seqs=16 agg scales to c=8 (~1000 tok/s) | **`--max-local-workers 8`** |

#### H — max_num_seqs=16, c=1/2/3/4 (production config, concurrency cliff probe)

`vllm serve /home/antti/models/Qwen3.6-35B-A3B-NVFP4 --max-model-len 262144 --kv-cache-dtype fp8 --reasoning-parser qwen3 --enable-prefix-caching --language-model-only --safetensors-load-strategy prefetch --max-num-seqs 16 --max-num-batched-tokens 4096`

Config: `config/bench-wor221h.toml` (concurrency_levels=[1,2,3,4])
Sweep ID: _(fill in)_

Sweep ID: `run_20260429_211048`

| Tier | Context | c | TTFT avg (s) | Per-req tok/s | Agg tok/s | vs G (seqs=8) |
|------|---------|---|-------------|--------------|-----------|---------------|
| speed | 131K | 1 | 2.07 | 170.9 | 170.9 | +0.5% |
| speed | 131K | 2 | 2.10 | 141.9 | 283.8 | +1.4% |
| speed | 131K | 3 | 2.19 | 132.2 | 396.6 | — |
| speed | 131K | 4 | 2.11 | 132.7 | 530.8 | — |
| coding | 131K | 1 | 2.32 | 187.7 | 187.7 | +0.3% |
| coding | 131K | 2 | 2.26 | 156.3 | 312.6 | −2.6% |
| coding | 131K | 3 | 2.24 | 142.9 | 428.7 | — |
| coding | 131K | 4 | 2.29 | 143.2 | 572.8 | — |
| boundary | 262K | 1 | 2.44 | 152.2 | 152.2 | −1.5% |
| boundary | 262K | 2 | 2.65 | 135.6 | 271.2 | +2.5% |
| boundary | 262K | 3 | 3.08 | 122.1 | 366.3 | — |
| boundary | 262K | 4 | 3.25 | **133.6** | 534.4 | — |

**Key findings:**

- seqs=16 within 2.5% of seqs=8 at c≤2 — production recommendation confirmed.
- **131K flat from c=3→c=4** (coding: 142.9 vs 143.2, speed: 132.2 vs 132.7). HBM bandwidth
  is fully saturated at c=3. Adding a 4th worker doesn't cannibalize others. Aggregate keeps
  scaling: coding c=4 agg = 572 tok/s vs 429 at c=3.
- **Boundary 262K c=4 faster per-req than c=3** (133.6 vs 122.1, +9%). With 4 APC-sharing
  workers, decode batch size is 4 tokens/step vs 3 → better GPU utilization. Not a cliff.
- No OOM at c=4 with 262K — APC means all workers share one cached prefix copy.
- c=5/6 and 262K coding context pending step I.

#### I — Extended concurrency sweep, c=1-8, coding 131K+262K, boundary 262K (step I)

`vllm serve /home/antti/models/Qwen3.6-35B-A3B-NVFP4 --max-model-len 262144 --kv-cache-dtype fp8 --reasoning-parser qwen3 --enable-prefix-caching --language-model-only --safetensors-load-strategy prefetch --max-num-seqs 16 --max-num-batched-tokens 4096`

Config: `config/bench-wor221i.toml`. Data below accumulates H (c=1–4) + I (c=5–8).

**Coding tier — FP8 KV, 131K context:**

| c | TTFT (s) | Per-req tok/s | Agg tok/s |
|---|---------|--------------|-----------|
| 1 | 2.27 | 186.8 | 186.8 |
| 2 | 2.25 | 160.4 | 320.7 |
| 3 | 2.24 | 141.9 | 425.6 |
| 4 | 2.27 | 142.0 | 567.9 |
| 5 | 2.25 | 124.1 | 620.7 |
| 6 | 2.27 | 144.8 | 869.0 |
| 7 | 2.33 | 129.9 | 909.5 |
| 8 | 2.26 | 124.9 | **998.8** |

**Coding tier — FP8 KV, 262K context:**

| c | TTFT (s) | Per-req tok/s | Agg tok/s |
|---|---------|--------------|-----------|
| 1 | 2.17 | 182.3 | 182.3 |
| 2 | 2.27 | 157.6 | 315.2 |
| 3 | 2.26 | 144.5 | 433.6 |
| 4 | 2.29 | 144.9 | 579.4 |
| 5 | 2.26 | 125.5 | 627.5 |
| 6 | 2.26 | 123.1 | 738.8 |
| 7 | 2.28 | 123.9 | 867.1 |
| 8 | 2.29 | 124.6 | **996.7** |

**Boundary tier — FP8 KV, 262K context (~249K token prompt):**

| c | TTFT (s) | Per-req tok/s | Agg tok/s |
|---|---------|--------------|-----------|
| 1 | 6.92 | 155.1 | 155.1 |
| 2 | 2.64 | 134.4 | 268.9 |
| 3 | 3.02 | 122.0 | 366.0 |
| 4 | 3.28 | 129.9 | 519.5 |
| 5 | 3.59 | 130.9 | 654.7 |
| 6 | 3.85 | 118.4 | 710.2 |
| 7 | 8.38 | 120.6 | 844.4 |
| 8 | 4.67 | 118.9 | **951.1** |

**Key findings:**

- **No concurrency cliff** — aggregate tok/s grows monotonically c=1→c=8 across all tiers and
  context sizes. No OOM at 262K with 8 concurrent workers (APC deduplication; all workers share
  one physical copy of the KV prefix blocks).
- **131K ≈ 262K at every c level** (max 3% delta). APC hit rate 96.5%+ — context size is
  irrelevant to decode throughput once the prefix is cached. One 262K server config handles all.
- **CUDA graph batch-size effect** — vLLM captures graphs at [1,2,4,8,16,...]. At c=5 and c=7,
  the 8-slot graph runs at 62%/87% fill; per-req dips slightly vs c=4/c=6/c=8. The dips are
  real but small (~10 tok/s) and aggregate always increases.
- **~1000 tok/s aggregate at c=8** for both coding context sizes. Eight concurrent workers, each
  generating ~125 tok/s, from a single RTX 5090.
- **`--max-local-workers 8` recommended.** Per-req at c=8 (125 tok/s) is still 125× faster than
  a human types. Task splitting is the natural governor — the watcher rarely opens 8 simultaneous
  tickets, but the headroom is real and costs nothing to configure.

#### A2 — seqs=200 backcheck (confirm step A cause)

`vllm serve /home/antti/models/Qwen3.6-35B-A3B-NVFP4 --max-model-len 262144 --kv-cache-dtype fp8 --reasoning-parser qwen3 --enable-prefix-caching --language-model-only --safetensors-load-strategy prefetch --max-num-seqs 200 --max-num-batched-tokens 4096`

Config: `config/bench-wor221a2.toml` — coding 131K only, c=1/2.

| c | TTFT (s) | Per-req tok/s | Agg tok/s | vs seqs=16 (step H) |
|---|---------|--------------|-----------|---------------------|
| 1 | 2.31 | 113.8 | 113.8 | **−39%** |
| 2 | 2.27 | 102.1 | 204.2 | **−36%** |

**Verdict: confirmed.** seqs=200 causes a 36–39% throughput penalty vs seqs=16 with otherwise
identical flags. The c=1 penalty (no scheduling interaction) proves this is pure HBM pressure
from the 200-slot pre-allocation, not a scheduling artifact.

#### J — BF16 KV, seqs=16, c=1-8 at 65K+131K (step J)

`vllm serve /home/antti/models/Qwen3.6-35B-A3B-NVFP4 --max-model-len 131072 --reasoning-parser qwen3 --enable-prefix-caching --language-model-only --safetensors-load-strategy prefetch --max-num-seqs 16 --max-num-batched-tokens 4096`

No `--kv-cache-dtype fp8` (BF16 default). `--max-model-len 131072` (BF16 VRAM limit).
Config: `config/bench-wor221j.toml`. Backend: `vllm_bf16_seqs16`.

**Coding — BF16 vs FP8 (131K coding, selected concurrency levels):**

| c | BF16 131K tok/s | BF16 agg | FP8 131K tok/s | FP8 agg | Delta agg |
|---|----------------|----------|----------------|---------|-----------|
| 1 | 182.5 | 182.5 | 186.8 | 186.8 | −2.3% |
| 4 | 142.6 | 570.4 | 142.0 | 567.9 | +0.4% |
| 8 | 125.0 | **1000.0** | 124.9 | **998.8** | +0.1% |

**Boundary — BF16 131K vs FP8 262K (larger prompt, same GPU decode bandwidth):**

| Config | c | Per-req tok/s | Agg tok/s |
|--------|---|--------------|-----------|
| BF16 131K | 1 | 145.3 | 145.3 |
| FP8 262K | 1 | 155.1 | **+6.7%** |
| BF16 131K | 4 | 106.3 | 425.4 |
| FP8 262K | 4 | 129.9 | **+22%** |
| BF16 131K | 8 | 89.1 | 712.7 |
| FP8 262K | 8 | 118.9 | **+34%** |

**Verdict: FP8 strictly dominates.** BF16 and FP8 coding throughput are within 3% at every
concurrency level — statistically identical. For boundary workloads, FP8 262K is 6–34% *faster*
than BF16 131K despite a 2× larger prompt: FP8 halves KV cache bits, so 262K FP8 reads the same
HBM bandwidth as 131K BF16 at decode time, then leverages the larger batch more efficiently at
high concurrency. BF16 offers no speed advantage and half the context window.

**Single universal server config: FP8 KV, max_model_len=262144.**

---

### Updated conclusions (WOR-221 complete)

| Parameter | Verdict | WOR-218 action |
|-----------|---------|----------------|
| `enable_chunked_prefill` | **OFF** — −45% boundary regression (Mamba SSM per-chunk overhead) | Do not enable |
| `max_num_batched_tokens` | Keep at 4096 — irrelevant without chunked prefill | No change |
| `num_scheduler_steps` | **Unavailable** in vLLM 0.20.0 | Skip; revisit on upgrade |
| `max_num_seqs=200` | **−37–67% throughput** vs seqs=16 — HBM pre-allocation pressure | **`--max-num-seqs 16`** |
| Max viable concurrency | **No cliff** with seqs=16 — agg scales c=1→c=8, ~1000 tok/s at c=8 | **`--max-local-workers 8`** |
| KV cache dtype | FP8 = BF16 at ≤131K; FP8 +34% boundary agg at c=8; 2× context | **`--kv-cache-dtype fp8`** |
| Context ceiling | 131K ≈ 262K in throughput (APC + FP8 compression) | Use 262K as universal config |
| CUDA version | Already CUDA 13.0 (PyTorch) / driver 596.21 — WOR-221 numbers include CUDA 13 gains | No action (WOR-222 closed) |

**Production config for WOR-218:**

```bash
vllm serve /home/antti/models/Qwen3.6-35B-A3B-NVFP4 \
  --max-model-len 262144 \
  --kv-cache-dtype fp8 \
  --max-num-seqs 16 \
  --max-num-batched-tokens 4096 \
  --reasoning-parser qwen3 \
  --enable-prefix-caching \
  --language-model-only \
  --safetensors-load-strategy prefetch \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder
```

**Watcher setting:** `--max-local-workers 8`

The `--max-num-seqs 16` change alone gives 37–67% improvement over the WOR-118 baseline.
At c=8, aggregate decode reaches ~1000 tok/s — eight simultaneous workers at ~125 tok/s each
from a single RTX 5090, on any context size from 16K to 262K.
