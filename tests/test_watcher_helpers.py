"""Tests for app.core.watcher_helpers."""

from __future__ import annotations

from pathlib import Path

from app.core.watcher.watcher_helpers import (
    DEFAULT_KV_RESERVATION,
    KV_BUDGET_TOKENS,
    build_worker_cmd,
    build_worker_env,
    check_allowed_paths_overlap,
    count_main_ahead_of_epic,
    kv_admission_ok,
    resolve_effective_mode,
)
from tests.conftest import make_active_worker, make_manifest

# ---------------------------------------------------------------------------
# check_allowed_paths_overlap
# ---------------------------------------------------------------------------


def test_overlap_when_paths_share_entry() -> None:
    active = [make_active_worker("WOR-11", allowed_paths=["app/core/foo.py"])]
    candidate = make_manifest(allowed_paths=["app/core/foo.py"])
    assert check_allowed_paths_overlap(active, candidate) == ["WOR-11"]


def test_no_overlap_when_paths_are_disjoint() -> None:
    active = [make_active_worker("WOR-11", allowed_paths=["app/core/bar.py"])]
    candidate = make_manifest(allowed_paths=["app/core/foo.py"])
    assert check_allowed_paths_overlap(active, candidate) == []


def test_empty_candidate_paths_conflicts_with_all() -> None:
    active = [make_active_worker("WOR-11", allowed_paths=["app/core/bar.py"])]
    candidate = make_manifest(allowed_paths=[])
    assert check_allowed_paths_overlap(active, candidate) == ["WOR-11"]


def test_empty_active_paths_conflicts_with_candidate() -> None:
    active = [make_active_worker("WOR-11", allowed_paths=[])]
    candidate = make_manifest(allowed_paths=["app/core/foo.py"])
    assert check_allowed_paths_overlap(active, candidate) == ["WOR-11"]


def test_multiple_active_partial_overlap() -> None:
    active = [
        make_active_worker("WOR-11", allowed_paths=["app/core/foo.py"]),
        make_active_worker("WOR-12", allowed_paths=["app/core/baz.py"]),
    ]
    candidate = make_manifest(allowed_paths=["app/core/foo.py"])
    assert check_allowed_paths_overlap(active, candidate) == ["WOR-11"]


# WOR-410: __init__.py overlap carve-out


def test_init_py_only_overlap_does_not_block() -> None:
    """Two manifests whose only shared path is __init__.py may dispatch concurrently.

    Append-only barrel files (re-export __init__.py) admit commutative edits
    in the typical case — each worker registers its own new module name. The
    overlap gate skips them so package-split waves don't serialize.
    """
    active = [
        make_active_worker(
            "WOR-11",
            allowed_paths=[
                "app/core/watcher/watcher_signals.py",
                "app/core/watcher/__init__.py",
            ],
        )
    ]
    candidate = make_manifest(
        allowed_paths=[
            "app/core/watcher/watcher_promotion.py",
            "app/core/watcher/__init__.py",
        ]
    )
    assert check_allowed_paths_overlap(active, candidate) == []


def test_non_init_overlap_still_blocks() -> None:
    """The carve-out is __init__.py only — every other shared file still conflicts."""
    active = [
        make_active_worker("WOR-11", allowed_paths=["app/core/watcher/watcher.py"])
    ]
    candidate = make_manifest(allowed_paths=["app/core/watcher/watcher.py"])
    assert check_allowed_paths_overlap(active, candidate) == ["WOR-11"]


def test_overlap_on_init_plus_real_file_still_blocks() -> None:
    """If the intersection contains __init__.py *and* a regular file, it still
    blocks — the regular file is the real conflict surface, not the barrel."""
    active = [
        make_active_worker(
            "WOR-11",
            allowed_paths=[
                "app/core/watcher/watcher.py",
                "app/core/watcher/__init__.py",
            ],
        )
    ]
    candidate = make_manifest(
        allowed_paths=[
            "app/core/watcher/watcher.py",
            "app/core/watcher/__init__.py",
        ]
    )
    assert check_allowed_paths_overlap(active, candidate) == ["WOR-11"]


