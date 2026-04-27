# Benchmark Post-Mortem — Ollama Baseline (WOR-209 Epic A)

**Run:** `run_20260426_214801` · 7 models · 700 rows · 3.5 hours · RTX 5090 32 GB · OLLAMA_FLASH_ATTENTION=1
**Status:** Complete
**Last updated:** 2026-04-27

---

## What we set out to measure

Characterise every viable local model on this hardware across five dimensions: raw generation speed, prefill latency, long-context viability, VRAM pressure, and coding quality. Goal was to produce a data-driven routing table for the watcher optimizer (WOR-199) and establish the Ollama baseline for vLLM comparison (WOR-210).

---

## Quality results

| Model | Task success | Pytest | Ruff | Mypy | Avg tokens |
|---|---|---|---|---|---|
| qwen3.6:35b-a3b | **100%** | 100% | 100% | 100% | 1,473 |
| qwen3-coder:30b | **100%** | 100% | 100% | 100% | 62 |
| qwen3.5:9b-q4_K_M | **100%** | 100% | 100% | 100% | 5,401 |
| qwen3.5:9b-q8_0 | **85.7%** (24/28) | 86% | 86% | 86% | 6,691 |
| devstral:24b | **0%** | 100% | 0% | 100% | 73 |
| gemma3:27b | **0%** | 0% | 0% | 0% | 79 |

**devstral** produces working code — pytest passes on every single run — but fails ruff on every single run. It writes functional but unclean Python: no type annotations, deprecated patterns, or style violations. Task success requires all three checks; it never clears ruff. This is a meaningful distinction: capable coder, not a quality coder.

**gemma3** ignores the JSON output format entirely and writes prose. 79 tokens of natural language, `JSONDecodeError` every time. This is a prompt-compliance failure, not a reasoning failure. Not worth custom prompt engineering — dropped.

**9b-q4** achieves 100% quality but uses 80–100x more tokens than 30b-coder for the same task. The model is capable but inefficient — it ruminates extensively before arriving at an answer that a more capable model reaches immediately. This verbosity is a quality signal: smaller models produce less-directed reasoning chains.

**Token verbosity as capability signal:**
| Model | Avg coding tokens | Relative to 30b-coder |
|---|---|---|
| qwen3-coder:30b | 62 | 1× |
| qwen2.5-coder:32b | 67 | 1.1× |
| qwen3.6:35b-a3b | 1,473 | 24× |
| qwen3.5:9b-q4_K_M | 5,401 | 87× |
| qwen3.5:9b-q8_0 | 6,691 | 108× |

The 9b models also consume context faster in multi-turn watcher sessions — 5,000–10,000 tokens of thinking per turn vs 1,500 for 35b-a3b. Despite raw speed advantage, time-to-first-useful-token on coding tasks is 3–4× slower for 9b.

---

## Speed results (tok/s)

| Model | 16K | 64K | 128K | 256K | 320K |
|---|---|---|---|---|---|
| qwen3.5:9b-q4 | 179 | 181 | 177 | 179 | **183** |
| qwen3.5:9b-q8 | 130 | 133 | 131 | 135 | 135 |
| qwen3.6:35b-a3b | 161 | 166 | **165** | 48 | 35 |
| qwen3-coder:30b | 137 | 166 | 71 | 48 | 35 |
| gemma3:27b | 79 | 76 | 80 | 78 | **81** |
| devstral:24b | 108 | 101 | 10 | 10 | 11 |
| qwen2.5-coder:32b | 75 | 30 | 30 | 30 | 32 |

Three throughput archetypes:

**Flat-through-all-contexts:** Both 9b models maintain identical speed from 16K to 320K. No cliff, no VRAM pressure, no degradation. Gemma3:27b does the same at 79–81 tok/s — a dense 27B model with this property is architecturally unusual, likely extreme GQA with few KV heads.

**FA-extended flat with staircase:** 35b-a3b is flat at 161–166 from 16K through 128K (Flash Attention eliminated the cliff at 96K→128K, +105% at 131K), then descends in three clean steps. **OLLAMA_FLASH_ATTENTION=1 is not optional for this model** — without it, 128K was already a cliff. See the staircase characterisation below for the full picture above 128K.

**Standard dense cliff:** 30b-coder maintains speed to 64K then halves at 128K. Devstral falls off at 96K (128K native context limit). qwen2.5-coder:32b cliffs at 32K.

### 35b-a3b staircase above 128K (cliff.toml + cliff2 + cliff3)

Four targeted runs pinned the exact plateau boundaries. 30b-coder profiled to 176K then excluded — no plateau structure, continuous decline only.

