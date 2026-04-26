"""Tests for reporter._metric_delta() and print_compare_table()."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from typing import Any

from scripts.bench.reporter import (
    _metric_delta,
    print_compare_table,
)


def _cmp_row(
    sweep_id: str,
    fp: str = "case1",
    *,
    model_id: str = "m",
    tier: str = "speed",
    context_size: int = 4096,
    concurrency: int = 1,
    repeat_index: int = 1,
    ttft_s: float | None = 1.0,
    throughput_tok_s: float | None = 100.0,
    outcome: str = "ok",
    cpu_offload_detected: bool = False,
) -> dict[str, Any]:
    return {
        "run_id": f"{sweep_id}::{fp}",
        "model_id": model_id,
        "tier": tier,
        "context_size": context_size,
        "concurrency": concurrency,
        "repeat_index": repeat_index,
        "ttft_s": ttft_s,
        "throughput_tok_s": throughput_tok_s,
        "outcome": outcome,
        "cpu_offload_detected": cpu_offload_detected,
    }


def _capture_compare(
    rows1: list[dict[str, Any]],
    rows2: list[dict[str, Any]],
    id1: str = "s1",
    id2: str = "s2",
    **kwargs: Any,
) -> tuple[str, bool]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = print_compare_table(rows1, rows2, id1, id2, **kwargs)
    return buf.getvalue(), result


# ── _metric_delta() unit tests ────────────────────────────────────────────────


class TestMetricDelta:
    def test_none_v1_returns_dash_no_regression(self) -> None:
        delta_str, is_reg = _metric_delta(
            None,
            1.0,
            abs_fmt="+.3f",
            higher_is_better=False,
            regression_threshold_pct=10.0,
        )
        assert delta_str == "--"
        assert is_reg is False

    def test_none_v2_returns_dash_no_regression(self) -> None:
        delta_str, is_reg = _metric_delta(
            1.0,
            None,
            abs_fmt="+.3f",
            higher_is_better=False,
            regression_threshold_pct=10.0,
        )
        assert delta_str == "--"
        assert is_reg is False

    def test_both_none_returns_dash_no_regression(self) -> None:
        delta_str, is_reg = _metric_delta(
            None,
            None,
            abs_fmt="+.3f",
            higher_is_better=False,
            regression_threshold_pct=10.0,
        )
        assert delta_str == "--"
        assert is_reg is False

    def test_zero_v1_returns_abs_with_dash_pct(self) -> None:
        delta_str, is_reg = _metric_delta(
            0.0,
            1.0,
            abs_fmt="+.3f",
            higher_is_better=False,
            regression_threshold_pct=10.0,
        )
        assert "(--)" in delta_str
        assert is_reg is False

    def test_increase_lower_is_better_above_threshold_is_regression(self) -> None:
        # TTFT: v1=1.0, v2=1.15 → +15% increase, threshold=10 → regression
        _, is_reg = _metric_delta(
            1.0,
            1.15,
            abs_fmt="+.3f",
            higher_is_better=False,
            regression_threshold_pct=10.0,
        )
        assert is_reg is True

    def test_increase_lower_is_better_below_threshold_not_regression(self) -> None:
        # TTFT: v1=1.0, v2=1.05 → +5% increase, threshold=10 → ok
        _, is_reg = _metric_delta(
            1.0,
            1.05,
            abs_fmt="+.3f",
            higher_is_better=False,
            regression_threshold_pct=10.0,
        )
        assert is_reg is False

    def test_decrease_lower_is_better_is_improvement_not_regression(self) -> None:
        # TTFT: v1=1.0, v2=0.5 → -50% (improvement)
        _, is_reg = _metric_delta(
            1.0,
            0.5,
            abs_fmt="+.3f",
            higher_is_better=False,
            regression_threshold_pct=10.0,
        )
        assert is_reg is False

    def test_decrease_higher_is_better_above_threshold_is_regression(self) -> None:
        # Tok/s: v1=100, v2=85 → -15%, threshold=10 → regression
        _, is_reg = _metric_delta(
            100.0,
            85.0,
            abs_fmt="+.0f",
            higher_is_better=True,
            regression_threshold_pct=10.0,
        )
        assert is_reg is True

    def test_decrease_higher_is_better_below_threshold_not_regression(self) -> None:
        # Tok/s: v1=100, v2=95 → -5%, threshold=10 → ok
        _, is_reg = _metric_delta(
            100.0,
            95.0,
            abs_fmt="+.0f",
            higher_is_better=True,
            regression_threshold_pct=10.0,
        )
        assert is_reg is False

    def test_increase_higher_is_better_is_improvement_not_regression(self) -> None:
        # Tok/s: v1=100, v2=150 → +50% (improvement)
        _, is_reg = _metric_delta(
            100.0,
            150.0,
            abs_fmt="+.0f",
            higher_is_better=True,
            regression_threshold_pct=10.0,
        )
        assert is_reg is False

    def test_delta_string_shows_absolute_and_pct(self) -> None:
        delta_str, _ = _metric_delta(
            1.0,
            1.2,
            abs_fmt="+.3f",
            higher_is_better=False,
            regression_threshold_pct=10.0,
        )
        assert "+0.200" in delta_str
        assert "+20%" in delta_str

    def test_custom_threshold_respected(self) -> None:
        # +8% increase, lower-is-better: regression at 5%, not at 10%
        _, is_reg_5 = _metric_delta(
            1.0,
            1.08,
            abs_fmt="+.3f",
            higher_is_better=False,
            regression_threshold_pct=5.0,
        )
        _, is_reg_10 = _metric_delta(
            1.0,
            1.08,
            abs_fmt="+.3f",
            higher_is_better=False,
            regression_threshold_pct=10.0,
        )
        assert is_reg_5 is True
        assert is_reg_10 is False

    def test_well_below_threshold_not_regression(self) -> None:
        # +9% increase (v1=100, v2=109), threshold=10 → clearly below → not a regression
        _, is_reg = _metric_delta(
            100.0,
            109.0,
            abs_fmt="+.3f",
            higher_is_better=False,
            regression_threshold_pct=10.0,
        )
        assert is_reg is False


# ── print_compare_table() basic tests ────────────────────────────────────────


class TestPrintCompareTableBasic:
    def test_no_rows_returns_false(self) -> None:
        _, result = _capture_compare([], [], "s1", "s2")
        assert result is False

    def test_no_rows_prints_message(self) -> None:
        output, _ = _capture_compare([], [], "s1", "s2")
        assert "No rows found" in output

    def test_returns_false_when_no_regression(self) -> None:
        r1 = _cmp_row("s1", ttft_s=1.0, throughput_tok_s=100.0)
        r2 = _cmp_row("s2", ttft_s=1.05, throughput_tok_s=95.0)  # <10% change
        _, result = _capture_compare([r1], [r2])
        assert result is False

    def test_no_regression_rows_have_no_bang_prefix(self) -> None:
        r1 = _cmp_row("s1", ttft_s=1.0, throughput_tok_s=100.0)
        r2 = _cmp_row("s2", ttft_s=1.05, throughput_tok_s=95.0)
        output, _ = _capture_compare([r1], [r2])
        assert "!!" not in output

    def test_header_includes_delta_columns(self) -> None:
        r1 = _cmp_row("s1")
        r2 = _cmp_row("s2")
        output, _ = _capture_compare([r1], [r2])
        assert "ΔTTFT" in output
        assert "ΔTok/s" in output

    def test_header_includes_tok_columns(self) -> None:
        r1 = _cmp_row("s1")
        r2 = _cmp_row("s2")
        output, _ = _capture_compare([r1], [r2])
        assert "Tok/s-1" in output
        assert "Tok/s-2" in output

    def test_regression_threshold_shown_in_header(self) -> None:
        r1 = _cmp_row("s1")
        r2 = _cmp_row("s2")
        output, _ = _capture_compare([r1], [r2], regression_threshold_pct=15.0)
        assert "15%" in output


# ── print_compare_table() regression detection tests ─────────────────────────


class TestPrintCompareTableRegressionDetection:
    def test_ttft_increase_above_threshold_returns_true(self) -> None:
        r1 = _cmp_row("s1", ttft_s=1.0, throughput_tok_s=100.0)
        r2 = _cmp_row("s2", ttft_s=1.15, throughput_tok_s=100.0)  # +15%
        _, result = _capture_compare([r1], [r2])
        assert result is True

    def test_ttft_regression_row_prefixed_with_bang(self) -> None:
        r1 = _cmp_row("s1", ttft_s=1.0, throughput_tok_s=100.0)
        r2 = _cmp_row("s2", ttft_s=1.15, throughput_tok_s=100.0)
        output, _ = _capture_compare([r1], [r2])
        assert "!!" in output

    def test_tok_decrease_above_threshold_returns_true(self) -> None:
        r1 = _cmp_row("s1", ttft_s=1.0, throughput_tok_s=100.0)
        r2 = _cmp_row("s2", ttft_s=1.0, throughput_tok_s=85.0)  # -15%
        _, result = _capture_compare([r1], [r2])
        assert result is True

    def test_tok_regression_row_prefixed_with_bang(self) -> None:
        r1 = _cmp_row("s1", ttft_s=1.0, throughput_tok_s=100.0)
        r2 = _cmp_row("s2", ttft_s=1.0, throughput_tok_s=85.0)
        output, _ = _capture_compare([r1], [r2])
        assert "!!" in output

    def test_ttft_improvement_not_flagged(self) -> None:
        r1 = _cmp_row("s1", ttft_s=1.0, throughput_tok_s=100.0)
        r2 = _cmp_row("s2", ttft_s=0.5, throughput_tok_s=100.0)  # TTFT improved
        _, result = _capture_compare([r1], [r2])
        assert result is False

    def test_tok_improvement_not_flagged(self) -> None:
        r1 = _cmp_row("s1", ttft_s=1.0, throughput_tok_s=100.0)
        r2 = _cmp_row("s2", ttft_s=1.0, throughput_tok_s=150.0)  # tok/s improved
        _, result = _capture_compare([r1], [r2])
        assert result is False

    def test_none_ttft_not_flagged_as_regression(self) -> None:
        r1 = _cmp_row("s1", ttft_s=None, throughput_tok_s=100.0)
        r2 = _cmp_row("s2", ttft_s=None, throughput_tok_s=100.0)
        _, result = _capture_compare([r1], [r2])
        assert result is False

    def test_none_tok_not_flagged_as_regression(self) -> None:
        r1 = _cmp_row("s1", ttft_s=1.0, throughput_tok_s=None)
        r2 = _cmp_row("s2", ttft_s=1.0, throughput_tok_s=None)
        _, result = _capture_compare([r1], [r2])
        assert result is False

    def test_regression_summary_shown_when_regression_detected(self) -> None:
        r1 = _cmp_row("s1", ttft_s=1.0, throughput_tok_s=100.0)
        r2 = _cmp_row("s2", ttft_s=1.5, throughput_tok_s=100.0)
        output, _ = _capture_compare([r1], [r2])
        assert "REGRESSIONS DETECTED" in output

    def test_no_summary_when_no_regression(self) -> None:
        r1 = _cmp_row("s1", ttft_s=1.0, throughput_tok_s=100.0)
        r2 = _cmp_row("s2", ttft_s=1.05, throughput_tok_s=100.0)
        output, _ = _capture_compare([r1], [r2])
        assert "REGRESSIONS DETECTED" not in output

    def test_custom_threshold_applied(self) -> None:
        # +8% TTFT: regression at threshold=5, not at threshold=10
        r1 = _cmp_row("s1", ttft_s=1.0, throughput_tok_s=100.0)
        r2 = _cmp_row("s2", ttft_s=1.08, throughput_tok_s=100.0)
        _, result_strict = _capture_compare([r1], [r2], regression_threshold_pct=5.0)
        assert result_strict is True
        _, result_lax = _capture_compare([r1], [r2], regression_threshold_pct=10.0)
        assert result_lax is False

    def test_multiple_rows_any_regression_returns_true(self) -> None:
        r1a = _cmp_row("s1", "case1", ttft_s=1.0, throughput_tok_s=100.0)
        r1b = _cmp_row("s1", "case2", ttft_s=1.0, throughput_tok_s=100.0)
        r2a = _cmp_row("s2", "case1", ttft_s=1.0, throughput_tok_s=100.0)  # ok
        r2b = _cmp_row("s2", "case2", ttft_s=1.5, throughput_tok_s=100.0)  # regression
        _, result = _capture_compare([r1a, r1b], [r2a, r2b])
        assert result is True


# ── print_compare_table() delta value tests ───────────────────────────────────


class TestPrintCompareTableDeltaValues:
    def test_delta_string_contains_absolute_and_pct(self) -> None:
        r1 = _cmp_row("s1", ttft_s=1.0, throughput_tok_s=100.0)
        r2 = _cmp_row("s2", ttft_s=1.2, throughput_tok_s=100.0)
        output, _ = _capture_compare([r1], [r2])
        # +20% TTFT increase: absolute ~+0.200, pct +20%
        assert "+20%" in output

    def test_tok_delta_string_contains_pct(self) -> None:
        r1 = _cmp_row("s1", ttft_s=1.0, throughput_tok_s=100.0)
        r2 = _cmp_row("s2", ttft_s=1.0, throughput_tok_s=80.0)
        output, _ = _capture_compare([r1], [r2])
        # -20% tok/s
        assert "-20%" in output

    def test_missing_row_in_sweep2_shows_dashes(self) -> None:
        r1 = _cmp_row("s1", "only_in_s1", ttft_s=1.0)
        output, result = _capture_compare([r1], [], "s1", "s2")
        assert "--" in output
        assert result is False
