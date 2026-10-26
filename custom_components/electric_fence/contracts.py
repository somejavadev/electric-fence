"""Registry of runtime contract checks for Home Assistant internals."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import inspect
import logging
import re
from types import SimpleNamespace
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
    """Every command H9 wraps is registered once its component is loaded.

    `config` registers most; `person` registers `person/update_own_profile`.
    A component not loaded yet is checked again when it loads.
    """
    from homeassistant.components.websocket_api.const import (  # noqa: PLC0415
        DOMAIN as WEBSOCKET_API_DOMAIN,
    )

    from . import attribution  # noqa: PLC0415

    handlers = hass.data.get(WEBSOCKET_API_DOMAIN, {})
    missing = [
        c
        for c in attribution.ADMIN_WS_COMMANDS
        if attribution.command_component(c) in hass.config.components
        and c not in handlers
    ]
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


def _check_config_entry_signal(hass: HomeAssistant) -> str | None:
    """`SIGNAL_CONFIG_ENTRY_CHANGED` exists; `ConfigEntryChange` has our changes."""
    from homeassistant import config_entries  # noqa: PLC0415

    if getattr(config_entries, "SIGNAL_CONFIG_ENTRY_CHANGED", None) is None:
        return "config_entries.SIGNAL_CONFIG_ENTRY_CHANGED is missing"
    change = getattr(config_entries, "ConfigEntryChange", None)
    for member in ("ADDED", "REMOVED", "UPDATED"):
        if not hasattr(change, member):
            return f"config_entries.ConfigEntryChange.{member} is missing"
    return None


def _check_user_events(hass: HomeAssistant) -> str | None:
    """Check the auth manager fires `user_added`, `user_updated`, `user_removed`."""
    from homeassistant import auth  # noqa: PLC0415

    for attr, expected in (
        ("EVENT_USER_ADDED", "user_added"),
        ("EVENT_USER_UPDATED", "user_updated"),
        ("EVENT_USER_REMOVED", "user_removed"),
    ):
        if getattr(auth, attr, None) != expected:
            return f"auth.{attr} is not {expected!r}"
    return None


def _check_hass_provider_password(hass: HomeAssistant) -> str | None:
    """Check the `homeassistant` provider has the change methods H5 wraps.

    Not applicable (passes) when no `homeassistant` provider is configured.
    """
    provider = hass.auth.get_auth_provider("homeassistant", None)
    if provider is None:
        return None
    label = "homeassistant auth provider"
    for attr, expected in (
        (
            "async_change_password",
            [("username", _POS, _EMPTY), ("new_password", _POS, _EMPTY)],
        ),
        (
            "async_change_username",
            [("credential", _POS, _EMPTY), ("new_username", _POS, _EMPTY)],
        ),
    ):
        fn = getattr(provider, attr, None)
        if reason := _params_reason(f"{label}.{attr}", fn, expected, coro=True):
            return reason
    return None


def _check_core_config_event(hass: HomeAssistant) -> str | None:
    """Core fires `core_config_updated` with the changed keys."""
    from homeassistant import const  # noqa: PLC0415

    if getattr(const, "EVENT_CORE_CONFIG_UPDATE", None) != "core_config_updated":
        return "const.EVENT_CORE_CONFIG_UPDATE is not 'core_config_updated'"
    return None


for _name, _collector, _check in (
    ("config_entry_signal", "config_entries", _check_config_entry_signal),
    ("user_events", "users", _check_user_events),
    ("hass_provider_password", "users", _check_hass_provider_password),
    ("core_config_event", "core_config", _check_core_config_event),
):
    register_contract(Contract(_name, _collector, _check))


# Members of `ActiveConnection` the system settings collector reads or watches.
_WATCHED_CONNECTION_ATTRS = (
    "user",
    "remote",
    "send_result",
    "send_error",
    "send_message",
    "async_handle_exception",
)


def _check_system_settings_ws(hass: HomeAssistant) -> str | None:
    """Every command the collector wraps is registered once its component loads.

    And `ActiveConnection` has the members the result watch relies on. A
    component not loaded yet is checked again when it loads.
    """
    from homeassistant.components.websocket_api import ActiveConnection  # noqa: PLC0415
    from homeassistant.components.websocket_api.const import (  # noqa: PLC0415
        DOMAIN as WEBSOCKET_API_DOMAIN,
    )

    from .collectors.system_settings import SystemSettingsCollector  # noqa: PLC0415

    for attr in _WATCHED_CONNECTION_ATTRS:
        if not hasattr(ActiveConnection, attr):
            return f"ActiveConnection.{attr} is missing"
    handlers = hass.data.get(WEBSOCKET_API_DOMAIN, {})
    missing = [
        spec.command
        for spec in SystemSettingsCollector.WS_SPECS
        if spec.component in hass.config.components and spec.command not in handlers
    ]
    if missing:
        return f"WebSocket commands not registered: {', '.join(missing)}"
    for spec in SystemSettingsCollector.WS_SPECS:
        if spec.message_keys and spec.command in handlers:
            if reason := _message_keys_reason(spec, handlers[spec.command][1]):
                return reason
    for component, check in _AREA_CHECKS.items():
        if component in hass.config.components and (reason := check(hass)):
            return f"{component}: {reason}"
    return None


def _schema_keys(schema: Any) -> set[str] | None:
    """Return the top-level field names of a registered command schema.

    Core registers `False` for a command without fields, a `Schema` over a
    dict of markers, or an `All` whose first validator is that `Schema`.
    """
    if schema is False:
        return set()
    if (validators := getattr(schema, "validators", None)) is not None:
        schema = validators[0] if validators else None
    fields = getattr(schema, "schema", None)
    if not isinstance(fields, dict):
        return None
    return {str(key) for key in fields}


def _message_keys_reason(spec: Any, schema: Any) -> str | None:
    """Check a command's schema still has the fields its reader uses."""
    if (keys := _schema_keys(schema)) is None:
        return f"{spec.command}: the message schema has no field names"
    if missing := [key for key in spec.message_keys if key not in keys]:
        return f"{spec.command}: message fields missing: {', '.join(missing)}"
    return None


