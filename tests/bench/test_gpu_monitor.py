"""Tests for GpuMonitor and SysMonitor."""

from __future__ import annotations

import time
from itertools import chain, repeat
from unittest.mock import MagicMock, patch

import pytest

from scripts.bench.gpu_monitor import (
    GpuMonitor,
    GpuSample,
    _all_none,
    _parse_nvidia_smi,
)
from scripts.bench.sys_monitor import SysMonitor


class TestParseNvidiaSmi:
    def test_valid_line(self) -> None:
        stdout = "2048, 80, 50, 200.5, 75, 1530, 7000\n"
        result = _parse_nvidia_smi(stdout)
        assert result is not None
        vram, gpu_util, mem_util, power, temp, sm_clk, mem_clk = result
        assert vram == pytest.approx(2048 / 1024)
        assert gpu_util == pytest.approx(80.0)
        assert mem_util == pytest.approx(50.0)
        assert power == pytest.approx(200.5)
        assert temp == pytest.approx(75.0)
        assert sm_clk == pytest.approx(1530.0)
        assert mem_clk == pytest.approx(7000.0)

    def test_empty_returns_none(self) -> None:
        assert _parse_nvidia_smi("") is None

    def test_wrong_field_count_returns_none(self) -> None:
        assert _parse_nvidia_smi("100, 50, 30\n") is None

    def test_non_numeric_returns_none(self) -> None:
        assert _parse_nvidia_smi("[N/A], 80, 50, 200, 75, 1530, 7000\n") is None


class TestAllNone:
    def test_all_fields_are_none(self) -> None:
        sample = _all_none()
        assert sample.peak_vram_gb is None
        assert sample.avg_gpu_util_pct is None
        assert sample.avg_gpu_mem_util_pct is None
        assert sample.avg_power_w is None
        assert sample.peak_temp_c is None
        assert sample.avg_sm_clock_mhz is None
        assert sample.avg_mem_clock_mhz is None


class TestGpuMonitorPopulated:
    @patch("scripts.bench.gpu_monitor.subprocess.run")
    def test_stop_returns_all_seven_fields(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="2048, 80, 50, 200.5, 75, 1530, 7000\n",
        )
        monitor = GpuMonitor(interval=0.001)
        monitor.start()
        time.sleep(0.05)
        sample = monitor.stop()

        assert isinstance(sample, GpuSample)
        assert sample.peak_vram_gb == pytest.approx(2048 / 1024)
        assert sample.avg_gpu_util_pct == pytest.approx(80.0)
        assert sample.avg_gpu_mem_util_pct == pytest.approx(50.0)
        assert sample.avg_power_w == pytest.approx(200.5)
        assert sample.peak_temp_c == pytest.approx(75.0)
        assert sample.avg_sm_clock_mhz == pytest.approx(1530.0)
        assert sample.avg_mem_clock_mhz == pytest.approx(7000.0)

    def test_peak_fields_track_maximum(self) -> None:
        monitor = GpuMonitor()
        # Inject samples directly to test aggregation without threading
        monitor._samples = [
            (1024 / 1024, 60.0, 40.0, 150.0, 70.0, 1400.0, 6000.0),
            (3072 / 1024, 90.0, 60.0, 250.0, 85.0, 1600.0, 8000.0),
            (2048 / 1024, 75.0, 50.0, 200.0, 78.0, 1500.0, 7000.0),
        ]
        sample = monitor.stop()

        assert sample.peak_vram_gb == pytest.approx(3072 / 1024)
        assert sample.peak_temp_c == pytest.approx(85.0)
        assert sample.avg_gpu_util_pct == pytest.approx((60.0 + 90.0 + 75.0) / 3)


class TestGpuMonitorAbsent:
    @patch("scripts.bench.gpu_monitor.subprocess.run")
    def test_no_exception_when_nvidia_smi_absent(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = FileNotFoundError("nvidia-smi not found")
        monitor = GpuMonitor(interval=0.001)
        monitor.start()
        time.sleep(0.05)
        sample = monitor.stop()

        assert isinstance(sample, GpuSample)
        assert sample.peak_vram_gb is None
        assert sample.avg_gpu_util_pct is None
        assert sample.avg_gpu_mem_util_pct is None
        assert sample.avg_power_w is None
        assert sample.peak_temp_c is None
        assert sample.avg_sm_clock_mhz is None
        assert sample.avg_mem_clock_mhz is None

    @patch("scripts.bench.gpu_monitor.subprocess.run")
    def test_no_exception_when_nonzero_returncode(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        monitor = GpuMonitor(interval=0.001)
        monitor.start()
        time.sleep(0.05)
        sample = monitor.stop()

        assert sample.peak_vram_gb is None

    def test_stop_before_start_returns_all_none(self) -> None:
        monitor = GpuMonitor()
        sample = monitor.stop()
        assert sample.peak_vram_gb is None


class TestSysMonitorOffload:
    @patch("scripts.bench.sys_monitor._read_ram_gb")
    def test_cpu_offload_detected_on_spike(self, mock_read: MagicMock) -> None:
        # baseline=8.0, then spike to 11.5 (delta 3.5 GB > threshold 2.0 GB)
        mock_read.side_effect = chain([8.0], repeat(11.5))
        monitor = SysMonitor(interval=0.001)
        monitor.start()
        time.sleep(0.05)
        result = monitor.stop()

        assert result.cpu_offload_detected is True

    @patch("scripts.bench.sys_monitor._read_ram_gb")
    def test_no_offload_when_spike_below_threshold(self, mock_read: MagicMock) -> None:
        # baseline=8.0, small increase of 1.5 GB (below 2.0 threshold)
        mock_read.side_effect = chain([8.0], repeat(9.5))
        monitor = SysMonitor(interval=0.001)
        monitor.start()
        time.sleep(0.05)
        result = monitor.stop()

        assert result.cpu_offload_detected is False

    @patch("scripts.bench.sys_monitor._read_ram_gb")
    def test_no_offload_at_exact_threshold(self, mock_read: MagicMock) -> None:
        # baseline=8.0, spike of exactly 2.0 GB (not strictly greater)
        mock_read.side_effect = chain([8.0], repeat(10.0))
        monitor = SysMonitor(interval=0.001)
        monitor.start()
        time.sleep(0.05)
        result = monitor.stop()

        assert result.cpu_offload_detected is False

    @patch("scripts.bench.sys_monitor._read_ram_gb")
    def test_peak_ram_tracked(self, mock_read: MagicMock) -> None:
        mock_read.side_effect = chain([8.0], repeat(9.0))
        monitor = SysMonitor(interval=0.001)
        monitor.start()
        time.sleep(0.05)
        result = monitor.stop()

        assert result.peak_ram_gb is not None
        assert result.peak_ram_gb >= 9.0
