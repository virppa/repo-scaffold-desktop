"""Tests for scripts/check_file_sizes.py (WOR-377).

Covers tier classification (production vs test), severity boundaries,
non-Python passthrough, and the main() exit code contract.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add scripts/ to path so we can import the hook module by name.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import check_file_sizes as cfs  # noqa: E402

# ---------------------------------------------------------------------------
# is_test_file / thresholds_for
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected_test",
    [
        ("tests/test_foo.py", True),
        ("tests/sub/test_bar.py", True),
        ("app/core/foo.py", False),
        ("scripts/run.py", False),
        ("tests/conftest.py", True),
    ],
)
def test_is_test_file(path: str, expected_test: bool) -> None:
    assert cfs.is_test_file(path) is expected_test


def test_thresholds_for_production_file() -> None:
    assert cfs.thresholds_for("app/core/foo.py") == (
        cfs.PROD_ADVISORY,
        cfs.PROD_RECOMMEND,
        cfs.PROD_BLOCK,
    )


def test_thresholds_for_test_file() -> None:
    assert cfs.thresholds_for("tests/test_foo.py") == (
        cfs.TEST_ADVISORY,
        cfs.TEST_RECOMMEND,
        cfs.TEST_BLOCK,
    )


# ---------------------------------------------------------------------------
# classify — boundary table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "loc,path,expected",
    [
        # Production thresholds: 500 / 700 / 1200
        (1, "app/core/foo.py", None),
        (499, "app/core/foo.py", None),
        (500, "app/core/foo.py", "advisory"),
        (699, "app/core/foo.py", "advisory"),
        (700, "app/core/foo.py", "recommend"),
        (1199, "app/core/foo.py", "recommend"),
        (1200, "app/core/foo.py", "block"),
        (5000, "app/core/foo.py", "block"),
        # Test thresholds: 800 / 1200 / 2000
        (1, "tests/test_foo.py", None),
        (799, "tests/test_foo.py", None),
        (800, "tests/test_foo.py", "advisory"),
        (1199, "tests/test_foo.py", "advisory"),
        (1200, "tests/test_foo.py", "recommend"),
        (1999, "tests/test_foo.py", "recommend"),
        (2000, "tests/test_foo.py", "block"),
        (5000, "tests/test_foo.py", "block"),
    ],
)
def test_classify_boundaries(loc: int, path: str, expected: str | None) -> None:
    assert cfs.classify(loc, path) == expected


# ---------------------------------------------------------------------------
# count_lines / check_file
# ---------------------------------------------------------------------------


def test_count_lines_basic(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a\nb\nc\n", encoding="utf-8")
    assert cfs.count_lines(f) == 3


def test_count_lines_no_trailing_newline(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a\nb", encoding="utf-8")
    assert cfs.count_lines(f) == 2


def test_count_lines_empty(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("", encoding="utf-8")
    assert cfs.count_lines(f) == 0


def test_check_file_under_threshold(tmp_path: Path) -> None:
    f = tmp_path / "small.py"
    f.write_text("\n" * 100, encoding="utf-8")
    loc, sev = cfs.check_file(str(f))
    assert loc == 100
    assert sev is None


def test_check_file_block_for_production(tmp_path: Path) -> None:
    f = tmp_path / "big.py"
    f.write_text("\n" * 1500, encoding="utf-8")
    loc, sev = cfs.check_file(str(f))
    assert loc == 1500
    # 1500 LOC: production tier — block (>=1200), test tier — recommend (>=1200)
    # str(f) is an absolute path with no `tests` parent, so production tier.
    assert sev == "block"


def test_check_file_skips_non_python(tmp_path: Path) -> None:
    f = tmp_path / "data.txt"
    f.write_text("\n" * 5000, encoding="utf-8")
    loc, sev = cfs.check_file(str(f))
    assert loc == 0
    assert sev is None


def test_check_file_skips_missing(tmp_path: Path) -> None:
    loc, sev = cfs.check_file(str(tmp_path / "does_not_exist.py"))
    assert loc == 0
    assert sev is None


# ---------------------------------------------------------------------------
# main — exit code contract
# ---------------------------------------------------------------------------


def test_main_exit_zero_when_nothing_staged(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cfs.main([])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_main_exit_zero_for_advisory_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tmp_path / "modest.py"
    f.write_text("\n" * 600, encoding="utf-8")  # production advisory band
    rc = cfs.main([str(f)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "advisory" in captured.err.lower()


def test_main_exit_zero_for_recommend(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tmp_path / "warning.py"
    f.write_text("\n" * 900, encoding="utf-8")  # production recommend band
    rc = cfs.main([str(f)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "RECOMMEND" in captured.err


def test_main_exit_one_for_block(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tmp_path / "huge.py"
    f.write_text("\n" * 1500, encoding="utf-8")  # production block band
    rc = cfs.main([str(f)])
    assert rc == 1
    captured = capsys.readouterr()
    assert "BLOCK" in captured.err
    assert "--no-verify" in captured.err


def test_main_continues_after_first_block(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A block on file A should still emit info for file B; both reported."""
    big = tmp_path / "huge.py"
    big.write_text("\n" * 1500, encoding="utf-8")
    medium = tmp_path / "medium.py"
    medium.write_text("\n" * 800, encoding="utf-8")
    rc = cfs.main([str(big), str(medium)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "huge.py" in err
    assert "medium.py" in err


def test_main_skips_non_python_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    txt = tmp_path / "data.txt"
    txt.write_text("\n" * 5000, encoding="utf-8")
    rc = cfs.main([str(txt)])
    assert rc == 0
    assert capsys.readouterr().err == ""
