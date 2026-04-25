"""Tests for EnvSnapshot."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from scripts.bench.config import (
    BackendConfig,
    BenchConfig,
    MatrixConfig,
    ModelConfig,
    TierConfig,
)
from scripts.bench.env_snapshot import EnvSnapshot, _hash_settings


@pytest.fixture
def bench_config() -> BenchConfig:
    return BenchConfig(
        matrix=MatrixConfig(
            context_sizes=[1024],
            boundary_context_sizes=[4096],
            concurrency_levels=[1],
        ),
        backends=[BackendConfig(id="test-backend", base_url="http://localhost:11434")],
        models=[ModelConfig(id="test-model", backend_id="test-backend")],
        tiers=[TierConfig(name="standard")],
    )


class TestSettingsHash:
    def test_stable_on_repeated_calls(self) -> None:
        settings = {"temperature": 0.7, "max_tokens": 2048, "model": "test"}
        h1 = _hash_settings(settings)
        h2 = _hash_settings(settings)
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex digest

    def test_key_order_independent(self) -> None:
        s1 = {"a": 1, "b": 2}
        s2 = {"b": 2, "a": 1}
        assert _hash_settings(s1) == _hash_settings(s2)

    def test_different_inputs_produce_different_hashes(self) -> None:
        assert _hash_settings({"a": 1}) != _hash_settings({"a": 2})

    def test_empty_dict_produces_stable_hash(self) -> None:
        h1 = _hash_settings({})
        h2 = _hash_settings({})
        assert h1 == h2
        assert len(h1) == 64


class TestEnvSnapshotCapture:
    @patch("scripts.bench.env_snapshot.subprocess.run")
    def test_capture_no_ops_when_nvidia_smi_absent(
        self, mock_run: MagicMock, bench_config: BenchConfig
    ) -> None:
        mock_run.side_effect = FileNotFoundError("nvidia-smi not found")
        snap = EnvSnapshot.capture("ollama", "llama3", bench_config)

        assert snap.gpu_driver_version is None
        assert snap.cuda_version is None
        assert snap.backend == "ollama"
        assert snap.model == "llama3"
        assert len(snap.settings_hash) == 64

    @patch("scripts.bench.env_snapshot.subprocess.run")
    def test_capture_no_ops_when_nonzero_returncode(
        self, mock_run: MagicMock, bench_config: BenchConfig
    ) -> None:
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        snap = EnvSnapshot.capture("litellm", "gemma2", bench_config)

        assert snap.gpu_driver_version is None
        assert snap.cuda_version is None

    @patch("scripts.bench.env_snapshot.subprocess.run")
    def test_capture_parses_driver_and_cuda(
        self, mock_run: MagicMock, bench_config: BenchConfig
    ) -> None:
        # First call: --query-gpu=driver_version; second call: plain nvidia-smi
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="545.23.08\n"),
            MagicMock(returncode=0, stdout="| CUDA Version: 12.3     |\n"),
        ]
        snap = EnvSnapshot.capture("litellm", "gemma2", bench_config)

        assert snap.gpu_driver_version == "545.23.08"
        assert snap.cuda_version == "12.3"
        assert snap.backend == "litellm"
        assert snap.model == "gemma2"

    @patch("scripts.bench.env_snapshot.subprocess.run")
    def test_settings_hash_stable_in_capture(
        self, mock_run: MagicMock, bench_config: BenchConfig
    ) -> None:
        mock_run.side_effect = FileNotFoundError("nvidia-smi not found")
        snap1 = EnvSnapshot.capture("ollama", "qwen3", bench_config)
        snap2 = EnvSnapshot.capture("ollama", "qwen3", bench_config)

        assert snap1.settings_hash == snap2.settings_hash

    def test_python_and_os_version_populated(self, bench_config: BenchConfig) -> None:
        import platform
        import sys

        with patch("scripts.bench.env_snapshot.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError
            snap = EnvSnapshot.capture("test", "test", bench_config)

        assert snap.python_version == sys.version
        assert snap.os_version == platform.platform()
