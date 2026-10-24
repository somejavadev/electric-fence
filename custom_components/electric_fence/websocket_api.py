"""Admin WebSocket API: the `electric_fence/*` commands behind the panel.

Every command is admin only. The JSON shapes are documented in `shapes.py`.
"""

from datetime import UTC, datetime, timedelta
import logging
from typing import Any

import probatio

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.loader import async_get_loaded_integration
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from . import posture, sessions
from .const import DOMAIN
from .models import ChangeKind, LoginResult, Record
from .runtime import Runtime
from .shapes import change_item, geo_status_item, login_item, record_event
from .storage import ChangeFilter, ChangeRow, FenceStore, LoginFilter, LoginRow

_LOGGER = logging.getLogger(__name__)

ERR_NOT_LOADED = "not_loaded"
MAX_LIMIT = 200
DEFAULT_LIMIT = 50
OVERVIEW_ITEMS = 5
FAILED_WINDOW = timedelta(hours=24)
CHANGES_WINDOW = timedelta(days=7)
_COUNT_PAGE = 500

# entry_id -> callbacks that end that entry's subscriptions on unload.
DATA_SUBSCRIPTIONS: HassKey[dict[str, set[CALLBACK_TYPE]]] = HassKey(
    f"{DOMAIN}_subscriptions"
)


def _utc(value: datetime) -> datetime:
    """Read a time without an offset as UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


_TIME = probatio.All(cv.datetime, _utc)
_LIMIT = probatio.All(probatio.Coerce(int), probatio.Range(min=1, max=MAX_LIMIT))
_PAGING: dict[str | probatio.Marker, Any] = {
    probatio.Optional("since"): _TIME,
    probatio.Optional("until"): _TIME,
    probatio.Optional("before_id"): cv.positive_int,
    probatio.Optional("limit", default=DEFAULT_LIMIT): _LIMIT,
}


@callback
def async_register_commands(hass: HomeAssistant) -> None:
    """Register the commands (once per Home Assistant run)."""
    for handler in (
        ws_overview,
        ws_logins,
        ws_changes,
        ws_users,
        ws_status,
        ws_subscribe,
    ):
        websocket_api.async_register_command(hass, handler)


@callback
def async_end_subscriptions(hass: HomeAssistant, entry_id: str) -> None:
    """End the subscriptions of an entry that is unloading."""
    closers = hass.data.get(DATA_SUBSCRIPTIONS, {}).pop(entry_id, set())
    for close in list(closers):
        close()


def _loaded_entry(hass: HomeAssistant) -> ConfigEntry[Runtime] | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0] if entries else None


def _runtime(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> Runtime | None:
    """Return the loaded entry's runtime, or answer `not_loaded`."""
    if (entry := _loaded_entry(hass)) is None:
        connection.send_error(msg["id"], ERR_NOT_LOADED, "Electric Fence is not loaded")
        return None
    return entry.runtime_data


def _next_before_id(rows: list[LoginRow] | list[ChangeRow], limit: int) -> int | None:
    return rows[-1].id if len(rows) == limit else None


def _count_changes(store: FenceStore, flt: ChangeFilter) -> int:
    """Count changes by paging through them (the store has no change count)."""
    total = 0
    before: int | None = None
    while True:
        page = store.query_changes(flt, limit=_COUNT_PAGE, before_id=before)
        total += len(page)
        if len(page) < _COUNT_PAGE:
            return total
        before = page[-1].id


def _overview(store: FenceStore, now: datetime) -> dict[str, Any]:
    """Collect the three record cards. Runs in the executor."""
    day = now - FAILED_WINDOW
    failed = [
        *store.query_logins(
            LoginFilter(result=LoginResult.FAILED, since=day), limit=OVERVIEW_ITEMS
        ),
        *store.query_logins(
            LoginFilter(result=LoginResult.BLOCKED, since=day), limit=OVERVIEW_ITEMS
        ),
    ]
    failed.sort(key=lambda r: (r.ts, r.id), reverse=True)
    changes = ChangeFilter(since=now - CHANGES_WINDOW)
    success = store.query_logins(
        LoginFilter(result=LoginResult.SUCCESS), limit=OVERVIEW_ITEMS
    )
    return {
        "failed_24h": {
            "count": store.count_failed_since(day, include_blocked=True),
            "items": [login_item(r) for r in failed[:OVERVIEW_ITEMS]],
        },
        "changes_7d": {
            "count": _count_changes(store, changes),
            "items": [
                change_item(r)
                for r in store.query_changes(changes, limit=OVERVIEW_ITEMS)
            ],
        },
        "recent_success": {"items": [login_item(r) for r in success]},
    }


