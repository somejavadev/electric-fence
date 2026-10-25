"""System settings collector: admin WebSocket commands that change settings.

Unlike the v0.1 commands, these fire no event afterwards, so the wrapper
records the change itself. For each command in `WS_SPECS`:

- The spec's reader takes the area's settings before Core's handler runs.
- Core's handler gets a `_ResultWatch` in place of the connection. When the
  handler sends its result for the message, the reader runs again and the
  difference is recorded (only setting names unless `record_values` is on).
  An error, an exception or no answer records nothing; so does a success
  that changed nothing.
- Action-style specs (`always_record`) record the parameters the reader
  returned before the call instead of a difference, on success.
  `record_before` records them before Core's handler runs, whatever the
  outcome (for commands that restart Home Assistant before answering).
- A reader returning None means the area is not available (or, for some
  readers, that Core will change nothing): nothing is recorded.
- The before snapshot is a deep copy, so a setting Core changes in place
  still shows its old value.

For each service in `SERVICE_SPECS` (the logger and recorder services; not
service calls in general), the registered `Service`'s job is replaced by a
wrapper (`hooks.wrap_service`). It reads before, awaits Core's handler, and
records only when the handler returned: the difference, or for `always_record`
specs the parameters. The actor is the call context's user (System without
one), the IP that of the same user's action scope, WebSocket connection or
HTTP request (`attribution.call_ip`). The services have their own contract
(`system_settings_services`): when it fails, only the service wrapping is
disabled and the collector's repair issue is raised; the WebSocket part stays.

Safety (spec §4): Core's handler is called exactly once and its return value
or exception passes through unchanged. Reading, diffing and submitting run
inside `self.guard`; when any of that fails, the call goes through untouched
(with the real connection if the failure was before the call).
"""

from collections.abc import Callable, Mapping
import copy
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any, ClassVar, cast

from homeassistant.components.websocket_api import ActiveConnection
from homeassistant.components.websocket_api.const import (
    DOMAIN as WEBSOCKET_API_DOMAIN,
    TYPE_RESULT,
    WebSocketCommandHandler,
)
from homeassistant.const import EVENT_COMPONENT_LOADED
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    HomeAssistant,
    ServiceCall,
    callback,
)
from homeassistant.setup import ATTR_COMPONENT, EventComponentLoaded
from homeassistant.util import dt as dt_util
from homeassistant.util.json import json_loads

from ..attribution import auth_users, call_ip, ws_ip
from ..contracts import CONTRACTS, check_contracts
from ..failopen import FailOpen
from ..hooks import HookHandle, ServiceHandler, wrap_service, wrap_ws_command
from ..models import ChangeAction, ChangeKind, SettingsChange
from ..redact import diff_details
from ..repairs import async_report_disabled_collector
from .base import BaseCollector

if TYPE_CHECKING:
    from ..runtime import Runtime

type Reader = Callable[[HomeAssistant, dict[str, Any]], Mapping[str, Any] | None]
type ActionFor = Callable[[dict[str, Any]], ChangeAction]

_CONTRACT = "system_settings_ws"
# The services part has its own contract "collector", so a failure disables
# the service wrapping only (see `SystemSettingsCollector.install`).
SERVICES_CONTRACT_GROUP = "system_settings_services"
# Message keys that are not command parameters.
_ENVELOPE = frozenset({"id", "type"})


@dataclass(frozen=True)
class WsSpec:
    """How to record one admin WebSocket command.

    `read(hass, msg)` returns a fresh snapshot of the area's settings (or,
    for action-style specs, the parameters to record), or None when the
    area is not available. `read_after`, when set, reads the after snapshot
    instead (for commands whose before and after are different things).
    `action` is fixed, or chosen from the message.
    """

    command: str
    component: str
    kind: ChangeKind
    action: ChangeAction | ActionFor
    target_name: str
    read: Reader
    # Record the parameters before Core's handler runs, whatever the outcome.
    record_before: bool = False
    # Record the parameters on success, instead of a before/after difference.
    always_record: bool = False
    read_after: Reader | None = None
    # Message fields the reader or action uses; the contract checks Core's
    # schema for the command still has them.
    message_keys: tuple[str, ...] = ()

    def action_for(self, msg: dict[str, Any]) -> ChangeAction:
        """Return the action recorded for this message."""
        if isinstance(self.action, ChangeAction):
            return self.action
        return self.action(msg)


