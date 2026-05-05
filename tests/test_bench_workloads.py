"""Tests for scripts/bench/workloads.py."""

from __future__ import annotations

from pathlib import Path

from scripts.bench.workloads import (
    PhaseConfig,
    WatcherPatternWorkload,
    _load_session_config,
    run_workload_session,
)

_TEST_CONFIG = Path(__file__).parents[1] / "config" / "bench-watcher-pattern.toml"


def _make_phase(name: str, n_turns: int, **kw) -> PhaseConfig:
    defaults = {
        "message_template": "Turn {turn}: task {idx}",
        "task_template": "do task {idx}",
        "tool_result_size_base": 200,
        "tool_result_size_growth": 10,
        "post_context_template": "",
        "tool_result_is_summary": False,
        "tool_result_size": 500,
    }
    defaults.update(kw)
    return PhaseConfig(name=name, n_turns=n_turns, **defaults)


# ── Config loading ──────────────────────────────────────────────────────────


class TestLoadSessionConfig:
    def test_loads_from_toml(self):
        cfg = _load_session_config(_TEST_CONFIG)
        assert cfg.n_turns == 100
        assert cfg.compaction_turn == 60
        assert len(cfg.phases) == 3
        assert cfg.max_tool_result_size == 800

    def test_phases_sum_to_n_turns(self):
        cfg = _load_session_config(_TEST_CONFIG)
        phase_turns = sum(p.n_turns for p in cfg.phases)
        assert phase_turns == cfg.n_turns

    def test_phase_names(self):
        cfg = _load_session_config(_TEST_CONFIG)
        names = [p.name for p in cfg.phases]
        assert names == ["pre-compact", "compact", "post-compact"]

    def test_compaction_turn_matches_phases(self):
        cfg = _load_session_config(_TEST_CONFIG)
        pre_n = next(p.n_turns for p in cfg.phases if p.name == "pre-compact")
        assert cfg.compaction_turn == pre_n


# ── WatcherPatternWorkload.generate_turns ───────────────────────────────────


class TestGenerateTurns:
    def setup_method(self):
        self.workload = WatcherPatternWorkload()

    def test_generates_correct_turn_count(self):
        cfg = _load_session_config(_TEST_CONFIG)
        turns, _ = self.workload.generate_turns(cfg)
        assert len(turns) == cfg.n_turns

    def test_turns_have_phase_ordering(self):
        cfg = _load_session_config(_TEST_CONFIG)
        turns, _ = self.workload.generate_turns(cfg)
        phase_order = [t.phase_name for t in turns]
        expected = ["pre-compact"] * 60 + ["compact"] * 1 + ["post-compact"] * 39
        assert phase_order == expected

    def test_compaction_returned(self):
        cfg = _load_session_config(_TEST_CONFIG)
        turns, compaction = self.workload.generate_turns(cfg)
        assert compaction is not None
        assert "raw_count" in compaction
        assert "summary_size" in compaction
        assert compaction["raw_count"] == 60

    def test_turn_indices_are_sequential(self):
        cfg = _load_session_config(_TEST_CONFIG)
        turns, _ = self.workload.generate_turns(cfg)
        indices = [t.turn_index for t in turns]
        assert indices == list(range(cfg.n_turns))

    def test_pre_compact_growing_tool_result_size(self):
        cfg = _load_session_config(_TEST_CONFIG)
        turns, _ = self.workload.generate_turns(cfg)
        pre = [t for t in turns if t.phase_name == "pre-compact"]
        sizes = [t.tool_result_size_chars for t in pre]
        # First turn should be smaller than last
        assert sizes[0] < sizes[-1]

    def test_post_compact_tool_results_smaller(self):
        cfg = _load_session_config(_TEST_CONFIG)
        turns, _ = self.workload.generate_turns(cfg)
        post = [t for t in turns if t.phase_name == "post-compact"]
        sizes = [t.tool_result_size_chars for t in post]
        assert all(0 < s < 1000 for s in sizes)

    def test_compact_turn_has_summary_size(self):
        cfg = _load_session_config(_TEST_CONFIG)
        turns, _ = self.workload.generate_turns(cfg)
        compact = [t for t in turns if t.phase_name == "compact"]
        assert len(compact) == 1
        # Summary should be larger than a single tool_result
        assert compact[0].tool_result_size_chars > 100

    def test_message_sizes_nonzero(self):
        cfg = _load_session_config(_TEST_CONFIG)
        turns, _ = self.workload.generate_turns(cfg)
        assert all(t.message_size_chars > 0 for t in turns)


# ── Phase validation ────────────────────────────────────────────────────────


class TestPhaseConfig:
    def test_defaults(self):
        p = _make_phase("test", 10)
        assert p.message_template == "Turn {turn}: task {idx}"
        assert p.tool_result_size_base == 200
        assert p.tool_result_size_growth == 10

    def test_custom_values(self):
        p = _make_phase("test", 5, message_template="X {y}", tool_result_size_base=100)
        assert p.message_template == "X {y}"
        assert p.tool_result_size_base == 100


# ── run_workload_session (no-op, since no driver is available) ─────────────


class TestRunWorkloadSession:
    def test_empty_base_url_skips_execution(self):
        workload = WatcherPatternWorkload()
        cfg = _load_session_config(_TEST_CONFIG)
        result = run_workload_session(
            workload,
            cfg,
            base_url="",
            model="qwen3-coder",
            report=False,
        )
        assert result.n_turns == cfg.n_turns
        assert len(result.phases) == 3
        assert result.any_failure is False

    def test_phase_names_in_result(self):
        workload = WatcherPatternWorkload()
        cfg = _load_session_config(_TEST_CONFIG)
        result = run_workload_session(
            workload,
            cfg,
            base_url="",
            model="qwen3-coder",
            report=False,
        )
        phase_names = [p.name for p in result.phases]
        assert phase_names == ["pre-compact", "compact", "post-compact"]

    def test_turn_count_matches_config(self):
        workload = WatcherPatternWorkload()
        cfg = _load_session_config(_TEST_CONFIG)
        result = run_workload_session(
            workload,
            cfg,
            base_url="",
            model="qwen3-coder",
            report=False,
        )
        assert result.n_turns == cfg.n_turns

    def test_compaction_in_result(self):
        workload = WatcherPatternWorkload()
        cfg = _load_session_config(_TEST_CONFIG)
        result = run_workload_session(
            workload,
            cfg,
            base_url="",
            model="qwen3-coder",
            report=False,
        )
        assert result.compaction is not None
        assert result.compaction["raw_count"] == 60

    def test_avg_tool_result_size_computed(self):
        workload = WatcherPatternWorkload()
        cfg = _load_session_config(_TEST_CONFIG)
        result = run_workload_session(
            workload,
            cfg,
            base_url="",
            model="qwen3-coder",
            report=False,
        )
        assert result.avg_tool_result_size is not None
        assert result.avg_tool_result_size > 0

    def test_summary_message_size_computed(self):
        workload = WatcherPatternWorkload()
        cfg = _load_session_config(_TEST_CONFIG)
        result = run_workload_session(
            workload,
            cfg,
            base_url="",
            model="qwen3-coder",
            report=False,
        )
        assert result.summary_message_size is not None
        assert result.summary_message_size > 0
