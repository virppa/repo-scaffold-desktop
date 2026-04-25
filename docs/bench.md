# Local LLM Benchmark Harness

Standalone benchmark suite for evaluating local LLM models and backends before
production use in the watcher. Lives entirely in `scripts/bench/` — no app.*
imports allowed.

## Quick start

```bash
# 1. Generate the 50k-token context fixture (needed for prefill tiers)
python scripts/bench/run_bench.py --generate-fixtures

# 2. Edit config/bench.toml to match your models and backend URLs

# 3. Run a quick speed-only sweep
python scripts/bench/run_bench.py --tier speed

# 4. Run the full matrix (all tiers, all models in config)
python scripts/bench/run_bench.py

# 5. View results in Datasette browser
python scripts/bench/run_bench.py --browse
```

## CLI reference

```
python scripts/bench/run_bench.py [OPTIONS]

Options:
  --config PATH       Path to bench.toml (default: config/bench.toml)
  --tier TIER         Filter to a single tier: speed | coding | prefill_shared
                      | prefill_unshared | boundary
  --resume SWEEP_ID   Resume an interrupted run — skips already-recorded cases
  --compare ID1 ID2   Side-by-side TTFT comparison of two sweep IDs
  --generate-fixtures Write scripts/bench/fixtures/project_summary_50k.txt
  --export-json PATH  Export sweep results to JSON
  --export-csv PATH   Export sweep results to CSV
  --browse            Open bench.db in Datasette browser UI
```

## Benchmark tiers

| Tier | What it measures |
|------|-----------------|
| `speed` | Raw generation speed — short prompt, ~256 tokens out |
| `prefill_shared` | TTFT with shared 50k repo prefix — tests KV-cache reuse (vLLM APC benefit) |
| `prefill_unshared` | TTFT with randomized same-length content — APC null baseline |
| `coding` | Real coding task with pytest/ruff/mypy quality evaluation |
| `boundary` | Long-context OOM probe at configured context sizes |

## Config (config/bench.toml)

```toml
[matrix]
context_sizes = [1024, 4096]          # token context sizes for non-boundary tiers
boundary_context_sizes = [8192, 16384] # boundary-only context sizes
concurrency_levels = [1, 4]            # concurrent requests per probe
repeats = 1                            # real runs per probe point (warmup is always 1)

[[backends]]
id = "local_qwen"
enabled = true
base_url = "http://localhost:11434/v1"

[[models]]
id = "qwen3-coder-30b"
backend_id = "local_qwen"

[[tiers]]
name = "speed"

[[tiers]]
name = "coding"
```

## Prerequisites

**Ollama backend:**
```bash
# Start Ollama (auto-managed by the runner if on localhost:11434)
ollama serve
```

**vLLM backend:**
```bash
# vLLM is NOT auto-managed — start it manually before running
# WSL2 example:
python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen2.5-Coder-32B-Instruct \
  --port 8000 --gpu-memory-utilization 0.90
```

Set `enabled = true` and `base_url = "http://localhost:8000"` in bench.toml for
the vllm backend entry, and include `"vllm"` in the backend `id` so the runner
selects `VllmDriver`.

## Resume interrupted runs

```bash
# A sweep ID is printed at start: e.g. "Sweep: run_20260425_192600"
python scripts/bench/run_bench.py --tier coding --resume run_20260425_192600
```

Each case is written to `bench.db` before the next one starts — the runner
never loses completed results on interruption.

## Ranking and recommendations

The reporter prints a quality-gated ranking after each sweep. A config is
**eligible** for recommendation only if it passes all gates:

- No OOM during the run
- No CPU offload detected (RAM spike > 2 GB above baseline)
- Error rate ≤ 5% across repeats
- Task success ≥ 70% on coding tier (if coding data is present)

Ineligible configs are listed with the reason they were excluded.

## Data store

Results are written to `bench.db` (platform config dir, same parent as
`metrics.db`). Use Datasette to explore:

```bash
python scripts/bench/run_bench.py --browse
# or directly:
datasette ~/AppData/Roaming/repo-scaffold/bench.db   # Windows
datasette ~/.config/repo-scaffold/bench.db            # Linux/macOS
```

## Adding a new backend

1. Add a backend entry in `config/bench.toml` with a unique `id`.
2. If `"vllm"` appears in the `id`, `VllmDriver` is used; otherwise `OllamaDriver`.
3. For a fully custom backend, implement `BackendDriver` Protocol from
   `scripts/bench/drivers/base.py` and register it in `_make_driver()` in
   `run_bench.py`.

## Schema

`BenchRun` fields written per case (see `app/core/bench_store.py`):

| Group | Fields |
|-------|--------|
| Identity | run_id, case_id, repeat_index |
| Config | tier, context_size, concurrency, backend_id, model_id, settings_hash, prompt_hash |
| Environment | backend_base_url, gpu_driver_version, cuda_version, python_version, os_version |
| Timing | ttft_s, wall_time_s, throughput_tok_s |
| Tokens | prompt_tokens, completion_tokens, total_tokens |
| GPU | peak_vram_gb, avg_gpu_util_pct, avg_gpu_mem_util_pct, avg_power_w, peak_temp_c, avg_sm_clock_mhz, avg_mem_clock_mhz |
| CPU/RAM | peak_ram_gb, cpu_offload_detected |
| Ollama | ollama_model_loaded, ollama_num_ctx |
| Quality | quality_task_success, quality_pytest_passed, quality_ruff_passed, quality_mypy_passed |
| Outcome | outcome, error_message, recorded_at |
