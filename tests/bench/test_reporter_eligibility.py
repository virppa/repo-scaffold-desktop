"""Tests for reporter._is_eligible() gates, _percentile(), and _cv()."""

from __future__ import annotations

from typing import Any

from scripts.bench.reporter import (
    _cv,
    _is_eligible,
    _percentile,
)


def _row(
    *,
    repeat_index: int = 1,
    outcome: str | None = "ok",
    cpu_offload_detected: bool | None = False,
    context_size: int | None = 4096,
    throughput_tok_s: float | None = 80.0,
    tier: str | None = "speed",
    quality_task_success: bool | None = None,
) -> dict[str, Any]:
    return {
        "repeat_index": repeat_index,
        "outcome": outcome,
        "cpu_offload_detected": cpu_offload_detected,
        "context_size": context_size,
        "throughput_tok_s": throughput_tok_s,
        "tier": tier,
        "quality_task_success": quality_task_success,
        "backend_id": "b",
        "model_id": "m",
        "concurrency": 1,
        "ttft_s": 0.3,
    }


def _config_rows(*rows: dict[str, Any]) -> list[dict[str, Any]]:
    return list(rows)


# ── _is_eligible() unit tests ─────────────────────────────────────────────────


class TestIsEligibleBaseCases:
    def test_empty_input_returns_reason(self) -> None:
        reason = _is_eligible([])
        assert reason is not None
        assert reason == "no data"

    def test_only_warmup_runs_returns_reason(self) -> None:
        rows = _config_rows(_row(repeat_index=0))
        reason = _is_eligible(rows)
        assert reason == "no real runs"

    def test_passing_config_returns_none(self) -> None:
        rows = _config_rows(_row())
        assert _is_eligible(rows) is None


class TestOomGate:
    def test_oom_outcome_disqualifies(self) -> None:
        rows = _config_rows(_row(outcome="oom"))
        reason = _is_eligible(rows)
        assert reason == "OOM"

    def test_ok_outcome_passes(self) -> None:
        rows = _config_rows(_row(outcome="ok"))
        assert _is_eligible(rows) is None

    def test_oom_on_one_run_disqualifies_config(self) -> None:
        rows = _config_rows(_row(outcome="ok"), _row(outcome="oom"))
        reason = _is_eligible(rows)
        assert reason == "OOM"

    def test_warmup_oom_does_not_disqualify(self) -> None:
        # repeat_index=0 rows are warmup and excluded from gate checks
        rows = _config_rows(_row(repeat_index=0, outcome="oom"), _row(outcome="ok"))
        assert _is_eligible(rows) is None


class TestCpuOffloadGate:
    def test_cpu_offload_disqualifies(self) -> None:
        rows = _config_rows(_row(cpu_offload_detected=True))
        reason = _is_eligible(rows)
        assert reason == "CPU offload"

    def test_no_offload_passes(self) -> None:
        rows = _config_rows(_row(cpu_offload_detected=False))
        assert _is_eligible(rows) is None


class TestContextTooSmallGate:
    def test_all_ctx_below_threshold_disqualifies(self) -> None:
        rows = _config_rows(_row(context_size=512), _row(context_size=1024))
        reason = _is_eligible(rows, min_useful_ctx=4096)
        assert reason is not None
        assert "context too small" in reason
        assert "1024" in reason

    def test_ctx_equal_to_threshold_passes(self) -> None:
        rows = _config_rows(_row(context_size=4096))
        assert _is_eligible(rows, min_useful_ctx=4096) is None

    def test_ctx_above_threshold_passes(self) -> None:
        rows = _config_rows(_row(context_size=8192))
        assert _is_eligible(rows, min_useful_ctx=4096) is None

    def test_mixed_ctx_passes_if_any_at_or_above_threshold(self) -> None:
        # One run below, one at threshold — should pass (not ALL below threshold)
        rows = _config_rows(_row(context_size=1024), _row(context_size=4096))
        assert _is_eligible(rows, min_useful_ctx=4096) is None

    def test_custom_threshold_respected(self) -> None:
        rows = _config_rows(_row(context_size=2048))
        assert _is_eligible(rows, min_useful_ctx=2048) is None
        reason = _is_eligible(rows, min_useful_ctx=4096)
        assert reason is not None
        assert "context too small" in reason

    def test_none_context_size_skipped(self) -> None:
        rows = _config_rows(_row(context_size=None))
        # No valid ctx_values → gate is skipped → eligible
        assert _is_eligible(rows, min_useful_ctx=4096) is None


class TestThroughputTooLowGate:
    def test_low_median_throughput_disqualifies(self) -> None:
        rows = _config_rows(_row(throughput_tok_s=2.0), _row(throughput_tok_s=3.0))
        reason = _is_eligible(rows, min_throughput_toks_per_s=5.0)
        assert reason is not None
        assert "throughput too low" in reason
        assert "tok/s" in reason

    def test_throughput_equal_to_floor_passes(self) -> None:
        rows = _config_rows(_row(throughput_tok_s=5.0))
        assert _is_eligible(rows, min_throughput_toks_per_s=5.0) is None

    def test_throughput_above_floor_passes(self) -> None:
        rows = _config_rows(_row(throughput_tok_s=80.0))
        assert _is_eligible(rows, min_throughput_toks_per_s=5.0) is None

    def test_custom_throughput_floor(self) -> None:
        rows = _config_rows(_row(throughput_tok_s=10.0))
        assert _is_eligible(rows, min_throughput_toks_per_s=10.0) is None
        reason = _is_eligible(rows, min_throughput_toks_per_s=20.0)
        assert reason is not None
        assert "throughput too low" in reason

    def test_none_throughput_skips_gate(self) -> None:
        rows = _config_rows(_row(throughput_tok_s=None))
        assert _is_eligible(rows, min_throughput_toks_per_s=5.0) is None


