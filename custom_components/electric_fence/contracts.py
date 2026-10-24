"""Registry of runtime contract checks for Home Assistant internals."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import inspect
from typing import Any

from homeassistant.core import HomeAssistant


@dataclass(frozen=True)
class Contract:
    """A check that Home Assistant behaves the way a collector expects."""

    name: str
    collector: str
    check: Callable[[HomeAssistant], str | None]


CONTRACTS: dict[str, Contract] = {}


def register_contract(contract: Contract) -> None:
    """Register (or replace) a contract by name."""
    CONTRACTS[contract.name] = contract


def check_contracts(hass: HomeAssistant, collector: str) -> list[str]:
    """Return failure reasons for a collector's contracts; [] means OK."""
    reasons: list[str] = []
    for contract in list(CONTRACTS.values()):
        if contract.collector != collector:
            continue
        try:
            reason = contract.check(hass)
        except Exception as err:  # noqa: BLE001
            reason = f"{contract.name} check raised {type(err).__name__}"
        if reason is not None:
            reasons.append(reason)
    return reasons


_EMPTY = inspect.Parameter.empty
_POS = inspect.Parameter.POSITIONAL_OR_KEYWORD.name
_KW = inspect.Parameter.KEYWORD_ONLY.name

# (name, kind name, default); default `_EMPTY` means required.
type _Params = Sequence[tuple[str, str, Any]]


def _params_reason(label: str, fn: Any, expected: _Params, *, coro: bool) -> str | None:
    """Check `fn` is callable with `expected`, and is a coroutine iff `coro`."""
    if not callable(fn):
        return f"{label} is missing"
    is_coro = inspect.iscoroutinefunction(fn)
    if coro and not is_coro:
        return f"{label} is not a coroutine function"
    if not coro and is_coro:
        # Called synchronously by the hook.
        return f"{label} is a coroutine function"
    params = [
        (p.name, p.kind.name, p.default)
        for p in inspect.signature(fn).parameters.values()
    ]
    if params != list(expected):
        return f"{label} has an unexpected signature"
    return None


# The flow manager methods wrapped by the login and attribution collectors.
_FLOW_MANAGER_CHECKS: list[tuple[str, _Params, bool]] = [
    (
        "async_init",
        [("handler", _POS, _EMPTY), ("context", _KW, None), ("data", _KW, None)],
        True,
    ),
    ("async_configure", [("flow_id", _POS, _EMPTY), ("user_input", _POS, None)], True),
    ("async_get", [("flow_id", _POS, _EMPTY)], False),
]


def _flow_manager_reason(label: str, mgr: Any) -> str | None:
    """Check a flow manager has the methods the hooks wrap and call."""
    for attr, expected, coro in _FLOW_MANAGER_CHECKS:
        fn = getattr(mgr, attr, None)
        if reason := _params_reason(f"{label}.{attr}", fn, expected, coro=coro):
            return reason
    return None


def _check_login_flow_manager(hass: HomeAssistant) -> str | None:
    """`hass.auth.login_flow` has the methods the login collector wraps."""
    mgr = getattr(hass.auth, "login_flow", None)
    if mgr is None:
        return "hass.auth.login_flow is missing"
    return _flow_manager_reason("hass.auth.login_flow", mgr)


def _check_refresh_token_create(hass: HomeAssistant) -> str | None:
    """`hass.auth.async_create_refresh_token(user, client_id, client_name, ...)`."""
    label = "hass.auth.async_create_refresh_token"
    fn = getattr(hass.auth, "async_create_refresh_token", None)
    if not callable(fn):
        return f"{label} is missing"
    if not inspect.iscoroutinefunction(fn):
        return f"{label} is not a coroutine function"
    names = list(inspect.signature(fn).parameters)
    if names[:1] != ["user"] or not {"client_id", "client_name"} <= set(names):
        return f"{label} has an unexpected signature"
    return None


