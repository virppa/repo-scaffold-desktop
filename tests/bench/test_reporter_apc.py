"""Tests for reporter.compute_apc_speedup(), compute_concurrency_efficiency(),
and print_apc_section()."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from typing import Any

import pytest

from scripts.bench.reporter import (
    compute_apc_speedup,
    compute_concurrency_efficiency,
    print_apc_section,
    print_concurrency_scaling_section,
    print_ranking,
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


def _capture(rows: list[dict[str, Any]], **kwargs: Any) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        print_ranking(rows, **kwargs)
    return buf.getvalue()


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


# ── compute_apc_speedup() unit tests ─────────────────────────────────────────


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


# ── print_apc_section() integration tests ────────────────────────────────────


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


# ── print_apc_section() conditional label tests ───────────────────────────────


class TestPrintApcSectionLabels:
    def _capture_apc(self, rows: list[dict[str, Any]]) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_apc_section(rows)
        return buf.getvalue()

    def test_speedup_above_1p5_shows_apc_effective(self) -> None:
        rows = [
            _prefill_row("prefill_shared", ttft_s=1.0),
            _prefill_row("prefill_unshared", ttft_s=2.0),  # 2.0x > 1.5
        ]
        assert "APC effective" in self._capture_apc(rows)

    def test_speedup_below_1p1_shows_no_apc_benefit(self) -> None:
        rows = [
            _prefill_row("prefill_shared", ttft_s=1.0),
            _prefill_row("prefill_unshared", ttft_s=1.05),  # 1.05x < 1.1
        ]
        assert "no APC benefit" in self._capture_apc(rows)

    def test_speedup_between_1p1_and_1p5_shows_no_label(self) -> None:
        rows = [
            _prefill_row("prefill_shared", ttft_s=1.0),
            _prefill_row("prefill_unshared", ttft_s=1.3),  # 1.3x
        ]
        output = self._capture_apc(rows)
        assert "APC effective" not in output
        assert "no APC benefit" not in output

    def test_na_ratio_shows_no_label(self) -> None:
        rows = [_prefill_row("prefill_shared", ttft_s=0.5)]
        output = self._capture_apc(rows)
        assert "APC effective" not in output
        assert "no APC benefit" not in output

    def test_exactly_1p5x_no_label(self) -> None:
        # boundary: ratio == 1.5 (not strictly > 1.5)
        rows = [
            _prefill_row("prefill_shared", ttft_s=1.0),
            _prefill_row("prefill_unshared", ttft_s=1.5),
        ]
        output = self._capture_apc(rows)
        assert "APC effective" not in output

    def test_exactly_1p1x_no_label(self) -> None:
        # boundary: ratio == 1.1 (not strictly < 1.1)
        rows = [
            _prefill_row("prefill_shared", ttft_s=1.0),
            _prefill_row("prefill_unshared", ttft_s=1.1),
        ]
        output = self._capture_apc(rows)
        assert "no APC benefit" not in output


# ── print_concurrency_scaling_section() tests ────────────────────────────────


class TestPrintConcurrencyScalingSection:
    def _capture_scaling(self, rows: list[dict[str, Any]]) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_concurrency_scaling_section(rows)
        return buf.getvalue()

    def test_no_concurrency_gt1_no_output(self) -> None:
        assert self._capture_scaling([_eff_row(concurrency=1)]) == ""

    def test_no_baseline_no_output(self) -> None:
        assert self._capture_scaling([_eff_row(concurrency=2)]) == ""

    def test_shows_concurrency_scaling_header(self) -> None:
        rows = [
            _eff_row(concurrency=1, throughput_tok_s=100.0),
            _eff_row(concurrency=2, throughput_tok_s=80.0),
        ]
        assert "CONCURRENCY SCALING" in self._capture_scaling(rows)

    def test_low_efficiency_shows_serialised(self) -> None:
        # eff = 80 / (2 * 100) = 0.40 < 0.5
        rows = [
            _eff_row(concurrency=1, throughput_tok_s=100.0),
            _eff_row(concurrency=2, throughput_tok_s=80.0),
        ]
        assert "serialised" in self._capture_scaling(rows)

    def test_high_efficiency_shows_scales_well(self) -> None:
        # eff = 180 / (2 * 100) = 0.90 > 0.8
        rows = [
            _eff_row(concurrency=1, throughput_tok_s=100.0),
            _eff_row(concurrency=2, throughput_tok_s=180.0),
        ]
        assert "scales well" in self._capture_scaling(rows)

    def test_medium_efficiency_no_label(self) -> None:
        # eff = 140 / (2 * 100) = 0.70 (between 0.5 and 0.8)
        rows = [
            _eff_row(concurrency=1, throughput_tok_s=100.0),
            _eff_row(concurrency=2, throughput_tok_s=140.0),
        ]
        output = self._capture_scaling(rows)
        assert "serialised" not in output
        assert "scales well" not in output

    def test_efficiency_value_shown(self) -> None:
        rows = [
            _eff_row(concurrency=1, throughput_tok_s=100.0),
            _eff_row(concurrency=2, throughput_tok_s=150.0),  # eff=0.750
        ]
        assert "0.750" in self._capture_scaling(rows)

    def test_ranking_includes_scaling_section_when_concurrency_gt1(self) -> None:
        base = {
            "repeat_index": 1,
            "outcome": "ok",
            "cpu_offload_detected": False,
            "context_size": 4096,
            "tier": "speed",
            "quality_task_success": None,
            "backend_id": "b",
            "model_id": "m",
            "ttft_s": 0.3,
        }
        rows = [
            {**base, "concurrency": 1, "throughput_tok_s": 100.0},
            {**base, "concurrency": 2, "throughput_tok_s": 80.0},
        ]
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_ranking(rows)
        assert "CONCURRENCY SCALING" in buf.getvalue()
