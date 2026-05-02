"""Tests for reporter._is_eligible() gates, _percentile(), and _cv()."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from typing import Any

from scripts.bench.reporter import (
    OOM_RISK_HEADROOM_GB,
    _cv,
    _is_eligible,
    _percentile,
    print_ranking,
)


def _capture(rows: list[dict[str, Any]], **kwargs: Any) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        print_ranking(rows, **kwargs)
    return buf.getvalue()


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


# ── _is_eligible() VRAM headroom gate tests ───────────────────────────────────


class TestVramHeadroomGate:
    def _vram_row(
        self,
        peak_vram_gb: float | None,
        total_vram_gb: float | None,
    ) -> dict[str, Any]:
        r = _row()
        r["peak_vram_gb"] = peak_vram_gb
        r["total_vram_gb"] = total_vram_gb
        return r

    def test_low_headroom_disqualifies(self) -> None:
        # headroom = 24.0 - 23.7 = 0.3 < OOM_RISK_HEADROOM_GB
        rows = [self._vram_row(23.7, 24.0)]
        reason = _is_eligible(rows)
        assert reason is not None
        assert "OOM risk" in reason

    def test_headroom_at_threshold_passes(self) -> None:
        # headroom = 24.0 - 23.5 = 0.5 (exactly at threshold — not strictly less than)
        rows = [self._vram_row(24.0 - OOM_RISK_HEADROOM_GB, 24.0)]
        assert _is_eligible(rows) is None

    def test_sufficient_headroom_passes(self) -> None:
        rows = [self._vram_row(20.0, 24.0)]  # headroom = 4.0
        assert _is_eligible(rows) is None

    def test_no_peak_vram_skips_gate(self) -> None:
        rows = [self._vram_row(None, 24.0)]
        assert _is_eligible(rows) is None

    def test_no_total_vram_skips_gate(self) -> None:
        rows = [self._vram_row(23.9, None)]
        assert _is_eligible(rows) is None

    def test_oom_risk_checked_after_cpu_offload(self) -> None:
        # CPU offload takes priority over VRAM headroom gate
        r = self._vram_row(23.9, 24.0)
        r["cpu_offload_detected"] = True
        reason = _is_eligible([r])
        assert reason == "CPU offload"

    def test_oom_risk_constant_is_float(self) -> None:
        assert isinstance(OOM_RISK_HEADROOM_GB, float)
        assert OOM_RISK_HEADROOM_GB == 0.5


# ── Parametrized threshold configurability tests ──────────────────────────────


class TestParametrizedThresholdConfig:
    """Verify that min_useful_ctx and min_throughput_toks_per_s work independently
    — config that passes at default fails when raised, vice versa, and both args
    respected independently in both _is_eligible and print_ranking."""

    def _row(self, **kwargs: Any) -> dict[str, Any]:
        defaults = dict(
            repeat_index=1,
            outcome="ok",
            cpu_offload_detected=False,
            context_size=4096,
            throughput_tok_s=80.0,
            tier="speed",
            quality_task_success=None,
        )
        defaults.update(kwargs)
        return defaults

    # ── _is_eligible ───────────────────────────────────────────────────────────

    def test_ctx_threshold_default_passes(self) -> None:
        assert _is_eligible([self._row(context_size=4096)]) is None

    def test_ctx_threshold_raises_at_default(self) -> None:
        rows = [self._row(context_size=2048)]
        reason = _is_eligible(rows, min_useful_ctx=4096)
        assert reason is not None
        assert "context too small" in reason

    def test_throughput_threshold_default_passes(self) -> None:
        assert _is_eligible([self._row(throughput_tok_s=80.0)]) is None

    def test_throughput_threshold_raises_at_default(self) -> None:
        rows = [self._row(throughput_tok_s=3.0)]
        reason = _is_eligible(rows, min_throughput_toks_per_s=5.0)
        assert reason is not None
        assert "throughput too low" in reason

    def test_increasing_ctx_floor_always_stricter(self) -> None:
        """Raising min_useful_ctx should never make previously-eligible configs
        become eligible — only stricter."""
        rows_ctx_ok = [self._row(context_size=4096)]
        assert _is_eligible(rows_ctx_ok) is None
        assert _is_eligible(rows_ctx_ok, min_useful_ctx=8192) is not None, (
            "Raised floor should disqualify"
        )

    def test_increasing_throughput_floor_always_stricter(self) -> None:
        rows = [self._row(throughput_tok_s=80.0)]
        assert _is_eligible(rows) is None
        assert _is_eligible(rows, min_throughput_toks_per_s=100.0) is not None, (
            "Raised floor should disqualify"
        )

    def test_decreasing_ctx_floor_relaxes_strict(self) -> None:
        rows = [self._row(context_size=1024)]
        assert _is_eligible(rows, min_useful_ctx=4096) is not None
        assert _is_eligible(rows, min_useful_ctx=512) is None, (
            "Lowered floor should re-qualify"
        )

    def test_decreasing_throughput_floor_relaxes_strict(self) -> None:
        rows = [self._row(throughput_tok_s=3.0)]
        assert _is_eligible(rows, min_throughput_toks_per_s=5.0) is not None
        assert _is_eligible(rows, min_throughput_toks_per_s=1.0) is None, (
            "Lowered floor should re-qualify"
        )

    def test_ctx_and_throughput_respected_independently(self) -> None:
        """Raising only one threshold should not affect the other gate."""
        rows = [
            self._row(context_size=1024, throughput_tok_s=2.0),
        ]
        # Both thresholds high → rejected by ctx gate first
        reason = _is_eligible(rows, min_useful_ctx=4096, min_throughput_toks_per_s=5.0)
        assert reason is not None
        assert "context too small" in reason

        # Lower ctx threshold → ctx passes, throughput gate fires
        reason = _is_eligible(rows, min_useful_ctx=512, min_throughput_toks_per_s=5.0)
        assert reason is not None
        assert "throughput too low" in reason

        # Lower throughput threshold → both pass
        assert (
            _is_eligible(rows, min_useful_ctx=512, min_throughput_toks_per_s=1.0)
            is None
        )

    def test_print_ranking_respects_min_useful_ctx(self) -> None:
        rows = [
            dict(
                backend_id="b",
                model_id="m",
                context_size=2048,
                concurrency=1,
                repeat_index=1,
                ttft_s=0.3,
                throughput_tok_s=80.0,
                outcome="ok",
                cpu_offload_detected=False,
                tier="speed",
                quality_task_success=None,
            )
        ]
        output_default = _capture(rows)  # min_useful_ctx=4096 → ineligible
        assert "context too small" in output_default

        output_relaxed = _capture(rows, min_useful_ctx=1024)  # → eligible
        assert "INELIGIBLE CONFIGS" not in output_relaxed
        assert "RECOMMENDED" in output_relaxed

    def test_print_ranking_respects_min_throughput(self) -> None:
        rows = [
            dict(
                backend_id="b",
                model_id="m",
                context_size=4096,
                concurrency=1,
                repeat_index=1,
                ttft_s=0.3,
                throughput_tok_s=3.0,
                outcome="ok",
                cpu_offload_detected=False,
                tier="speed",
                quality_task_success=None,
            )
        ]
        output_default = _capture(rows)  # min_throughput=5.0 → ineligible
        assert "throughput too low" in output_default

        output_relaxed = _capture(rows, min_throughput_toks_per_s=1.0)  # → eligible
        assert "RECOMMENDED" in output_relaxed
        assert "INELIGIBLE CONFIGS" not in output_relaxed

    def test_both_thresholds_respected_independently_in_print_ranking(self) -> None:
        """Both parameters must work independently: raising one should not
        silently affect the other gate."""
        rows = [
            dict(
                backend_id="b",
                model_id="m",
                context_size=2048,
                concurrency=1,
                repeat_index=1,
                ttft_s=0.3,
                throughput_tok_s=3.0,
                outcome="ok",
                cpu_offload_detected=False,
                tier="speed",
                quality_task_success=None,
            )
        ]
        # Both strict → rejected by ctx gate first
        output_both_strict = _capture(
            rows, min_useful_ctx=4096, min_throughput_toks_per_s=5.0
        )
        assert "context too small" in output_both_strict

        # Relaxed ctx, strict throughput → ctx passes, throughput gate fires
        output_relaxed_ctx = _capture(
            rows, min_useful_ctx=1024, min_throughput_toks_per_s=5.0
        )
        assert "throughput too low" in output_relaxed_ctx
        assert "context too small" not in output_relaxed_ctx

        # Relaxed both → eligible
        output_both_relaxed = _capture(
            rows, min_useful_ctx=1024, min_throughput_toks_per_s=1.0
        )
        assert "RECOMMENDED" in output_both_relaxed