class TestErrorRateGate:
    def test_high_error_rate_disqualifies(self) -> None:
        rows = _config_rows(
            _row(outcome="error"),
            _row(outcome="error"),
            _row(outcome="ok"),
        )
        reason = _is_eligible(rows)
        assert reason is not None
        assert "error rate" in reason

    def test_borderline_5pct_passes(self) -> None:
        # 1 error out of 20 = 5% — should pass
        rows = [_row(outcome="ok")] * 19 + [_row(outcome="error")]
        assert _is_eligible(rows) is None

    def test_just_over_5pct_disqualifies(self) -> None:
        # 2 errors out of 19 ≈ 10.5% — disqualifies
        rows = [_row(outcome="ok")] * 17 + [
            _row(outcome="error"),
            _row(outcome="error"),
        ]
        reason = _is_eligible(rows)
        assert reason is not None
        assert "error rate" in reason


class TestTaskSuccessGate:
    def test_low_task_success_disqualifies(self) -> None:
        rows = _config_rows(
            _row(tier="coding", quality_task_success=False),
            _row(tier="coding", quality_task_success=False),
            _row(tier="coding", quality_task_success=False),
        )
        reason = _is_eligible(rows)
        assert reason is not None
        assert "task success" in reason

    def test_sufficient_task_success_passes(self) -> None:
        rows = _config_rows(
            _row(tier="coding", quality_task_success=True),
            _row(tier="coding", quality_task_success=True),
            _row(tier="coding", quality_task_success=True),
        )
        assert _is_eligible(rows) is None

    def test_non_coding_tier_skips_task_gate(self) -> None:
        rows = _config_rows(_row(tier="speed", quality_task_success=False))
        assert _is_eligible(rows) is None

    def test_no_quality_data_skips_task_gate(self) -> None:
        rows = _config_rows(_row(tier="coding", quality_task_success=None))
        assert _is_eligible(rows) is None


class TestGatePriority:
    def test_oom_checked_before_ctx(self) -> None:
        rows = _config_rows(_row(outcome="oom", context_size=512))
        reason = _is_eligible(rows, min_useful_ctx=4096)
        assert reason == "OOM"

    def test_offload_checked_before_ctx(self) -> None:
        rows = _config_rows(_row(cpu_offload_detected=True, context_size=512))
        reason = _is_eligible(rows, min_useful_ctx=4096)
        assert reason == "CPU offload"

    def test_ctx_checked_before_throughput(self) -> None:
        rows = _config_rows(_row(context_size=512, throughput_tok_s=1.0))
        reason = _is_eligible(rows, min_useful_ctx=4096, min_throughput_toks_per_s=5.0)
        assert reason is not None
        assert "context too small" in reason


# ── _percentile() unit tests ──────────────────────────────────────────────────


class TestPercentile:
    def test_empty_returns_none(self) -> None:
        assert _percentile([], 95) is None

    def test_single_value_returns_none(self) -> None:
        assert _percentile([1.0], 95) is None

    def test_two_values_p95_in_range(self) -> None:
        result = _percentile([0.0, 1.0], 95)
        assert result is not None
        assert 0.0 <= result <= 1.0

    def test_p95_higher_than_p50_with_outlier(self) -> None:
        values = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 5.0]
        p50 = _percentile(values, 50)
        p95 = _percentile(values, 95)
        assert p50 is not None and p95 is not None
        assert p95 > p50

    def test_uniform_values_returns_that_value(self) -> None:
        result = _percentile([3.0, 3.0, 3.0], 95)
        assert result == 3.0

    def test_p100_returns_max(self) -> None:
        values = [0.1, 0.5, 1.0]
        assert _percentile(values, 100) == 1.0


# ── _cv() unit tests ──────────────────────────────────────────────────────────


class TestCV:
    def test_empty_returns_none(self) -> None:
        assert _cv([]) is None

    def test_single_value_returns_none(self) -> None:
        assert _cv([1.0]) is None

    def test_zero_mean_returns_none(self) -> None:
        assert _cv([0.0, 0.0]) is None

    def test_identical_values_returns_zero(self) -> None:
        result = _cv([2.0, 2.0, 2.0])
        assert result == 0.0

    def test_known_cv(self) -> None:
        # mean=2.0, sample stdev=sqrt(2)≈1.414 → CV≈0.707
        result = _cv([1.0, 3.0])
        assert result is not None
        assert abs(result - (2**0.5 / 2)) < 0.001

    def test_high_variance_exceeds_threshold(self) -> None:
        result = _cv([0.1, 1.9])
        assert result is not None
        assert result > 0.3

    def test_low_variance_below_threshold(self) -> None:
        result = _cv([0.30, 0.31])
        assert result is not None
        assert result < 0.3
