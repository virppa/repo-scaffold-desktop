# Model Profile: qwen3.6:35b-a3b

**Last updated:** 2026-04-27
**Measured on:** RTX 5090 32 GB, Ollama, OLLAMA_FLASH_ATTENTION=1
**Companion model benchmarked:** qwen3-coder:30b (same session)

---

## Architecture

Sparse Mixture of Experts (MoE). 35B total parameters, ~3B active per forward pass (hence "A3B"). The MoE router activates only a fraction of expert layers per token, giving the representational capacity of a 35B dense model at roughly the compute cost of a 3B model. This is the explanation for every performance number that looks surprising below.

Released 2026-04-16 by Alibaba Qwen team under Apache 2.0.
Native context window: 262,144 tokens. Extensible to >1M via YaRN scaling.
Multimodal: text + image (not video/audio — use Qwen3.5 Omni for that).

**Thinking Preservation** — the most important architectural differentiator for agent use. Standard reasoning models discard chain-of-thought traces after each turn — every new message starts with a blank reasoning slate. Qwen3.6 retains reasoning context across turns at the model level, not through brute-force context stuffing.

In practice: an agent loop doing "analyse this repo → write a test suite → fix the failing tests" maintains coherent context about earlier decisions without re-injecting the full reasoning history manually. The model remembers what it was thinking.

**When to enable:** iterative multi-step coding agents (watcher sessions). **When to disable:** one-shot single-turn tasks — the overhead isn't worth it.

**Trade-off:** retained reasoning traces consume context window faster. Effective usable context for multi-turn work is meaningfully less than the 262K nominal limit. Plan context budgets accordingly for long watcher sessions.

---

## Coding quality (external benchmarks)

| Benchmark | qwen3.6:35b-a3b | Notes |
|-----------|-----------------|-------|
| SWE-bench Verified | **73.4%** | Previous open-model SOTA at similar active-param budget was <60% |
| MCPMark (tool use) | **37.0%** | vs Gemma 4-31B at 18.1% — 2× advantage on agentic tool calls |

SWE-bench is the most credible real-world software engineering benchmark. 73.4% on a model that runs locally on an RTX 5090 is the core reason this model is in the benchmark matrix.

---

## Throughput: empirical results (post Flash Attention)

### Speed tier — tok/s by context size

| Context | qwen3.6:35b-a3b | qwen3-coder:30b | Winner |
|---------|-----------------|-----------------|--------|
| 16K | 161 | 169 | 30b +5% |
| 32K | 161 | 176 | 30b +8% |
| 65K | 159 | 160 | tie |
| 98K | 161 | 183 | 30b +12% |
| **131K** | **158** | **80** | **35b-a3b +98%** |
| 196K | 76 | 59 | 35b-a3b +22% |
| 262K | 49 | 47 | tie |

**The crossover is at 131K.** Below that, 30b-coder is 5–12% faster. At and above 131K, 35b-a3b dominates — the MoE architecture maintains full speed where the dense model halves.

### Flash Attention impact on 35b-a3b

FA completely eliminated the 98K→131K throughput cliff:

| Context | Pre-FA | Post-FA | Change |
|---------|--------|---------|--------|
| 16K–98K | 165–167 | 159–161 | −2–4% (noise) |
| **131K** | **77** | **158** | **+105%** |
| 196K | 61 | 76 | +24% |
| 262K | 48 | 49 | +3% |

Without FA: hard cliff at 131K (throughput halved). With FA: flat ~160 tok/s from 16K through 131K. **Set `OLLAMA_FLASH_ATTENTION=1` before starting Ollama — this is not optional.**

### TTFT stability

Post-FA TTFT is steady at 2.2–2.3s across all context sizes. Pre-FA it was noisy (1.2–5.4s). FA also fixed the attention allocation jitter.

---

## VRAM profile (post-FA, speed tier)

| Context | Peak VRAM | Headroom |
|---------|-----------|----------|
| 16K | 26.7 GB | 5.2 GB |
| 32K | 27.1 GB | 4.7 GB |
| 65K | 28.1 GB | 3.8 GB |
| 98K | 29.1 GB | 2.8 GB |
| 131K | 30.1 GB | 1.8 GB |
| 196K | 31.1 GB | **0.7 GB** ⚠ |
| 262K | 30.7 GB | 1.2 GB |

Model base weight: ~26.7 GB at 16K context.
KV cache grows slowly (+4.4 GB from 16K to 196K) — MoE with fewer KV heads than a comparable dense model.
196K is the tightest point (0.7 GB headroom). 262K recovers slightly — likely measurement variance at the boundary.
320K extrapolation: ~30.3 GB, ~1.6 GB headroom. Confirmed viable in boundary tier pre-FA run (30.81 GB peak).

Thermal: 43–46°C at load, GPU util 15–30%. Well within RTX 5090 operating range (throttle threshold 83°C).

---

## Comparison: qwen3.6:35b-a3b vs qwen3-coder:30b