def _check_backup_area(hass: HomeAssistant) -> str | None:
    from homeassistant.components.backup.const import DATA_MANAGER  # noqa: PLC0415

    if (manager := hass.data.get(DATA_MANAGER)) is None:
        return "the backup manager is missing"
    data = getattr(getattr(manager, "config", None), "data", None)
    if not callable(getattr(data, "to_dict", None)):
        return "BackupManager.config.data.to_dict is missing"
    return None


def _check_http_area(hass: HomeAssistant) -> str | None:
    from homeassistant.components.http import config as http  # noqa: PLC0415

    for name in (
        "HTTP_CONFIG_CREATED_AT",
        "HTTP_CONFIG_ERROR",
        "HTTP_CONFIG_ERROR_MESSAGE",
    ):
        if not hasattr(http, name):
            return f"config.{name} is missing"
    if (store := hass.data.get(http.DATA_STORE)) is None:
        return "the HTTP config store is missing"
    for attr in ("stable", "pending"):
        if not hasattr(store, attr):
            return f"HTTPConfigStore.{attr} is missing"
    return None


def _check_network_area(hass: HomeAssistant) -> str | None:
    from homeassistant.components.network.network import DATA_NETWORK  # noqa: PLC0415

    if (network := hass.data.get(DATA_NETWORK)) is None:
        return "the network object is missing"
    if not hasattr(network, "configured_adapters"):
        return "Network.configured_adapters is missing"
    return None


def _check_analytics_area(hass: HomeAssistant) -> str | None:
    from homeassistant.components.analytics import DATA_COMPONENT  # noqa: PLC0415

    if (analytics := hass.data.get(DATA_COMPONENT)) is None:
        return "the analytics object is missing"
    if not hasattr(analytics, "preferences"):
        return "Analytics.preferences is missing"
    return None


