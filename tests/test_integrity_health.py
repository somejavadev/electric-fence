"""Tests for the integration health check."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest

from custom_components.electric_fence.integrity.health import HealthResult, health_of
from custom_components.electric_fence.integrity.inventory import (
    InstalledIntegration,
    RemovedInfo,
)

NOW = datetime(2026, 10, 26, tzinfo=UTC)


def _item(
    *,
    source: Literal["marketplace", "manual"] = "marketplace",
    archived: bool = False,
    removed: RemovedInfo | None = None,
    age_days: int | None = 1,
) -> InstalledIntegration:
    return InstalledIntegration(
        domain="demo",
        path=Path("/config/custom_components/demo"),
        version="1.0",
        source=source,
        archived=archived,
        removed=removed,
        last_updated=None if age_days is None else NOW - timedelta(days=age_days),
    )


@pytest.mark.parametrize(
    ("item", "status", "level", "reason"),
    [
        (_item(), "ok", "ok", None),
        (_item(age_days=119), "ok", "ok", None),
        (_item(age_days=120), "ok", "ok", None),
        (_item(age_days=121), "unmaintained", "notice", None),
        (_item(age_days=None), "ok", "ok", None),
        (_item(archived=True), "archived", "warning", None),
        (_item(archived=True, age_days=500), "archived", "warning", None),
        (
            _item(removed=RemovedInfo("abandoned", "remove")),
            "removed",
            "critical",
            "abandoned",
        ),
        (
            _item(archived=True, removed=RemovedInfo("malware", "remove")),
            "removed",
            "critical",
            "malware",
        ),
        (_item(source="manual", age_days=None), "unknown", "ok", None),
        (_item(source="manual", archived=True, age_days=900), "unknown", "ok", None),
    ],
)
def test_health_of(
    item: InstalledIntegration, status: str, level: str, reason: str | None
) -> None:
    """Each state maps to its status, level and reason."""
    result = health_of(item, NOW, 4)
    assert result == HealthResult(
        "demo",
        status,
        level,
        item.last_updated,
        reason,  # type: ignore[arg-type]
    )
