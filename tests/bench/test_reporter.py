"""Tests for scripts/bench/reporter.py — _is_eligible() gates and print_ranking()."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from typing import Any

import pytest

from scripts.bench.reporter import (
    VRAM_HEADROOM_WARN_GB,
    _cv,
    _is_eligible,
    _percentile,
    compute_apc_speedup,
    compute_concurrency_efficiency,
    print_apc_section,
    print_ranking,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


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


def _capture(rows: list[dict[str, Any]], **kwargs: Any) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        print_ranking(rows, **kwargs)
    return buf.getvalue()


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


# ── print_ranking() integration tests ────────────────────────────────────────


class TestPrintRankingRejectionReasons:
    def _make_row(
        self,
        backend_id: str = "b",
        model_id: str = "m",
        context_size: int = 4096,
        concurrency: int = 1,
        repeat_index: int = 1,
        ttft_s: float = 0.3,
        throughput_tok_s: float = 80.0,
        outcome: str = "ok",
        cpu_offload_detected: bool = False,
    ) -> dict[str, Any]:
        return {
            "backend_id": backend_id,
            "model_id": model_id,
            "context_size": context_size,
            "concurrency": concurrency,
            "repeat_index": repeat_index,
            "ttft_s": ttft_s,
            "throughput_tok_s": throughput_tok_s,
            "outcome": outcome,
            "cpu_offload_detected": cpu_offload_detected,
            "tier": "speed",
            "quality_task_success": None,
        }

    def test_oom_config_shown_with_reason(self) -> None:
        rows = [
            self._make_row(model_id="good", ttft_s=0.2),
            self._make_row(model_id="bad", outcome="oom"),
        ]
        output = _capture(rows)
        assert "INELIGIBLE CONFIGS" in output
        assert "[OOM]" in output

    def test_small_ctx_config_shown_with_reason(self) -> None:
        rows = [
            self._make_row(model_id="good", context_size=4096, ttft_s=0.2),
            self._make_row(model_id="small", context_size=512),
        ]
        output = _capture(rows)
        assert "INELIGIBLE CONFIGS" in output
        assert "context too small" in output

    def test_low_throughput_config_shown_with_reason(self) -> None:
        rows = [
            self._make_row(model_id="good", throughput_tok_s=80.0, ttft_s=0.2),
            self._make_row(model_id="slow", throughput_tok_s=1.0),
        ]
        output = _capture(rows)
        assert "INELIGIBLE CONFIGS" in output
        assert "throughput too low" in output

    def test_all_ineligible_shows_ineligible_section(self) -> None:
        rows = [self._make_row(outcome="oom")]
        output = _capture(rows)
        assert "No quality-eligible configurations found" in output
        assert "INELIGIBLE CONFIGS" in output
        assert "[OOM]" in output

    def test_all_eligible_no_ineligible_section(self) -> None:
        rows = [self._make_row(ttft_s=0.2)]
        output = _capture(rows)
        assert "INELIGIBLE CONFIGS" not in output

    def test_empty_rows_no_output(self) -> None:
        output = _capture([])
        assert output == ""

    def test_custom_thresholds_propagated(self) -> None:
        rows = [self._make_row(throughput_tok_s=15.0)]
        # With default 5.0 — eligible
        output_default = _capture(rows)
        assert "INELIGIBLE CONFIGS" not in output_default
        # With raised floor of 20.0 — ineligible
        output_strict = _capture(rows, min_throughput_toks_per_s=20.0)
        assert "INELIGIBLE CONFIGS" in output_strict
        assert "throughput too low" in output_strict

    def test_custom_min_ctx_propagated(self) -> None:
        rows = [self._make_row(context_size=2048)]
        # With default 4096 — ineligible
        output_default = _capture(rows)
        assert "context too small" in output_default
        # With relaxed floor of 1024 — eligible
        output_relaxed = _capture(rows, min_useful_ctx=1024)
        assert "INELIGIBLE CONFIGS" not in output_relaxed

    def test_backward_compat_no_kwargs(self) -> None:
        rows = [self._make_row(ttft_s=0.5)]
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_ranking(rows)
        output = buf.getvalue()
        assert "RECOMMENDED" in output


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


# ── print_ranking() variance / stability tests ────────────────────────────────


class TestPrintRankingVariance:
    def _make_row(
        self,
        *,
        backend_id: str = "b",
        model_id: str = "m",
        context_size: int = 4096,
        concurrency: int = 1,
        repeat_index: int = 1,
        ttft_s: float = 0.3,
        throughput_tok_s: float = 80.0,
        outcome: str = "ok",
        cpu_offload_detected: bool = False,
    ) -> dict[str, Any]:
        return {
            "backend_id": backend_id,
            "model_id": model_id,
            "context_size": context_size,
            "concurrency": concurrency,
            "repeat_index": repeat_index,
            "ttft_s": ttft_s,
            "throughput_tok_s": throughput_tok_s,
            "outcome": outcome,
            "cpu_offload_detected": cpu_offload_detected,
            "tier": "speed",
            "quality_task_success": None,
        }

    def test_header_includes_new_columns(self) -> None:
        rows = [self._make_row()]
        output = _capture(rows)
        assert "TTFT p95(s)" in output
        assert "CV" in output
        assert "Stable" in output

    def test_single_repeat_shows_dashes_for_p95_cv_stable(self) -> None:
        rows = [self._make_row(ttft_s=0.3)]
        output = _capture(rows)
        # Single-repeat group: no [!] and no OK — stable column is "--"
        assert "[!]" not in output
        assert "OK" not in output

    def test_multi_repeat_stable_shows_ok(self) -> None:
        # ttft values [0.30, 0.31] → very low CV → stable
        rows = [
            self._make_row(repeat_index=1, ttft_s=0.30),
            self._make_row(repeat_index=2, ttft_s=0.31),
        ]
        output = _capture(rows)
        assert "OK" in output
        assert "[!]" not in output

    def test_multi_repeat_unstable_shows_warning(self) -> None:
        # ttft values [0.1, 1.9] → CV ≈ 1.27 > 0.3 → unstable
        rows = [
            self._make_row(repeat_index=1, ttft_s=0.1),
            self._make_row(repeat_index=2, ttft_s=1.9),
        ]
        output = _capture(rows)
        assert "[!]" in output

    def test_custom_cv_threshold_changes_stability(self) -> None:
        # ttft [0.2, 0.4]: mean=0.3, stdev≈0.141, CV≈0.471
        # default threshold 0.3 → unstable; relaxed threshold 0.5 → stable
        rows = [
            self._make_row(repeat_index=1, ttft_s=0.2),
            self._make_row(repeat_index=2, ttft_s=0.4),
        ]
        output_strict = _capture(rows)  # cv_threshold=0.3 (default)
        assert "[!]" in output_strict

        output_relaxed = _capture(rows, cv_threshold=0.5)
        assert "OK" in output_relaxed

    def test_p95_appears_in_recommendation_for_multi_repeat(self) -> None:
        rows = [
            self._make_row(repeat_index=1, ttft_s=0.3),
            self._make_row(repeat_index=2, ttft_s=0.32),
        ]
        output = _capture(rows)
        assert "TTFT p95=" in output

    def test_p95_absent_from_recommendation_for_single_repeat(self) -> None:
        rows = [self._make_row(ttft_s=0.3)]
        output = _capture(rows)
        assert "TTFT p95=" not in output


# ── APC speedup tests ─────────────────────────────────────────────────────────


def _prefill_row(
    tier: str,
    *,
    model_id: str = "m",
    context_size: int = 4096,
    concurrency: int = 1,
    repeat_index: int = 1,
    ttft_s: float | None = 1.0,
    prompt_eval_duration_s: float | None = None,
    backend_id: str = "b",
) -> dict[str, Any]:
    return {
        "tier": tier,
        "model_id": model_id,
        "context_size": context_size,
        "concurrency": concurrency,
        "repeat_index": repeat_index,
        "ttft_s": ttft_s,
        "prompt_eval_duration_s": prompt_eval_duration_s,
        "backend_id": backend_id,
    }


class TestComputeApcSpeedup:
    def test_both_tiers_computes_ratio(self) -> None:
        rows = [
            _prefill_row("prefill_shared", ttft_s=1.0),
            _prefill_row("prefill_unshared", ttft_s=2.0),
        ]
        results = compute_apc_speedup(rows)
        assert len(results) == 1
        r = results[0]
        assert r["shared_ttft_p50"] == pytest.approx(1.0)
        assert r["unshared_ttft_p50"] == pytest.approx(2.0)
        assert r["apc_speedup_ratio"] == pytest.approx(2.0)

    def test_only_shared_ratio_is_none(self) -> None:
        rows = [_prefill_row("prefill_shared", ttft_s=0.5)]
        results = compute_apc_speedup(rows)
        assert len(results) == 1
        assert results[0]["apc_speedup_ratio"] is None
        assert results[0]["unshared_ttft_p50"] is None

    def test_only_unshared_ratio_is_none(self) -> None:
        rows = [_prefill_row("prefill_unshared", ttft_s=2.0)]
        results = compute_apc_speedup(rows)
        assert len(results) == 1
        assert results[0]["apc_speedup_ratio"] is None
        assert results[0]["shared_ttft_p50"] is None

    def test_zero_shared_ttft_ratio_is_none(self) -> None:
        rows = [
            _prefill_row("prefill_shared", ttft_s=0.0),
            _prefill_row("prefill_unshared", ttft_s=2.0),
        ]
        results = compute_apc_speedup(rows)
        assert results[0]["apc_speedup_ratio"] is None

    def test_uses_prompt_eval_duration_s_over_ttft_s(self) -> None:
        rows = [
            _prefill_row("prefill_shared", ttft_s=1.0, prompt_eval_duration_s=0.5),
            _prefill_row("prefill_unshared", ttft_s=2.0, prompt_eval_duration_s=1.5),
        ]
        results = compute_apc_speedup(rows)
        r = results[0]
        assert r["shared_ttft_p50"] == pytest.approx(0.5)
        assert r["unshared_ttft_p50"] == pytest.approx(1.5)
        assert r["apc_speedup_ratio"] == pytest.approx(3.0)

    def test_falls_back_to_ttft_s_when_no_prompt_eval(self) -> None:
        rows = [
            _prefill_row("prefill_shared", ttft_s=0.8, prompt_eval_duration_s=None),
            _prefill_row("prefill_unshared", ttft_s=1.6, prompt_eval_duration_s=None),
        ]
        results = compute_apc_speedup(rows)
        assert results[0]["shared_ttft_p50"] == pytest.approx(0.8)
        assert results[0]["apc_speedup_ratio"] == pytest.approx(2.0)

    def test_warmup_runs_excluded(self) -> None:
        rows = [
            _prefill_row("prefill_shared", ttft_s=0.5, repeat_index=0),
            _prefill_row("prefill_unshared", ttft_s=2.0, repeat_index=0),
        ]
        assert compute_apc_speedup(rows) == []

    def test_non_prefill_tiers_ignored(self) -> None:
        rows = [
            _prefill_row("speed", ttft_s=0.5),
            _prefill_row("coding", ttft_s=1.0),
            _prefill_row("prefill_shared", ttft_s=0.5),
        ]
        results = compute_apc_speedup(rows)
        assert len(results) == 1

    def test_multiple_configs_grouped_separately(self) -> None:
        rows = [
            _prefill_row("prefill_shared", model_id="A", context_size=4096, ttft_s=1.0),
            _prefill_row(
                "prefill_unshared", model_id="A", context_size=4096, ttft_s=3.0
            ),
            _prefill_row("prefill_shared", model_id="B", context_size=8192, ttft_s=2.0),
            _prefill_row(
                "prefill_unshared", model_id="B", context_size=8192, ttft_s=4.0
            ),
        ]
        results = compute_apc_speedup(rows)
        assert len(results) == 2
        a = next(r for r in results if r["model"] == "A")
        b = next(r for r in results if r["model"] == "B")
        assert a["apc_speedup_ratio"] == pytest.approx(3.0)
        assert b["apc_speedup_ratio"] == pytest.approx(2.0)

    def test_median_used_for_multiple_runs(self) -> None:
        rows = [
            _prefill_row("prefill_shared", ttft_s=1.0, repeat_index=1),
            _prefill_row("prefill_shared", ttft_s=3.0, repeat_index=2),
            _prefill_row("prefill_unshared", ttft_s=4.0, repeat_index=1),
        ]
        results = compute_apc_speedup(rows)
        # median of [1.0, 3.0] = 2.0; ratio = 4.0 / 2.0 = 2.0
        assert results[0]["shared_ttft_p50"] == pytest.approx(2.0)
        assert results[0]["apc_speedup_ratio"] == pytest.approx(2.0)

    def test_empty_rows_returns_empty(self) -> None:
        assert compute_apc_speedup([]) == []


class TestPrintApcSection:
    def _capture_apc(self, rows: list[dict[str, Any]]) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_apc_section(rows)
        return buf.getvalue()

    def test_no_prefill_rows_no_output(self) -> None:
        rows = [_row(tier="speed")]
        assert self._capture_apc(rows) == ""

    def test_shows_apc_header(self) -> None:
        rows = [
            _prefill_row("prefill_shared", ttft_s=0.5),
            _prefill_row("prefill_unshared", ttft_s=1.5),
        ]
        output = self._capture_apc(rows)
        assert "APC EFFECTIVENESS" in output
        assert "Speedup" in output

    def test_shows_na_for_missing_tier(self) -> None:
        rows = [_prefill_row("prefill_shared", ttft_s=0.5)]
        output = self._capture_apc(rows)
        assert "N/A" in output

    def test_shows_speedup_ratio(self) -> None:
        rows = [
            _prefill_row("prefill_shared", ttft_s=1.0),
            _prefill_row("prefill_unshared", ttft_s=3.0),
        ]
        output = self._capture_apc(rows)
        assert "3.00x" in output

    def test_ranking_includes_apc_section_when_prefill_rows_present(self) -> None:
        rows = [
            _prefill_row("prefill_shared", ttft_s=0.5),
            _prefill_row("prefill_unshared", ttft_s=1.5),
        ]
        output = _capture(rows)
        assert "APC EFFECTIVENESS" in output


# ── compute_concurrency_efficiency() unit tests ───────────────────────────────


def _eff_row(
    *,
    backend_id: str = "b",
    model_id: str = "m",
    context_size: int = 4096,
    concurrency: int = 1,
    repeat_index: int = 1,
    throughput_tok_s: float | None = 100.0,
) -> dict[str, Any]:
    return {
        "backend_id": backend_id,
        "model_id": model_id,
        "context_size": context_size,
        "concurrency": concurrency,
        "repeat_index": repeat_index,
        "throughput_tok_s": throughput_tok_s,
    }


class TestComputeConcurrencyEfficiency:
    def test_near_linear_scaling(self) -> None:
        rows = [
            _eff_row(concurrency=1, throughput_tok_s=100.0),
            _eff_row(concurrency=2, throughput_tok_s=190.0),
        ]
        result = compute_concurrency_efficiency(rows)
        assert result[("b", "m", 4096, 2)] == pytest.approx(0.95)

    def test_no_baseline_returns_none(self) -> None:
        rows = [_eff_row(concurrency=2, throughput_tok_s=200.0)]
        result = compute_concurrency_efficiency(rows)
        assert result[("b", "m", 4096, 2)] is None

    def test_zero_baseline_returns_none(self) -> None:
        rows = [
            _eff_row(concurrency=1, throughput_tok_s=0.0),
            _eff_row(concurrency=2, throughput_tok_s=100.0),
        ]
        result = compute_concurrency_efficiency(rows)
        assert result[("b", "m", 4096, 2)] is None

    def test_none_throughput_baseline_returns_none(self) -> None:
        # concurrency=1 row has None throughput → no baseline built
        rows = [
            _eff_row(concurrency=1, throughput_tok_s=None),
            _eff_row(concurrency=2, throughput_tok_s=100.0),
        ]
        result = compute_concurrency_efficiency(rows)
        assert result[("b", "m", 4096, 2)] is None

    def test_concurrency_1_not_in_result(self) -> None:
        rows = [_eff_row(concurrency=1, throughput_tok_s=100.0)]
        result = compute_concurrency_efficiency(rows)
        assert ("b", "m", 4096, 1) not in result

    def test_super_linear_not_clamped(self) -> None:
        rows = [
            _eff_row(concurrency=1, throughput_tok_s=50.0),
            _eff_row(concurrency=2, throughput_tok_s=150.0),
        ]
        result = compute_concurrency_efficiency(rows)
        eff = result[("b", "m", 4096, 2)]
        assert eff is not None
        assert eff == pytest.approx(1.5)

    def test_warmup_runs_excluded(self) -> None:
        # Only warmup (repeat_index=0) run for c=1 → no baseline
        rows = [
            _eff_row(concurrency=1, repeat_index=0, throughput_tok_s=100.0),
            _eff_row(concurrency=2, repeat_index=1, throughput_tok_s=200.0),
        ]
        result = compute_concurrency_efficiency(rows)
        assert result[("b", "m", 4096, 2)] is None

    def test_median_used_for_multiple_repeats(self) -> None:
        rows = [
            _eff_row(concurrency=1, repeat_index=1, throughput_tok_s=80.0),
            _eff_row(concurrency=1, repeat_index=2, throughput_tok_s=120.0),
            _eff_row(concurrency=2, repeat_index=1, throughput_tok_s=180.0),
        ]
        result = compute_concurrency_efficiency(rows)
        # median c=1 = 100.0; eff = 180.0 / (2 * 100.0) = 0.90
        assert result[("b", "m", 4096, 2)] == pytest.approx(0.90)

    def test_empty_rows_returns_empty_dict(self) -> None:
        assert compute_concurrency_efficiency([]) == {}

    def test_multiple_models_grouped_separately(self) -> None:
        rows = [
            _eff_row(model_id="A", concurrency=1, throughput_tok_s=100.0),
            _eff_row(model_id="A", concurrency=2, throughput_tok_s=160.0),
            _eff_row(model_id="B", concurrency=1, throughput_tok_s=200.0),
            _eff_row(model_id="B", concurrency=2, throughput_tok_s=300.0),
        ]
        result = compute_concurrency_efficiency(rows)
        assert result[("b", "A", 4096, 2)] == pytest.approx(0.80)
        assert result[("b", "B", 4096, 2)] == pytest.approx(0.75)

    def test_different_backends_use_own_baseline(self) -> None:
        rows = [
            _eff_row(backend_id="x", concurrency=1, throughput_tok_s=100.0),
            _eff_row(backend_id="x", concurrency=2, throughput_tok_s=160.0),
            _eff_row(backend_id="y", concurrency=1, throughput_tok_s=50.0),
            _eff_row(backend_id="y", concurrency=2, throughput_tok_s=60.0),
        ]
        result = compute_concurrency_efficiency(rows)
        assert result[("x", "m", 4096, 2)] == pytest.approx(0.80)
        assert result[("y", "m", 4096, 2)] == pytest.approx(0.60)


# ── print_ranking() VRAM headroom column tests ────────────────────────────────


class TestPrintRankingVramHeadroom:
    def _make_row(
        self,
        *,
        model_id: str = "m",
        backend_id: str = "b",
        context_size: int = 4096,
        concurrency: int = 1,
        repeat_index: int = 1,
        ttft_s: float = 0.3,
        throughput_tok_s: float = 80.0,
        outcome: str = "ok",
        cpu_offload_detected: bool = False,
        peak_vram_gb: float | None = 20.0,
        total_vram_gb: float | None = 24.0,
    ) -> dict[str, Any]:
        return {
            "model_id": model_id,
            "backend_id": backend_id,
            "context_size": context_size,
            "concurrency": concurrency,
            "repeat_index": repeat_index,
            "ttft_s": ttft_s,
            "throughput_tok_s": throughput_tok_s,
            "outcome": outcome,
            "cpu_offload_detected": cpu_offload_detected,
            "tier": "speed",
            "quality_task_success": None,
            "peak_vram_gb": peak_vram_gb,
            "total_vram_gb": total_vram_gb,
        }

    def test_header_includes_vram_headroom_column(self) -> None:
        rows = [self._make_row()]
        output = _capture(rows)
        assert "VRAM Hdrm" in output

    def test_headroom_value_computed_and_shown(self) -> None:
        # total=24.0, peak=20.0 → headroom=4.0
        rows = [self._make_row(total_vram_gb=24.0, peak_vram_gb=20.0)]
        output = _capture(rows)
        assert "4.0" in output

    def test_low_headroom_shows_warning_indicator(self) -> None:
        # headroom = 24.0 - 22.5 = 1.5 < VRAM_HEADROOM_WARN_GB
        rows = [self._make_row(total_vram_gb=24.0, peak_vram_gb=22.5)]
        output = _capture(rows)
        assert "1.5[!]" in output

    def test_headroom_at_warn_threshold_no_warning(self) -> None:
        # headroom exactly at threshold — no warning
        rows = [
            self._make_row(
                total_vram_gb=24.0,
                peak_vram_gb=24.0 - VRAM_HEADROOM_WARN_GB,
            )
        ]
        output = _capture(rows)
        assert f"{VRAM_HEADROOM_WARN_GB:.1f}" in output
        assert "[!]" not in output

    def test_none_peak_vram_shows_na(self) -> None:
        rows = [self._make_row(peak_vram_gb=None, total_vram_gb=24.0)]
        output = _capture(rows)
        assert "N/A" in output

    def test_none_total_vram_shows_na(self) -> None:
        rows = [self._make_row(peak_vram_gb=20.0, total_vram_gb=None)]
        output = _capture(rows)
        assert "N/A" in output

    def test_both_none_shows_na(self) -> None:
        rows = [self._make_row(peak_vram_gb=None, total_vram_gb=None)]
        output = _capture(rows)
        assert "N/A" in output

    def test_vram_headroom_warn_gb_constant_is_float(self) -> None:
        assert isinstance(VRAM_HEADROOM_WARN_GB, float)
        assert VRAM_HEADROOM_WARN_GB == 2.0


# ── print_ranking() concurrency efficiency column tests ───────────────────────


class TestPrintRankingConcurrencyEfficiency:
    def _make_row(
        self,
        *,
        model_id: str = "m",
        backend_id: str = "b",
        context_size: int = 4096,
        concurrency: int = 1,
        repeat_index: int = 1,
        ttft_s: float = 0.3,
        throughput_tok_s: float = 100.0,
        outcome: str = "ok",
        cpu_offload_detected: bool = False,
    ) -> dict[str, Any]:
        return {
            "model_id": model_id,
            "backend_id": backend_id,
            "context_size": context_size,
            "concurrency": concurrency,
            "repeat_index": repeat_index,
            "ttft_s": ttft_s,
            "throughput_tok_s": throughput_tok_s,
            "outcome": outcome,
            "cpu_offload_detected": cpu_offload_detected,
            "tier": "speed",
            "quality_task_success": None,
        }

    def test_header_contains_conc_eff(self) -> None:
        rows = [self._make_row()]
        output = _capture(rows)
        assert "Conc.Eff" in output

    def test_concurrency_1_shows_na(self) -> None:
        rows = [self._make_row(concurrency=1)]
        output = _capture(rows)
        assert "N/A" in output

    def test_concurrency_gt1_with_baseline_shows_ratio(self) -> None:
        rows = [
            self._make_row(concurrency=1, throughput_tok_s=100.0, ttft_s=0.3),
            self._make_row(concurrency=2, throughput_tok_s=150.0, ttft_s=0.4),
        ]
        output = _capture(rows)
        # efficiency = 150 / (2 * 100) = 0.750
        assert "0.750" in output

    def test_concurrency_gt1_without_baseline_shows_na(self) -> None:
        rows = [self._make_row(concurrency=2, throughput_tok_s=150.0)]
        output = _capture(rows)
        assert "N/A" in output

    def test_super_linear_efficiency_displayed(self) -> None:
        rows = [
            self._make_row(concurrency=1, throughput_tok_s=50.0, ttft_s=0.3),
            self._make_row(concurrency=2, throughput_tok_s=150.0, ttft_s=0.4),
        ]
        output = _capture(rows)
        # efficiency = 150 / (2 * 50) = 1.500
        assert "1.500" in output