def request_params(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    """Return the command's parameters (the message without `id` and `type`)."""
    return {k: v for k, v in msg.items() if k not in _ENVELOPE}


# The area readers (spec "What gets recorded"). Each reads state Core has
# already changed when its handler sends the result; Core's modules are
# imported late, as the components load after this one may.


def _backup_config(
    hass: HomeAssistant, msg: dict[str, Any]
) -> Mapping[str, Any] | None:
    """Return the automatic backup settings."""
    from homeassistant.components.backup.const import DATA_MANAGER  # noqa: PLC0415

    if (manager := hass.data.get(DATA_MANAGER)) is None:
        return None
    return manager.config.data.to_dict()


_BACKUP_ID = "backup_id"
_HTTP_CONFIG = "config"
_LOGGER_MODULE = "module"
_LOGGER_INTEGRATION = "integration"


def _backup_delete(hass: HomeAssistant, msg: dict[str, Any]) -> Mapping[str, Any]:
    """Return the backup to delete."""
    return {_BACKUP_ID: msg[_BACKUP_ID]}


_RESTORE_PARAMS = (
    _BACKUP_ID,
    "agent_id",
    "restore_database",
    "restore_folders",
    "restore_homeassistant",
    "restore_addons",
)


def _backup_restore(hass: HomeAssistant, msg: dict[str, Any]) -> Mapping[str, Any]:
    """Return what to restore (never the password)."""
    return {key: msg.get(key) for key in _RESTORE_PARAMS}


def _http_store(hass: HomeAssistant) -> Any:
    from homeassistant.components.http.config import DATA_STORE  # noqa: PLC0415

    return hass.data.get(DATA_STORE)


def _http_settings(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return an HTTP config without its created_at and error metadata."""
    from homeassistant.components.http import config as http  # noqa: PLC0415

    meta = (
        http.HTTP_CONFIG_CREATED_AT,
        http.HTTP_CONFIG_ERROR,
        http.HTTP_CONFIG_ERROR_MESSAGE,
    )
    return {k: v for k, v in config.items() if k not in meta}


def _http_configure_before(
    hass: HomeAssistant, msg: dict[str, Any]
) -> Mapping[str, Any] | None:
    """Return what the submitted config is compared with, or None if no change.

    A new config is compared with the stable one, which is in force. Core
    drops the pending config when the submission is None or equals stable:
    that is compared from the pending config. Core keeps an identical,
    untried pending config as is: nothing changes.
    """
    from homeassistant.components.http.config import HTTP_CONFIG_ERROR  # noqa: PLC0415

    if (store := _http_store(hass)) is None:
        return None
    stable = _http_settings(store.stable)
    pending = store.pending
    config = msg[_HTTP_CONFIG]
    if config is None or _http_settings(config) == stable:
        return None if pending is None else _http_settings(pending)
    if (
        pending is not None
        and pending.get(HTTP_CONFIG_ERROR) is None
        and _http_settings(config) == _http_settings(pending)
    ):
        return None
    return stable


def _http_configure_after(
    hass: HomeAssistant, msg: dict[str, Any]
) -> Mapping[str, Any] | None:
    """Return the submitted config, or stable when the pending one was dropped."""
    if (store := _http_store(hass)) is None:
        return None
    config = msg[_HTTP_CONFIG]
    return _http_settings(store.stable if config is None else config)


def _http_configure_action(msg: dict[str, Any]) -> ChangeAction:
    return ChangeAction.WITHDRAWN if msg[_HTTP_CONFIG] is None else ChangeAction.UPDATED


def _http_stable(hass: HomeAssistant, msg: dict[str, Any]) -> Mapping[str, Any] | None:
    """Return the stable (confirmed) HTTP config."""
    if (store := _http_store(hass)) is None:
        return None
    return _http_settings(store.stable)


def _network(hass: HomeAssistant, msg: dict[str, Any]) -> Mapping[str, Any] | None:
    """Return the adapter selection (once the network singleton is loaded)."""
    from homeassistant.components.network.network import DATA_NETWORK, Network  # noqa: PLC0415

    network = hass.data.get(DATA_NETWORK)
    if not isinstance(network, Network):
        return None
    return {"configured_adapters": network.configured_adapters}


def _analytics(hass: HomeAssistant, msg: dict[str, Any]) -> Mapping[str, Any] | None:
    """Return the analytics preferences."""
    from homeassistant.components.analytics import DATA_COMPONENT  # noqa: PLC0415

    if (analytics := hass.data.get(DATA_COMPONENT)) is None:
        return None
    return dict(analytics.preferences)


def _level(name: str) -> str:
    """Return a logger's effective level name, without creating the logger."""
    loggers = logging.Logger.manager.loggerDict
    while name:
        if isinstance(found := loggers.get(name), logging.Logger):
            return logging.getLevelName(found.getEffectiveLevel())
        name = name.rpartition(".")[0]
    return logging.getLevelName(logging.getLogger().getEffectiveLevel())


def _logger_module(hass: HomeAssistant, msg: dict[str, Any]) -> Mapping[str, Any]:
    """Return the module logger's level name."""
    return {msg[_LOGGER_MODULE]: _level(msg[_LOGGER_MODULE])}


def _logger_integration(hass: HomeAssistant, msg: dict[str, Any]) -> Mapping[str, Any]:
    """Return the integration logger's level name, keyed by its domain."""
    domain = msg[_LOGGER_INTEGRATION]
    return {domain: _level(f"homeassistant.components.{domain}")}


AREA_SPECS: tuple[WsSpec, ...] = (
    WsSpec(
        "backup/config/update",
        "backup",
        ChangeKind.BACKUP,
        ChangeAction.UPDATED,
        "Backups",
        _backup_config,
    ),
    WsSpec(
        "backup/delete",
        "backup",
        ChangeKind.BACKUP,
        ChangeAction.REMOVED,
        "Backup",
        _backup_delete,
        always_record=True,
        message_keys=(_BACKUP_ID,),
    ),
    WsSpec(
        "backup/restore",
        "backup",
        ChangeKind.BACKUP,
        ChangeAction.RESTORE_REQUESTED,
        "Backup",
        _backup_restore,
        message_keys=_RESTORE_PARAMS,
        # A successful restore restarts Home Assistant before any result.
        record_before=True,
    ),
    WsSpec(
        "http/config/configure",
        "http",
        ChangeKind.HTTP,
        _http_configure_action,
        "HTTP",
        _http_configure_before,
        read_after=_http_configure_after,
        message_keys=(_HTTP_CONFIG,),
    ),
    WsSpec(
        "http/config/promote",
        "http",
        ChangeKind.HTTP,
        ChangeAction.CONFIRMED,
        "HTTP",
        _http_stable,
    ),
    WsSpec(
        "network/configure",
        "network",
        ChangeKind.NETWORK,
        ChangeAction.UPDATED,
        "Network",
        _network,
    ),
    WsSpec(
        "analytics/preferences",
        "analytics",
        ChangeKind.ANALYTICS,
        ChangeAction.UPDATED,
        "Analytics",
        _analytics,
    ),
    WsSpec(
        "logger/log_level",
        "logger",
        ChangeKind.LOGGER,
        ChangeAction.UPDATED,
        "Logging",
        _logger_module,
        message_keys=(_LOGGER_MODULE,),
    ),
    WsSpec(
        "logger/integration_log_level",
        "logger",
        ChangeKind.LOGGER,
        ChangeAction.UPDATED,
        "Logging",
        _logger_integration,
        message_keys=(_LOGGER_INTEGRATION,),
    ),
)


type ServiceReader = Callable[[HomeAssistant, ServiceCall], Mapping[str, Any] | None]
type ServiceActionFor = Callable[[Mapping[str, Any], Mapping[str, Any]], ChangeAction]


@dataclass(frozen=True)
class ServiceSpec:
    """How to record one registered service.

    `read(hass, call)` returns a fresh snapshot of the area's settings (or,
    for `always_record` specs, the parameters to record), or None when the
    area is not available. `action` is fixed, or chosen from the before and
    after snapshots.
    """

    domain: str
    service: str
    kind: ChangeKind
    action: ChangeAction | ServiceActionFor
    target_name: str
    read: ServiceReader
    # Record the parameters on success, instead of a before/after difference.
    always_record: bool = False
    # `call.data` fields the reader uses; the contract checks Core's service
    # schema still has them.
    service_keys: tuple[str, ...] = ()

    def action_for(
        self, before: Mapping[str, Any], after: Mapping[str, Any]
    ) -> ChangeAction:
        """Return the action recorded for this change."""
        if isinstance(self.action, ChangeAction):
            return self.action
        return self.action(before, after)


def _logger_levels(hass: HomeAssistant, call: ServiceCall) -> Mapping[str, Any]:
    """Return the level names of the loggers named in the call."""
    return {str(name): _level(str(name)) for name in call.data}


def _logger_default(hass: HomeAssistant, call: ServiceCall) -> Mapping[str, Any]:
    """Return the root logger's level name."""
    return {"default": logging.getLevelName(logging.getLogger().level)}


def _recorder_enabled(
    hass: HomeAssistant, call: ServiceCall
) -> Mapping[str, Any] | None:
    """Return whether the recorder records."""
    from homeassistant.helpers.recorder import DATA_INSTANCE  # noqa: PLC0415

    if (instance := hass.data.get(DATA_INSTANCE)) is None:
        return None
    return {"enabled": instance.enabled}


PURGE_PARAMS = ("keep_days", "repack", "apply_filter")
PURGE_ENTITIES_PARAMS = ("entity_id", "domains", "entity_globs", "keep_days")


def _call_params(keys: tuple[str, ...]) -> ServiceReader:
    def read(hass: HomeAssistant, call: ServiceCall) -> Mapping[str, Any]:
        """Return the call's parameters the service uses."""
        return {key: call.data.get(key) for key in keys}

    return read


SERVICE_AREA_SPECS: tuple[ServiceSpec, ...] = (
    ServiceSpec(
        "logger",
        "set_level",
        ChangeKind.LOGGER,
        ChangeAction.UPDATED,
        "Logging",
        _logger_levels,
    ),
    ServiceSpec(
        "logger",
        "set_default_level",
        ChangeKind.LOGGER,
        ChangeAction.UPDATED,
        "Logging",
        _logger_default,
    ),
    ServiceSpec(
        "recorder",
        "disable",
        ChangeKind.RECORDER,
        ChangeAction.DISABLED,
        "Recorder",
        _recorder_enabled,
    ),
    ServiceSpec(
        "recorder",
        "enable",
        ChangeKind.RECORDER,
        ChangeAction.ENABLED,
        "Recorder",
        _recorder_enabled,
    ),
    ServiceSpec(
        "recorder",
        "purge",
        ChangeKind.RECORDER,
        ChangeAction.PURGED,
        "Recorder",
        _call_params(PURGE_PARAMS),
        always_record=True,
        service_keys=PURGE_PARAMS,
    ),
    ServiceSpec(
        "recorder",
        "purge_entities",
        ChangeKind.RECORDER,
        ChangeAction.PURGED,
        "Recorder",
        _call_params(PURGE_ENTITIES_PARAMS),
        always_record=True,
        service_keys=PURGE_ENTITIES_PARAMS,
    ),
)


class _ResultWatch:
    """An `ActiveConnection` stand-in that watches one message's answer.

    Every attribute read, write and delete goes to the real connection, so
    Core's handler sees the same object state. `ActiveConnection` has
    `__slots__`, so its methods can't be swapped on the instance, and a
    per-call subclass would need a copy of its state; a proxy keeps the one
    real connection. `__class__` reports `ActiveConnection`, so an
    `isinstance` check still passes.

    The first answer for our message ID decides: a result calls `on_success`
    (inside the guard, before the result is forwarded); an error (by
    `send_error`, `async_handle_exception` or an error message) means no
    success. Answers are always forwarded unchanged.
    """

    __slots__ = ("_connection", "_decided", "_guard", "_msg_id", "_on_success")

    _connection: ActiveConnection
    _decided: bool
    _guard: FailOpen
    _msg_id: int
    _on_success: Callable[[], None]

    def __init__(
        self,
        connection: ActiveConnection,
        msg_id: int,
        on_success: Callable[[], None],
        guard: FailOpen,
    ) -> None:
        """Watch `connection` for the answer to message `msg_id`."""
        set_own = object.__setattr__
        set_own(self, "_connection", connection)
        set_own(self, "_msg_id", msg_id)
        set_own(self, "_on_success", on_success)
        set_own(self, "_guard", guard)
        set_own(self, "_decided", False)

    @property  # type: ignore[misc]
    def __class__(self) -> type:
        return ActiveConnection

    def __getattr__(self, name: str) -> Any:
        """Read everything else from the real connection."""
        return getattr(self._connection, name)

    def __setattr__(self, name: str, value: Any) -> None:
        """Write to the real connection."""
        setattr(self._connection, name, value)

    def __delattr__(self, name: str) -> None:
        """Delete on the real connection."""
        delattr(self._connection, name)

    def __repr__(self) -> str:
        """Return the real connection's representation."""
        return repr(self._connection)

    def _decide(self, success: bool) -> None:
        if self._decided:
            return
        object.__setattr__(self, "_decided", True)
        if success:
            self._guard.call(self._on_success)

    def _check_message(self, msg: Any) -> None:
        """Decide on a result or error message for our ID sent as a message."""
        if self._decided:
            return
        if isinstance(msg, (bytes, bytearray, memoryview, str)):
            try:
                msg = json_loads(msg)
            except ValueError:
                return
        if (
            isinstance(msg, dict)
            and msg.get("id") == self._msg_id
            and msg.get("type") == TYPE_RESULT
        ):
            self._decide(msg.get("success") is True)

    def send_result(self, msg_id: int, result: Any | None = None) -> None:
        """Note a success for our ID, then send the result."""
        if msg_id == self._msg_id:
            self._decide(True)
        self._connection.send_result(msg_id, result)

    def send_error(self, msg_id: int, *args: Any, **kwargs: Any) -> None:
        """Note a failure for our ID, then send the error."""
        if msg_id == self._msg_id:
            self._decide(False)
        self._connection.send_error(msg_id, *args, **kwargs)

    def send_message(self, msg: Any) -> None:
        """Note a result or error message for our ID, then send it."""
        self._guard.call(self._check_message, msg)
        self._connection.send_message(msg)

    def async_handle_exception(self, msg: dict[str, Any], err: Exception) -> None:
        """Note a failure for our ID, then let Core answer the exception."""
        if self._guard.call(lambda: msg.get("id") == self._msg_id):
            self._decide(False)
        self._connection.async_handle_exception(msg, err)


@dataclass(frozen=True, slots=True)
class _Actor:
    user_id: str | None
    user_name: str | None
    ip: str | None


def _actor(connection: ActiveConnection) -> _Actor:
    """Return who sent the command (admin commands always have a user)."""
    user = connection.user
    return _Actor(
        user_id=user.id if user is not None else None,
        user_name=user.name if user is not None else None,
        ip=ws_ip(connection),
    )


def _call_actor(hass: HomeAssistant, call: ServiceCall) -> _Actor:
    """Return who called the service: its context's user, else System."""
    if (user_id := call.context.user_id) is None:
        return _Actor(user_id=None, user_name=None, ip=None)
    users = auth_users(hass)
    user = users.get(user_id) if users is not None else None
    return _Actor(
        user_id=user_id,
        user_name=user.name if user is not None else None,
        ip=call_ip(user_id),
    )


class SystemSettingsCollector(BaseCollector):
    """Record admin WebSocket commands and services that change system settings."""

    name = "system_settings"

    WS_SPECS: ClassVar[tuple[WsSpec, ...]] = AREA_SPECS
    SERVICE_SPECS: ClassVar[tuple[ServiceSpec, ...]] = SERVICE_AREA_SPECS

    def __init__(self, hass: HomeAssistant, runtime: Runtime) -> None:
        """Initialise the collector."""
        super().__init__(hass, runtime)
        # Components whose commands wait for them to load, and the listener.
        self._waiting: set[str] = set()
        self._loaded_unsub: CALLBACK_TYPE | None = None
        self._wrapped: set[str] = set()
        # The service part: its own handles, and why it is disabled (if so).
        self._service_handles: list[HookHandle] = []
        self._wrapped_services: set[str] = set()
        self._services_reasons: list[str] = []

    def install(self) -> bool:
        """Install; a failed services contract disables the services only.

        The WebSocket part stays installed. The collector's repair issue is
        raised with the services' reasons (prefixed "services: "), which are
        also its `disabled_reason`.
        """
        if not super().install():
            return False
        if self._services_reasons:
            self._report_services_disabled()
        return True

    def _install(self) -> None:
        # The WebSocket contract already passed for the components loaded.
        self._wrap_ws_commands()
        self._services_reasons = check_contracts(self.hass, SERVICES_CONTRACT_GROUP)
        if not self._services_reasons:
            self._wrap_services()
        loaded = self.hass.config.components
        self._waiting = {
            spec.component for spec in self.WS_SPECS if spec.component not in loaded
        } | {spec.domain for spec in self.SERVICE_SPECS if spec.domain not in loaded}
        if self._waiting:
            self._loaded_unsub = self.hass.bus.async_listen(
                EVENT_COMPONENT_LOADED,
                self._on_component_loaded,
                event_filter=self._is_waiting,
            )

    def _uninstall(self) -> None:
        self._waiting.clear()
        self._wrapped.clear()
        self._unwrap_services()
        if self._loaded_unsub is not None:
            unsub, self._loaded_unsub = self._loaded_unsub, None
            unsub()

    @callback
    def _is_waiting(self, event_data: EventComponentLoaded) -> bool:
        return event_data[ATTR_COMPONENT] in self._waiting

    @callback
    def _on_component_loaded(self, event: Event[EventComponentLoaded]) -> None:
        """Wrap a component's commands once it has registered them."""
        self._waiting.discard(event.data[ATTR_COMPONENT])
        if not self._waiting and self._loaded_unsub is not None:
            unsub, self._loaded_unsub = self._loaded_unsub, None
            unsub()
        if self._installed:
            self.guard.call(self._install_ws_late)

    def _install_ws_late(self) -> None:
        reason = CONTRACTS[_CONTRACT].check(self.hass)
        if reason is not None:
            # Same outcome as a contract failing at install.
            self.uninstall()
            self.disabled_reason = [reason]
            async_report_disabled_collector(self.hass, self.name, [reason])
            return
        self._wrap_ws_commands()
        if self._services_reasons:
            return
        if reasons := check_contracts(self.hass, SERVICES_CONTRACT_GROUP):
            self._unwrap_services()
            self._services_reasons = reasons
            self._report_services_disabled()
            return
        self._wrap_services()

    def _report_services_disabled(self) -> None:
        self.disabled_reason = [f"services: {r}" for r in self._services_reasons]
        async_report_disabled_collector(self.hass, self.name, self.disabled_reason)

    def _wrap_ws_commands(self) -> None:
        """Wrap the registered commands of loaded components, each once."""
        handlers = self.hass.data.get(WEBSOCKET_API_DOMAIN, {})
        loaded = self.hass.config.components
        for spec in self.WS_SPECS:
            if (
                spec.command not in self._wrapped
                and spec.component in loaded
                and spec.command in handlers
            ):
                self._handles.append(
                    wrap_ws_command(self.hass, spec.command, self._wrap_ws(spec))
                )
                self._wrapped.add(spec.command)

    def _wrap_services(self) -> None:
        """Wrap the registered services of loaded components, each once."""
        services = self.hass.services._services  # noqa: SLF001
        loaded = self.hass.config.components
        for spec in self.SERVICE_SPECS:
            key = f"{spec.domain}.{spec.service}"
            if (
                key not in self._wrapped_services
                and spec.domain in loaded
                and spec.service in services.get(spec.domain, {})
            ):
                self._service_handles.append(
                    wrap_service(
                        self.hass, spec.domain, spec.service, self._wrap_service(spec)
                    )
                )
                self._wrapped_services.add(key)

    def _unwrap_services(self) -> None:
        handles, self._service_handles = self._service_handles, []
        self._wrapped_services.clear()
        for handle in handles:
            try:
                handle.restore()
            except Exception:  # noqa: BLE001
                self.guard.errors += 1

    # The wrappers. Core's call runs untouched; ours never raises.

    def _wrap_service(
        self, spec: ServiceSpec
    ) -> Callable[[ServiceHandler], ServiceHandler]:
        def make(original: ServiceHandler) -> ServiceHandler:
            async def handler(call: ServiceCall) -> Any:
                pending = self.guard.call(self._service_before, spec, call)
                # Core's handler: its result or exception passes unchanged.
                result = await original(call)
                if pending is not None:
                    self.guard.call(self._service_after, spec, call, *pending)
                return result

            return handler

        return make

    def _service_before(
        self, spec: ServiceSpec, call: ServiceCall
    ) -> tuple[_Actor, dict[str, Any]] | None:
        """Read before Core's handler: who calls, and the snapshot."""
        if (found := spec.read(self.hass, call)) is None:
            return None
        # Deep: Core may change nested settings in place.
        return _call_actor(self.hass, call), copy.deepcopy(dict(found))

    def _service_after(
        self,
        spec: ServiceSpec,
        call: ServiceCall,
        actor: _Actor,
        before: dict[str, Any],
    ) -> None:
        """Record once Core's handler returned (inside the guard)."""
        if spec.always_record:
            after: Mapping[str, Any] = before
            details = self._params_details(before)
        else:
            if (found := spec.read(self.hass, call)) is None:
                return
            after = found
            details = diff_details(
                before, after, include_values=self.runtime.settings.record_values
            )
            if not details["changed"]:
                return
        self._submit(
            spec.kind,
            spec.action_for(before, after),
            spec.target_name,
            actor,
            details,
        )

    def _wrap_ws(
        self, spec: WsSpec
    ) -> Callable[[WebSocketCommandHandler], WebSocketCommandHandler]:
        def make(original: WebSocketCommandHandler) -> WebSocketCommandHandler:
            def handler(
                hass: HomeAssistant,
                connection: ActiveConnection,
                msg: dict[str, Any],
            ) -> None:
                watch = self.guard.call(self._before, spec, connection, msg)
                # The watch stands in for the connection (see `_ResultWatch`).
                target = connection if watch is None else cast(ActiveConnection, watch)
                return original(hass, target, msg)

            return handler

        return make

    def _before(
        self, spec: WsSpec, connection: ActiveConnection, msg: dict[str, Any]
    ) -> _ResultWatch | None:
        """Read before Core's handler; return the watch to pass it, if any."""
        found = spec.read(self.hass, msg)
        if found is None:
            return None
        # Deep: Core may change nested settings in place.
        before = copy.deepcopy(dict(found))
        actor = _actor(connection)
        if spec.record_before:
            self._submit_ws(spec, actor, msg, self._params_details(before))
            return None

        def on_success() -> None:
            self._after(spec, actor, before, msg)

        return _ResultWatch(connection, msg["id"], on_success, self.guard)

    def _after(
        self,
        spec: WsSpec,
        actor: _Actor,
        before: dict[str, Any],
        msg: dict[str, Any],
    ) -> None:
        """Record once Core's handler sent its result (inside the guard)."""
        if spec.always_record:
            details = self._params_details(before)
        else:
            after = (spec.read_after or spec.read)(self.hass, msg)
            if after is None:
                return
            details = diff_details(
                before, after, include_values=self.runtime.settings.record_values
            )
            if not details["changed"]:
                return
        self._submit_ws(spec, actor, msg, details)

    def _params_details(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Details of an action: the parameters given (not None), redacted."""
        given = {k: v for k, v in params.items() if v is not None}
        return diff_details(
            {}, given, include_values=self.runtime.settings.record_values
        )

    def _submit_ws(
        self,
        spec: WsSpec,
        actor: _Actor,
        msg: dict[str, Any],
        details: dict[str, Any],
    ) -> None:
        self._submit(spec.kind, spec.action_for(msg), spec.target_name, actor, details)

    def _submit(
        self,
        kind: ChangeKind,
        action: ChangeAction,
        target_name: str,
        actor: _Actor,
        details: dict[str, Any],
    ) -> None:
        self.runtime.worker.submit(
            SettingsChange(
                ts=dt_util.utcnow(),
                kind=kind,
                action=action,
                target_id=None,
                target_name=target_name,
                details=details,
                actor_user_id=actor.user_id,
                actor_name=actor.user_name,
                actor_ip=actor.ip,
            )
        )
