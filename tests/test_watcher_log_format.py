"""Tests for app.core.watcher.log_format — ColorFormatter."""

from __future__ import annotations

import logging
import sys
from unittest.mock import patch

from app.core.watcher.log_format import ColorFormatter

# ANSI escape sequences used by ColorFormatter
_RESET = "\033[0m"
_YELLOW = "\033[33m"
_RED = "\033[31m"


def _make_record(level: int, msg: str) -> logging.LogRecord:
    """Build a LogRecord for format() testing."""
    return logging.LogRecord(
        name="test",
        level=level,
        pathname="test.py",
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )


# ---------------------------------------------------------------------------
# TTY = True — colour applied per level
# ---------------------------------------------------------------------------


def test_info_prefix_on_tty() -> None:
    record = _make_record(logging.INFO, "info message")
    with patch.object(sys.stderr, "isatty", return_value=True):
        formatter = ColorFormatter()
        result = formatter.format(record)
    # INFO uses white (plain) — same as reset
    expected = f"\033[0minfo message{_RESET}"
    assert result == expected


def test_warning_prefix_on_tty() -> None:
    record = _make_record(logging.WARNING, "warn message")
    with patch.object(sys.stderr, "isatty", return_value=True):
        formatter = ColorFormatter()
        result = formatter.format(record)
    expected = f"{_YELLOW}warn message{_RESET}"
    assert result == expected


def test_error_prefix_on_tty() -> None:
    record = _make_record(logging.ERROR, "error message")
    with patch.object(sys.stderr, "isatty", return_value=True):
        formatter = ColorFormatter()
        result = formatter.format(record)
    expected = f"{_RED}error message{_RESET}"
    assert result == expected


def test_debug_no_colour_on_tty() -> None:
    """DEBUG is not in _COLOURS — plain message returned."""
    record = _make_record(logging.DEBUG, "debug message")
    with patch.object(sys.stderr, "isatty", return_value=True):
        formatter = ColorFormatter()
        result = formatter.format(record)
    assert result == "debug message"


def test_critical_no_colour_on_tty() -> None:
    """CRITICAL is not in _COLOURS — plain message returned."""
    record = _make_record(logging.CRITICAL, "critical message")
    with patch.object(sys.stderr, "isatty", return_value=True):
        formatter = ColorFormatter()
        result = formatter.format(record)
    assert result == "critical message"


# ---------------------------------------------------------------------------
# TTY = False — plain text for all levels
# ---------------------------------------------------------------------------


def test_tty_false_strips_colours_info() -> None:
    record = _make_record(logging.INFO, "info message")
    with patch.object(sys.stderr, "isatty", return_value=False):
        formatter = ColorFormatter()
        result = formatter.format(record)
    assert result == "info message"


def test_tty_false_strips_colours_warning() -> None:
    record = _make_record(logging.WARNING, "warn message")
    with patch.object(sys.stderr, "isatty", return_value=False):
        formatter = ColorFormatter()
        result = formatter.format(record)
    assert result == "warn message"


def test_tty_false_strips_colours_error() -> None:
    record = _make_record(logging.ERROR, "error message")
    with patch.object(sys.stderr, "isatty", return_value=False):
        formatter = ColorFormatter()
        result = formatter.format(record)
    assert result == "error message"


# ---------------------------------------------------------------------------
# Custom format string
# ---------------------------------------------------------------------------


def test_custom_format_string() -> None:
    record = _make_record(logging.INFO, "hello")
    with patch.object(sys.stderr, "isatty", return_value=True):
        formatter = ColorFormatter(fmt="[%(levelname)s] %(message)s")
        result = formatter.format(record)
    assert result == f"\033[0m[INFO] hello{_RESET}"