# The component state each area reader uses, checked once the component loads.
_AREA_CHECKS: dict[str, Callable[[HomeAssistant], str | None]] = {
    "backup": _check_backup_area,
    "http": _check_http_area,
    "network": _check_network_area,
    "analytics": _check_analytics_area,
}


register_contract(
    Contract("system_settings_ws", "system_settings", _check_system_settings_ws)
)


def _check_system_settings_services(hass: HomeAssistant) -> str | None:
    """Every service the collector wraps is Core's `Service` with a `HassJob`.

    The registry keeps them in its private `_services[domain][service]`, and
    runs `Service.job` from `_execute_service`. Each service is checked once
    its component is loaded, with the `call.data` fields its reader uses
    and the component state the reader reads.
    """
    from homeassistant.core import HassJob, Service, ServiceRegistry  # noqa: PLC0415

    from .collectors.system_settings import SystemSettingsCollector  # noqa: PLC0415

    if not callable(getattr(ServiceRegistry, "_execute_service", None)):
        return "ServiceRegistry._execute_service is missing"
    services = getattr(hass.services, "_services", None)
    if not isinstance(services, dict):
        return "ServiceRegistry._services is not a mapping"
    specs = [
        spec
        for spec in SystemSettingsCollector.SERVICE_SPECS
        if spec.domain in hass.config.components
    ]
    missing = [
        f"{spec.domain}.{spec.service}"
        for spec in specs
        if spec.service not in services.get(spec.domain, {})
    ]
    if missing:
        return f"services not registered: {', '.join(missing)}"
    for spec in specs:
        name = f"{spec.domain}.{spec.service}"
        service = services[spec.domain][spec.service]
        if not isinstance(service, Service):
            return f"{name} is not a Service"
        if not isinstance(getattr(service, "job", None), HassJob):
            return f"{name}: Service.job is not a HassJob"
        if spec.service_keys:
            if (keys := _schema_keys(service.schema)) is None:
                return f"{name}: the service schema has no field names"
            if absent := [key for key in spec.service_keys if key not in keys]:
                return f"{name}: service fields missing: {', '.join(absent)}"
    for component, check in _SERVICE_AREA_CHECKS.items():
        if component in hass.config.components and (reason := check(hass)):
            return f"{component}: {reason}"
    return None


def _check_recorder_area(hass: HomeAssistant) -> str | None:
    from homeassistant.helpers.recorder import DATA_INSTANCE  # noqa: PLC0415

    if (instance := hass.data.get(DATA_INSTANCE)) is None:
        return "the recorder instance is missing"
    if not hasattr(instance, "enabled"):
        return "Recorder.enabled is missing"
    return None


_SERVICE_AREA_CHECKS: dict[str, Callable[[HomeAssistant], str | None]] = {
    "recorder": _check_recorder_area,
}


register_contract(
    Contract(
        "system_settings_services",
        "system_settings_services",
        _check_system_settings_services,
    )
)


def _check_ban_manager(hass: HomeAssistant) -> str | None:
    """Core's ban manager keeps the names and attributes the backend uses."""
    from homeassistant.components.http import ban  # noqa: PLC0415

    for name in ("KEY_BAN_MANAGER", "KEY_FAILED_LOGIN_ATTEMPTS"):
        if not hasattr(ban, name):
            return f"http.ban.{name} is missing"
    manager = getattr(ban, "IpBanManager", None)
    if manager is None or not callable(getattr(manager, "async_add_ban", None)):
        return "IpBanManager.async_add_ban is missing"
    if not inspect.iscoroutinefunction(manager.async_add_ban):
        return "IpBanManager.async_add_ban is not a coroutine function"
    ban_cls = getattr(ban, "IpBan", None)
    if ban_cls is None:
        return "IpBan is missing"
    sample = ban_cls("203.0.113.1")
    for attr in ("ip_address", "banned_at"):
        if not hasattr(sample, attr):
            return f"IpBan.{attr} is missing"
    instance = manager(hass)
    for attr in ("ip_bans_lookup", "path"):
        if not hasattr(instance, attr):
            return f"IpBanManager.{attr} is missing"
    if not isinstance(instance.ip_bans_lookup, dict):
        return "IpBanManager.ip_bans_lookup is not a dict"
    return None


