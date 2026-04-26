"""Benchmark execution engine."""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from app.core.bench_store import BenchRun, BenchStore
from scripts.bench.config import BenchCase, BenchConfig, ModelConfig
from scripts.bench.drivers.ollama import OllamaDriver
from scripts.bench.drivers.vllm import VllmDriver
from scripts.bench.env_snapshot import EnvSnapshot
from scripts.bench.gpu_monitor import GpuMonitor
from scripts.bench.lifecycle.ollama_manager import OllamaManager
from scripts.bench.quality import evaluate_coding_output
from scripts.bench.sys_monitor import SysMonitor
from scripts.bench.tasks import BenchPrompt
from scripts.bench.tasks.boundary import make_boundary_prompt
from scripts.bench.tasks.coding import make_coding_prompt
from scripts.bench.tasks.prefill_shared import make_prefill_shared_prompt
from scripts.bench.tasks.prefill_unshared import make_prefill_unshared_prompt
from scripts.bench.tasks.speed import make_speed_prompt

# RTX 5090 begins thermal throttling around 83-85°C.
# Previous SM-clock-variance approach produced false positives because min_sm_clock
# always captured the GPU idle/base clock (~456 MHz) at run start, not a real drop.
THROTTLE_TEMP_C = 83


def _case_id(case: BenchCase) -> str:
    return (
        f"{case.backend_id}/{case.model_id}"
        f"/{case.tier}/{case.context_size}/{case.concurrency}"
    )


def _row_run_id(sweep_id: str, case: BenchCase) -> str:
    return f"{sweep_id}::{_case_id(case)}::{case.repeat_index}"


def _is_oom(error: str) -> bool:
    low = error.lower()
    return (
        "507" in error
        or "out of memory" in low
        or "connection reset" in low
        or "connectionreset" in low
    )


def _make_prompt(tier: str, repeat_index: int) -> BenchPrompt:
    if tier == "speed":
        return make_speed_prompt()
    if tier == "coding":
        return make_coding_prompt()
    if tier == "prefill_shared":
        return make_prefill_shared_prompt(suffix_index=repeat_index)
    if tier == "prefill_unshared":
        return make_prefill_unshared_prompt()
    if tier == "boundary":
        return make_boundary_prompt()
    raise ValueError(f"Unknown tier: {tier!r}")


def _make_driver(backend_id: str, base_url: str) -> OllamaDriver | VllmDriver:
    if "vllm" in backend_id.lower():
        return VllmDriver(base_url=base_url)
    return OllamaDriver(base_url=base_url)


def _should_skip_oom(
    case: BenchCase,
    oom_ctx: dict[tuple[str, str], int],
    skip_oom_larger_ctx: bool,
) -> bool:
    if not skip_oom_larger_ctx:
        return False
    threshold = oom_ctx.get((case.model_id, case.backend_id))
    return threshold is not None and case.context_size > threshold


def _should_skip_concurrency_gate(
    case: BenchCase,
    concurrency_gate: set[tuple[str, str, int]],
    require_single_concurrency_first: bool,
) -> bool:
    if not require_single_concurrency_first or case.concurrency <= 1:
        return False
    return (case.model_id, case.backend_id, case.context_size) not in concurrency_gate


def _update_adaptive_state(
    case: BenchCase,
    outcome: str,
    oom_ctx: dict[tuple[str, str], int],
    max_working_ctx: dict[tuple[str, str], int],
    concurrency_gate: set[tuple[str, str, int]],
) -> None:
    key = (case.model_id, case.backend_id)
    if outcome == "oom":
        if key not in oom_ctx or case.context_size < oom_ctx[key]:
            oom_ctx[key] = case.context_size
    elif outcome == "ok":
        if key not in max_working_ctx or case.context_size > max_working_ctx[key]:
            max_working_ctx[key] = case.context_size
        if case.concurrency == 1:
            concurrency_gate.add((case.model_id, case.backend_id, case.context_size))


def _make_skipped_run(run_id: str, case: BenchCase, outcome: str) -> BenchRun:
    return BenchRun(
        run_id=run_id,
        case_id=_case_id(case),
        repeat_index=case.repeat_index,
        tier=case.tier,
        context_size=case.context_size,
        concurrency=case.concurrency,
        backend_id=case.backend_id,
        model_id=case.model_id,
        outcome=outcome,
    )