def test_root_level_init_py_treated_as_append_only() -> None:
    """A bare 'app/__init__.py' entry should also count as the barrel — the
    helper checks for the literal name and any '/__init__.py' suffix."""
    active = [make_active_worker("WOR-11", allowed_paths=["app/__init__.py"])]
    candidate = make_manifest(allowed_paths=["app/__init__.py"])
    assert check_allowed_paths_overlap(active, candidate) == []


# ---------------------------------------------------------------------------
# build_worker_env
# ---------------------------------------------------------------------------


def test_cloud_mode_strips_base_url() -> None:
    base = {
        "ANTHROPIC_BASE_URL": "http://localhost:8000",
        "PATH": "/usr/bin",
        "HOME": "/root",
    }
    env = build_worker_env("cloud", base)
    assert "ANTHROPIC_BASE_URL" not in env
    assert env["PATH"] == "/usr/bin"


def test_cloud_mode_strips_model_var() -> None:
    base = {"ANTHROPIC_MODEL": "qwen3-coder", "PATH": "/usr/bin"}
    env = build_worker_env("cloud", base)
    assert "ANTHROPIC_MODEL" not in env


def test_local_mode_injects_vllm_base_url() -> None:
    """WOR-368: local mode points ANTHROPIC_BASE_URL at vLLM directly (port 8000),
    not the retired LiteLLM proxy on 8082."""
    base = {"PATH": "/usr/bin"}
    env = build_worker_env("local", base)
    assert env["ANTHROPIC_BASE_URL"] == "http://localhost:8000"


def test_local_env_routes_by_tier() -> None:
    """WOR-368: with no --model on the cmd, Claude Code picks a model by tier
    via ANTHROPIC_DEFAULT_*_MODEL. All three must point at the vLLM-served name."""
    env = build_worker_env("local", {"PATH": "/usr/bin"})
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "qwen3-coder"
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "qwen3-coder"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "qwen3-coder"


def test_local_env_sets_dummy_auth_credentials() -> None:
    """vLLM does not validate the API key, but Claude Code requires the env var
    to be present. 'dummy' is the vLLM-doc-recommended placeholder."""
    env = build_worker_env("local", {"PATH": "/usr/bin"})
    assert env["ANTHROPIC_API_KEY"] == "dummy"  # pragma: allowlist secret
    assert env["ANTHROPIC_AUTH_TOKEN"] == "dummy"  # pragma: allowlist secret


def test_default_mode_passes_env_unchanged() -> None:
    base = {"ANTHROPIC_BASE_URL": "http://localhost:8000", "PATH": "/usr/bin"}
    env = build_worker_env("default", base)
    assert env == base


def test_local_mode_sets_watcher_worker_flag() -> None:
    """WOR-391: local-mode worker subprocesses get WATCHER_WORKER=1 in env so
    env-divergent tests (e.g. test_contribute_skills_workflow) can self-skip."""
    env = build_worker_env("local", {"PATH": "/usr/bin"})
    assert env["WATCHER_WORKER"] == "1"


def test_cloud_mode_sets_watcher_worker_flag() -> None:
    """WOR-391: cloud-mode worker subprocesses also get WATCHER_WORKER=1."""
    env = build_worker_env("cloud", {"PATH": "/usr/bin"})
    assert env["WATCHER_WORKER"] == "1"


def test_default_mode_does_not_set_watcher_worker_flag() -> None:
    """Default mode is a fallback for non-watcher contexts (tests, edge cases);
    WATCHER_WORKER must NOT be set there to avoid breaking the WOR-391 skip
    contract for callers that build env manually for non-worker uses."""
    env = build_worker_env("default", {"PATH": "/usr/bin"})
    assert "WATCHER_WORKER" not in env


def test_cloud_mode_does_not_inject_base_url_if_absent() -> None:
    base = {"PATH": "/usr/bin"}
    env = build_worker_env("cloud", base)
    assert "ANTHROPIC_BASE_URL" not in env


# ---------------------------------------------------------------------------
# build_worker_cmd
# ---------------------------------------------------------------------------


def test_cloud_cmd_has_no_model_flag(tmp_path: Path) -> None:
    cmd = build_worker_cmd("WOR-10", "cloud", tmp_path)
    assert "--model" not in cmd
    assert "/implement-ticket WOR-10" in " ".join(cmd)