def _check_ban_middleware_lookup(hass: HomeAssistant) -> str | None:
    """Check the ban middleware still enforces bans through `ip_bans_lookup`."""
    from homeassistant.components.http import ban  # noqa: PLC0415

    if not callable(getattr(ban, "ban_middleware", None)):
        return "http.ban.ban_middleware is missing"
    try:
        source = inspect.getsource(ban.ban_middleware)
    except OSError, TypeError:
        return "the ban_middleware source is unavailable"
    if "ip_bans_lookup" not in source:
        return "ban_middleware no longer reads ip_bans_lookup"
    return None


register_contract(Contract("ban_manager", "protection", _check_ban_manager))
register_contract(
    Contract("ban_middleware_lookup", "protection", _check_ban_middleware_lookup)
)


def _not_supervisor(hass: HomeAssistant) -> bool:
    """Return True off Supervisor: the app contracts don't apply (they pass)."""
    return "hassio" not in hass.config.components


def _ws_command_reason(
    hass: HomeAssistant, command: str, fields: tuple[str, ...]
) -> str | None:
    """Check a WebSocket command is registered with these message fields."""
    from homeassistant.components.websocket_api import ActiveConnection  # noqa: PLC0415
    from homeassistant.components.websocket_api.const import (  # noqa: PLC0415
        DOMAIN as WEBSOCKET_API_DOMAIN,
    )

    for attr in _WATCHED_CONNECTION_ATTRS:
        if not hasattr(ActiveConnection, attr):
            return f"ActiveConnection.{attr} is missing"
    handlers = hass.data.get(WEBSOCKET_API_DOMAIN, {})
    if command not in handlers:
        return f"WebSocket command not registered: {command}"
    if (keys := _schema_keys(handlers[command][1])) is None:
        return f"{command}: the message schema has no field names"
    if missing := [key for key in fields if key not in keys]:
        return f"{command}: message fields missing: {', '.join(missing)}"
    return None


def _check_supervisor_ws_api(hass: HomeAssistant) -> str | None:
    """`supervisor/api` takes `endpoint`, `method` and `data`."""
    if _not_supervisor(hass):
        return None
    return _ws_command_reason(hass, "supervisor/api", ("endpoint", "method", "data"))


def _check_hassio_update_addon_ws(hass: HomeAssistant) -> str | None:
    """`hassio/update/addon` takes the app's slug as `addon`."""
    if _not_supervisor(hass):
        return None
    return _ws_command_reason(hass, "hassio/update/addon", ("addon",))


def _service_reason(
    hass: HomeAssistant, domain: str, service: str, fields: tuple[str, ...]
) -> str | None:
    """Check a registered service is Core's `Service` with these fields."""
    from homeassistant.core import HassJob, Service  # noqa: PLC0415

    name = f"{domain}.{service}"
    found = getattr(hass.services, "_services", {}).get(domain, {}).get(service)
    if not isinstance(found, Service):
        return f"{name} is not a Service"
    if not isinstance(getattr(found, "job", None), HassJob):
        return f"{name}: Service.job is not a HassJob"
    if fields:
        if (keys := _schema_keys(found.schema)) is None:
            return f"{name}: the service schema has no field names"
        if absent := [key for key in fields if key not in keys]:
            return f"{name}: service fields missing: {', '.join(absent)}"
    return None


def _check_hassio_addon_services(hass: HomeAssistant) -> str | None:
    """Check the app start, stop and restart services take the slug we read."""
    if _not_supervisor(hass):
        return None
    from .collectors.apps import APP_SERVICES, HASSIO  # noqa: PLC0415

    services = getattr(hass.services, "_services", None)
    if not isinstance(services, dict):
        return "ServiceRegistry._services is not a mapping"
    registered = services.get(HASSIO, {})
    if missing := [f"{HASSIO}.{s}" for s in APP_SERVICES if s not in registered]:
        return f"services not registered: {', '.join(missing)}"
    for service, (key, _action) in APP_SERVICES.items():
        if reason := _service_reason(hass, HASSIO, service, (key,)):
            return reason
    return None