def run(
    config_path: str,
    db_path: Path,
    *,
    tier: str | None = None,
    model: str | None = None,
    backend: str | None = None,
    resume: str | None = None,
    output_json: str | None = None,
    output_csv: str | None = None,
) -> None:
    config = BenchConfig.from_toml(config_path)
    cases = config.expand_matrix()

    if tier:
        cases = [c for c in cases if c.tier == tier]
    if model:
        cases = [c for c in cases if c.model_id == model]
    if backend:
        cases = [c for c in cases if c.backend_id == backend]

    sweep_id: str = (
        resume
        if resume
        else f"run_{datetime.now(tz=timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    )
    print(f"Sweep: {sweep_id}  DB: {db_path}")

    store = BenchStore(db_path)

    backends = {b.id: b for b in config.backends if b.enabled}
    model_cfgs: dict[str, ModelConfig] = {m.id: m for m in config.models}
    # Cache /api/show result per (backend_id, model_id) — called once per model.
    model_info_cache: dict[tuple[str, str], dict[str, str | None]] = {}

    prev_model_id: str | None = None
    prev_backend_id: str | None = None
    # Tracks which models have had at least one prefill_shared run (for cache_state)
    prefill_shared_seen: set[str] = set()

    # Adaptive scheduling state — per-session in-memory only, not persisted across runs
    oom_ctx: dict[tuple[str, str], int] = {}
    max_working_ctx: dict[tuple[str, str], int] = {}
    concurrency_gate: set[tuple[str, str, int]] = set()

    for case in cases:
        row_run_id = _row_run_id(sweep_id, case)

        if resume and (existing := store.get_by_run_id(row_run_id)):
            for row in existing:
                _update_adaptive_state(
                    case, row.outcome or "", oom_ctx, max_working_ctx, concurrency_gate
                )
            print(
                f"[SKIP] {case.model_id}/{case.tier}"
                f"/ctx={case.context_size}/c={case.concurrency}/r={case.repeat_index}"
            )
            continue

        backend_cfg = backends.get(case.backend_id)
        if backend_cfg is None:
            print(
                f"[WARN] Unknown backend {case.backend_id!r}, skipping",
                file=sys.stderr,
            )
            continue

        if _should_skip_oom(case, oom_ctx, config.matrix.skip_oom_larger_ctx):
            store.record(_make_skipped_run(row_run_id, case, "skipped_oom"))
            print(
                f"[SKIP_OOM  ] {case.model_id} / {case.tier}"
                f" / ctx={case.context_size} / c={case.concurrency}"
                f" / r={case.repeat_index}"
            )
            continue

        if _should_skip_concurrency_gate(
            case, concurrency_gate, config.matrix.require_single_concurrency_first
        ):
            store.record(
                _make_skipped_run(row_run_id, case, "skipped_concurrency_gate")
            )
            print(
                f"[SKIP_GATE ] {case.model_id} / {case.tier}"
                f" / ctx={case.context_size} / c={case.concurrency}"
                f" / r={case.repeat_index}"
            )
            continue

        driver = _make_driver(case.backend_id, backend_cfg.base_url)

        info_key = (case.backend_id, case.model_id)
        if info_key not in model_info_cache:
            if isinstance(driver, OllamaDriver):
                info = driver.fetch_model_info(case.model_id)
            else:
                info = {
                    "model_quant": None,
                    "model_family": None,
                    "model_param_count": None,
                }
            m_cfg = model_cfgs.get(case.model_id)
            if m_cfg is not None and m_cfg.quant is not None:
                info = {**info, "model_quant": m_cfg.quant}
            model_info_cache[info_key] = info
        model_info = model_info_cache[info_key]

        manager: OllamaManager | None = None
        if "vllm" not in case.backend_id.lower():
            manager = OllamaManager(base_url=backend_cfg.base_url)
            try:
                manager.ensure_running()
                manager.pull_if_needed(case.model_id)
            except TimeoutError as exc:
                print(f"[ERROR] Ollama not ready: {exc}", file=sys.stderr)
                continue

        # Flush previous model when switching models within a backend
        if (
            manager is not None
            and prev_model_id is not None
            and (prev_model_id != case.model_id or prev_backend_id != case.backend_id)
        ):
            try:
                manager.flush_model(prev_model_id)
            except Exception as exc:
                logging.warning("flush_model failed: %s", exc)

        prev_model_id = case.model_id
        prev_backend_id = case.backend_id

        env = EnvSnapshot.capture(
            backend=case.backend_id, model=case.model_id, config=config
        )

        # First prefill_shared per model = warm; subsequent = prefix_warm
        cache_state: str | None = None
        if case.tier == "prefill_shared":
            key = f"{case.backend_id}/{case.model_id}"
            if key not in prefill_shared_seen:
                prefill_shared_seen.add(key)
                cache_state = "warm"
            else:
                cache_state = "prefix_warm"

        prompt = _make_prompt(case.tier, case.repeat_index)

        gpu_mon = GpuMonitor()
        sys_mon = SysMonitor()
        gpu_mon.start()
        sys_mon.start()

        messages: list[dict[str, str]] = [{"role": "user", "content": prompt.text}]
        t_start = time.monotonic()
        result = driver.generate(
            case.model_id,
            messages,
            case.context_size,
            prompt.max_tokens,
            prompt.temperature,
            prompt.seed,
        )
        wall_time_s = time.monotonic() - t_start

        gpu_sample = gpu_mon.stop()
        sys_result = sys_mon.stop()

        peak_temp = gpu_sample.peak_temp_c
        if peak_temp is None:
            thermal_throttle_detected: bool | None = None
        else:
            thermal_throttle_detected = peak_temp >= THROTTLE_TEMP_C

        oom = False
        outcome = "ok"
        error_message: str | None = None
        if result.error:
            error_message = result.error
            if _is_oom(result.error):
                oom = True
                outcome = "oom"
            else:
                outcome = "error"

        quality_task_success: bool | None = None
        quality_pytest_passed: bool | None = None
        quality_ruff_passed: bool | None = None
        quality_mypy_passed: bool | None = None
        if case.tier == "coding" and not oom and result.text:
            repo_path = Path(__file__).parent.parent.parent
            qr = evaluate_coding_output(result.text, repo_path)
            quality_task_success = qr.task_success
            quality_pytest_passed = qr.pytest_passed
            quality_ruff_passed = qr.ruff_passed
            quality_mypy_passed = qr.mypy_passed
            if qr.error_message and error_message is None:
                error_message = qr.error_message

        total_tokens: int | None = None
        if result.input_tokens is not None and result.output_tokens is not None:
            total_tokens = result.input_tokens + result.output_tokens

        throughput_tok_s: float | None = None
        if (
            result.output_tokens
            and result.decode_time_s is not None
            and result.decode_time_s > 0
        ):
            throughput_tok_s = result.output_tokens / result.decode_time_s

        ollama_model_loaded: bool | None = None
        if manager is not None:
            try:
                status = manager.get_ps_status(case.model_id)
                ollama_model_loaded = status is not None
            except Exception as exc:
                logging.debug("Could not check model status: %s", exc)

        bench_run = BenchRun(
            run_id=row_run_id,
            case_id=_case_id(case),
            repeat_index=case.repeat_index,
            tier=case.tier,
            context_size=case.context_size,
            concurrency=case.concurrency,
            backend_id=case.backend_id,
            model_id=case.model_id,
            settings_hash=env.settings_hash,
            prompt_hash=prompt.prompt_hash,
            backend_base_url=backend_cfg.base_url,
            gpu_driver_version=env.gpu_driver_version,
            cuda_version=env.cuda_version,
            python_version=env.python_version,
            os_version=env.os_version,
            ttft_s=result.ttft_s if not oom else None,
            ttfut_s=result.ttfut_s if not oom else None,
            wall_time_s=wall_time_s if not oom else None,
            throughput_tok_s=throughput_tok_s,
            prompt_eval_duration_s=result.prompt_eval_duration_s,
            load_duration_s=result.load_duration_s,
            decode_time_s=result.decode_time_s,
            prompt_tokens=result.input_tokens,
            completion_tokens=result.output_tokens,
            total_tokens=total_tokens,
            peak_vram_gb=gpu_sample.peak_vram_gb,
            total_vram_gb=env.total_vram_gb,
            avg_gpu_util_pct=gpu_sample.avg_gpu_util_pct,
            avg_gpu_mem_util_pct=gpu_sample.avg_gpu_mem_util_pct,
            avg_power_w=gpu_sample.avg_power_w,
            peak_temp_c=gpu_sample.peak_temp_c,
            avg_sm_clock_mhz=gpu_sample.avg_sm_clock_mhz,
            min_sm_clock_mhz=gpu_sample.min_sm_clock_mhz,
            thermal_throttle_detected=thermal_throttle_detected,
            avg_mem_clock_mhz=gpu_sample.avg_mem_clock_mhz,
            peak_ram_gb=sys_result.peak_ram_gb,
            cpu_offload_detected=sys_result.cpu_offload_detected,
            cache_state=cache_state,
            ollama_model_loaded=ollama_model_loaded,
            ollama_num_ctx=case.context_size,
            model_quant=model_info["model_quant"],
            model_family=model_info["model_family"],
            model_param_count=model_info["model_param_count"],
            quality_task_success=quality_task_success,
            quality_pytest_passed=quality_pytest_passed,
            quality_ruff_passed=quality_ruff_passed,
            quality_mypy_passed=quality_mypy_passed,
            outcome=outcome,
            error_message=error_message,
        )

        # Write before moving to next case (resume safety invariant)
        store.record(bench_run)
        _update_adaptive_state(
            case, outcome, oom_ctx, max_working_ctx, concurrency_gate
        )

        ttft_str = f" ttft={result.ttft_s:.2f}s" if result.ttft_s else ""
        tok_str = f" tok/s={throughput_tok_s:.0f}" if throughput_tok_s else ""
        cache_str = f" [{cache_state}]" if cache_state else ""
        print(
            f"[{outcome.upper():5}] {case.model_id} / {case.tier}"
            f" / ctx={case.context_size} / c={case.concurrency} / r={case.repeat_index}"
            f"{cache_str}{ttft_str}{tok_str}"
        )

    print("\nRun complete.")

    if max_working_ctx:
        print("Max working context per (model, backend):")
        for (model_id, backend_id), ctx in sorted(max_working_ctx.items()):
            print(f"  {model_id} / {backend_id}: {ctx}")

    from scripts.bench import reporter

    rows = reporter.load_sweep(db_path, sweep_id)
    reporter.print_summary_table(rows)
    reporter.print_ranking(rows)

    if output_json:
        reporter.export_json(rows, Path(output_json))
        print(f"JSON exported: {output_json}")
    if output_csv:
        reporter.export_csv(rows, Path(output_csv))
        print(f"CSV exported: {output_csv}")