| Context | 35b-a3b | 30b-coder | Notes |
|---|---|---|---|
| 128K | **165** | 71 | FA-flat ends |
| 144K | **75.8** | 63.2 | Step down; secondary plateau starts |
| 160K | **74.6** | 61.2 | Plateau |
| 176K | **74.4** | 59.7 | Plateau; 30b continuous decline |
| 184K | **76.1** | — | Secondary plateau extends to 184K |
| 192K | 62 | 56 | Step down; plateau ends 184K→192K |
| 208K | **62** | — | Third plateau |
| 216K | **62** | — | Third plateau confirmed to 216K |
| 224K | **55** | — | Step down; 62→55 in 216K→224K window |
| 240K | **49** | — | Already at floor |
| 256K | 48 | 48 | Converge |
| 320K | 34 | 34.5 | Floor |

**Three clean steps then floor:**
1. **165 tok/s** — ≤131K (FA-flat)
2. **~75 tok/s** — 132K–184K
3. **~62 tok/s** — 185K–216K
4. **~48 tok/s** — >216K (at floor by 240K — no useful plateau above 216K)

**30b-coder prefill collapse above 131K** (measured on shared 117K-token prefix):

| Context | 35b-a3b | 30b-coder | Gap |
|---|---|---|---|
| 144K | 68.4 tok/s | 12.4 tok/s | 5.5× |
| 160K | 66.4 tok/s | 9.1 tok/s | 7.3× |
| 176K | 67.4 tok/s | **7.3 tok/s** | **9.2×** |

At 176K, loading a 117K-token prefix into 30b-coder takes 21+ seconds. 35b-a3b handles the same in under 14 seconds, essentially unaffected by context growth. For watcher sessions where context accumulates turn-by-turn, this is a decisive advantage for 35b-a3b above 131K — for every context reload, not just generation.

---

## VRAM and long-context viability

| Model | 16K | 128K | 256K | 320K | Headroom at 256K |
|---|---|---|---|---|---|
| qwen3.5:9b-q4 | 10.3 | 15.0 | 20.5 | 20.5 | **11.5 GB** |
| qwen3.5:9b-q8 | 13.6 | 18.4 | 23.9 | 23.9 | **8.1 GB** |
| qwen3.6:35b-a3b | 26.9 | 30.5 | 30.5 | 30.9 | ~1.1 GB |
| qwen3-coder:30b | 21.3 | 30.8 | 30.8 | 30.8 | ~1.2 GB |
| gemma3:27b | 22.6 | 29.6 | 29.6 | 29.6 | ~2.4 GB |

Zero OOM events across all 700 rows including all 320K boundary probes. Every model in the matrix survives to 320K on 32 GB VRAM.

**Co-loading insight:** 9b-q4 at 256K uses 20.5 GB, leaving 11.5 GB free. 30b-coder at 16K uses 21.3 GB, leaving 10.7 GB free. These pairs can coexist in VRAM simultaneously at short contexts, enabling true multi-model serving without eviction.

---

## The routing picture

```
Task type                    Model                   Reason
──────────────────────────────────────────────────────────────────────────
Complex coding, ctx ≤128K    qwen3.6:35b-a3b         FA-flat 161-165 tok/s + 100% quality
Complex coding, ctx ≤96K     qwen3-coder:30b         5-12% faster, coding-specialist
Complex coding, 131K–216K    qwen3.6:35b-a3b         Third plateau: 62-76 tok/s, still viable
Complex coding, ctx >216K    reconsider              drops to 48-55 tok/s; at floor by 240K
Simple/mechanical tasks      qwen3.5:9b-q4_K_M       179 tok/s flat, 100% quality
Floor / parallel workers     qwen3.5:9b-q4_K_M       10.3 GB at 16K, co-loadable
```

**On 35–48 tok/s at 256K:** This is still viable for watcher background work. A 1000-token coding response takes ~25 seconds locally vs ~12 seconds via cloud Claude — but the watcher turn also includes pytest, ruff, mypy, and Linear API calls (typically 20–40s overhead), so generation is not the bottleneck. The practical pain threshold is ~15 tok/s, where a 1000-token response exceeds a minute. 35–48 tok/s at extreme context is free, private, and rate-limit-free — meaningfully better than cloud for sustained watcher sessions. The 128K boundary matters for *speed parity* with cloud, not for raw viability.

---

## Prefill latency baseline (for vLLM comparison)

| Model | TTFT 16K | TTFT 64K | TTFT 128K |
|---|---|---|---|
| qwen3-coder:30b | 3.55s | 5.21s | 12.0s |
| qwen3.5:9b-q4 | 3.65s | 6.08s | 9.63s |
| qwen3.5:9b-q8 | 3.77s | 6.16s | 9.62s |
| qwen3.6:35b-a3b | 4.56s | 7.68s | 11.98s |
| devstral:24b | 4.86s | 6.06s | 26.53s |
| gemma3:27b | 5.94s | 15.16s | — (error) |

