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


IP_BAN_DISABLED = "ip_ban_disabled"


def async_report_ip_ban_disabled(hass: HomeAssistant) -> None:
    """Raise a repair issue: Core's IP ban is off, so bans can't be enforced."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        IP_BAN_DISABLED,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=IP_BAN_DISABLED,
    )


def async_clear_ip_ban_disabled(hass: HomeAssistant) -> None:
    """Remove the repair issue for Core's IP ban being off."""
    ir.async_delete_issue(hass, DOMAIN, IP_BAN_DISABLED)


INTEGRATION_FILES_CHANGED = "integration_files_changed"
INTEGRATION_ARCHIVED = "integration_archived"
INTEGRATION_UNMAINTAINED = "integration_unmaintained"
_INTEGRATION_PREFIXES = tuple(
    f"{key}_"
    for key in (
        INTEGRATION_FILES_CHANGED,
        INTEGRATION_ARCHIVED,
        INTEGRATION_UNMAINTAINED,
    )
)


def async_report_integration_issue(
    hass: HomeAssistant, issue_id: str, key: str, placeholders: dict[str, str]
) -> None:
    """Raise (or update) a repair issue about one custom integration."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=key,
        translation_placeholders=placeholders,
    )


def async_sync_integration_issues(hass: HomeAssistant, keep: set[str]) -> None:
    """Remove the custom integration repair issues not in `keep`."""
    for domain, issue_id in list(ir.async_get(hass).issues):
        if (
            domain == DOMAIN
            and issue_id.startswith(_INTEGRATION_PREFIXES)
            and issue_id not in keep
        ):
            ir.async_delete_issue(hass, DOMAIN, issue_id)
