"""Shared test fixtures for watcher sub-module tests."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.core.manifest import ArtifactPaths, ExecutionManifest
from app.core.watcher.watcher_types import ActiveWorker

# WOR-426: Block any test from spawning a real `claude` subprocess.
#
# WOR-312's retry-loop tests in tests/test_watcher_finalize.py mock run_checks
# but several forgot to mock launch_worker too — the retry path fires for real,
# spawning a real claude binary against vLLM and writing to production
# .claude/artifacts/wor_*/ paths. CI passes because the claude binary doesn't
# exist in CI runners; local devs paid the cost silently.
#
# Patch Popen.__init__ rather than the class itself so MagicMock(spec=...) still
# resolves the real attribute list.
_REAL_POPEN_INIT = subprocess.Popen.__init__


def _extract_first_arg(args: Any) -> str:
    if isinstance(args, (list, tuple)) and args:
        return str(args[0])
    if isinstance(args, str) and args:
        return args.split()[0]
    return ""


@pytest.fixture(autouse=True)
def _block_real_claude_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refuse to spawn a real `claude` binary from any test."""

    def _guarded_init(
        self: subprocess.Popen[Any],
        args: Any,
        *rest: Any,
        **kwargs: Any,
    ) -> None:
        first = _extract_first_arg(args)
        binary = Path(first).name.lower() if first else ""
        if binary in ("claude", "claude.exe"):
            raise AssertionError(
                "Test attempted to spawn real `claude` binary "
                f"(argv[0]={first!r}). Mock launch_worker via "
                "patch('app.core.watcher.watcher_finalize.launch_worker', "
                "return_value=MagicMock()) inside the test's with-block."
            )
        if binary in ("wt.exe", "wt"):
            # WOR-492: the win32 vLLM auto-start opens a real Windows
            # Terminal tab running `vllm serve` via
            # ServiceManager._open_vllm_terminal. On the Windows dev box
            # sys.platform is genuinely "win32", so a full pytest run
            # spawned real GPU vLLM instances. Raise FileNotFoundError —
            # _open_vllm_terminal catches it ("wt.exe not found") and the
            # probe just returns "vLLM down", with no real spawn. Tests
            # that assert the spawn (test_watcher_services.py) patch
            # subprocess.Popen wholesale and never reach this guard.
            raise FileNotFoundError(
                "test guard (WOR-492): refusing real `wt.exe` vLLM "
                f"auto-start (argv[0]={first!r})."
            )
        _REAL_POPEN_INIT(self, args, *rest, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", _guarded_init)


@pytest.fixture(autouse=True)
def _block_vllm_autostart(monkeypatch: pytest.MonkeyPatch) -> None:
    """WOR-492: never touch a real WSL/vLLM from a test.

    Two leaks, one fixture:

    1. **Spawn.** ``ServiceManager._open_vllm_terminal`` (reached when the
       health probe fails on a genuine ``win32`` box) writes a WSL script
       and opens a real ``vllm serve`` in a ``wt.exe`` tab. The
       ``_block_real_claude_subprocess`` guard only blocked ``claude``;
       ``wt.exe`` slipped through and a full ``pytest -n 8`` run on
       Windows spawned two real GPU vLLM instances (the second OOM'd).
       The ``wt.exe`` branch of that guard now raises
       ``FileNotFoundError``, which ``_open_vllm_terminal`` catches → no
       spawn. ``_write_vllm_script_file`` is also no-op'd as defence in
       depth (no WSL-VM boot via ``subprocess.run(["wsl", …])``).

    2. **Outbound HTTP.** Even with no spawn, if an operator's vLLM is
       already up on localhost:8000, unmocked tests hit it for real —
       probe (``GET /v1/models``), metrics (``GET /metrics``) and the
       Anthropic-mode check (``POST /v1/messages`` — a real inference).
       All go through ``http.client.HTTPConnection``; this refuses *only*
       the vLLM host:port, which every call site already wraps in
       ``except (OSError, …)`` → graceful "vLLM unavailable".

    NB: we deliberately do **not** patch ``sys.platform`` — forcing
    "linux" on a Windows box breaks ``pytest-qt`` (its plugin calls
    ``os.getuid()``). The ``wt.exe`` Popen guard achieves the same
    spawn-prevention without that blast radius.

    Tests that assert the real spawn / HTTP behaviour
    (``tests/test_watcher_services.py``) patch ``subprocess.Popen`` /
    ``http.client.HTTPConnection`` wholesale and never reach these guards.
    """
    monkeypatch.setattr(
        "app.core.watcher.watcher_services._write_vllm_script_file",
        lambda *args, **kwargs: None,
    )

    # Refuse real *outbound HTTP* to a running
    # vLLM. The win32 no-op above stops tests *spawning* a server, but if
    # an operator's vLLM is already up on localhost:8000, unmocked tests
    # hit it for real — probe (GET /v1/models), metrics capture
    # (GET /metrics) and the Anthropic-mode check (POST /v1/messages, a
    # real model inference). All of these go through
    # http.client.HTTPConnection. Refuse *only* the vLLM host:port so any
    # other HTTP is untouched; the watcher's `except (OSError, ...)`
    # arms at every call site turn this into a graceful "vLLM
    # unavailable" (probe→False, metrics→None). Tests that assert real
    # HTTP behaviour (tests/test_watcher_services.py) re-patch
    # http.client.HTTPConnection locally and shadow this default.
    import http.client as _http_client

    from app.core.watcher import watcher_services as _svc

    _real_http_connection = _http_client.HTTPConnection
    _vllm_host = str(_svc._VLLM_HOST)
    _vllm_port = int(_svc._VLLM_PORT)

    class _GuardedHTTPConnection(_real_http_connection):  # type: ignore[valid-type,misc]
        """HTTPConnection subclass that refuses the vLLM host:port only.

        Must be a real subclass (not a function) so class attributes
        (``debuglevel`` etc.), ``isinstance`` checks and urllib internals
        keep working for every non-vLLM connection.
        """

        def __init__(self, host: Any, port: Any = None, *rest: Any, **kw: Any):
            same_host = str(host) == _vllm_host
            same_port = port is None or int(port) == _vllm_port
            if same_host and same_port:
                raise OSError(
                    "test guard (WOR-492): refusing real vLLM connection "
                    f"to {host}:{port}. Mock http.client.HTTPConnection "
                    "in the test if it needs the vLLM HTTP path."
                )
            super().__init__(host, port, *rest, **kw)

    monkeypatch.setattr(_http_client, "HTTPConnection", _GuardedHTTPConnection)


@pytest.fixture(autouse=True)
def _isolate_watcher_pid_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """WOR-506: redirect the watcher PID file to a per-test tmp path.

    ``watcher_types._PID_FILE`` is a repo-root-*relative* path. Under
    ``pytest -n8`` every xdist worker process shares the repo cwd, so
    ``is_watcher_running`` / ``write_pid_file`` / ``remove_pid_file``
    all read/write the ONE real ``.claude/watcher.pid`` — a cross-worker
    bleed that nondeterministically flaked test_watcher_gestures /
    test_watcher_sonar_concurrent. Patch the single ``pid_file_path``
    resolver everywhere it is looked up so no test can ever touch the
    operator's real pid file, regardless of xdist schedule.
    """
    import app.core.watcher.watcher_signals as _wsig
    import app.core.watcher.watcher_types as _wtypes

    # Dedicated subdir — NOT tmp_path/.claude. Many watcher tests create
    # ``tmp_path / ".claude"`` themselves with ``mkdir(parents=True)``
    # (exist_ok=False); pre-creating that shared path here made their own
    # mkdir raise FileExistsError. The resolver makes the pid location
    # arbitrary, so an isolated subdir no test touches is correct.
    pid = tmp_path / "_wor506_watcher_pid_home" / "watcher.pid"
    pid.parent.mkdir(parents=True, exist_ok=True)

    def _fake_pid_file_path() -> Path:
        return pid

    monkeypatch.setattr(_wtypes, "pid_file_path", _fake_pid_file_path)
    monkeypatch.setattr(_wsig, "pid_file_path", _fake_pid_file_path)


@pytest.fixture(autouse=True)
def _isolate_read_cap_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """WOR-506: redirect the Read-cap hook's state file to a per-test tmp
    path.

    ``.claude/hooks/check_read_cap.py`` deliberately anchors its
    ``.read_counts.json`` to the hook script's own location (WOR-422 — cwd
    is unreliable), so it is the ONE real ``<repo>/.claude/.read_counts.json``
    no ``chdir``/``tmp_path`` fixture can redirect. ``test_hooks_read_cap``
    spawns the hook as a subprocess (inheriting ``os.environ``); under
    ``pytest --dist loadgroup`` xdist co-schedules those subprocesses across
    workers and they clobber the one shared state file — the same
    cross-worker shared-FS bleed class as the watcher pid file. The hook
    honours ``READ_CAP_STATE_PATH`` (unset in production); set it per-test.
    """
    monkeypatch.setenv("READ_CAP_STATE_PATH", str(tmp_path / ".read_counts.json"))


# Session-scoped QApplication fixture (pytest-qt)
@pytest.fixture(scope="session")
def qapp():
    """Create a single QApplication instance for the test session.

    Guards against missing Qt so the fixture skips gracefully when
    pytest-qt is not installed in the dev environment.
    """
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        pytest.skip("PySide6 not installed — Qt fixture unavailable")

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app
    app.quit()


def make_manifest(**overrides: object) -> ExecutionManifest:
    defaults: dict[str, object] = {
        "ticket_id": "WOR-10",
        "epic_id": "WOR-96",
        "title": "Test ticket",
        "priority": 2,
        "status": "ReadyForLocal",
        "parallel_safe": True,
        "risk_level": "low",
        "implementation_mode": "local",
        "routing": "local",
        "review_mode": "auto",
        "base_branch": "wor-96-local-worker-engine",
        "worker_branch": "wor-10-test-ticket",
        "objective": "Do the thing.",
        "artifact_paths": ArtifactPaths.from_ticket_id("WOR-10"),
        "allowed_paths": ["app/core/foo.py"],
        # WOR-378: dispatch refuses manifests with empty required_checks, so
        # the conftest fixture defaults a non-empty list. Tests that exercise
        # the empty-required_checks path can override explicitly.
        "required_checks": ["pytest"],
    }
    defaults.update(overrides)
    return ExecutionManifest(**defaults)  # type: ignore[arg-type]


_SENTINEL: list[str] = ["app/core/bar.py"]


def make_active_worker(
    ticket_id: str = "WOR-11", allowed_paths: list[str] | None = None
) -> ActiveWorker:
    paths = _SENTINEL if allowed_paths is None else allowed_paths
    manifest = make_manifest(
        ticket_id=ticket_id,
        worker_branch=f"wor-{ticket_id.lower().replace('-', '')}-branch",
        artifact_paths=ArtifactPaths.from_ticket_id(ticket_id),
        allowed_paths=paths,
    )
    return ActiveWorker(
        ticket_id=ticket_id,
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=Path(f"/tmp/{ticket_id}"),
        process=MagicMock(spec=subprocess.Popen),
    )


# ---------------------------------------------------------------------------
# WOR-506 L5: identity-keyed mock-runner helper
# ---------------------------------------------------------------------------


def make_command_keyed_run(
    returncodes: dict[str, int],
    *,
    default_returncode: int = 0,
    record: list[str] | None = None,
) -> Any:
    """Build a ``subprocess.run`` / ``run_checks`` ``side_effect`` keyed on
    COMMAND IDENTITY — never on call order.

    WOR-506: a ``side_effect`` that branches on ``mock.call_count`` (e.g.
    ``return fail if mock.call_count == 1``) races whenever the SUT issues
    the calls *concurrently* — ``run_checks`` runs the 4 required checks in
    parallel (WOR-413); ``finalize_worker`` fetches Sonar in a thread while
    checks run (WOR-451 / WOR-465). Under concurrency the call that arrives
    "first" is nondeterministic, so an order-keyed fake flakes (this was the
    #1042 ``test_run_checks_returns_false_on_check_failure`` race).

    Match on *what the command is* instead. ``returncodes`` maps a substring
    of the joined command to the exit code to return when that substring is
    present (first match wins by dict order); anything unmatched returns
    ``default_returncode``. Pass ``record`` to capture the joined commands
    in invocation order for post-hoc assertions (order-independent counts
    are still safe; order-keyed dispatch is the antipattern).

    Example — make only ``ruff`` fail, regardless of which check the
    concurrent ``run_checks`` happens to launch first::

        run = make_command_keyed_run({"ruff": 1})
        with patch("…watcher_subprocess.subprocess.run", side_effect=run):
            ...
    """

    def _run(cmd: Any, **_kwargs: Any) -> MagicMock:
        if isinstance(cmd, (list, tuple)):
            joined = " ".join(str(c) for c in cmd)
        else:
            joined = str(cmd)
        if record is not None:
            record.append(joined)
        rc = default_returncode
        for needle, code in returncodes.items():
            if needle in joined:
                rc = code
                break
        result = MagicMock()
        result.returncode = rc
        result.stdout = ""
        result.stderr = ""
        return result

    return _run


# ---------------------------------------------------------------------------
# WOR-511: isolated finalize repo_root
# ---------------------------------------------------------------------------


def make_isolated_repo_root() -> Path:
    """A unique throwaway dir for a `_call_finalize(...)` default `repo_root`.

    WOR-511: the three finalize test harnesses defaulted `repo_root` to
    ``Path(".")`` — the real shared repo cwd. `finalize_worker` then
    resolved ``repo_root / artifact_paths.result_json`` and the WOR-457
    `last_failure.json` writer to ``./.claude/artifacts/<slug>/`` (slug is
    the conftest `make_manifest` default `wor_10` for every ticket), so
    concurrent finalize tests on different `pytest -n8` xdist workers
    clobbered the one real path (`PermissionError [WinError 32]` on
    Windows; silent corruption on Linux). A fresh per-call dir makes every
    default-`repo_root` finalize run collision-proof regardless of xdist
    schedule or the shared slug. Same cross-worker shared-FS bleed class as
    the watcher pid file / read-cap state file (WOR-506).

    Tests that assert on artifact contents pass an explicit `repo_root`
    (usually their own `tmp_path`); this only replaces the unsafe default.
    """
    return Path(tempfile.mkdtemp(prefix="wor511_finalize_repo_"))


# ---------------------------------------------------------------------------
# WOR-510 PR-c: dispatch context
# ---------------------------------------------------------------------------


def make_dispatch_context(**overrides: Any) -> Any:
    """Build a ``dispatch.DispatchContext`` with inert test defaults.

    WOR-510 PR-c: ``start_ticket``'s 11 watcher-cycle params were bundled
    into ``DispatchContext`` (S107). Mirrors make_manifest /
    make_active_worker — tests override only the fields they assert on
    (local_active, repo_root, linear, services …); the rest default to
    inert MagicMock / empty values. ``repo_root`` defaults to an isolated
    tmp dir (WOR-511 — never the shared cwd).
    """
    from app.core.watcher.dispatch import DispatchContext

    services = MagicMock()
    services._mode = "default"
    defaults: dict[str, Any] = {
        "linear": MagicMock(),
        "services": services,
        "worker_verbose": False,
        "local_active": [],
        "cloud_active": [],
        "max_cloud_workers": 3,
        "repo_root": make_isolated_repo_root(),
        "processed_tickets": [],
        "escalation_policy": MagicMock(),
        "dedup_state": {},
        "kv_budget": None,
    }
    defaults.update(overrides)
    return DispatchContext(**defaults)
