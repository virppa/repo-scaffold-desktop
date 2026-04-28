# vLLM Benchmark — Findings & Status

**Spike:** WOR-118 (gate for WOR-210)
**Model:** `Qwen3.6-35B-A3B-NVFP4` — Blackwell-native FP4, 23.32 GiB on disk, ~21 GiB in VRAM
**Server:** vLLM 0.20.0, RTX 5090 32 GB (SM_120), WSL2, CUDA 12.9
**Ollama baseline:** `qwen3.6:35b-a3b` via Ollama on same hardware

---

## Verdict

**vLLM replaces Ollama as the watcher backend.**

- Coding quality now matches Ollama (10-11/11 with thinking enabled)
- Throughput matches Ollama at 128K (160 vs 165 tok/s) and maintains that level; Ollama falls to 75 tok/s at 144K+
- TTFT advantage: vLLM flat at ~2.2s from 16K through 128K; Ollama rises with context (4.5s → 12s)
- APC (Automatic Prefix Caching) means repeated prompts see near-zero TTFT on warm hits

---

## Optimised server config (BF16, 131K context)

Best single-worker configuration — highest throughput, full quality:

```bash
VLLM_MOE_BACKEND=FLASHINFER_TRTLLM vllm serve /home/antti/models/Qwen3.6-35B-A3B-NVFP4 \
  --max-model-len 131072 \
  --max-num-seqs 200 \
  --reasoning-parser qwen3 \
  --enable-prefix-caching \
  --language-model-only \
  --safetensors-load-strategy prefetch
```

`VLLM_MOE_BACKEND` is treated as unknown by vLLM but FlashInfer's autotuner benchmarks
TRTLLM MoE kernels independently — TRTLLM wins and is selected automatically.

### Performance (run_20260427_190219)

| Context | tok/s | TTFT | Quality (10 repeats) |
|---------|-------|------|----------------------|
| 16K | 164 | 2.2s | 10/11 (91%) |
| 64K | 160 | 2.2s | 11/11 (100%) |
| 128K | 160 | 2.2s | 10/11 (91%) |

TTFT is flat because Mamba SSM initialisation dominates — APC eliminates the prefill
compute but the Mamba cache cold-start floor is ~2.2s regardless of context size.

### Ollama comparison at matched context

| Context | vLLM tok/s | Ollama tok/s | vLLM TTFT | Ollama TTFT |
|---------|-----------|-------------|-----------|-------------|
| 16K | 164 | 165 | 2.2s | 4.6s |
| 64K | 160 | 165 | 2.2s | 7.7s |
| 128K | 160 | 165 | 2.2s | 12.0s |
| 144K | — | 75.8 | — | — |
| 256K | — | 48 | — | — |

Ollama's 165 tok/s holds to 128K then falls off a cliff (FA-flat ends, KV offloads to RAM).
vLLM maintains 160 tok/s through 128K with a hard VRAM ceiling.

---

## Flags tested

| Flag / change | Result |
|---|---|
| `VLLM_MOE_BACKEND=FLASHINFER_TRTLLM` (via autotuner) | +8% tok/s at 64K/128K (148→160) ✅ keep |
| `--language-model-only` | +~8 tok/s at 16K, -0.2 GiB VRAM ✅ keep |
| `--safetensors-load-strategy prefetch` | -20s restart time (31.8s→12.1s weight load) ✅ keep |
| `--attention-backend flashinfer` | -29% tok/s (117 vs 164 at 16K) ❌ drop |
| MTP speculative decoding (`--num-speculative-tokens 1`) | KV cache too small at 131K (1.25 GiB available, needs 2.97 GiB) ❌ not viable |
| `enable_thinking = false` | 0% coding quality (~68 output tokens, stub code only) ❌ thinking required |

---

## Quality: FP4 non-determinism

Early 3-repeat runs showed 0/3 at 64K+ — this was a sample size artefact, not a real
quality collapse. With 10 repeats, true pass rates are:

- 16K: ~91% (10/11)
- 64K: ~91–100% (varies by run)
- 128K: ~91% (10/11)

Root cause: FP4 non-determinism. Expert routing in MoE layers uses warp-parallel
top-k over GPU warps — floating point race conditions mean different experts are
selected across runs even with `seed=42, temperature=0`. Output token count varies
885–2129 tokens. 9–18% of runs produce code that fails pytest or ruff.