def test_local_cmd_omits_model_flag(tmp_path: Path) -> None:
    """WOR-368: local mode no longer passes --model. vLLM only serves
    'qwen3-coder', so a hard-coded 'claude-sonnet-4-6' would fail Claude
    Code's /v1/models validation. Routing happens via
    ANTHROPIC_DEFAULT_*_MODEL env vars (see test_local_env_routes_by_tier)."""
    cmd = build_worker_cmd("WOR-10", "local", tmp_path)
    assert "--model" not in cmd


def test_cmd_includes_dangerously_skip_permissions(tmp_path: Path) -> None:
    for mode in ("cloud", "local"):
        cmd = build_worker_cmd("WOR-10", mode, tmp_path)
        assert "--dangerously-skip-permissions" in cmd


def test_local_cmd_uses_worktree_path(tmp_path: Path) -> None:
    cmd = build_worker_cmd("WOR-10", "local", tmp_path)
    assert "--bare" not in cmd
    idx = cmd.index("--add-dir")
    assert cmd[idx + 1] == str(tmp_path)


def test_no_bare_flag_in_any_mode(tmp_path: Path) -> None:
    for mode in ("cloud", "local"):
        cmd = build_worker_cmd("WOR-10", mode, tmp_path)
        assert "--bare" not in cmd, f"--bare should not appear in {mode} mode"


def test_cmd_disallowed_tools_appended(tmp_path: Path) -> None:
    tools = ["Read(*watcher.py)", "Read(*metrics.py)"]
    cmd = build_worker_cmd("WOR-10", "cloud", tmp_path, disallowed_tools=tools)
    assert "--disallowed-tools" in cmd
    idx = cmd.index("--disallowed-tools")
    assert cmd[idx + 1] == "Read(*watcher.py),Read(*metrics.py)"


def test_cmd_no_disallowed_tools_when_none(tmp_path: Path) -> None:
    cmd = build_worker_cmd("WOR-10", "cloud", tmp_path, disallowed_tools=None)
    assert "--disallowed-tools" not in cmd


def test_cmd_uses_empty_mcp_config_by_default(tmp_path: Path) -> None:
    cmd = build_worker_cmd("WOR-10", "cloud", tmp_path)
    assert "--mcp-config" in cmd
    idx = cmd.index("--mcp-config")
    assert cmd[idx + 1] == '{"mcpServers":{}}'


def test_cmd_uses_custom_mcp_config_when_provided(tmp_path: Path) -> None:
    config = '{"mcpServers":{"linear-server":{"type":"http","url":"https://mcp.linear.app/mcp"}}}'
    cmd = build_worker_cmd("WOR-10", "cloud", tmp_path, mcp_config_json=config)
    assert "--mcp-config" in cmd
    idx = cmd.index("--mcp-config")
    assert cmd[idx + 1] == config


# ---------------------------------------------------------------------------
# build_worker_cmd — effort (WOR-214)
# ---------------------------------------------------------------------------


def test_build_worker_cmd_with_explicit_effort(tmp_path: Path) -> None:
    cmd = build_worker_cmd("WOR-10", "local", tmp_path, effort="high")
    assert "--effort" in cmd
    idx = cmd.index("--effort")
    assert cmd[idx + 1] == "high"


def test_build_worker_cmd_effort_none_local_falls_back_to_xhigh(tmp_path: Path) -> None:
    cmd = build_worker_cmd("WOR-10", "local", tmp_path, effort=None)
    assert "--effort" in cmd
    idx = cmd.index("--effort")
    assert cmd[idx + 1] == "xhigh"


def test_build_worker_cmd_effort_none_cloud_falls_back_to_max(tmp_path: Path) -> None:
    cmd = build_worker_cmd("WOR-10", "cloud", tmp_path, effort=None)
    assert "--effort" in cmd
    idx = cmd.index("--effort")
    assert cmd[idx + 1] == "max"


