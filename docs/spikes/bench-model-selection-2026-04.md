# Benchmark Model Selection and Speed-Tier Findings — April 2026

**Hardware:** RTX 5090 — 32 GB GDDR7
**Date:** 2026-04-26
**Benchmark branch:** wor-209-epic-benchmark-harness-complete-ollama-data-foundation

---

## Context

The WOR-209 epic shipped a full benchmark harness (5 tiers, adaptive OOM skip, ranking, APC, regression detection, thermal throttle). This doc records the model selection decisions made when populating the initial registry and the findings from the first speed-tier run across all 9 candidates.

---

## Model registry decisions

### What was evaluated

Starting from the 5 original spike models (WOR-76), the registry was expanded by evaluating:

- **Qwen3 base family (8b / 14b / 32b)** — eliminated: Ollama reports 40K context window. For watcher coding sessions, Claude Code system prompt + CLAUDE.md + tool outputs alone approach 20–40K tokens before any code is written. 40K is not viable.
- **Qwen3.5 family** — 256K context across all sizes, multimodal (text + image). `qwen3.5:9b` added as floor model at Q4_K_M and Q8_0.
- **Gemma 3 27B** — 128K context, cross-vendor comparison at the 27B size tier.
- **Devstral 24B** — 128K context, Mistral's coding-agent specialist designed for local deployment.
- **DeepSeek-R1 32B** — 128K context, reasoning distill. Eliminated after speed-tier results showed 8 tok/s at 64K context (see below). Better accessed via DeepSeek API.
- **Codestral 22B** — 32K context. Eliminated.
- **Phi-4 14B** — 16K context. Eliminated.

### Context window requirement

Minimum viable context for watcher coding tasks: **≥64K** (standard tier). All retained models have ≥128K native context.

| Eliminated model | Stated context | Reason |
|---|---|---|
| qwen3:8b / 14b / 32b | 40K | Insufficient for watcher sessions |
| codestral:22b | 32K | Insufficient |
| phi4:14b | 16K | Insufficient |
| deepseek-r1:32b | 128K | Context cliff: 8 tok/s at 64K (VRAM pressure) |

### Important correction: qwen3-coder:30b is MoE, not Dense

The WOR-76 spike doc described `qwen3-coder:30b` as "Dense 30B". It is in fact **MoE: 30B total / 3.3B active parameters**. This is consistent with its VRAM behaviour (23 GB peak at speed tier) and its flat throughput across context sizes. The bench.toml comment has been corrected.

---

## Final model registry (7 models)

| Model | Size | Context | Type | Role |
|---|---|---|---|---|
| qwen3.5:9b-q4_K_M | 6.6 GB | 256K | Dense | Floor — fast cheap worker |
| qwen3.5:9b-q8_0 | 11 GB | 256K | Dense | Floor — quality variant |
| devstral:24b | 14 GB | 128K | MoE | Coding-agent specialist |
| qwen3.6:27b | 17 GB | 256K | Dense | Mid-range candidate |
| gemma3:27b | 17 GB | 128K | Dense | Cross-vendor comparison |
| qwen3-coder:30b | 19 GB | 256K | MoE | Current production baseline |
| qwen3.6:35b-a3b | 23 GB | 256K | MoE | Strong candidate; tightest VRAM |
| qwen2.5-coder:32b Q4 | 19 GB | 128K | Dense | **Reference only** — regression anchor |

---

## Speed-tier results

**Setup:** short prompt (~few hundred tokens), ~256 tokens output, 4 context sizes × 2 concurrency levels × 3 repeats + warmup = 252 total runs. All 216 real runs returned `outcome=ok`.

### Throughput (tok/s, concurrency=1, averaged across context sizes)

| Model | Avg tok/s | CV | Notes |
|---|---|---|---|
| qwen3.5:9b-q4_K_M | 178 | 3.9% | Stable across all contexts |
| qwen3.6:35b-a3b | 163 | 3.5% | MoE: faster than dense 27B despite larger total params |
| qwen3-coder:30b | 136 | 18.4% | MoE; higher CV from context-size variance |
| qwen3.5:9b-q8_0 | 133 | 2.0% | Most stable in the set |
| devstral:24b | 92 | 25.2% | Drops at 64K (see below) |
| gemma3:27b | 79 | 5.1% | Flat across contexts |
| qwen3.6:27b | 67 | 3.4% | Stable but slower than MoE 35B |
| qwen2.5-coder:32b | 49 | 51.8% | Reference only; severe context cliff |
| ~~deepseek-r1:32b~~ | ~~46~~ | ~~65.4%~~ | Dropped; 8 tok/s at 64K |

