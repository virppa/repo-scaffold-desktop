"""Facade module — re-exports the metrics public surface.

The implementation moved to three sibling modules in WOR-433:

* app.core.metrics_models — Pydantic row models + read aggregates.
* app.core.metrics_store  — MetricsStore + SQLite migrations.
* app.core.metrics_tags   — compute_tags + anomaly/category helpers.

Existing call sites can keep importing from ``app.core.metrics`` for
back-compat. New code should import directly from the appropriate
sibling module.
"""

from __future__ import annotations

from app.core.metrics_models import (
    CheckOutcome,
    CheckRunEntry,
    CheckStats,
    CostRollup,
    EpicSummary,
    ImplementationMode,
    Outcome,
    RoutingDistribution,
    TicketMetrics,
    TicketRunLog,
)
from app.core.metrics_store import MetricsStore
from app.core.metrics_tags import compute_tags

__all__ = [
    "CheckOutcome",
    "CheckRunEntry",
    "CheckStats",
    "CostRollup",
    "EpicSummary",
    "ImplementationMode",
    "MetricsStore",
    "Outcome",
    "RoutingDistribution",
    "TicketMetrics",
    "TicketRunLog",
    "compute_tags",
]