def _check_hassio_update_install(hass: HomeAssistant) -> str | None:
    """`update.install` is wrappable; app update entities keep their unique ID.

    Checked once `update` is loaded. The unique ID must be `<slug>` plus
    `APP_UPDATE_SUFFIX` (`app_slug_from_unique_id`), checked on a real app
    update entity built for a made-up app.
    """
    if _not_supervisor(hass):
        return None
    from .collectors.apps import UPDATE, UPDATE_INSTALL, app_slug_from_unique_id  # noqa: PLC0415

    if UPDATE not in hass.config.components:
        return None
    services = getattr(hass.services, "_services", {})
    if UPDATE_INSTALL not in services.get(UPDATE, {}):
        return "update.install is not registered"
    if reason := _service_reason(hass, UPDATE, UPDATE_INSTALL, ()):
        return reason
    from homeassistant.components.hassio import update  # noqa: PLC0415

    probe = "electric_fence_probe"
    try:
        entity = update.SupervisorAddonUpdateEntity(
            coordinator=object(),  # type: ignore[arg-type]
            entity_description=update.ENTITY_DESCRIPTION,
            addon=SimpleNamespace(addon=SimpleNamespace(slug=probe)),  # type: ignore[arg-type]
        )
        unique_id = entity.unique_id
    except Exception as err:  # noqa: BLE001
        return f"a hassio app update entity can't be built: {type(err).__name__}"
    if not isinstance(unique_id, str) or app_slug_from_unique_id(unique_id) != probe:
        return "hassio app update entities have a new unique ID shape"
    return None


def _check_supervisor_client(hass: HomeAssistant) -> str | None:
    """Check the Supervisor client has the reads the collector makes."""
    if _not_supervisor(hass):
        return None
    from aiohasupervisor import SupervisorClient  # noqa: PLC0415
    from aiohasupervisor.addons import AddonsClient  # noqa: PLC0415
    from aiohasupervisor.store import StoreClient  # noqa: PLC0415

    from homeassistant.components import hassio  # noqa: PLC0415

    if not callable(getattr(hassio, "get_supervisor_client", None)):
        return "hassio.get_supervisor_client is missing"
    for attr in ("addons", "store"):
        if not isinstance(getattr(SupervisorClient, attr, None), property):
            return f"aiohasupervisor SupervisorClient.{attr} is missing"
    for cls, attr in ((AddonsClient, "addon_info"), (StoreClient, "repositories_list")):
        if not inspect.iscoroutinefunction(getattr(cls, attr, None)):
            return f"aiohasupervisor {cls.__name__}.{attr} is missing"
    return None


for _name, _check in (
    ("supervisor_ws_api", _check_supervisor_ws_api),
    ("hassio_update_addon_ws", _check_hassio_update_addon_ws),
    ("hassio_addon_services", _check_hassio_addon_services),
    ("supervisor_client", _check_supervisor_client),
):
    register_contract(Contract(_name, "apps", _check))

register_contract(
    Contract(
        "hassio_update_install", "apps_update_install", _check_hassio_update_install
    )
)


def _check_hassio_addon_manager(hass: HomeAssistant) -> str | None:
    """Core's `AddonManager` keeps the methods wrapped and its `addon_slug`."""
    if _not_supervisor(hass):
        return None
    from homeassistant.components.hassio import addon_manager  # noqa: PLC0415

    from .collectors.apps import MANAGER_METHODS  # noqa: PLC0415

    cls = getattr(addon_manager, "AddonManager", None)
    if not isinstance(cls, type):
        return "hassio AddonManager is missing"
    self_only: list[tuple[str, str, Any]] = [("self", _POS, _EMPTY)]
    for method in MANAGER_METHODS:
        expected = self_only
        if method == "async_set_addon_options":
            expected = [*self_only, ("config", _POS, _EMPTY)]
        label = f"AddonManager.{method}"
        if reason := _params_reason(
            label, getattr(cls, method, None), expected, coro=True
        ):
            return reason
    try:
        manager = cls(
            hass, logging.getLogger(__name__), "probe", "electric_fence_probe"
        )
        slug = manager.addon_slug
    except Exception as err:  # noqa: BLE001
        return f"hassio AddonManager can't be built: {type(err).__name__}"
    if slug != "electric_fence_probe":
        return "hassio AddonManager.addon_slug is not the app's slug"
    return None


