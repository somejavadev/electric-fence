"""Classify how well maintained a Marketplace integration is."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from .inventory import InstalledIntegration

Status = Literal["ok", "unmaintained", "archived", "removed", "unknown"]
Level = Literal["ok", "notice", "warning", "critical"]


@dataclass(frozen=True)
class HealthResult:
    """The health of one installed integration."""

    domain: str
    status: Status
    level: Level
    last_updated: datetime | None
    reason: str | None


def health_of(item: InstalledIntegration, now: datetime, months: int) -> HealthResult:
    """Classify an integration; missing update data is never unmaintained."""
    updated = item.last_updated
    if item.source == "manual":
        return HealthResult(item.domain, "unknown", "ok", updated, None)
    if item.removed is not None:
        return HealthResult(
            item.domain, "removed", "critical", updated, item.removed.reason
        )
    if item.archived:
        return HealthResult(item.domain, "archived", "warning", updated, None)
    if updated is not None and now - updated > timedelta(days=months * 30):
        return HealthResult(item.domain, "unmaintained", "notice", updated, None)
    return HealthResult(item.domain, "ok", "ok", updated, None)