@websocket_api.websocket_command({probatio.Required("type"): "electric_fence/overview"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_overview(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return the four Overview cards."""
    if (runtime := _runtime(hass, connection, msg)) is None:
        return
    store = runtime.store
    result = await hass.async_add_executor_job(_overview, store, dt_util.utcnow())
    result["posture"] = await posture.async_check(hass, runtime, store)
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {
        probatio.Required("type"): "electric_fence/logins",
        probatio.Optional("result"): probatio.In([r.value for r in LoginResult]),
        probatio.Optional("user_id"): str,
        probatio.Optional("username"): str,
        probatio.Optional("ip"): str,
        probatio.Optional("country"): str,
        **_PAGING,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_logins(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return a page of login attempts, newest first."""
    if (runtime := _runtime(hass, connection, msg)) is None:
        return
    result = msg.get("result")
    flt = LoginFilter(
        result=LoginResult(result) if result is not None else None,
        user_id=msg.get("user_id"),
        username_contains=msg.get("username"),
        ip=msg.get("ip"),
        country_code=msg.get("country"),
        since=msg.get("since"),
        until=msg.get("until"),
    )
    limit: int = msg["limit"]
    rows = await hass.async_add_executor_job(
        _query_logins, runtime.store, flt, limit, msg.get("before_id")
    )
    connection.send_result(
        msg["id"],
        {
            "items": [login_item(r) for r in rows],
            "next_before_id": _next_before_id(rows, limit),
        },
    )


def _query_logins(
    store: FenceStore, flt: LoginFilter, limit: int, before_id: int | None
) -> list[LoginRow]:
    return store.query_logins(flt, limit=limit, before_id=before_id)


def _query_changes(
    store: FenceStore, flt: ChangeFilter, limit: int, before_id: int | None
) -> list[ChangeRow]:
    return store.query_changes(flt, limit=limit, before_id=before_id)


@websocket_api.websocket_command(
    {
        probatio.Required("type"): "electric_fence/changes",
        probatio.Optional("kind"): probatio.In([k.value for k in ChangeKind]),
        probatio.Optional("actor_user_id"): str,
        probatio.Optional("include_system", default=False): bool,
        **_PAGING,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_changes(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return a page of settings changes, newest first."""
    if (runtime := _runtime(hass, connection, msg)) is None:
        return
    kind = msg.get("kind")
    flt = ChangeFilter(
        kind=ChangeKind(kind) if kind is not None else None,
        actor_user_id=msg.get("actor_user_id"),
        include_system=msg["include_system"],
        since=msg.get("since"),
        until=msg.get("until"),
    )
    limit: int = msg["limit"]
    rows = await hass.async_add_executor_job(
        _query_changes, runtime.store, flt, limit, msg.get("before_id")
    )
    connection.send_result(
        msg["id"],
        {
            "items": [change_item(r) for r in rows],
            "next_before_id": _next_before_id(rows, limit),
        },
    )


@websocket_api.websocket_command({probatio.Required("type"): "electric_fence/users"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_users(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return users with their sessions (H7). Never token values."""
    if (runtime := _runtime(hass, connection, msg)) is None:
        return
    items = await sessions.snapshot_users(hass, runtime.store)
    connection.send_result(msg["id"], {"items": items})


@websocket_api.websocket_command({probatio.Required("type"): "electric_fence/status"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_status(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return version, settings (no licence key), GeoIP, collectors, worker."""
    if (runtime := _runtime(hass, connection, msg)) is None:
        return
    settings = runtime.settings.to_options()
    settings["maxmind_license_key_set"] = bool(settings.pop("maxmind_license_key"))
    version = async_get_loaded_integration(hass, DOMAIN).version
    worker = runtime.worker
    connection.send_result(
        msg["id"],
        {
            "version": str(version) if version is not None else None,
            "settings": settings,
            "geo": geo_status_item(runtime.geo_status),
            "collectors": [
                {
                    "name": c.name,
                    "enabled": not c.disabled_reason,
                    "reason": list(c.disabled_reason),
                }
                for c in runtime.collectors
            ],
            "worker": {
                "queued": worker.queued,
                "dropped": worker.dropped,
                "storage_errors": worker.storage_errors,
            },
        },
    )


@websocket_api.websocket_command(
    {probatio.Required("type"): "electric_fence/subscribe"}
)
@websocket_api.require_admin
@callback
def ws_subscribe(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Push each stored record until unsubscribed or the entry unloads."""
    if (entry := _loaded_entry(hass)) is None:
        connection.send_error(msg["id"], ERR_NOT_LOADED, "Electric Fence is not loaded")
        return
    msg_id: int = msg["id"]

    @callback
    def _on_record(rec: Record) -> None:
        try:
            event = record_event(rec)
        except Exception as err:  # noqa: BLE001
            # Skip this record only; the subscription stays.
            _LOGGER.debug(
                "Electric Fence skipped a live record: %s", type(err).__name__
            )
            return
        connection.send_message(websocket_api.event_message(msg_id, event))

    remove_listener = entry.runtime_data.worker.add_listener(_on_record)
    closers = hass.data.setdefault(DATA_SUBSCRIPTIONS, {}).setdefault(
        entry.entry_id, set()
    )

    @callback
    def _unsubscribe() -> None:
        remove_listener()
        closers.discard(_close_on_unload)

    @callback
    def _close_on_unload() -> None:
        _unsubscribe()
        if connection.subscriptions.get(msg_id) is _unsubscribe:
            del connection.subscriptions[msg_id]
        connection.send_message(
            websocket_api.event_message(msg_id, {"type": "unloaded"})
        )

    closers.add(_close_on_unload)
    connection.subscriptions[msg_id] = _unsubscribe
    connection.send_result(msg_id)
