"""Escalation policy for the local worker engine.

Loads config/escalation_policy.toml and exposes typed classification methods
so the watcher can decide — without hardcoded logic — when to escalate a local
worker session to cloud LLM.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, field_validator

Action = Literal["escalate", "fix_locally", "human"]

_VALID_SONAR_SEVERITIES = frozenset({"blocker", "critical", "major", "minor", "info"})
_VALID_HUMAN_TRIGGERS = frozenset(
    {
        "architecture_change",
        "schema_migration",
        "cross_module_refactor",
        "auth_payments_touched",
    }
)
_VALID_ACTIONS: frozenset[str] = frozenset({"escalate", "fix_locally", "human"})

DEFAULT_POLICY_PATH = (
    Path(__file__).parent.parent.parent / "config" / "escalation_policy.toml"
)


class RetryConfig(BaseModel):
    model_config = {"extra": "forbid"}

    max_consecutive_failures: int

    @field_validator("max_consecutive_failures")
    @classmethod
    def positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_consecutive_failures must be >= 1")
        return v


class AutoEscalateConfig(BaseModel):
    model_config = {"extra": "forbid"}

    scope_drift: Action
    forbidden_path_touched: Action
    import_linter_violation: Action
    security_blocker: Action


class HumanEscalateConfig(BaseModel):
    model_config = {"extra": "forbid"}

    architecture_change: Action
    schema_migration: Action
    cross_module_refactor: Action
    auth_payments_touched: Action


class SonarConfig(BaseModel):
    model_config = {"extra": "forbid"}

    blocker: Action
    critical: Action
    major: Action
    minor: Action
    info: Action


class ImprovementLogConfig(BaseModel):
    model_config = {"extra": "forbid"}

    ticket_id: str
    review_threshold: int = 15
    runtime_threshold_minutes: int = 60


class EscalationPolicy(BaseModel):
    """Typed representation of escalation_policy.toml."""

    model_config = {"extra": "forbid"}

    retry: RetryConfig
    auto_escalate: AutoEscalateConfig
    human_escalate: HumanEscalateConfig
    sonar: SonarConfig
    improvement_log: ImprovementLogConfig | None = None

    # ------------------------------------------------------------------
    # Classification helpers
    # ------------------------------------------------------------------

    def classify_result(
        self,
        *,
        scope_drift: bool = False,
        forbidden_path_touched: bool = False,
        import_linter_violation: bool = False,
        security_blocker: bool = False,
    ) -> Action:
        """Return the action for a worker result artifact.

        Checks flags in priority order: the first truthy flag wins.
        Returns "fix_locally" when no flags are set.
        """
        if scope_drift:
            return self.auto_escalate.scope_drift
        if forbidden_path_touched:
            return self.auto_escalate.forbidden_path_touched
        if import_linter_violation:
            return self.auto_escalate.import_linter_violation
        if security_blocker:
            return self.auto_escalate.security_blocker
        return "fix_locally"

    def classify_human_trigger(self, trigger: str) -> Action:
        """Return the action for a human-escalation trigger name.

        Raises ValueError for unrecognised trigger names so that unknown
        triggers fail loudly rather than being silently ignored.
        """
        trigger = trigger.lower()
        if trigger not in _VALID_HUMAN_TRIGGERS:
            raise ValueError(
                f"Unknown human trigger {trigger!r}. "
                f"Valid values: {sorted(_VALID_HUMAN_TRIGGERS)}"
            )
        return cast(Action, getattr(self.human_escalate, trigger))

    def classify_sonar_finding(self, severity: str) -> Action:
        """Return the action for a SonarLint/SonarCloud finding severity.

        Returns "fix_locally" for unrecognised severity strings so that unknown
        findings are handled safely rather than raising an error.
        """
        severity = severity.lower()
        if severity not in _VALID_SONAR_SEVERITIES:
            return "fix_locally"
        return cast(Action, getattr(self.sonar, severity))

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_toml(cls, path: Path | str | None = None) -> "EscalationPolicy":
        """Load and validate escalation policy from a TOML file.

        Uses DEFAULT_POLICY_PATH when path is None.
        Raises FileNotFoundError if the file is missing.
        Raises pydantic.ValidationError if the config is structurally invalid.
        """
        resolved = Path(path) if path is not None else DEFAULT_POLICY_PATH
        if ".." in resolved.parts:
            raise ValueError(f"Policy path must not contain '..': {path!r}")
        raw = resolved.read_bytes()
        data = tomllib.loads(raw.decode())
        return cls.model_validate(data)
