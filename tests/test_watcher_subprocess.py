"""Tests for app.core.watcher_subprocess."""

from __future__ import annotations

import io
import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.core.manifest import ArtifactPaths, ExecutionManifest
from app.core.watcher_helpers import _tee_worker_output
from app.core.watcher_subprocess import (
    build_snippet_tool_restrictions,
    create_pr,
    expand_skill,
    fetch_sonar_findings,
    launch_worker,
    run_checks,
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
    manifest = _make_manifest(base_branch="epic/wor-96-parent")
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


def test_fetch_sonar_findings_breaks_and_returns_partial_on_mid_pagination_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    monkeypatch.setenv("SONAR_TOKEN", "fake-token")
    monkeypatch.setenv("SONAR_PROJECT_KEY", "my-project")

    page1_payload = json.dumps(
        {
            "issues": [{"key": f"I{i}", "severity": "MAJOR"} for i in range(500)],
            "total": 1000,
        }
    ).encode()

    mock_resp1 = _make_sonar_resp_mock(page1_payload)

    with patch(
        "urllib.request.urlopen",
        side_effect=[mock_resp1, Exception("network error on page 2")],
    ):
        findings = fetch_sonar_findings("wor-171-branch")

    assert findings is not None
    assert len(findings) == 500
    assert all(s == "MAJOR" for s in findings)


# ---------------------------------------------------------------------------
# expand_skill
# ---------------------------------------------------------------------------


def test_expand_skill_returns_substituted_content(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".claude" / "commands"
    skill_dir.mkdir(parents=True)
    (skill_dir / "implement-ticket.md").write_text(
        "Implement $ARGUMENTS and report.", encoding="utf-8"
    )
    result = expand_skill(tmp_path, "WOR-171")
    assert result == "Implement WOR-171 and report."


def test_expand_skill_returns_none_on_missing_skill_file(tmp_path: Path) -> None:
    result = expand_skill(tmp_path, "WOR-171")
    assert result is None


# ---------------------------------------------------------------------------
# launch_worker
# ---------------------------------------------------------------------------


def test_launch_worker_quiet_mode_returns_popen(tmp_path: Path) -> None:
    manifest = _make_manifest()
    mock_process = MagicMock()

    with (
        patch("app.core.watcher_subprocess.expand_skill", return_value=None),
        patch(
            "app.core.watcher_subprocess.build_worker_cmd",
            return_value=["claude", "--dangerously-skip-permissions"],
        ),
        patch("app.core.watcher_subprocess.build_worker_env", return_value={}),
        patch(
            "app.core.watcher_subprocess.subprocess.Popen", return_value=mock_process
        ) as mock_popen,
    ):
        result = launch_worker(tmp_path, manifest, tmp_path, "local", verbose=False)

    assert result is mock_process
    mock_popen.assert_called_once()
    assert mock_popen.call_args.kwargs.get("stdout") != subprocess.PIPE


def test_launch_worker_verbose_mode_starts_tee_thread(tmp_path: Path) -> None:
    manifest = _make_manifest()
    mock_process = MagicMock()
    mock_process.stdout = io.BytesIO(b"worker output\n")

    with (
        patch("app.core.watcher_subprocess.expand_skill", return_value=None),
        patch(
            "app.core.watcher_subprocess.build_worker_cmd",
            return_value=["claude"],
        ),
        patch("app.core.watcher_subprocess.build_worker_env", return_value={}),
        patch(
            "app.core.watcher_subprocess.subprocess.Popen", return_value=mock_process
        ) as mock_popen,
        patch("app.core.watcher_subprocess.threading.Thread") as mock_thread,
    ):
        result = launch_worker(tmp_path, manifest, tmp_path, "local", verbose=True)

    assert result is mock_process
    assert mock_popen.call_args.kwargs.get("stdout") == subprocess.PIPE
    mock_thread.assert_called_once()
    mock_thread.return_value.start.assert_called_once()


def test_launch_worker_cloud_mode_with_snippets_prepends_critical_warning(
    tmp_path: Path,
) -> None:
    snippets = ["# app/core/watcher.py lines 1-10\nsome code here"]
    manifest = _make_manifest(context_snippets=snippets)
    mock_process = MagicMock()
    captured_prompts: list[object] = []

    def capture_cmd(
        ticket_id: str,
        mode: str,
        wt: Path,
        prompt: object,
        disallowed: object,
    ) -> list[str]:
        captured_prompts.append(prompt)
        return ["claude"]

    with (
        patch(
            "app.core.watcher_subprocess.expand_skill",
            return_value="Run /implement-ticket WOR-10",
        ),
        patch(
            "app.core.watcher_subprocess.build_worker_cmd",
            side_effect=capture_cmd,
        ),
        patch("app.core.watcher_subprocess.build_worker_env", return_value={}),
        patch(
            "app.core.watcher_subprocess.subprocess.Popen", return_value=mock_process
        ),
    ):
        launch_worker(tmp_path, manifest, tmp_path, "cloud", verbose=False)

    assert len(captured_prompts) == 1
    assert isinstance(captured_prompts[0], str)
    assert "CRITICAL" in captured_prompts[0]
    assert "watcher.py" in captured_prompts[0]


# ---------------------------------------------------------------------------
# run_checks
# ---------------------------------------------------------------------------


def test_run_checks_returns_true_when_all_pass(tmp_path: Path) -> None:
    manifest = _make_manifest(required_checks=["ruff check .", "mypy app/"])

    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    with patch("app.core.watcher_subprocess.subprocess.run", side_effect=fake_run):
        assert run_checks(manifest, tmp_path) is True


def test_run_checks_returns_false_on_check_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manifest = _make_manifest(required_checks=["ruff check .", "mypy app/"])
    call_count = 0

    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        result.stdout = "error on line 1" if call_count == 1 else ""
        result.stderr = ""
        result.returncode = 1 if call_count == 1 else 0
        return result

    with (
        patch("app.core.watcher_subprocess.subprocess.run", side_effect=fake_run),
        caplog.at_level(logging.ERROR, logger="app.core.watcher_subprocess"),
    ):
        passed = run_checks(manifest, tmp_path)

    assert passed is False
    assert any("Check failed" in msg for msg in caplog.messages)
    assert call_count == 2


def test_run_checks_writes_last_failure_json_on_failure(tmp_path: Path) -> None:
    manifest = _make_manifest(required_checks=["ruff check ."])

    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        result = MagicMock()
        result.returncode = 1
        result.stdout = "E501 line too long\n" * 50  # > 4000 chars after repetition
        result.stderr = "some stderr"
        return result

    with patch("app.core.watcher_subprocess.subprocess.run", side_effect=fake_run):
        run_checks(manifest, tmp_path)

    artifact_dir = tmp_path / Path(manifest.artifact_paths.result_json).parent
    failure_file = artifact_dir / "last_failure.json"
    assert failure_file.exists(), "last_failure.json should be written on check failure"

    import json as json_mod

    data = json_mod.loads(failure_file.read_text(encoding="utf-8"))
    assert data["check"] == "ruff check ."
    assert "failed_at" in data
    assert len(data["stdout"]) <= 4000
    assert data["stderr"] == "some stderr"


def test_run_checks_stdout_trimmed_to_4000_chars(tmp_path: Path) -> None:
    manifest = _make_manifest(required_checks=["ruff check ."])
    long_stdout = "x" * 8000

    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        result = MagicMock()
        result.returncode = 1
        result.stdout = long_stdout
        result.stderr = ""
        return result

    with patch("app.core.watcher_subprocess.subprocess.run", side_effect=fake_run):
        run_checks(manifest, tmp_path)

    artifact_dir = tmp_path / Path(manifest.artifact_paths.result_json).parent
    import json as json_mod

    data = json_mod.loads(
        (artifact_dir / "last_failure.json").read_text(encoding="utf-8")
    )
    assert len(data["stdout"]) == 4000


def test_run_checks_deletes_last_failure_json_on_success(tmp_path: Path) -> None:
    manifest = _make_manifest(required_checks=["ruff check ."])

    # Pre-create a stale last_failure.json in the artifact dir
    artifact_dir = tmp_path / Path(manifest.artifact_paths.result_json).parent
    artifact_dir.mkdir(parents=True, exist_ok=True)
    stale = artifact_dir / "last_failure.json"
    stale.write_text('{"check": "old"}', encoding="utf-8")

    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    with patch("app.core.watcher_subprocess.subprocess.run", side_effect=fake_run):
        assert run_checks(manifest, tmp_path) is True

    assert not stale.exists(), (
        "last_failure.json should be deleted after successful run"
    )


def test_run_checks_last_failure_overwritten_by_last_failing_check(
    tmp_path: Path,
) -> None:
    manifest = _make_manifest(required_checks=["ruff check .", "mypy app/"])
    call_count = 0

    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        result.returncode = 1
        result.stdout = f"output from check {call_count}"
        result.stderr = ""
        return result

    with patch("app.core.watcher_subprocess.subprocess.run", side_effect=fake_run):
        run_checks(manifest, tmp_path)

    artifact_dir = tmp_path / Path(manifest.artifact_paths.result_json).parent
    import json as json_mod

    data = json_mod.loads(
        (artifact_dir / "last_failure.json").read_text(encoding="utf-8")
    )
    # Last failing check (mypy) should overwrite the first (ruff)
    assert data["check"] == "mypy app/"
    assert data["stdout"] == "output from check 2"


# ---------------------------------------------------------------------------
# create_pr — main-targeting guard (no auto-merge)
# ---------------------------------------------------------------------------


def test_create_pr_skips_auto_merge_when_targeting_main(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manifest = _make_manifest(base_branch="main")
    pr_url = "https://github.com/example/pr/99"
    called_cmds: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        called_cmds.append(cmd)
        result = MagicMock()
        result.returncode = 0
        result.stdout = (
            pr_url if cmd[:3] == ["gh", "pr", "create"] else "abc1234 some commit"
        )
        result.stderr = ""
        return result

    with (
        patch("app.core.watcher_subprocess.subprocess.run", side_effect=fake_run),
        caplog.at_level(logging.INFO, logger="app.core.watcher_subprocess"),
    ):
        returned_url = create_pr(manifest, tmp_path)

    assert returned_url == pr_url
    assert any("targets main" in msg for msg in caplog.messages)
    assert not any(cmd[:3] == ["gh", "pr", "merge"] for cmd in called_cmds)


# ---------------------------------------------------------------------------
# create_pr — additional failure paths
# ---------------------------------------------------------------------------


def test_create_pr_raises_when_no_commits_ahead(tmp_path: Path) -> None:
    manifest = _make_manifest()

    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    with (
        patch("app.core.watcher_subprocess.subprocess.run", side_effect=fake_run),
        pytest.raises(subprocess.CalledProcessError, match="git log"),
    ):
        create_pr(manifest, tmp_path)


def test_create_pr_falls_back_to_immediate_merge_on_auto_merge_api_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manifest = _make_manifest(base_branch="epic/wor-96-parent")
    pr_url = "https://github.com/example/pr/42"

    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        if cmd[:2] == ["git", "log"]:
            result.stdout = "abc1234 some commit"
        elif cmd[:3] == ["gh", "pr", "create"]:
            result.stdout = pr_url
        elif cmd[:3] == ["gh", "pr", "merge"] and "--auto" in cmd:
            result.returncode = 1
            result.stderr = "GraphQL: Field 'enablePullRequestAutoMerge' doesn't exist"
        return result

    with (
        patch("app.core.watcher_subprocess.subprocess.run", side_effect=fake_run),
        caplog.at_level(logging.INFO, logger="app.core.watcher_subprocess"),
    ):
        returned_url = create_pr(manifest, tmp_path)

    assert returned_url == pr_url
    assert any("No required checks on target branch" in msg for msg in caplog.messages)


def test_create_pr_warns_when_immediate_merge_also_fails(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manifest = _make_manifest(base_branch="epic/wor-96-parent")
    pr_url = "https://github.com/example/pr/42"

    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        if cmd[:2] == ["git", "log"]:
            result.stdout = "abc1234 some commit"
        elif cmd[:3] == ["gh", "pr", "create"]:
            result.stdout = pr_url
        elif cmd[:3] == ["gh", "pr", "merge"]:
            result.returncode = 1
            result.stderr = (
                "clean status required" if "--auto" in cmd else "conflicts detected"
            )
        return result

    with (
        patch("app.core.watcher_subprocess.subprocess.run", side_effect=fake_run),
        caplog.at_level(logging.WARNING, logger="app.core.watcher_subprocess"),
    ):
        returned_url = create_pr(manifest, tmp_path)

    assert returned_url == pr_url
    assert any("gh pr merge --squash also failed" in msg for msg in caplog.messages)