| Dimension | 35b-a3b | 30b-coder |
|-----------|---------|-----------|
| Architecture | MoE (35B total / 3B active) | Dense (30B) |
| Weights size (GGUF) | ~23 GB | ~19 GB |
| VRAM at 16K | 26.7 GB | 21.0 GB |
| VRAM headroom at 16K | 5.2 GB | **10.8 GB** |
| Throughput ≤98K | 159–161 tok/s | 169–183 tok/s |
| Throughput at 131K | **158 tok/s** | 80 tok/s |
| Throughput at 196K | **76 tok/s** | 59 tok/s |
| KV cache growth (16K→196K) | +4.4 GB | +9.7 GB (then plateaus) |
| SWE-bench | 73.4% | not published for this variant |
| Coding specialisation | General | Yes (fine-tuned for code) |
| GPU util at load | 15–30% | 2–10% |
| Flash Attention benefit | Eliminates 131K cliff | Improves 32K–98K; creates new 131K cliff |

**30b-coder is nearly memory-bandwidth-bound at inference** (2–10% GPU util). Its KV cache grows fast until ~131K then plateaus — likely GQA with very few KV heads. This gives it 10.8 GB VRAM headroom at short contexts, making co-loading a floor model viable.

---

## Recommended routing thresholds

```
required_ctx > 98K   → qwen3.6:35b-a3b   (only model that holds speed at 131K+)
required_ctx ≤ 98K   → qwen3-coder:30b    (5–12% faster, coding-specialist)
simple/mechanical    → qwen3.5:9b         (fits alongside 30b-coder in VRAM at short ctx)
```

The 131K boundary is data-derived, not arbitrary. Below it 30b-coder wins on speed; at it and above 35b-a3b wins decisively.

---

## Multi-worker and vLLM notes

### VRAM headroom enables co-loading

At 16K–65K context (typical watcher task range):
- 30b-coder uses 21–26 GB → 6–11 GB free
- qwen3.5:9b-q4_K_M (~6.6 GB) fits alongside 30b-coder at these sizes
- Both models resident in VRAM simultaneously: ~28 GB total at 16K

35b-a3b leaves only 4–5 GB free at 16K — not enough for a second model without eviction.

### vLLM differences to measure

Ollama serialises requests; vLLM uses continuous batching. For multi-worker watcher:
- vLLM serves concurrent workers from one model instance
- No model load/unload overhead between requests
- Prefix caching (APC) on `prefill_shared` tier will reduce TTFT significantly for shared-context requests — Ollama has no equivalent

When benchmarking vLLM, re-enable concurrency levels [1, 2, 4] in bench.toml for the vLLM backend — concurrency=1 only makes sense for Ollama's serialised model. The concurrency dimension is where the architectural difference shows up.

### vLLM VRAM note

vLLM with HF FP16 weights for a 35B MoE model is large — quantisation likely required to fit 32 GB. Use `--quantization awq` if needed. The production target is `--max-model-len 262144` (native context limit); bench.toml currently uses 131072 as a conservative starting point — update once VRAM headroom is confirmed with the quantised weights. The benchmark's `prefill_shared` tier with APC enabled is specifically designed to measure the vLLM prefix-caching win over Ollama.

---

## Deployment

```bash
# Ollama (current)
ollama run qwen3.6:35b-a3b   # or qwen3.6 for latest tag

# Required env var for Ollama — not optional, eliminates the 131K cliff
OLLAMA_FLASH_ATTENTION=1   # set as persistent user env var, restart Ollama

# vLLM — single GPU (planned)
# --reasoning-parser and tool-call flags required for agentic tool use
vllm serve Qwen/Qwen3.6-35B-A3B \
  --port 8000 \
  --tensor-parallel-size 1 \
  --max-model-len 262144 \
  --trust-remote-code \
  --reasoning-parser qwen3 \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \
  --gpu-memory-utilization 0.90 \
  --enable-prefix-caching
  # Add --quantization awq if FP16 weights exceed VRAM budget

# vLLM — dual GPU (if available)
# --tensor-parallel-size 2 splits the model across both GPUs
vllm serve Qwen/Qwen3.6-35B-A3B \
  --tensor-parallel-size 2 \
  --max-model-len 262144 \
  --trust-remote-code \
  --reasoning-parser qwen3 \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder
```

**Critical vLLM flags for agentic use:**
- `--reasoning-parser qwen3` — parses the model's thinking output correctly
- `--enable-auto-tool-choice` + `--tool-call-parser qwen3_coder` — required for function/tool calls; omitting these breaks all MCP and tool-use integrations
- Without these flags the model runs but tool calls silently fail

HuggingFace: `Qwen/Qwen3.6-35B-A3B`
Approximate API cost if cloud fallback needed: ~$0.29/M input, $1.65/M output (Alibaba Cloud Bailian, projected).

---

## Open questions for overnight benchmark run

- [ ] `quality_task_success` vs qwen3-coder:30b — does general-purpose MoE match coding-specialist on task pass rate?
- [ ] Prefill tier numbers at 98K–131K post-FA (fixture fills 75% of KV buffer at these sizes — real long-context prefill data)
- [ ] Boundary tier at 320K post-FA (pre-FA confirmed viable; FA may improve throughput there too)
- [ ] vLLM APC benefit on `prefill_shared` — expected to halve TTFT vs Ollama baseline
- [ ] GGUF coding fine-tune of 35b-a3b: watch HuggingFace for community quantisations of `qwen3.6:35b-a3b-coding` — MLX-only variants exist but no GGUF as of 2026-04-27
