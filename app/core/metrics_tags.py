"""Auto-detected tag rules for ticket metrics.

Pure functions — no I/O, no side effects. Easy to unit-test.

Anomaly rules originate in the 2026-05-03 retro; categorization rules
draw on result.json signals and metric thresholds (incl. WOR-366
backend_unstable cutoff).
"""

from __future__ import annotations

from typing import Any

from app.core.metrics_models import TicketMetrics


def compute_tags(
    ticket_metrics_row: TicketMetrics,
    result_json_status: str,
    result_json_flags: dict[str, bool] | None = None,
    tracked_prs: list[Any] | None = None,
) -> list[str]:
    """Return auto-detected tags for a ticket metrics row.

    Pure function — no I/O, no logging, no side effects.  Easy to unit-test.

    Four anomaly-detection rules (from the 2026-05-03 retro) and five
    categorization rules (from existing result.json signals).

    Args:
        ticket_metrics_row: The TicketMetrics record as written by finalize_worker.
        result_json_status: The ``status`` field from the worker's result.json.
        result_json_flags: Parsed flags dict from result.json (scope_drift, etc.).
        tracked_prs: List of TrackedPR objects that were populated during PR creation.

    Returns:
        A list of tag name strings.  May be empty.
    """
    return [
        *_anomaly_tags(ticket_metrics_row, result_json_status, tracked_prs),
        *_category_tags(ticket_metrics_row, result_json_flags),
    ]


def _anomaly_tags(
    m: TicketMetrics,
    result_json_status: str,
    tracked_prs: list[Any] | None,
) -> list[str]:
    """Anomaly-detection tags (2026-05-03 retro).

    Type-strict isinstance guards keep the function robust to non-numeric inputs
    leaking from MagicMocks in tests that mock metrics.get_by_ticket.
    """
    tags: list[str] = []
    if (
        isinstance(m.local_tokens, (int, float))
        and isinstance(m.local_wall_time, (int, float))
        and m.local_tokens < 100_000
        and m.local_wall_time > 1_800
    ):
        tags.append("zero_tokens_high_wall_time")
    if (
        m.outcome == "failure"
        and isinstance(m.lines_changed, int)
        and m.lines_changed == 0
    ):
        tags.append("no_diff_against_base")
    if result_json_status == "success" and m.outcome == "failure":
        tags.append("success_outcome_state_mismatch")
    if m.outcome == "success" and tracked_prs is not None and len(tracked_prs) == 0:
        tags.append("success_pr_create_failed")
    return tags


def _category_tags(
    m: TicketMetrics,
    result_json_flags: dict[str, bool] | None,
) -> list[str]:
    """Categorization tags from result.json signals and metric thresholds.

    backend_unstable threshold (6) calibrated 2026-05-04: Pearson r=0.665 vs
    wall_time, 6+ bucket runs 4x slower than no-retry baseline (WOR-366).
    """
    tags: list[str] = []
    if result_json_flags and result_json_flags.get("scope_drift"):
        tags.append("scope_drift")
    if m.outcome == "escalated":
        tags.append("escalated")
    if isinstance(m.waste_score, (int, float)) and m.waste_score > 80:
        tags.append("high_waste")
    if isinstance(m.retry_count, int) and m.retry_count > 0:
        tags.append("rework")
    if isinstance(m.context_compactions, int) and m.context_compactions > 0:
        tags.append("mid_session_compaction")
    if isinstance(m.api_retry_count, int) and m.api_retry_count >= 6:
        tags.append("backend_unstable")
    return tags
