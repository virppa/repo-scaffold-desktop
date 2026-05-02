"""Color-aware logging formatter for the watcher daemon.

Provides a TTY-aware :class:`ColorFormatter` that distinguishes log levels
visually on interactive terminals while emitting plain text when output is
piped or redirected.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

# ANSI colour codes — only used when TTY
_COLOURS: dict[str, str] = {
    "INFO": "\033[0m",  # white (plain)
    "WARNING": "\033[33m",  # yellow
    "ERROR": "\033[31m",  # red
}
_RESET = "\033[0m"


class ColorFormatter(logging.Formatter):
    """Log formatter that colour-codes messages on TTY.

    * INFO   → white (no visible difference from plain)
    * WARNING → yellow
    * ERROR  → red

    Falls back to plain text for redirected/piped output (``isatty() == False``).
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN003, ANN002
        super().__init__(*args, **kwargs)
        self._is_tty = sys.stderr.isatty()

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)

        if not self._is_tty:
            return msg

        level_name = record.levelname
        colour = _COLOURS.get(level_name, "")
        if colour:
            return f"{colour}{msg}{_RESET}"
        return msg