register_contract(
    Contract("hassio_addon_manager", "apps_addon_manager", _check_hassio_addon_manager)
)


def _check_hassio_rest_proxy(hass: HomeAssistant) -> str | None:
    """Core's `/api/hassio` proxy lets no admin reach an app change.

    So the app collector needs no hook there. `HassIOView` checks the path
    against `PATHS_ADMIN` and refuses anything else.
    """
    if _not_supervisor(hass):
        return None
    from homeassistant.components.hassio import http  # noqa: PLC0415

    from .collectors.apps import APP_CHANGE_PATHS  # noqa: PLC0415

    allowed = getattr(http, "PATHS_ADMIN", None)
    if not isinstance(allowed, re.Pattern):
        return "hassio.http.PATHS_ADMIN is missing"
    if opened := [path for path in APP_CHANGE_PATHS if allowed.match(path)]:
        return "the /api/hassio proxy now lets admins reach app changes: " + ", ".join(
            opened
        )
    return None


register_contract(
    Contract("hassio_rest_proxy", "apps_rest_proxy", _check_hassio_rest_proxy)
)


INTEGRATION_CHECKS = "integration_checks"

_REPOSITORY_DATA_FIELDS = (
    "installed",
    "installed_version",
    "installed_commit",
    "full_name",
    "domain",
    "category",
    "archived",
    "last_updated",
)
_REPOSITORY_MANIFEST_FIELDS = (
    "zip_release",
    "filename",
    "content_in_root",
    "persistent_directory",
)


def _check_marketplace_repositories(hass: HomeAssistant) -> str | None:
    """Check the Marketplace objects the inventory reads keep their shape."""
    for entry in hass.config_entries.async_loaded_entries("marketplace"):
        repositories = getattr(entry.runtime_data, "repositories", None)
        if repositories is None:
            return "Marketplace manager has no repositories"
        for method in ("list_all", "is_removed", "removed_repository"):
            if not callable(getattr(repositories, method, None)):
                return f"Marketplace repositories.{method} is missing"
        if not hasattr(
            getattr(entry.runtime_data, "common", None), "archived_repositories"
        ):
            return "Marketplace common.archived_repositories is missing"
        try:
            listed = list(repositories.list_all())
        except Exception as err:  # noqa: BLE001
            return f"Marketplace repositories.list_all raised {type(err).__name__}"
        for repository in listed:
            data = getattr(repository, "data", None)
            manifest = getattr(repository, "repository_manifest", None)
            for field in _REPOSITORY_DATA_FIELDS:
                if not hasattr(data, field):
                    return f"Marketplace repository data.{field} is missing"
            for field in _REPOSITORY_MANIFEST_FIELDS:
                if not hasattr(manifest, field):
                    return f"Marketplace repository_manifest.{field} is missing"
            if repositories.is_removed(getattr(data, "full_name", "")):
                removed = repositories.removed_repository(
                    getattr(data, "full_name", "")
                )
                for field in ("reason", "removal_type"):
                    if not hasattr(removed, field):
                        return f"Marketplace removed repository {field} is missing"
    return None


register_contract(
    Contract(
        "marketplace_repositories", INTEGRATION_CHECKS, _check_marketplace_repositories
    )
)


def marketplace_contract_ok(hass: HomeAssistant) -> bool:
    """Return True when the integration checks may rely on the Marketplace shape."""
    return not check_contracts(hass, INTEGRATION_CHECKS)
