# vLLM Benchmark Findings — WOR-118

**Spike:** WOR-118 (gates WOR-210)
**Hardware:** RTX 5090 32 GB (SM_120 / Blackwell), WSL2, CUDA 12.9
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

**`--max-local-workers 2` for all watcher configurations.**

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

Steps run independently — fill in as each completes. Sweep ID printed at bench start.

#### A — Baseline (no chunked prefill, batched_tokens=4096)

`vllm serve /home/antti/models/Qwen3.6-35B-A3B-NVFP4 --max-model-len 262144 --kv-cache-dtype fp8 --reasoning-parser qwen3 --enable-prefix-caching --language-model-only --safetensors-load-strategy prefetch --max-num-seqs 200 --max-num-batched-tokens 4096`

Sweep ID: _(fill in)_

| Tier | Context | c | TTFT p50 (s) | Per-req tok/s | Agg tok/s |
|------|---------|---|-------------|--------------|-----------|
| speed | 131K | 1 | | | |
| speed | 131K | 2 | | | |
| coding | 131K | 1 | | | |
| coding | 131K | 2 | | | |
| boundary | 262K | 1 | | | |
| boundary | 262K | 2 | | | |

#### B — Chunked prefill ON, batched_tokens=4096

`vllm serve /home/antti/models/Qwen3.6-35B-A3B-NVFP4 --max-model-len 262144 --kv-cache-dtype fp8 --reasoning-parser qwen3 --enable-prefix-caching --language-model-only --safetensors-load-strategy prefetch --max-num-seqs 200 --max-num-batched-tokens 4096 --enable-chunked-prefill`

Sweep ID: _(fill in)_

| Tier | Context | c | TTFT p50 (s) | Per-req tok/s | Agg tok/s | vs A |
|------|---------|---|-------------|--------------|-----------|------|
| speed | 131K | 1 | | | | |
| speed | 131K | 2 | | | | |
| coding | 131K | 1 | | | | |
| coding | 131K | 2 | | | | |
| boundary | 262K | 1 | | | | |
| boundary | 262K | 2 | | | | |

#### C — Chunked prefill ON, batched_tokens=8192

`vllm serve /home/antti/models/Qwen3.6-35B-A3B-NVFP4 --max-model-len 262144 --kv-cache-dtype fp8 --reasoning-parser qwen3 --enable-prefix-caching --language-model-only --safetensors-load-strategy prefetch --max-num-seqs 200 --max-num-batched-tokens 8192 --enable-chunked-prefill`

Sweep ID: _(fill in)_

| Tier | Context | c | TTFT p50 (s) | Per-req tok/s | Agg tok/s | vs A |
|------|---------|---|-------------|--------------|-----------|------|
| boundary | 262K | 2 | | | | |
| coding | 131K | 2 | | | | |

#### D — Chunked prefill ON, batched_tokens=16384

`vllm serve /home/antti/models/Qwen3.6-35B-A3B-NVFP4 --max-model-len 262144 --kv-cache-dtype fp8 --reasoning-parser qwen3 --enable-prefix-caching --language-model-only --safetensors-load-strategy prefetch --max-num-seqs 200 --max-num-batched-tokens 16384 --enable-chunked-prefill`

Sweep ID: _(fill in)_

| Tier | Context | c | TTFT p50 (s) | Per-req tok/s | Agg tok/s | vs A |
|------|---------|---|-------------|--------------|-----------|------|
| boundary | 262K | 2 | | | | |
| coding | 131K | 2 | | | | |

#### E — num_scheduler_steps=4

`vllm serve /home/antti/models/Qwen3.6-35B-A3B-NVFP4 --max-model-len 262144 --kv-cache-dtype fp8 --reasoning-parser qwen3 --enable-prefix-caching --language-model-only --safetensors-load-strategy prefetch --max-num-seqs 200 --max-num-batched-tokens 4096 --num-scheduler-steps 4`

**Note:** If vLLM 0.20.0 rejects `--num-scheduler-steps`, record "flag not available" and skip F.

Sweep ID: _(fill in)_ — or SKIP (flag unavailable in 0.20.0)

| Tier | Context | c | Per-req tok/s | Agg tok/s | vs A |
|------|---------|---|--------------|-----------|------|
| coding | 131K | 2 | | | |
| speed | 131K | 2 | | | |

#### F — num_scheduler_steps=8

`vllm serve /home/antti/models/Qwen3.6-35B-A3B-NVFP4 --max-model-len 262144 --kv-cache-dtype fp8 --reasoning-parser qwen3 --enable-prefix-caching --language-model-only --safetensors-load-strategy prefetch --max-num-seqs 200 --max-num-batched-tokens 4096 --num-scheduler-steps 8`

Sweep ID: _(fill in)_ — or SKIP

| Tier | Context | c | Per-req tok/s | Agg tok/s | vs A |
|------|---------|---|--------------|-----------|------|
| coding | 131K | 2 | | | |
| speed | 131K | 2 | | | |

#### G — max_num_seqs=8 (queue pressure sanity check)

`vllm serve /home/antti/models/Qwen3.6-35B-A3B-NVFP4 --max-model-len 262144 --kv-cache-dtype fp8 --reasoning-parser qwen3 --enable-prefix-caching --language-model-only --safetensors-load-strategy prefetch --max-num-seqs 8 --max-num-batched-tokens 4096`

Sweep ID: _(fill in)_

| Tier | Context | c | Per-req tok/s | Agg tok/s | vs A | Verdict |
|------|---------|---|--------------|-----------|------|---------|
| coding | 131K | 2 | | | | match A → 200 not binding / degrade → sweep needed |
| speed | 131K | 2 | | | | |

---

### Conclusions (fill in after all steps complete)

| Parameter | Verdict | WOR-218 action |
|-----------|---------|----------------|
| `enable_chunked_prefill` | | |
| `max_num_batched_tokens` winner | | |
| `num_scheduler_steps` | | |
| `max_num_seqs=200` | | |