register_contract(Contract("login_flow_manager", "login", _check_login_flow_manager))
register_contract(
    Contract("refresh_token_create", "login", _check_refresh_token_create)
)


def _check_wrong_login_notification(hass: HomeAssistant) -> str | None:
    """`process_wrong_login` uses `http-login`/`ip-ban`; the helpers exist."""
    # Imported here, so a moved module fails the contract, not the import.
    from homeassistant.components import persistent_notification  # noqa: PLC0415
    from homeassistant.components.http import ban  # noqa: PLC0415

    for attr, expected in (
        ("NOTIFICATION_ID_LOGIN", "http-login"),
        ("NOTIFICATION_ID_BAN", "ip-ban"),
    ):
        if getattr(ban, attr, None) != expected:
            return f"http.ban.{attr} is not {expected!r}"
    for attr in ("async_register_callback", "async_dismiss"):
        if not callable(getattr(persistent_notification, attr, None)):
            return f"persistent_notification.{attr} is missing"
    return None


register_contract(
    Contract(
        "wrong_login_notification", "auth_failures", _check_wrong_login_notification
    )
)


def _check_flow_managers(hass: HomeAssistant) -> str | None:
    """Check the config, options and subentry flow managers H9 wraps."""
    for attr in ("flow", "options", "subentries"):
        label = f"hass.config_entries.{attr}"
        mgr = getattr(hass.config_entries, attr, None)
        if mgr is None:
            return f"{label} is missing"
        if reason := _flow_manager_reason(label, mgr):
            return reason
    return None


def _check_config_entries_remove(hass: HomeAssistant) -> str | None:
    """`hass.config_entries.async_remove(entry_id)` is a coroutine function."""
    return _params_reason(
        "hass.config_entries.async_remove",
        getattr(hass.config_entries, "async_remove", None),
        [("entry_id", _POS, _EMPTY)],
        coro=True,
    )


def _check_request_handler_task(hass: HomeAssistant) -> str | None:
    """Aiohttp runs each view in a `RequestHandler._handle_request` task (R10)."""
    from aiohttp.web_protocol import RequestHandler  # noqa: PLC0415

    from .attribution import HANDLE_REQUEST_QUALNAME  # noqa: PLC0415

    fn = getattr(RequestHandler, "_handle_request", None)
    if (
        not inspect.iscoroutinefunction(fn)
        or getattr(fn, "__qualname__", None) != HANDLE_REQUEST_QUALNAME
    ):
        return f"aiohttp {HANDLE_REQUEST_QUALNAME} is missing"
    return None


def _check_ws_connection(hass: HomeAssistant) -> str | None:
    """`ActiveConnection` has the `user` and `remote` the WebSocket hook reads."""
    from homeassistant.components.websocket_api import ActiveConnection  # noqa: PLC0415

    for attr in ("user", "remote"):
        if not hasattr(ActiveConnection, attr):
            return f"ActiveConnection.{attr} is missing"
    return None


def _check_admin_ws_commands(hass: HomeAssistant) -> str | None:
    """Once `config` is loaded, every admin command H9 wraps is registered."""
    from homeassistant.components.websocket_api.const import (  # noqa: PLC0415
        DOMAIN as WEBSOCKET_API_DOMAIN,
    )

    from . import attribution  # noqa: PLC0415

    if "config" not in hass.config.components:
        # Checked again when `config` loads.
        return None
    handlers = hass.data.get(WEBSOCKET_API_DOMAIN, {})
    missing = [c for c in attribution.ADMIN_WS_COMMANDS if c not in handlers]
    if missing:
        return f"WebSocket commands not registered: {', '.join(missing)}"
    return None


for _name, _check in (
    ("flow_managers", _check_flow_managers),
    ("config_entries_remove", _check_config_entries_remove),
    ("request_handler_task", _check_request_handler_task),
    ("ws_connection", _check_ws_connection),
    ("admin_ws_commands", _check_admin_ws_commands),
):
    register_contract(Contract(_name, "attribution", _check))
