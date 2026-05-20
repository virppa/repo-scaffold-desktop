"""WOR-336 — tests for the KV-budget concurrency-ceiling helper.

Pure-function tests. The helper encodes the spike's central finding:
the local-worker throughput limit is KV-cache bytes (prefix-cache
eviction under oversubscription), not ``--max-num-seqs`` sequence slots.
"""

from __future__ import annotations

import pytest

from app.core.watcher.watcher_helpers import (
    COMPACTION_CONTEXT_CEILING,
    PRODUCTION_KV_CACHE_TOKENS,
    kv_concurrency_ceiling,
)


class TestKvConcurrencyCeiling:
    def test_heavy_worker_at_compaction_ceiling_is_solo(self) -> None:
        """A worker near the ~134k compaction ceiling fills the pool —
        only one fits at the default 0.9 utilization target."""
        assert kv_concurrency_ceiling(COMPACTION_CONTEXT_CEILING) == 1

    def test_light_worker_scales_up(self) -> None:
        """A 30k-token worker leaves room for several concurrent peers."""
        # floor(173968 * 0.9 / 30000) = floor(5.22) = 5
        assert kv_concurrency_ceiling(30_000) == 5

    def test_mid_worker(self) -> None:
        # floor(173968 * 0.9 / 67000) = floor(2.34) = 2
        assert kv_concurrency_ceiling(67_000) == 2
        # at full utilisation two mid workers still fit
        assert kv_concurrency_ceiling(67_000, utilization_target=1.0) == 2

    def test_monotonic_decreasing_in_context_size(self) -> None:
        """Larger per-worker context never yields a higher ceiling."""
        prev = None
        for ctx in range(10_000, 150_000, 5_000):
            c = kv_concurrency_ceiling(ctx)
            if prev is not None:
                assert c <= prev
            prev = c

    def test_utilization_target_raises_ceiling(self) -> None:
        low = kv_concurrency_ceiling(20_000, utilization_target=0.5)
        high = kv_concurrency_ceiling(20_000, utilization_target=1.0)
        assert high >= low
        # floor(173968*0.5/20000)=4 ; floor(173968*1.0/20000)=8
        assert low == 4
        assert high == 8

    def test_min_workers_floor_when_context_exceeds_pool(self) -> None:
        """Even an over-pool context returns at least one worker."""
        assert (
            kv_concurrency_ceiling(PRODUCTION_KV_CACHE_TOKENS * 3, min_workers=1) == 1
        )
        assert (
            kv_concurrency_ceiling(PRODUCTION_KV_CACHE_TOKENS * 3, min_workers=2) == 2
        )

    def test_custom_kv_pool(self) -> None:
        # double the pool -> double the ceiling for the same context
        base = kv_concurrency_ceiling(
            20_000, kv_cache_tokens=100_000, utilization_target=1.0
        )
        big = kv_concurrency_ceiling(
            20_000, kv_cache_tokens=200_000, utilization_target=1.0
        )
        assert base == 5
        assert big == 10

    @pytest.mark.parametrize(
        ("kwargs",),
        [
            ({"kv_cache_tokens": 0},),
            ({"kv_cache_tokens": -1},),
            ({"utilization_target": 0.0},),
            ({"utilization_target": 1.5},),
            ({"utilization_target": -0.1},),
            ({"min_workers": 0},),
        ],
    )
    def test_invalid_args_raise(self, kwargs: dict[str, object]) -> None:
        with pytest.raises(ValueError):
            kv_concurrency_ceiling(50_000, **kwargs)  # type: ignore[arg-type]

    def test_invalid_context_raises(self) -> None:
        with pytest.raises(ValueError):
            kv_concurrency_ceiling(0)
        with pytest.raises(ValueError):
            kv_concurrency_ceiling(-100)

    def test_production_constant_sanity(self) -> None:
        """Guard against an accidental edit to the measured constants.

        WOR-504 Phase 0 (2026-05-20) updated PRODUCTION_KV_CACHE_TOKENS from
        148,816 (at the implicit 0.90 default) to 173,968 (live-measured at
        --gpu-memory-utilization 0.93 in WSL2 / RTX 5090 setup).
        """
        assert PRODUCTION_KV_CACHE_TOKENS == 173_968
        assert COMPACTION_CONTEXT_CEILING == 134_000
