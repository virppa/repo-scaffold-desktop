"""Tests for app.core.watcher_subprocess."""

from __future__ import annotations

import io
from unittest.mock import patch

import pytest

from app.core.watcher_helpers import _tee_worker_output
from app.core.watcher_subprocess import (
    build_snippet_tool_restrictions,
    fetch_sonar_findings,
)

# ---------------------------------------------------------------------------
# _tee_worker_output (lives in watcher_helpers; tested here as subprocess I/O)
# ---------------------------------------------------------------------------


class _CaptureSink:
    """Byte sink that accumulates writes and tracks close without discarding data."""

    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, b: bytes) -> int:
        self.data += b
        return len(b)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def test_tee_writes_prefixed_lines_to_dest() -> None:
    pipe = io.BytesIO(b"hello\nsecond line\n")
    log_sink: _CaptureSink = _CaptureSink()
    dest_sink = io.BytesIO()

    _tee_worker_output(pipe, log_sink, b"[WOR-62] ", dest_sink)  # type: ignore[arg-type]

    assert log_sink.data == b"hello\nsecond line\n"
    assert dest_sink.getvalue() == b"[WOR-62] hello\n[WOR-62] second line\n"


def test_tee_closes_log_file() -> None:
    pipe = io.BytesIO(b"line\n")
    log_sink = _CaptureSink()
    dest_sink = io.BytesIO()

    _tee_worker_output(pipe, log_sink, b"", dest_sink)  # type: ignore[arg-type]

    assert log_sink.closed


def test_tee_empty_pipe() -> None:
    pipe = io.BytesIO(b"")
    log_sink = _CaptureSink()
    dest_sink = io.BytesIO()

    _tee_worker_output(pipe, log_sink, b"[X] ", dest_sink)  # type: ignore[arg-type]

    assert log_sink.data == b""
    assert dest_sink.getvalue() == b""


# ---------------------------------------------------------------------------
# build_snippet_tool_restrictions
# ---------------------------------------------------------------------------


def test_build_snippet_tool_restrictions_extracts_basenames() -> None:
    snippets = [
        "# app/core/watcher.py lines 574-589\nsome code",
        "# app/core/metrics.py lines 1-20\nmore code",
        "# app/core/watcher.py lines 600-620\nduplicate file",
    ]
    patterns = build_snippet_tool_restrictions(snippets)
    assert patterns == ["Read(*watcher.py)", "Read(*metrics.py)"]


def test_build_snippet_tool_restrictions_ignores_malformed() -> None:
    snippets = ["no header here", "# missing path\ncode"]
    patterns = build_snippet_tool_restrictions(snippets)
    assert patterns == []


# ---------------------------------------------------------------------------
# fetch_sonar_findings
# ---------------------------------------------------------------------------


def _make_sonar_resp_mock(payload: bytes) -> object:
    """Return a context-manager mock whose .read() returns payload."""
    from unittest.mock import MagicMock

    mock_resp = MagicMock()
    mock_resp.read.return_value = payload
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def test_fetch_sonar_findings_returns_none_without_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SONAR_TOKEN", raising=False)
    monkeypatch.delenv("SONAR_PROJECT_KEY", raising=False)
    assert fetch_sonar_findings("wor-10-some-branch") is None


def test_fetch_sonar_findings_returns_none_without_project_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SONAR_TOKEN", "fake-token")
    monkeypatch.delenv("SONAR_PROJECT_KEY", raising=False)
    assert fetch_sonar_findings("wor-10-some-branch") is None


def test_fetch_sonar_findings_returns_severities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    monkeypatch.setenv("SONAR_TOKEN", "fake-token")
    monkeypatch.setenv("SONAR_PROJECT_KEY", "my-org_my-project")
    api_payload = json.dumps(
        {
            "issues": [
                {"key": "A", "severity": "MAJOR"},
                {"key": "B", "severity": "MINOR"},
                {"key": "C", "severity": "BLOCKER"},
            ]
        }
    ).encode()

    mock_resp = _make_sonar_resp_mock(api_payload)
    with patch("urllib.request.urlopen", return_value=mock_resp):
        findings = fetch_sonar_findings("wor-10-some-branch")

    assert findings == ["MAJOR", "MINOR", "BLOCKER"]


def test_fetch_sonar_findings_returns_empty_list_when_no_issues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    monkeypatch.setenv("SONAR_TOKEN", "fake-token")
    monkeypatch.setenv("SONAR_PROJECT_KEY", "my-project")
    api_payload = json.dumps({"issues": [], "total": 0}).encode()

    with patch(
        "urllib.request.urlopen",
        return_value=_make_sonar_resp_mock(api_payload),
    ):
        findings = fetch_sonar_findings("wor-10-some-branch")

    assert findings == []


def test_fetch_sonar_findings_returns_none_on_api_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import urllib.error

    monkeypatch.setenv("SONAR_TOKEN", "fake-token")
    monkeypatch.setenv("SONAR_PROJECT_KEY", "my-project")
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("connection refused"),
    ):
        count = fetch_sonar_findings("wor-10-some-branch")
    assert count is None