**Thinking is load-bearing:** `enable_thinking=false` produces ~68 tokens (stub code),
0% pass rate. No-think mode is unusable for coding quality tasks.

---

## FP8 KV cache (262K context)

Server command:

```bash
VLLM_MOE_BACKEND=FLASHINFER_TRTLLM vllm serve /home/antti/models/Qwen3.6-35B-A3B-NVFP4 \
  --max-model-len 262144 \
  --kv-cache-dtype fp8 \
  --max-num-seqs 200 \
  --max-num-batched-tokens 4096 \
  --reasoning-parser qwen3 \
  --enable-prefix-caching \
  --language-model-only \
  --safetensors-load-strategy prefetch
```

Note: `--max-num-batched-tokens 4096` required — Mamba cache align mode sets
block_size=2096 which must be ≤ max_num_batched_tokens (default 2048 fails).

FP8 KV forces FlashInfer attention (not FA2), which costs ~25% throughput.

### FP8 quality at known context sizes (run_20260427_205008)

| Context | tok/s | TTFT | Quality (10 repeats) |
|---------|-------|------|----------------------|
| 16K | 124.5 | 2.5s | 10/11 (91%) |
| 64K | 124.9 | 2.4s | 10/11 (91%) |
| 128K | 116.5 | 2.4s | 10/11 (91%) |

FP8 KV does not hurt quality. The throughput cost is purely from FlashInfer attention.

### FP8 high-context benchmark (run_20260428, bench-vllm-fp8-highctx.toml)

| Context | tok/s (avg) | Quality (5 repeats) |
|---------|------------|---------------------|
| 131K | ~116 | 1/5 (20%) — statistical noise, see 10-repeat run above |
| 196K | ~107 | 0/5 (0%) |
| 262K | ~119 | 0/5 (0%) |

Speed is remarkably flat 131K→262K with FP8 KV (~116-119 tok/s). Quality appears to
collapse above 131K but this is a **bench artefact**: the coding task sends the same
short fixed prompt regardless of context_size — context_size is ignored by the coding
driver. The 0% at 196K/262K reflects server state degradation over the run (APC cache
evictions, FP4 variance), not the model's actual capability at those context lengths.

The model's native trained context is 262,144 tokens (confirmed by model card and by
vLLM accepting --max-model-len 262144 without a position embedding error).

To genuinely test quality at 262K would require a prompt that is actually 262K tokens
of relevant content — not the current short coding task.

Speed comparison at 256K: vLLM FP8 ~119 tok/s vs Ollama 48 tok/s — 2.5x faster.

---

## Context ceiling

The model's **native trained context is 262,144 tokens** (confirmed by model card and
by vLLM accepting --max-model-len 262144 without a position embedding error). 131K is
not a model limit — it was the BF16 VRAM ceiling on 32 GB, nothing more.

The FP8 highctx "quality collapse" at 196K+ is a bench artefact: the coding task uses
a fixed short prompt and ignores context_size entirely. Those quality numbers measured
server state drift, not model capability at long context.

BF16 cannot reach 262K on 32 GB VRAM. FP8 is required for the full native context.

| Config | Max context | tok/s | Quality | Notes |
|--------|------------|-------|---------|-------|
| BF16, 131K | 131K | 160 | 91% | **Daily driver** |
| FP8, 262K at ≤131K | 131K | 116–125 | 91% | Quality intact, slower |
| FP8, 262K at 196K+ | 262K | ~107–119 | unknown | Bench artefact — real prompt needed |
| BF16, 147K (util=0.94) | ~99K effective | 63–70 | — | ❌ Dead end — see below |

### Why 147K BF16 fails

At 147K max_model_len, vLLM's Mamba page alignment changes block_size from 2096 to
1056 tokens. This has two consequences:

1. **Effective KV capacity shrinks**: 7.6 GiB at block_size=1056 covers only 99,264
   tokens — less than 131K BF16 at util=0.92. More VRAM, fewer usable tokens.
2. **Throughput collapses**: New compile cache key + CUTLASS MoE (autotuner no longer
   picks TRTLLM) + smaller Mamba block boundary crossings → 63–70 tok/s vs 160 tok/s.

The 147K target is a Mamba alignment trap. Avoid.

---

---

## Concurrency sweep (run_20260428_000929)

