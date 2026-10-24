"""Repair issues raised by Electric Fence."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN


def _disabled_id(collector: str) -> str:
    return f"collector_disabled_{collector}"


def async_report_disabled_collector(
    hass: HomeAssistant, collector: str, reasons: list[str]
) -> None:
    """Raise a repair issue for a collector that was not installed."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        _disabled_id(collector),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="collector_disabled",
        translation_placeholders={"collector": collector, "reason": "; ".join(reasons)},
    )


def async_clear_disabled_collector(hass: HomeAssistant, collector: str) -> None:
    """Remove the repair issue for a collector."""
    ir.async_delete_issue(hass, DOMAIN, _disabled_id(collector))


SCHEMA_TOO_NEW = "schema_too_new"


def async_report_schema_too_new(
    hass: HomeAssistant, version: int, supported: int
) -> None:
    """Raise a repair issue for a database written by a newer version."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        SCHEMA_TOO_NEW,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=SCHEMA_TOO_NEW,
        translation_placeholders={"version": str(version), "supported": str(supported)},
    )


def async_clear_schema_too_new(hass: HomeAssistant) -> None:
    """Remove the repair issue for a newer database."""
    ir.async_delete_issue(hass, DOMAIN, SCHEMA_TOO_NEW)
