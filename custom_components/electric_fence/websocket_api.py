"""Admin WebSocket API: the `electric_fence/*` commands behind the panel.

Every command is admin only. The JSON shapes are documented in `shapes.py`.
"""

from datetime import UTC, datetime, timedelta
import logging
from typing import Any, Literal

import probatio

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.loader import async_get_loaded_integration
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from . import posture, sessions
from .attribution import auth_users
from .collectors.apps import AppCollector
from .const import DOMAIN
from .contracts import marketplace_contract_ok
from .forwarding.forwarder import forwarding_state
from .integrity.scheduler import IntegrityScheduler
from .models import ChangeAction, ChangeKind, LoginResult, Record, SettingsChange
from .protection.throttle import UsernameGuard
from .runtime import Runtime
from .shapes import (
    UserNames,
    attack_row_item,
    attack_view_item,
    ban_row_item,
    ban_view_item,
    change_item,
    geo_status_item,
    integration_item,
    iso,
    login_item,
    record_event,
)
from .storage import (
    FAILED_OR_BLOCKED,
    AttackRow,
    BanRow,
    ChangeFilter,
    ChangeRow,
    FenceStore,
    LoginFilter,
    LoginRow,
)

_LOGGER = logging.getLogger(__name__)

ERR_NOT_LOADED = "not_loaded"
MAX_LIMIT = 200
DEFAULT_LIMIT = 50
OVERVIEW_ITEMS = 5
FAILED_WINDOW = timedelta(hours=24)
CHANGES_WINDOW = timedelta(days=7)
_COUNT_PAGE = 500
MAX_SEARCH = 200
SCAN_COOLDOWN = timedelta(minutes=5)

# entry_id -> callbacks that end that entry's subscriptions on unload.
DATA_SUBSCRIPTIONS: HassKey[dict[str, set[CALLBACK_TYPE]]] = HassKey(
    f"{DOMAIN}_subscriptions"
)

# entry_id -> when a scan was last started from the panel.
DATA_SCAN_REQUESTED: HassKey[dict[str, datetime]] = HassKey(f"{DOMAIN}_scan_requested")


def _utc(value: datetime) -> datetime:
    """Read a time without an offset as UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


_TIME = probatio.All(cv.datetime, _utc)
_LIMIT = probatio.All(probatio.Coerce(int), probatio.Range(min=1, max=MAX_LIMIT))
_SEARCH = probatio.All(str, probatio.Length(max=MAX_SEARCH))
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
        ws_bans,
        ws_unban,
        ws_attacks,
        ws_end_slowdown,
        ws_clear_countries,
        ws_integrations,
        ws_scan_integrations,
    ):
        websocket_api.async_register_command(hass, handler)


@callback
def async_end_subscriptions(hass: HomeAssistant, entry_id: str) -> None:
    """End the subscriptions of an entry that is unloading."""
    hass.data.get(DATA_SCAN_REQUESTED, {}).pop(entry_id, None)
    closers = hass.data.get(DATA_SUBSCRIPTIONS, {}).pop(entry_id, set())
    for close in list(closers):
        close()


def user_names(hass: HomeAssistant) -> dict[str, str | None]:
    """Return every user's name by ID, from the auth store in memory."""
    users = auth_users(hass)
    return {} if users is None else {uid: u.name for uid, u in users.items()}


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


def _overview(store: FenceStore, now: datetime, names: UserNames) -> dict[str, Any]:
    """Collect the three record cards. Runs in the executor."""
    day = now - FAILED_WINDOW
    failed = store.query_logins(
        LoginFilter(result=FAILED_OR_BLOCKED, since=day), limit=OVERVIEW_ITEMS
    )
    changes = ChangeFilter(since=now - CHANGES_WINDOW)
    success = store.query_logins(
        LoginFilter(result=LoginResult.SUCCESS), limit=OVERVIEW_ITEMS
    )
    return {
        "failed_24h": {
            "count": store.count_failed_since(day, include_blocked=True),
            "items": [login_item(r, names) for r in failed],
        },
        "changes_7d": {
            "count": _count_changes(store, changes),
            "items": [
                change_item(r)
                for r in store.query_changes(changes, limit=OVERVIEW_ITEMS)
            ],
        },
        "recent_success": {"items": [login_item(r, names) for r in success]},
    }


