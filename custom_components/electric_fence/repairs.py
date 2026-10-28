"""Repair issues raised by Electric Fence, and the fix flow some of them have."""

from typing import Any

import probatio

from homeassistant.auth.models import User
from homeassistant.components.http.const import KEY_HASS_USER
from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.http import current_request

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


TLS_CHECK_REQUIRED = "tls_check_required"


def async_report_tls_check_required(hass: HomeAssistant) -> None:
    """Raise a repair issue: TLS forwarding had its certificate check off."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        TLS_CHECK_REQUIRED,
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=TLS_CHECK_REQUIRED,
    )


def async_clear_tls_check_required(hass: HomeAssistant) -> None:
    """Remove the repair issue for the removed TLS certificate check setting."""
    ir.async_delete_issue(hass, DOMAIN, TLS_CHECK_REQUIRED)


FORWARD_PLAINTEXT = "forward_plaintext"


def async_report_forward_plaintext(
    hass: HomeAssistant, host: str, protocol: str
) -> None:
    """Raise a repair issue: events go unencrypted to a host off the network."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        FORWARD_PLAINTEXT,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=FORWARD_PLAINTEXT,
        translation_placeholders={"host": host, "protocol": protocol.upper()},
    )


def async_clear_forward_plaintext(hass: HomeAssistant) -> None:
    """Remove the repair issue for unencrypted forwarding."""
    ir.async_delete_issue(hass, DOMAIN, FORWARD_PLAINTEXT)


BAN_LIMIT = "ban_limit_reached"


def async_report_ban_limit(hass: HomeAssistant, limit: int) -> None:
    """Raise a repair issue: the active-ban limit is reached."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        BAN_LIMIT,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=BAN_LIMIT,
        translation_placeholders={"limit": str(limit)},
    )


def async_clear_ban_limit(hass: HomeAssistant) -> None:
    """Remove the repair issue for the active-ban limit."""
    ir.async_delete_issue(hass, DOMAIN, BAN_LIMIT)


STORAGE_LIMIT = "storage_limit_reached"


def async_report_storage_limit(hass: HomeAssistant, limit_mb: int) -> None:
    """Raise a repair issue: old failed logins were removed to stay under the limit."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        STORAGE_LIMIT,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=STORAGE_LIMIT,
        translation_placeholders={"limit": str(limit_mb)},
    )


def async_clear_storage_limit(hass: HomeAssistant) -> None:
    """Remove the repair issue for the database size limit."""
    ir.async_delete_issue(hass, DOMAIN, STORAGE_LIMIT)


INTEGRATION_FILES_CHANGED = "integration_files_changed"
# The same issue for a manual install, with a step to accept the files.
INTEGRATION_FILES_CHANGED_MANUAL = "integration_files_changed_manual"
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
    hass: HomeAssistant,
    issue_id: str,
    key: str,
    placeholders: dict[str, str],
    data: dict[str, str | int | float | None] | None = None,
) -> None:
    """Raise (or update) a repair issue about one custom integration.

    With `data` the issue is fixable: its fix flow gets the data.
    """
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=data is not None,
        severity=ir.IssueSeverity.WARNING,
        translation_key=key,
        translation_placeholders=placeholders,
        data=data,
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


class AcceptFilesFlow(RepairsFlow):
    """Accept the installed files of a manual install as its new baseline."""

    def __init__(self, domain: str) -> None:
        """Initialise for one integration."""
        self._domain = domain

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Go to the confirmation."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Ask, then accept the files in the name of the admin who confirmed."""
        if user_input is None:
            return self.async_show_form(
                step_id="confirm",
                data_schema=probatio.Schema({}),
                description_placeholders=self._placeholders(),
            )
        entries = self.hass.config_entries.async_loaded_entries(DOMAIN)
        scheduler = entries[0].runtime_data.integrity if entries else None
        user = _request_user()
        if scheduler is None or not await scheduler.async_accept_files(
            self._domain,
            user.id if user else None,
            user.name if user else None,
        ):
            return self.async_abort(
                reason="accept_failed", description_placeholders=self._placeholders()
            )
        return self.async_create_entry(data={})

    def _placeholders(self) -> dict[str, str] | None:
        issue = ir.async_get(self.hass).async_get_issue(DOMAIN, self.issue_id)
        return dict(issue.translation_placeholders or {}) if issue else None


def _request_user() -> User | None:
    """Return the user of the HTTP request running the flow (an admin)."""
    request = current_request.get()
    user = request.get(KEY_HASS_USER) if request is not None else None
    return user if isinstance(user, User) else None


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Return the fix flow of a fixable issue (only the manual files one)."""
    domain = data.get("domain") if data else None
    return AcceptFilesFlow(domain if isinstance(domain, str) else "")
