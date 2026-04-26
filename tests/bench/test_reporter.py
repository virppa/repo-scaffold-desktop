"""Tests for scripts/bench/reporter.py — _is_eligible() gates and print_ranking()."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from typing import Any

from scripts.bench.reporter import _is_eligible, print_ranking

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