def test_build_worker_cmd_explicit_effort_ignored_by_mode(tmp_path: Path) -> None:
    """Explicit effort value is used regardless of mode."""
    for mode in ("local", "cloud"):
        cmd = build_worker_cmd("WOR-10", mode, tmp_path, effort="low")
        assert "--effort" in cmd
        idx = cmd.index("--effort")
        assert cmd[idx + 1] == "low"


# ---------------------------------------------------------------------------
# resolve_effective_mode — 4-way routing matrix (WOR-290)
# ---------------------------------------------------------------------------


def test_worker_mode_cloud_routes_all_to_cloud() -> None:
    for routing in ("local", "cloud_preferred", "cloud_only"):
        assert resolve_effective_mode("cloud", routing) == "cloud"


def test_worker_mode_local_refuses_cloud_only() -> None:
    assert resolve_effective_mode("local", "cloud_only") == "refused"


def test_worker_mode_local_allows_local_routing() -> None:
    assert resolve_effective_mode("local", "local") == "local"


def test_worker_mode_local_allows_cloud_preferred() -> None:
    """cloud_preferred under local mode falls back to local."""
    assert resolve_effective_mode("local", "cloud_preferred") == "local"


def test_default_mode_local_routing() -> None:
    assert resolve_effective_mode("default", "local") == "local"


def test_default_mode_cloud_preferred() -> None:
    assert resolve_effective_mode("default", "cloud_preferred") == "cloud"


def test_default_mode_cloud_only() -> None:
    assert resolve_effective_mode("default", "cloud_only") == "cloud"


# ---------------------------------------------------------------------------
# count_main_ahead_of_epic — WOR-373 stale-epic detection
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> None:
    import subprocess  # nosec B404

    subprocess.run(  # nosec B603 B607
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def _setup_remote_and_main(tmp_path: Path) -> tuple[Path, Path]:
    """Create a bare 'origin' repo and a working clone with a main branch.

    Returns (origin_path, clone_path). Caller can then create epic branches
    and push them to origin to simulate the watcher's view of the repo.
    """
    origin = tmp_path / "origin.git"
    _git(["init", "-q", "--bare", "-b", "main", str(origin)], cwd=tmp_path)

    clone = tmp_path / "clone"
    _git(["clone", "-q", str(origin), str(clone)], cwd=tmp_path)
    _git(["config", "user.email", "test@example.com"], cwd=clone)
    _git(["config", "user.name", "Test"], cwd=clone)
    (clone / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "README.md"], cwd=clone)
    _git(["commit", "-q", "-m", "seed"], cwd=clone)
    _git(["push", "-q", "origin", "main"], cwd=clone)
    return origin, clone


def test_count_main_ahead_of_epic_returns_zero_for_main_branch(
    tmp_path: Path,
) -> None:
    """Sub-tickets targeting main directly are not subject to the check."""
    _, clone = _setup_remote_and_main(tmp_path)
    assert count_main_ahead_of_epic("main", clone) == 0


def test_count_main_ahead_of_epic_returns_zero_for_random_branch(
    tmp_path: Path,
) -> None:
    """Branches that don't start with `epic/` are not checked."""
    _, clone = _setup_remote_and_main(tmp_path)
    assert count_main_ahead_of_epic("feat/something", clone) == 0
    assert count_main_ahead_of_epic("wor-100-some-ticket", clone) == 0


def test_count_main_ahead_of_epic_returns_zero_when_in_sync(
    tmp_path: Path,
) -> None:
    """An epic branch at the same commit as main has zero drift."""
    _, clone = _setup_remote_and_main(tmp_path)
    _git(["checkout", "-b", "epic/wor-100-fresh"], cwd=clone)
    _git(["push", "-q", "origin", "epic/wor-100-fresh"], cwd=clone)
    assert count_main_ahead_of_epic("epic/wor-100-fresh", clone) == 0


