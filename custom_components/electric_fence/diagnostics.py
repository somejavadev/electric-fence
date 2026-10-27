"""Diagnostics for Electric Fence: status only, never records or secrets.

Contains no rows, usernames or IPs. The MaxMind account ID and license key
are redacted. An entry that is not loaded gets only its state and reason.
"""

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant

from . import ElectricFenceConfigEntry
from .contracts import CONTRACTS
from .runtime import Runtime

TO_REDACT = {"maxmind_account_id", "maxmind_license_key"}


def _settings(runtime: Runtime) -> dict[str, Any]:
    """Return the settings without secrets, the CA text or the forward host."""
    options = runtime.settings.to_options()
    options.pop("forward_ca_pem", None)
    options["forward_host"] = "set" if options["forward_host"] else "empty"
    return async_redact_data(options, TO_REDACT)


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


def _protection(runtime: Runtime) -> dict[str, Any]:
    """Return counts only: never an address or a username."""
    bans, guard = runtime.bans, runtime.username_guard
    try:
        return {
            "available": bans is not None and bans.available,
            "unavailable_reason": bans.unavailable_reason if bans else "not_loaded",
            "active_bans": len(bans.active()) if bans else 0,
            "active_attacks": len(guard.active_attacks()) if guard else 0,
        }
    except Exception as err:  # noqa: BLE001
        return {"error": type(err).__name__}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ElectricFenceConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    if entry.state is not ConfigEntryState.LOADED:
        # No runtime data (setup failed or not loaded): state and reason only.
        return {
            "state": entry.state.value,
            "ha_version": HA_VERSION,
            "reason": entry.reason,
        }
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
        "settings": _settings(runtime),
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
        "protection": _protection(runtime),
        "ha_version": runtime.ha_version,
    }
