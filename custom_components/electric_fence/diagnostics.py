"""Diagnostics for Electric Fence: status only, never records or secrets.

Contains no rows, usernames or IPs. The MaxMind account ID and license key
are redacted.
"""

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import ElectricFenceConfigEntry
from .contracts import CONTRACTS
from .runtime import Runtime

TO_REDACT = {"maxmind_account_id", "maxmind_license_key"}


def _contracts(hass: HomeAssistant) -> dict[str, str]:
    """Return each contract's name with "ok" or its failure reason."""
    out: dict[str, str] = {}
    for name, contract in list(CONTRACTS.items()):
        try:
            reason = contract.check(hass)
        except Exception as err:  # noqa: BLE001
            reason = f"{name} check raised {type(err).__name__}"
        out[name] = "ok" if reason is None else reason
    return out


def _geo(runtime: Runtime) -> dict[str, Any]:
    status = runtime.geo_status
    if status is None:
        return {
            "provider": runtime.settings.geoip_provider,
            "db_date": None,
            "last_error": None,
        }
    return {
        "provider": status.provider,
        "db_date": status.db_date.isoformat() if status.db_date else None,
        "last_error": status.last_error,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ElectricFenceConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = entry.runtime_data
    worker = runtime.worker
    try:
        stats = await hass.async_add_executor_job(runtime.store.stats)
    except Exception as err:  # noqa: BLE001
        store: dict[str, Any] = {"error": type(err).__name__}
    else:
        store = {
            "logins": stats.logins,
            "changes": stats.changes,
            "size_bytes": stats.size_bytes,
        }
    return {
        "settings": async_redact_data(runtime.settings.to_options(), TO_REDACT),
        "collectors": [
            {
                "name": collector.name,
                "enabled": collector.is_installed,
                "reason": "; ".join(collector.disabled_reason) or None,
                "errors": collector.guard.errors,
            }
            for collector in runtime.collectors
        ],
        "contracts": _contracts(hass),
        "worker": {
            "queued": worker.queued,
            "dropped": worker.dropped,
            "storage_errors": worker.storage_errors,
            "last_storage_error": worker.last_storage_error,
        },
        "store": store,
        "geo": _geo(runtime),
        "ha_version": runtime.ha_version,
    }