def test_count_main_ahead_of_epic_returns_drift_count(tmp_path: Path) -> None:
    """An epic that's N commits behind main returns N."""
    _, clone = _setup_remote_and_main(tmp_path)
    # Create epic from current main
    _git(["checkout", "-b", "epic/wor-100-stale"], cwd=clone)
    _git(["push", "-q", "origin", "epic/wor-100-stale"], cwd=clone)
    # Add 5 commits to main only
    _git(["checkout", "main"], cwd=clone)
    for i in range(5):
        f = clone / f"file_{i}.md"
        f.write_text(f"content {i}\n", encoding="utf-8")
        _git(["add", str(f)], cwd=clone)
        _git(["commit", "-q", "-m", f"commit {i}"], cwd=clone)
    _git(["push", "-q", "origin", "main"], cwd=clone)
    # Epic is 5 commits behind main now
    assert count_main_ahead_of_epic("epic/wor-100-stale", clone) == 5


def test_count_main_ahead_of_epic_fails_open_outside_git(tmp_path: Path) -> None:
    """When cwd is not a git repo, the helper returns 0 (does not crash)."""
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()
    assert count_main_ahead_of_epic("epic/wor-100-stale", not_a_repo) == 0


def test_count_main_ahead_of_epic_fails_open_when_branch_missing(
    tmp_path: Path,
) -> None:
    """If the epic branch doesn't exist on origin, return 0 (do not block)."""
    _, clone = _setup_remote_and_main(tmp_path)
    assert count_main_ahead_of_epic("epic/wor-100-nonexistent", clone) == 0


# ---------------------------------------------------------------------------
# KV-budget admission control (WOR-502)
# ---------------------------------------------------------------------------


def test_kv_admission_empty_pool_allows_candidate() -> None:
    """Empty in-flight pool: any candidate with reservation <= budget admits."""
    assert kv_admission_ok([], "low", budget=KV_BUDGET_TOKENS) is True
    assert (
        kv_admission_ok([], "max", budget=KV_BUDGET_TOKENS) is True
    )  # 130000 <= 133934


def test_kv_admission_unknown_effort_uses_default() -> None:
    """Unknown / None effort falls back to DEFAULT_KV_RESERVATION."""
    assert kv_admission_ok([], None, budget=KV_BUDGET_TOKENS) is True
    assert (
        kv_admission_ok([], "low", budget=DEFAULT_KV_RESERVATION) is True
    )  # 33000 <= 90000
    assert (
        kv_admission_ok([], "max", budget=DEFAULT_KV_RESERVATION) is False
    )  # 130000 > 90000


def test_kv_admission_mixed_batch_respects_budget() -> None:
    """A max-effort worker (130000) already in flight blocks a second max."""
    assert (
        kv_admission_ok(["max"], "max", budget=KV_BUDGET_TOKENS) is False
    )  # 130k+130k > 133934
    # low + low should still fit: 33k+33k=66k <= 133934
    assert (
        kv_admission_ok(["low", "low"], "low", budget=KV_BUDGET_TOKENS) is True
    )  # 99k <= 133934
    # low + low + low = 99k, add high (67k) = 166k > 133934
    assert (
        kv_admission_ok(["low", "low", "low"], "high", budget=KV_BUDGET_TOKENS) is False
    )


def test_kv_admission_all_small_packs_wide() -> None:
    """All-low workers should allow several concurrent (budget // 33000)."""
    # 3 × low = 99000 <= 133934 + 1 more low = 132000 <= 133934
    assert kv_admission_ok(["low"] * 3, "low", budget=KV_BUDGET_TOKENS) is True
    # 4 × low = 132000 <= 133934 + 1 more low = 165000 > 133934
    assert kv_admission_ok(["low"] * 4, "low", budget=KV_BUDGET_TOKENS) is False


def test_kv_admission_max_effort_runs_alone() -> None:
    """A single max-effort ticket (130000) admits when pool is empty."""
    assert (
        kv_admission_ok([], "max", budget=KV_BUDGET_TOKENS) is True
    )  # 130000 <= 133934


def test_kv_admission_custom_budget() -> None:
    """A custom budget caps the admission regardless of effort levels."""
    assert (
        kv_admission_ok(["high"], "high", budget=100_000) is False
    )  # 67000+67000=134000 > 100000
    assert (
        kv_admission_ok(["medium"], "medium", budget=90_000) is True
    )  # 45000+45000=90000 <= 90000
    assert (
        kv_admission_ok(["medium", "high"], "low", budget=100_000) is False
    )  # 45k+67k+33k=145k > 100k
