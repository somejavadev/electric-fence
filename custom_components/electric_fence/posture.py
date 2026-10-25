"""Posture checks for the Overview: a short security checklist.

Each check gives a PostureItem `{id, level, params}` (see `shapes.py`); the
panel holds the text. A check that raises is reported as an error item and
never stops the others.
"""

from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
import logging
from typing import Any

from homeassistant.components.http.ban import KEY_LOGIN_THRESHOLD
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .geo.base import classify_ip
from .models import LoginResult, Source
from .protection.backend import HEALTH_IP_BAN_DISABLED
from .protection.network import http_config
from .runtime import Runtime
from .shapes import iso
from .storage import FenceStore, LoginFilter, LoginRow

_LOGGER = logging.getLogger(__name__)

type PostureItem = dict[str, Any]

PROXY_WINDOW = timedelta(days=7)
PROXY_MIN_USERS = 3
STORAGE_ERROR_WINDOW = timedelta(hours=1)
# Successful logins looked at for the proxy check (newest first).
_PROXY_SCAN_ROWS = 5000
_PAGE = 500

_CONF_USE_X_FORWARDED_FOR = "use_x_forwarded_for"


def _item(item_id: str, level: str, **params: Any) -> PostureItem:
    return {"id": item_id, "level": level, "params": params}


async def async_check(
    hass: HomeAssistant, runtime: Runtime, store: FenceStore
) -> list[PostureItem]:
    """Run every posture check."""

    async def admins() -> list[PostureItem]:
        return [await _admins_without_mfa(hass)]

    async def auto_ban() -> list[PostureItem]:
        return [_auto_ban_off(hass, runtime)]

    async def ip_ban() -> list[PostureItem]:
        return [_ip_ban_disabled(runtime)]

    async def proxy() -> list[PostureItem]:
        return [await _proxy_suspected(hass, store)]

    async def geoip() -> list[PostureItem]:
        return [_geoip_missing(runtime)]

    async def collectors() -> list[PostureItem]:
        return _collectors_disabled(runtime)

    async def storage() -> list[PostureItem]:
        return [_storage_error(runtime)]

    checks: list[tuple[str, Callable[[], Awaitable[list[PostureItem]]]]] = [
        ("admins_without_mfa", admins),
        ("auto_ban_off", auto_ban),
        ("ip_ban_disabled", ip_ban),
        ("proxy_suspected", proxy),
        ("geoip_missing", geoip),
        ("collector_disabled", collectors),
        ("storage_error", storage),
    ]
    items: list[PostureItem] = []
    for item_id, run in checks:
        try:
            items.extend(await run())
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Posture check %s failed: %s", item_id, type(err).__name__)
            items.append(_item(item_id, "error", check_failed=type(err).__name__))
    return items


async def _admins_without_mfa(hass: HomeAssistant) -> PostureItem:
    users = [
        {"id": user.id, "name": user.name}
        for user in await hass.auth.async_get_users()
        if user.is_active
        and user.is_admin
        and not user.system_generated
        and not await hass.auth.async_get_enabled_mfa(user)
    ]
    return _item("admins_without_mfa", "warning" if users else "ok", users=users)


def _auto_ban_off(hass: HomeAssistant, runtime: Runtime) -> PostureItem:
    app = getattr(getattr(hass, "http", None), "app", None)
    threshold = app.get(KEY_LOGIN_THRESHOLD) if isinstance(app, Mapping) else None
    if not isinstance(threshold, int):
        threshold = None
    bans = runtime.bans
    electric_fence = runtime.settings.auto_ban and bans is not None and bans.available
    off = not electric_fence and (threshold is None or threshold < 1)
    return _item(
        "auto_ban_off",
        "warning" if off else "ok",
        threshold=threshold,
        electric_fence=electric_fence,
    )


def _ip_ban_disabled(runtime: Runtime) -> PostureItem:
    bans = runtime.bans
    disabled = bans is not None and bans.backend.health() == HEALTH_IP_BAN_DISABLED
    return _item("ip_ban_disabled", "warning" if disabled else "ok")


def use_x_forwarded_for(hass: HomeAssistant) -> bool | None:
    """Return whether the running HTTP server trusts forwarded headers.

    None when it can't be told (no HTTP config store, or an unknown layout).
    """
    conf = http_config(hass)
    if conf is None:
        return None
    value = conf.get(_CONF_USE_X_FORWARDED_FOR, False)
    return value if isinstance(value, bool) else None


def _recent_successes(store: FenceStore, since: Any) -> list[LoginRow]:
    rows: list[LoginRow] = []
    before: int | None = None
    flt = LoginFilter(result=LoginResult.SUCCESS, since=since)
    while len(rows) < _PROXY_SCAN_ROWS:
        page = store.query_logins(flt, limit=_PAGE, before_id=before)
        rows.extend(page)
        if len(page) < _PAGE:
            break
        before = page[-1].id
    return rows


async def _proxy_suspected(hass: HomeAssistant, store: FenceStore) -> PostureItem:
    since = dt_util.utcnow() - PROXY_WINDOW
    rows = await hass.async_add_executor_job(_recent_successes, store, since)
    rows = [r for r in rows if r.user_id is not None]
    users = {r.user_id for r in rows}
    ips = {r.ip for r in rows}
    ip = next(iter(ips)) if len(ips) == 1 else None
    xff = use_x_forwarded_for(hass)
    suspected = (
        len(users) >= PROXY_MIN_USERS
        and ip is not None
        and classify_ip(ip)[1] is Source.LOCAL
        and xff is not True
    )
    return _item(
        "proxy_suspected",
        "warning" if suspected else "ok",
        ip=ip,
        users=len(users),
        use_x_forwarded_for=xff,
    )


def _geoip_missing(runtime: Runtime) -> PostureItem:
    status = runtime.geo_status
    provider = (
        status.provider if status is not None else runtime.settings.geoip_provider
    )
    db_date = status.db_date if status is not None else None
    last_error = status.last_error if status is not None else None
    missing = provider != "none" and db_date is None
    return _item(
        "geoip_missing",
        "warning" if missing else "ok",
        provider=provider,
        last_error=last_error,
    )


def _collectors_disabled(runtime: Runtime) -> list[PostureItem]:
    items = [
        _item(
            "collector_disabled",
            "error",
            collector=c.name,
            reasons=list(c.disabled_reason),
        )
        for c in runtime.collectors
        if c.disabled_reason
    ]
    return items or [_item("collector_disabled", "ok")]


def _storage_error(runtime: Runtime) -> PostureItem:
    worker = runtime.worker
    at = worker.last_storage_error_at
    recent = (
        worker.storage_errors > 0
        and at is not None
        and dt_util.utcnow() - at <= STORAGE_ERROR_WINDOW
    )
    return _item(
        "storage_error",
        "error" if recent else "ok",
        storage_errors=worker.storage_errors,
        last_error=worker.last_storage_error,
        last_error_at=iso(at),
    )
