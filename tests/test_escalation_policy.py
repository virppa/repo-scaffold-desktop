"""Tests for app.core.escalation_policy."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.escalation_policy import EscalationPolicy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_policy(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "escalation_policy.toml"
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


MINIMAL_VALID_TOML = """
    [retry]
    max_consecutive_failures = 3

    [auto_escalate]
    scope_drift = "escalate"
    forbidden_path_touched = "escalate"
    import_linter_violation = "escalate"
    security_blocker = "escalate"

    [human_escalate]
    architecture_change = "human"
    schema_migration = "human"
    cross_module_refactor = "human"
    auth_payments_touched = "human"

    [sonar]
    blocker = "escalate"
    critical = "escalate"
    major = "fix_locally"
    minor = "fix_locally"
    info = "fix_locally"
"""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def test_policy_loads_from_toml(tmp_path: Path) -> None:
    path = write_policy(tmp_path, MINIMAL_VALID_TOML)
    policy = EscalationPolicy.from_toml(path)
    assert policy.retry.max_consecutive_failures == 3
    assert policy.sonar.blocker == "escalate"


def test_default_policy_path_loads() -> None:
    policy = EscalationPolicy.from_toml()
    assert policy.retry.max_consecutive_failures >= 1


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        EscalationPolicy.from_toml(tmp_path / "nonexistent.toml")


def test_missing_section_raises(tmp_path: Path) -> None:
    toml = "[retry]\nmax_consecutive_failures = 3\n"
    path = write_policy(tmp_path, toml)
    with pytest.raises(ValidationError):
        EscalationPolicy.from_toml(path)


def test_extra_keys_raise(tmp_path: Path) -> None:
    toml = MINIMAL_VALID_TOML + "\n[unexpected_section]\nfoo = 1\n"
    path = write_policy(tmp_path, toml)
    with pytest.raises(ValidationError):
        EscalationPolicy.from_toml(path)


def test_path_traversal_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must not contain"):
        EscalationPolicy.from_toml(tmp_path / ".." / "escalation_policy.toml")


# ---------------------------------------------------------------------------
# classify_result — auto escalation triggers
# ---------------------------------------------------------------------------


@pytest.fixture()
def policy(tmp_path: Path) -> EscalationPolicy:
    path = write_policy(tmp_path, MINIMAL_VALID_TOML)
    return EscalationPolicy.from_toml(path)


def test_scope_drift_triggers_escalation(policy: EscalationPolicy) -> None:
    assert policy.classify_result(scope_drift=True) == "escalate"


def test_forbidden_path_triggers_escalation(policy: EscalationPolicy) -> None:
    assert policy.classify_result(forbidden_path_touched=True) == "escalate"


def test_import_linter_violation_triggers_escalation(policy: EscalationPolicy) -> None:
    assert policy.classify_result(import_linter_violation=True) == "escalate"


def test_security_blocker_triggers_escalation(policy: EscalationPolicy) -> None:
    assert policy.classify_result(security_blocker=True) == "escalate"


def test_no_flags_returns_fix_locally(policy: EscalationPolicy) -> None:
    assert policy.classify_result() == "fix_locally"


def test_scope_drift_wins_over_other_flags(policy: EscalationPolicy) -> None:
    # Priority order: scope_drift is checked first.
    result = policy.classify_result(scope_drift=True, forbidden_path_touched=True)
    assert result == "escalate"


# ---------------------------------------------------------------------------
# classify_sonar_finding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "severity,expected",
    [
        ("blocker", "escalate"),
        ("critical", "escalate"),
        ("major", "fix_locally"),
        ("minor", "fix_locally"),
        ("info", "fix_locally"),
        ("BLOCKER", "escalate"),
        ("Minor", "fix_locally"),
        ("unknown_severity", "fix_locally"),
    ],
)
def test_classify_sonar_finding(
    policy: EscalationPolicy, severity: str, expected: str
) -> None:
    assert policy.classify_sonar_finding(severity) == expected


# ---------------------------------------------------------------------------
# classify_human_trigger
# ---------------------------------------------------------------------------


def test_human_trigger_architecture_change(policy: EscalationPolicy) -> None:
    assert policy.classify_human_trigger("architecture_change") == "human"


def test_human_trigger_schema_migration(policy: EscalationPolicy) -> None:
    assert policy.classify_human_trigger("schema_migration") == "human"


def test_human_trigger_cross_module_refactor(policy: EscalationPolicy) -> None:
    assert policy.classify_human_trigger("cross_module_refactor") == "human"


def test_human_trigger_auth_payments_touched(policy: EscalationPolicy) -> None:
    assert policy.classify_human_trigger("auth_payments_touched") == "human"


def test_human_trigger_case_insensitive(policy: EscalationPolicy) -> None:
    assert policy.classify_human_trigger("ARCHITECTURE_CHANGE") == "human"
    assert policy.classify_human_trigger("Schema_Migration") == "human"


def test_human_trigger_unknown_raises(policy: EscalationPolicy) -> None:
    with pytest.raises(ValueError, match="Unknown human trigger"):
        policy.classify_human_trigger("unknown_trigger")


def test_human_trigger_auto_escalate_key_rejected(policy: EscalationPolicy) -> None:
    with pytest.raises(ValueError, match="Unknown human trigger"):
        policy.classify_human_trigger("scope_drift")


# ---------------------------------------------------------------------------
# Retry config
# ---------------------------------------------------------------------------


def test_retry_limit_from_policy(policy: EscalationPolicy) -> None:
    assert policy.retry.max_consecutive_failures == 3


def test_retry_zero_raises(tmp_path: Path) -> None:
    toml = MINIMAL_VALID_TOML.replace(
        "max_consecutive_failures = 3", "max_consecutive_failures = 0"
    )
    path = write_policy(tmp_path, toml)
    with pytest.raises(ValidationError):
        EscalationPolicy.from_toml(path)


# ---------------------------------------------------------------------------
# Improvement log config
# ---------------------------------------------------------------------------


def test_improvement_log_section_loads(tmp_path: Path) -> None:
    toml = MINIMAL_VALID_TOML + "\n[improvement_log]\nticket_id = 'WOR-254'\n"
    path = write_policy(tmp_path, toml)
    policy = EscalationPolicy.from_toml(path)
    assert policy.improvement_log is not None
    assert policy.improvement_log.ticket_id == "WOR-254"


def test_improvement_log_defaults(tmp_path: Path) -> None:
    toml = MINIMAL_VALID_TOML + "\n[improvement_log]\nticket_id = 'WOR-254'\n"
    path = write_policy(tmp_path, toml)
    policy = EscalationPolicy.from_toml(path)
    assert policy.improvement_log.review_threshold == 15
    assert policy.improvement_log.runtime_threshold_minutes == 60


def test_improvement_log_custom_threshold(tmp_path: Path) -> None:
    toml = (
        MINIMAL_VALID_TOML + "\n[improvement_log]\nticket_id = 'WOR-254'\n"
        "review_threshold = 10\n"
        "runtime_threshold_minutes = 30\n"
    )
    path = write_policy(tmp_path, toml)
    policy = EscalationPolicy.from_toml(path)
    assert policy.improvement_log.review_threshold == 10
    assert policy.improvement_log.runtime_threshold_minutes == 30


def test_improvement_log_absent_is_none(tmp_path: Path) -> None:
    path = write_policy(tmp_path, MINIMAL_VALID_TOML)
    policy = EscalationPolicy.from_toml(path)
    assert policy.improvement_log is None


def test_improvement_log_extra_keys_raise(tmp_path: Path) -> None:
    toml = (
        MINIMAL_VALID_TOML + "\n[improvement_log]\nticket_id = 'WOR-254'\n"
        "fake_key = 123\n"
    )
    path = write_policy(tmp_path, toml)
    with pytest.raises(ValidationError):
        EscalationPolicy.from_toml(path)