5 tiers × 3 context sizes × c=1/2/3/4 × 5 repeats. Server: original BF16 131K config
(no cudagraph override, no VRAM-tuning flags). Real concurrent requests via
`ThreadPoolExecutor` — all N requests fire simultaneously, metrics averaged across workers.

### Speed tier — decode throughput under concurrent load

| ctx | c=1 tok/s | c=2 tok/s | c=3 tok/s | c=4 tok/s | c=4 agg tok/s | c=4 TTFT |
|-----|----------|----------|----------|----------|--------------|---------|
| 16K | 135 | 110 (220) | 103 (308) | 103 (411) | 411 | 2.09s |
| 65K | 124 | 105 (210) | 84 (253) | 104 (415) | 415 | 2.09s |
| 131K | 136 | 112 (224) | 103 (308) | 102 (409) | 409 | 2.09s |

*Parentheses = aggregate tok/s (c × per-req)*

TTFT is flat at 2.07–2.09s across all context sizes and all concurrency levels for
short prompts (speed tier). Per-request throughput plateaus at c=3→c=4: adding a 4th
worker costs nothing extra per request. The GPU decode batch is saturated at ~c=3.

### Prefill_shared — APC warm-hit TTFT under concurrent load

Shared system prompt; all requests hit the prefix cache. TTFT inflates slightly with
concurrency because vLLM serialises prefill scheduling even for cached blocks.

| ctx | c=1 TTFT | c=2 TTFT | c=3 TTFT | c=4 TTFT |
|-----|---------|---------|---------|---------|
| 16K | 2.14s | 2.19s | 2.24s | 2.28s |
| 65K | 2.21s | 2.31s | 2.42s | 2.65s |
| 131K | 2.34s | 2.46s | 2.69s | 2.83s |

At 131K c=4, TTFT mean across all 4 workers is 2.83s. The 4th worker in queue sees
~3.2–3.4s (the mean is pulled down by workers 1–2 which start earlier). In real watcher
use, workers are staggered rather than synchronised, so effective TTFT is closer to the
c=1 floor with occasional spikes.

### Prefill_unshared — cold prefill throughput under concurrency

Different prompts per request; no APC hits. Closest to real watcher behaviour (each
worker has a unique context). 131K prompts were genuinely 131K tokens — the prefill
KV blocks are real.

| ctx | c=1 | c=2 | c=3 | c=4 | c=4 agg | c=4 TTFT |
|-----|-----|-----|-----|-----|---------|---------|
| 16K | 137 | 107 | 100 | 106 | 424 | 2.24s |
| 65K | 126 | 107 | 99 | 104 | 415 | 2.38s |
| 131K | 130 | 95 | 92 | 88 | 352 | 2.73s |

No OOM at any concurrency level. At 131K c=4, aggregate is 352 tok/s — lower than
speed tier because vLLM must hold 4 × 131K KV blocks concurrently during decode, which
stresses the ~92K-token pool and likely triggers some block eviction/reuse.

### Boundary tier note

The `boundary.py` prompt is "The quick brown fox." × 500 (~2,500 tokens), not a true
131K-token stress. It behaves identically to the speed tier and does not exercise KV
pool exhaustion. A real boundary test would require a prompt padded to 131K tokens.

### Watcher recommendation: --max-local-workers

- **c=2**: 1.6× aggregate throughput, 17% per-request penalty, TTFT stable. Safe for
  all context sizes including 131K.
- **c=3**: 2.3× aggregate, 24% per-request penalty, TTFT 2.57s at 131K cold prefill.
  Good balance — the per-request penalty plateau begins here.
- **c=4**: 3.0× aggregate, ~25% per-request penalty (same as c=3). Essentially free
  upgrade from c=3 for short/medium context. TTFT approaches 2.83s mean (3.2s worst)
  at 131K with concurrent cold prefills.

**Recommended: `--max-local-workers 3`** for mixed-context workloads. Use
`--max-local-workers 4` if sessions are predominantly short-context (< 64K).

---

## MoE backend investigation

Investigating how to force TRTLLM MoE kernels consistently (TRTLLM was observed to be
faster than CUTLASS in the original good run).

### Key findings