### Throughput by context size (concurrency=1)

| Model | 4K | 16K | 32K | 64K |
|---|---|---|---|---|
| qwen3.5:9b-q4_K_M | 184 | 175 | 181 | 179 |
| qwen3.5:9b-q8_0 | 135 | 129 | 134 | 132 |
| qwen3.6:35b-a3b | 159 | 165 | 166 | 163 |
| qwen3-coder:30b | 115 | 149 | 140 | 134 |
| devstral:24b | 102 | 101 | 102 | **53** |
| gemma3:27b | 79 | 81 | 78 | 81 |
| qwen3.6:27b | 63 | 67 | 67 | 67 |
| qwen2.5-coder:32b | 70 | 76 | **24** | 24 |
| ~~deepseek-r1:32b~~ | 75 | 75 | **26** | **8** |

Models with bold values are hitting VRAM pressure: the GPU must juggle model weights plus a full KV cache allocation at that context size.

### VRAM headroom at speed tier

| Model | Peak VRAM | Headroom | Risk at 64K+ |
|---|---|---|---|
| qwen3.6:35b-a3b | 27.9 GB | **3.9 GB** | High — watch boundary tier |
| qwen3.6:27b | 26.2 GB | 5.6 GB | Moderate |
| qwen2.5-coder:32b | 26.1 GB | 5.7 GB | Moderate (but already slow) |
| qwen3-coder:30b | 23.0 GB | 8.8 GB | Low |
| gemma3:27b | 22.4 GB | 9.4 GB | Low |
| devstral:24b | 20.9 GB | 10.9 GB | Low |
| qwen3.5:9b-q8_0 | 15.0 GB | 16.9 GB | None |
| qwen3.5:9b-q4_K_M | 11.6 GB | 20.2 GB | None |

`qwen3.6:35b-a3b` at 3.9 GB headroom is the tightest candidate. Expect OOM at some boundary-tier context sizes — that is the intended behaviour and will be recorded.

### Concurrency efficiency

All 7 models showed ~98–105% efficiency at concurrency=2 vs concurrency=1. The GPU serialises requests cleanly with no meaningful queue penalty.

---

## Key findings

**MoE efficiency is real and significant.** `qwen3.6:35b-a3b` (35B total / 3B active) is faster than `qwen3.6:27b` (dense) despite being nominally larger. MoE models also maintain throughput more consistently across context sizes because the KV cache growth (not active parameter compute) is the bottleneck, and MoE doesn't help there — but their smaller weight footprint leaves more VRAM headroom for KV cache.

**Dense 32B models hit a context cliff on RTX 5090.** With ~20 GB of model weights, the remaining ~12 GB for KV cache is exhausted around 32K tokens, causing memory bandwidth collapse. Any dense model at this weight class should be treated as a ≤16K-context candidate on this hardware.

**devstral:24b shows a partial cliff at 64K** (102 → 53 tok/s). Still above 15 tok/s floor but degrading. Worth monitoring in the coding and prefill tiers where actual prompts are large.

---

## Thermal throttle correction

The initial throttle detection formula — `(avg_sm_clock - min_sm_clock) / avg_sm_clock > 10%` — produced false positives on 100% of runs including tiny 9B models at 37°C. Root cause: `min_sm_clock` always captured the GPU idle/base clock (~456 MHz) at run start, not a heat-induced frequency drop.

Fixed in PR #416: replaced with `peak_temp_c >= 83°C` (RTX 5090 throttle onset). No thermal throttling was observed in the speed tier; all models ran at 37–52°C.

---

## What's next

- **Full matrix run** (all 5 tiers): run after merging the throttle fix. Estimated 4–8 hours on RTX 5090. Start with `--tier coding` after `--tier speed` to get quality scores before the long prefill tiers.
- **qwen3.6:35b-a3b VRAM watch**: expect OOM at some boundary-tier sizes. The adaptive skip logic will handle it; the max working context recorded at sweep end will be the key number.
- **devstral:24b 64K degradation**: the coding tier will reveal whether quality compensates for the speed drop at large contexts.
- **qwen3.5:9b quant comparison**: Q4 vs Q8 quality delta in the coding tier is the main data point — if quality is equivalent, Q4 is the clear production choice at 6.6 GB.
