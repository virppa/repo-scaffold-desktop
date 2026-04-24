"""Tests for app.core.watcher_subprocess."""

from __future__ import annotations

import io
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.core.manifest import ArtifactPaths, ExecutionManifest
from app.core.watcher_helpers import _tee_worker_output
from app.core.watcher_subprocess import (
    build_snippet_tool_restrictions,
    create_pr,
    fetch_sonar_findings,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_manifest(**overrides: object) -> ExecutionManifest:
    from typing import Any

    defaults: dict[str, Any] = {
        "ticket_id": "WOR-10",
        "epic_id": "WOR-96",
        "title": "Test ticket",
        "priority": 2,
        "status": "ReadyForLocal",
        "parallel_safe": True,
        "risk_level": "low",
        "implementation_mode": "local",
        "review_mode": "auto",
        "base_branch": "main",
        "worker_branch": "wor-10-test-ticket",
        "objective": "Do the thing.",
        "artifact_paths": ArtifactPaths.from_ticket_id("WOR-10"),
        "allowed_paths": ["app/core/foo.py"],
        "done_definition": "It works.",
    }
    defaults.update(overrides)
    return ExecutionManifest(**defaults)


# ---------------------------------------------------------------------------
# create_pr
# ---------------------------------------------------------------------------


def test_create_pr_pushes_branch_before_gh_pr(tmp_path: Path) -> None:
    manifest = _make_manifest()
    call_order: list[str] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        if cmd[:2] == ["git", "push"]:
            call_order.append("push")
        elif cmd[:3] == ["gh", "pr", "create"]:
            call_order.append("gh_pr")
        result = MagicMock()
        result.stdout = "https://github.com/example/pr/1"
        return result

    with patch("app.core.watcher_subprocess.subprocess.run", side_effect=fake_run):
        create_pr(manifest, tmp_path)

    assert call_order == ["push", "gh_pr"]


def test_create_pr_logs_warning_on_auto_merge_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manifest = _make_manifest()
    pr_url = "https://github.com/example/pr/1"

    def fake_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        if cmd[:3] == ["gh", "pr", "merge"]:
            result.returncode = 1
            result.stderr = "auto-merge is not enabled for this repository"
            result.stdout = ""
        elif cmd[:3] == ["gh", "pr", "create"]:
            result.stdout = pr_url
        elif cmd[:2] == ["git", "log"]:
            result.stdout = "abc1234 some commit"
        else:
            result.stdout = pr_url
        return result

    with (
        patch("app.core.watcher_subprocess.subprocess.run", side_effect=fake_run),
        caplog.at_level(logging.WARNING, logger="app.core.watcher_subprocess"),
    ):
        returned_url = create_pr(manifest, tmp_path)

    assert returned_url == pr_url
    assert any(
        "gh pr merge --auto failed" in msg
        and pr_url in msg
        and "rc=1" in msg
        and "auto-merge is not enabled" in msg
        for msg in caplog.messages
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


def test_fetch_sonar_findings_paginates_multiple_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    monkeypatch.setenv("SONAR_TOKEN", "fake-token")
    monkeypatch.setenv("SONAR_PROJECT_KEY", "my-project")

    page1_payload = json.dumps(
        {
            "issues": [{"key": f"I{i}", "severity": "MAJOR"} for i in range(500)],
            "total": 600,
        }
    ).encode()
    page2_payload = json.dumps(
        {
            "issues": [{"key": f"J{i}", "severity": "CRITICAL"} for i in range(100)],
            "total": 600,
        }
    ).encode()

    mock_resp1 = _make_sonar_resp_mock(page1_payload)
    mock_resp2 = _make_sonar_resp_mock(page2_payload)

    with patch(
        "urllib.request.urlopen", side_effect=[mock_resp1, mock_resp2]
    ) as mock_urlopen:
        findings = fetch_sonar_findings("wor-157-branch")

    assert findings is not None
    assert len(findings) == 600
    assert findings.count("MAJOR") == 500
    assert findings.count("CRITICAL") == 100
    assert mock_urlopen.call_count == 2