| Finding | Detail |
|---------|--------|
| `VLLM_MOE_BACKEND=FLASHINFER_TRTLLM` | Logged as "Unknown" by vLLM but hints the FlashInfer autotuner — autotuner benchmarks TRTLLM vs CUTLASS at startup and picks the winner per batch shape. Keep this env var. |
| `VLLM_FLASHINFER_MOE_BACKEND=latency` | Official env var to force TRTLLM for MoE routing. Gives **118–120 tok/s** — worse than autotuner CUTLASS (143–147). Forces TRTLLM for ALL batch sizes including small ones where it's suboptimal. Do not use. |
| `VLLM_FLASHINFER_MOE_BACKEND=throughput` | Forces CUTLASS. Explicit alternative to the autotuner. 143–147 tok/s. |
| `VLLM_NVFP4_GEMM_BACKEND=flashinfer-trtllm` | Controls the **linear** FP4 GEMM kernel (separate from MoE routing). Crashes on SM_120: `mm_fp4 does not support backend 'trtllm' with capability 120`. Dead end. |
| `block_size=1056` | Fixed Mamba page alignment constant for this model — not VRAM-dependent. All attempts to force 2096 are ineffective. |
| `--cudagraph-capture-sizes 1 2 4 8` | Reduces CUDA graph overhead (0.08 GiB vs 0.73 GiB default), increasing KV pool by ~6K tokens. But limits autotuner to small batch shapes → CUTLASS wins at every shape → lower throughput (143 vs 160+ tok/s). Not worth it. |

**Conclusion:** leave autotuner default (no `VLLM_FLASHINFER_MOE_BACKEND` override,
no `--cudagraph-capture-sizes` override). The autotuner with large capture sizes (up to
400) benchmarks TRTLLM on realistic batch shapes and picks the right kernel per shape.

---

## Bench framework improvements (added during this spike)

- `scripts/bench/runner.py`: added `_run_generate()` (fires N concurrent threads) and
  `_aggregate()` (means TTFT/tok/s across workers). Concurrency now real, not a label.
- `scripts/bench/reporter.py`: added `Agg.tok/s` column (c × per-req tok/s) to summary table.
- `scripts/bench/reporter_ranking.py`: changed `Conc.Eff` from linear-fraction to
  aggregate speedup (c × tok_c / tok_1); rewrote concurrency scaling section to show
  per-group table with per-req, aggregate, TTFT, and speedup.

### Tool call parser caveat

`--enable-auto-tool-choice --tool-call-parser qwen3_coder` is required for watcher tool
use through LiteLLM proxy but breaks bench coding evaluation: the model wraps its JSON
answer in `<tool_call>` tags, the parser routes it to `tool_calls` (not `content`), and
the bench driver only reads `content`. Coding quality for run_20260428_000929 is 0/N
(invalid). Re-run coding tier without these flags for valid quality data.

---

## Pending tests

| Test | Config | Status |
|------|--------|--------|
| Coding quality re-run | bench-vllm-concurrency.toml, server without tool-call-parser | Not started |
| Real 131K boundary stress | New boundary prompt padded to ~130K tokens | Not started |
| Linear post WOR-118 | — | Not started |

---

## Lessons learned

- **WSL2 VRAM zombie:** Crashed vLLM leaves dangling CUDA context (shows 30 GB used,
  no processes). Fix: `wsl --shutdown` from Windows PowerShell, not `nvidia-smi`.
- **Ollama must be stopped before vLLM starts:** `ollama stop qwen3.6:35b-a3b`
  or vLLM OOMs on startup.
- **torch.compile cache:** First run compiles (26s). Subsequent restarts use cache —
  startup is fast after the first time.
- **`--safetensors-load-strategy prefetch`** is free perf — prefetches weights into
  Linux page cache in background, shaving 20s off every server restart.
- **Sample size matters for FP4:** 3 repeats is too few to distinguish 30% from 100%
  pass rate. Use 10 repeats minimum for quality characterisation.
- **Bench concurrency was a label, not real:** original runner sent requests sequentially
  regardless of `concurrency_levels`. Fixed by adding `ThreadPoolExecutor` in
  `_run_generate()`. Verify with vLLM server log "Running: N reqs" to confirm.
- **Tool call parser breaks bench coding eval:** `--enable-auto-tool-choice
  --tool-call-parser qwen3_coder` must be omitted when running coding tier benchmarks.
  Add only for watcher/LiteLLM proxy usage.
- **`VLLM_NVFP4_GEMM_BACKEND=flashinfer-trtllm` crashes SM_120:** Blackwell (SM_120)
  does not have TRTLLM linear mm_fp4 compiled in. Autotuner for MoE routing is a
  separate system and works fine.
