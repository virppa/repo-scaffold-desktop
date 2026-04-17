"""Tests for `python -m app.cli metrics browse`."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.cli import main


def test_metrics_browse_launches_datasette(tmp_path: Path) -> None:
    db = tmp_path / "metrics.db"
    db.touch()

    with (
        patch("app.cli.MetricsStore.get_db_path", return_value=db),
        patch("app.cli.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0)
        rc = main(["metrics", "browse"])

    assert rc == 0
    mock_run.assert_called_once_with(["datasette", str(db)], check=False)


def test_metrics_browse_missing_db_exits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "metrics.db"  # does not exist

    with patch("app.cli.MetricsStore.get_db_path", return_value=db):
        rc = main(["metrics", "browse"])

    assert rc == 1
    assert "not found" in capsys.readouterr().err


def test_metrics_browse_datasette_not_installed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "metrics.db"
    db.touch()

    with (
        patch("app.cli.MetricsStore.get_db_path", return_value=db),
        patch("app.cli.subprocess.run", side_effect=FileNotFoundError),
    ):
        rc = main(["metrics", "browse"])

    assert rc == 1
    assert "datasette not installed" in capsys.readouterr().err


def test_metrics_no_subcommand_prints_usage(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["metrics"])
    assert rc == 1
    assert "Usage" in capsys.readouterr().err