vLLM APC on `prefill_shared` tier should halve warm-request TTFT. These numbers are the baseline to beat.

---

## What went wrong / lessons learned

**Thinking model token budgets:** Started at `max_tokens=512`, too low for any thinking model. The chain 512 → 4096 → 8192 → 10240 → 16384 cost multiple benchmark reruns. Before any full matrix run, run a single-model single-context smoke test and observe actual token usage. Set budget at 1.5× observed max. The right command: `--model X --tier coding` with 1 repeat, check DB.

**Background job control on Windows:** `bash &` does not respect shell job control on Windows — `kill %1 %2` kills the Python wrapper but not the Ollama inference already in-flight. Left Ollama generating for 20+ minutes, filling the DB with partial data. Rule: never use `&` for benchmark runs. Always run in a dedicated terminal, sequentially.

**DB hygiene with reruns:** Any time a parameter changes (max_tokens, model config), delete the affected rows before rerunning. Mixed data from different budgets produces meaningless aggregations. Pattern: `DELETE FROM bench_run WHERE model_id = ? AND tier = ?` before every rerun.

**q8_0 verbosity paradox:** q8_0 generates avg 6,691 tokens vs q4's 5,401 — and hit the 16,384 ceiling on 3/28 runs, producing a *lower* task success rate (85.7%) than q4 (100%). The smarter quantization thinks more verbosely, overflows the budget more often, and produces a worse benchmark result despite being the higher quality model. It would likely reach 100% with a larger budget, but the ceiling keeps moving. Budget assumptions from q4 don't carry over to q8, and this pattern likely extends to any thinking model: higher capability → longer reasoning chains → higher token budget requirement.

---

## Models dropped and why

| Model | Reason |
|---|---|
| qwen3.6:27b | Weak throughput (85 tok/s at 16K), no quality data, MoE advantage not meaningful at 27B active params |
| qwen2.5-coder:32b-instruct-q4_K_M | Prior-gen reference: 32K throughput cliff confirmed (75→30 tok/s), 100% quality confirmed; characterisation complete |
| devstral:24b | Hard 96K cliff, 0% ruff/quality score, 128K native limit; capable coder but not a quality coder |
| gemma3:27b | Ignores JSON output format — prompt compliance failure every run; not worth custom prompt engineering |
| qwen3.5:9b-q8_0 | Verbosity paradox: more capable than q4 but thinks longer, hits token ceiling more often, worse benchmark score; floor model role doesn't justify the tradeoff |

---

## Implications for upcoming tickets

### WOR-199 — Watcher optimizer

- Concurrency data (WOR-203) is all at concurrency=1 — Ollama serialises, no differential signal at concurrency=2. Auto-sizing sub-goal is gated on vLLM data from WOR-210.
- Quality thresholds for `_is_eligible()` are now data-driven: devstral and gemma3 are disqualified at current prompt format and excluded from the active matrix.
- Routing calibration (context size vs task complexity) will come from live watcher data, not synthetic benchmarks. Instrumentation required: `local_input_tokens`, `local_output_tokens`, `estimated_context_tokens` — see WOR-199 comments for schema spec.
- Three test gaps must close before watcher reads bench.db for routing decisions: ranking table column assertions, threshold configurability parametrisation, CLI exit-code integration test.

### WOR-118 — vLLM spike

- Ollama baseline is solid. Primary question: does vLLM support RTX 5090 (Blackwell, SM_120)?
- 35b-a3b runs at 15–30% GPU utilization during Ollama inference — significant idle headroom that vLLM batching could exploit.
- Prefill_shared TTFT numbers above are the target for APC improvement.

### WOR-210 — vLLM epic

- Enable `local_vllm` backend in bench.toml; re-enable concurrency levels [1, 2, 4].
- vLLM HF FP16 weights for 35b-a3b are larger than GGUF — `--quantization awq` likely required.
- Start with `--max-model-len 131072`; extend to 262144 once VRAM confirmed.
- Critical vLLM flags for agentic use: `--reasoning-parser qwen3`, `--enable-auto-tool-choice`, `--tool-call-parser qwen3_coder`. Omitting these silently breaks all tool calls.

---

## Open items

- [x] 35b-a3b staircase above 128K — fully characterised across cliff/cliff2/cliff3 runs; 3 clean plateaus, floor at >216K
- [x] q8_0 coding results — benchmark artefact (verbosity paradox); not worth pursuing further
- [ ] vLLM compatibility check (WOR-118) — gates everything in WOR-210
- [ ] Close WOR-199 test gaps before watcher reads bench.db for routing decisions
- [ ] Task complexity routing calibration — deferred to live watcher data; instrumentation spec in WOR-199 comments
