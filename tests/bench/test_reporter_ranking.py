"""Tests for reporter.print_ranking() — rejection reasons, variance, VRAM headroom,
and concurrency efficiency columns."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from typing import Any

from scripts.bench.reporter import (
    VRAM_HEADROOM_WARN_GB,
    print_ranking,
)


def _capture(rows: list[dict[str, Any]], **kwargs: Any) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        print_ranking(rows, **kwargs)
    return buf.getvalue()


# ── print_ranking() rejection-reason integration tests ───────────────────────


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


# ── print_ranking() variance / stability column tests ─────────────────────────


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