def _protection(runtime: Runtime) -> dict[str, Any]:
    bans, guard = runtime.bans, runtime.username_guard
    return {
        "available": bans is not None and bans.available,
        "active_bans": len(bans.active()) if bans is not None else 0,
        "bans_24h": bans.bans_24h() if bans is not None else 0,
        "attacks": (
            [attack_view_item(a) for a in guard.active_attacks()]
            if guard is not None
            else []
        ),
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
    result = await hass.async_add_executor_job(
        _overview, store, dt_util.utcnow(), user_names(hass)
    )
    result["posture"] = await posture.async_check(hass, runtime, store)
    result["protection"] = _protection(runtime)
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {
        probatio.Required("type"): "electric_fence/logins",
        probatio.Optional("result"): probatio.In(
            [*(r.value for r in LoginResult), FAILED_OR_BLOCKED]
        ),
        probatio.Optional("user_id"): str,
        probatio.Optional("username"): str,
        probatio.Optional("ip"): str,
        probatio.Optional("country"): str,
        probatio.Optional("search"): _SEARCH,
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
    result_filter: LoginResult | Literal["failed_or_blocked"] | None = None
    if result == FAILED_OR_BLOCKED:
        result_filter = FAILED_OR_BLOCKED
    elif result is not None:
        result_filter = LoginResult(result)
    flt = LoginFilter(
        result=result_filter,
        user_id=msg.get("user_id"),
        username_contains=msg.get("username"),
        ip=msg.get("ip"),
        country_code=msg.get("country"),
        since=msg.get("since"),
        until=msg.get("until"),
        search=msg.get("search"),
    )
    limit: int = msg["limit"]
    rows = await hass.async_add_executor_job(
        _query_logins, runtime.store, flt, limit, msg.get("before_id")
    )
    connection.send_result(
        msg["id"],
        {
            "items": [login_item(r, user_names(hass)) for r in rows],
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
        probatio.Optional("action"): probatio.In([a.value for a in ChangeAction]),
        probatio.Optional("ip"): _SEARCH,
        probatio.Optional("search"): _SEARCH,
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
    action = msg.get("action")
    flt = ChangeFilter(
        kind=ChangeKind(kind) if kind is not None else None,
        actor_user_id=msg.get("actor_user_id"),
        include_system=msg["include_system"],
        since=msg.get("since"),
        until=msg.get("until"),
        action=ChangeAction(action) if action is not None else None,
        ip=msg.get("ip"),
        search=msg.get("search"),
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
    settings["notify_targets"] = list(settings["notify_targets"])
    settings["forward_ca_pem_set"] = bool(settings.pop("forward_ca_pem"))
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
            "forwarding": _forwarding(runtime),
            "apps": (
                "available"
                if any(
                    isinstance(c, AppCollector) and c.is_installed
                    for c in runtime.collectors
                )
                else "not_available"
            ),
        },
    )


def _forwarding(runtime: Runtime) -> dict[str, Any] | None:
    if (forwarder := runtime.forwarder) is None:
        return None
    stats = forwarder.stats()
    return {
        "state": forwarding_state(forwarder),
        "queued": stats.queued,
        "dropped": stats.dropped,
        "last_error": stats.last_error,
    }


@websocket_api.websocket_command(
    {probatio.Required("type"): "electric_fence/integrations"}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_integrations(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return the last integration check results."""
    if (runtime := _runtime(hass, connection, msg)) is None:
        return
    scheduler = runtime.integrity
    if scheduler is None:
        connection.send_result(
            msg["id"],
            {"items": [], "scanned_at": None, "running": False, "available": False},
        )
        return
    connection.send_result(
        msg["id"],
        {
            "items": [integration_item(s) for s in scheduler.results()],
            "scanned_at": iso(scheduler.scanned_at),
            "running": scheduler.running,
            "available": marketplace_contract_ok(hass),
        },
    )


@websocket_api.websocket_command(
    {probatio.Required("type"): "electric_fence/scan_integrations"}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_scan_integrations(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Start an integration scan in the background, at most every 5 minutes."""
    if (entry := _loaded_entry(hass)) is None:
        connection.send_error(msg["id"], ERR_NOT_LOADED, "Electric Fence is not loaded")
        return
    scheduler = entry.runtime_data.integrity
    requested = hass.data.setdefault(DATA_SCAN_REQUESTED, {})
    now = dt_util.utcnow()
    last = requested.get(entry.entry_id)
    if scheduler is None or (last is not None and now - last < SCAN_COOLDOWN):
        connection.send_result(msg["id"], {"started": False})
        return
    requested[entry.entry_id] = now
    hass.async_create_background_task(
        _run_scan(scheduler), "electric_fence integration scan (panel)"
    )
    connection.send_result(msg["id"], {"started": True})


async def _run_scan(scheduler: IntegrityScheduler) -> None:
    try:
        await scheduler.scan()
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Electric Fence scan failed: %s", type(err).__name__)


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
        user = connection.user
        if not user.is_admin or not user.is_active:
            # Demoted or deactivated since subscribing: end it, send nothing.
            _unsubscribe()
            if connection.subscriptions.get(msg_id) is _unsubscribe:
                del connection.subscriptions[msg_id]
            return
        try:
            event = record_event(rec, user_names(hass))
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


_STATE = probatio.In(["active", "history"])


def _query_bans(store: FenceStore, limit: int, before_id: int | None) -> list[BanRow]:
    return store.query_bans(active=False, limit=limit, before_id=before_id)


def _query_attacks(store: FenceStore, limit: int) -> list[AttackRow]:
    return store.query_attacks(active=False, limit=limit)


@websocket_api.websocket_command(
    {
        probatio.Required("type"): "electric_fence/bans",
        probatio.Required("state"): _STATE,
        probatio.Optional("before_id"): cv.positive_int,
        probatio.Optional("limit", default=DEFAULT_LIMIT): _LIMIT,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_bans(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return the active bans, or a page of ended ones."""
    if (runtime := _runtime(hass, connection, msg)) is None:
        return
    bans = runtime.bans
    if bans is None:
        connection.send_result(
            msg["id"],
            {
                "available": False,
                "reason": ERR_NOT_LOADED,
                "items": [],
                "next_before_id": None,
            },
        )
        return
    reason = bans.unavailable_reason
    items: list[dict[str, Any]]
    next_before_id: int | None = None
    if msg["state"] == "active":
        views = sorted(bans.active(), key=lambda v: v.banned_at, reverse=True)
        items = [ban_view_item(v) for v in views]
    else:
        limit: int = msg["limit"]
        rows = await hass.async_add_executor_job(
            _query_bans, runtime.store, limit, msg.get("before_id")
        )
        items = [ban_row_item(r) for r in rows]
        next_before_id = rows[-1].id if len(rows) == limit else None
    connection.send_result(
        msg["id"],
        {
            "available": reason is None,
            "reason": reason,
            "items": items,
            "next_before_id": next_before_id,
        },
    )


@websocket_api.websocket_command(
    {probatio.Required("type"): "electric_fence/unban", probatio.Required("ip"): str}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_unban(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Lift a ban, recording the admin."""
    if (runtime := _runtime(hass, connection, msg)) is None:
        return
    if runtime.bans is None:
        connection.send_error(msg["id"], ERR_NOT_LOADED, "Bans are not loaded")
        return
    user = connection.user
    unbanned = await runtime.bans.async_unban(msg["ip"], user.id, user.name)
    connection.send_result(msg["id"], {"unbanned": unbanned})


@websocket_api.websocket_command(
    {
        probatio.Required("type"): "electric_fence/attacks",
        probatio.Required("state"): _STATE,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_attacks(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return the active username attacks, or the ended ones."""
    if (runtime := _runtime(hass, connection, msg)) is None:
        return
    items: list[dict[str, Any]]
    if msg["state"] == "active":
        guard = runtime.username_guard
        items = (
            []
            if guard is None
            else [attack_view_item(a) for a in guard.active_attacks()]
        )
    else:
        rows = await hass.async_add_executor_job(
            _query_attacks, runtime.store, DEFAULT_LIMIT
        )
        items = [attack_row_item(r) for r in rows]
    connection.send_result(msg["id"], {"items": items})


@websocket_api.websocket_command(
    {
        probatio.Required("type"): "electric_fence/end_slowdown",
        probatio.Required("username"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_end_slowdown(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """End the slow-down of a username under attack."""
    if (runtime := _runtime(hass, connection, msg)) is None:
        return
    if (guard := runtime.username_guard) is None:
        connection.send_error(msg["id"], ERR_NOT_LOADED, "Protection is not loaded")
        return
    user = connection.user
    key = UsernameGuard.key(msg["username"])
    ended = key is not None and await guard.async_end(key, user.id, user.name)
    connection.send_result(msg["id"], {"ended": ended})


@websocket_api.websocket_command(
    {probatio.Required("type"): "electric_fence/clear_countries"}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_clear_countries(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Forget every user's known login countries."""
    if (runtime := _runtime(hass, connection, msg)) is None:
        return
    cleared = await hass.async_add_executor_job(runtime.store.clear_countries)
    user = connection.user
    runtime.worker.submit(
        SettingsChange(
            ts=dt_util.utcnow(),
            kind=ChangeKind.LOGIN_PROTECTION,
            action=ChangeAction.COUNTRIES_CLEARED,
            target_id=None,
            target_name=None,
            details={"cleared": cleared},
            actor_user_id=user.id,
            actor_name=user.name,
            actor_ip=None,
        )
    )
    connection.send_result(msg["id"], {"cleared": cleared})
