"""App collector: app (add-on) changes made through Home Assistant.

Supervisor installs only (`is_hassio`). The sources, each recorded only when
Supervisor or Core reports success:

- The `supervisor/api` WebSocket command, which the frontend uses for every
  app action; `classify` maps its endpoint and method to an action.
- The `hassio/update/addon` WebSocket command.
- The `hassio.addon_*` and `hassio.app_*` start, stop and restart services.
- `update.install` on an app's update entity (wrapped once `update` loads).
- Core's own `AddonManager` (Z-Wave JS, Matter, OTBR, the hardware
  integrations, ...), which calls the Supervisor client directly. Its
  changes are System unless an admin action that implies them is running
  (`credited_actor`).

The update entities and `AddonManager` have their own contract groups: a
failing one skips only that source, with its own repair issue.

Every classified call is marked in flight on the outside watcher before
Core's handler runs, and ended with Core's answer, so the steps Supervisor
reports while the call runs aren't recorded as outside changes.

Core's `/api/hassio` REST proxy is not a source: it only lets admins reach
backups, logs, changelogs and documentation, so it refuses every app change.
The `hassio_rest_proxy` contract keeps that true; if Core ever opens it, a
repair issue says so and the other sources keep recording.

Details come from Supervisor's own state, read with the Supervisor client:
before the call only when needed (versions, option values, a repository's
URL), and after it once it succeeded. A call `classify` doesn't match goes
straight to Core with nothing of ours awaited. A failing read only leaves
details out.

Safety (spec §4): Core's handler is called exactly once and its answer or
exception passes through unchanged. Everything of ours runs inside
`self.guard` or catches `Exception`.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
import re
from typing import TYPE_CHECKING, Any, Final, cast

from homeassistant.components.websocket_api import ActiveConnection
from homeassistant.components.websocket_api.const import WebSocketCommandHandler
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ENTITY_MATCH_ALL,
    EVENT_COMPONENT_LOADED,
    STATE_ON,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    HomeAssistant,
    ServiceCall,
    callback,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.hassio import is_hassio
from homeassistant.helpers.service import async_extract_entity_ids
from homeassistant.setup import ATTR_COMPONENT, EventComponentLoaded
from homeassistant.util import dt as dt_util

from ..attribution import credited_actor
from ..contracts import check_contracts
from ..failopen import reraise
from ..hooks import ServiceHandler, wrap_async_method, wrap_service, wrap_ws_command
from ..models import ChangeAction, ChangeKind, SettingsChange
from ..redact import HIDDEN, diff_details, redact_value
from ..repairs import async_clear_disabled_collector, async_report_disabled_collector
from .base import BaseCollector
from .system_settings import Actor, ResultWatch, call_actor, ws_actor

if TYPE_CHECKING:
    from ..runtime import Runtime

HASSIO: Final = "hassio"
UPDATE: Final = "update"
CMD_SUPERVISOR_API: Final = "supervisor/api"
CMD_UPDATE_ADDON: Final = "hassio/update/addon"
UPDATE_INSTALL: Final = "install"
# The rest proxy contract has its own group: a failure raises the repair
# issue but keeps the collector (see `AppCollector.install`).
REST_CONTRACT_GROUP: Final = "apps_rest_proxy"
# Each skips only its own source when it fails, with its own repair issue.
UPDATE_CONTRACT_GROUP: Final = "apps_update_install"
MANAGER_CONTRACT_GROUP: Final = "apps_addon_manager"
_PART_LABELS: Final = {
    UPDATE_CONTRACT_GROUP: "update entities",
    MANAGER_CONTRACT_GROUP: "app manager",
}

# Service name -> (the `call.data` field with the slug, action).
APP_SERVICES: Final[dict[str, tuple[str, ChangeAction]]] = {
    "addon_start": ("addon", ChangeAction.STARTED),
    "addon_stop": ("addon", ChangeAction.STOPPED),
    "addon_restart": ("addon", ChangeAction.RESTARTED),
    "app_start": ("app", ChangeAction.STARTED),
    "app_stop": ("app", ChangeAction.STOPPED),
    "app_restart": ("app", ChangeAction.RESTARTED),
}

# Core's `hassio.addon_manager.AddonManager` methods -> action. Only
# `async_set_addon_options` takes an argument (the options).
MANAGER_METHODS: Final[dict[str, ChangeAction]] = {
    "async_install_addon": ChangeAction.INSTALLED,
    "async_uninstall_addon": ChangeAction.UNINSTALLED,
    "async_update_addon": ChangeAction.UPDATED,
    "async_start_addon": ChangeAction.STARTED,
    "async_stop_addon": ChangeAction.STOPPED,
    "async_restart_addon": ChangeAction.RESTARTED,
    "async_set_addon_options": ChangeAction.OPTIONS_CHANGED,
}

# Core's app update entities have unique ID `<slug>_version_latest`; Core,
# Supervisor, OS and host use `home_assistant_<part>_version_latest`.
APP_UPDATE_SUFFIX: Final = "_version_latest"
_NOT_APP_PREFIX: Final = "home_assistant_"

# Seconds a Supervisor read may take before its details are left out.
READ_TIMEOUT: Final = 10.0

OPTIONS: Final = "options"
SETTINGS_KEYS: Final = (
    "boot",
    "watchdog",
    "auto_update",
    "ingress_panel",
    "network",
    "audio_input",
    "audio_output",
)
SECURITY_KEYS: Final = ("protected",)
_STATE_KEYS: Final = (OPTIONS, *SETTINGS_KEYS, *SECURITY_KEYS)
_REPOSITORY: Final = "repository"

_SLUG = r"([^/]+)"
_POST_ROUTES: Final = tuple(
    (re.compile(pattern), action)
    for pattern, action in (
        # The frontend installs with the `/addons/<slug>/install` alias.
        (rf"/(?:store/)?addons/{_SLUG}/install", ChangeAction.INSTALLED),
        (rf"/addons/{_SLUG}/uninstall", ChangeAction.UNINSTALLED),
        (rf"/(?:store/)?addons/{_SLUG}/update", ChangeAction.UPDATED),
        (rf"/addons/{_SLUG}/start", ChangeAction.STARTED),
        (rf"/addons/{_SLUG}/stop", ChangeAction.STOPPED),
        (rf"/addons/{_SLUG}/restart", ChangeAction.RESTARTED),
        (rf"/addons/{_SLUG}/rebuild", ChangeAction.REBUILT),
    )
)
_OPTIONS_ROUTE: Final = re.compile(rf"/addons/{_SLUG}/options")
_SECURITY_ROUTE: Final = re.compile(rf"/addons/{_SLUG}/security")
_REPOSITORIES: Final = "/store/repositories"
_REPOSITORY_ROUTE: Final = re.compile(rf"/store/repositories/{_SLUG}")

# Supervisor API paths (no leading slash) of every app change; the rest
# proxy contract checks Core's proxy lets none of them through.
APP_CHANGE_PATHS: Final = (
    "store/addons/x/install",
    "addons/x/install",
    "addons/x/uninstall",
    "store/addons/x/update",
    "addons/x/update",
    "addons/x/start",
    "addons/x/stop",
    "addons/x/restart",
    "addons/x/rebuild",
    "addons/x/options",
    "addons/x/security",
    "store/repositories",
    "store/repositories/x",
)

# Actions whose details need the app's state from before the call.
_READ_BEFORE: Final = frozenset(
    {
        ChangeAction.UPDATED,
        ChangeAction.UNINSTALLED,
        ChangeAction.OPTIONS_CHANGED,
        ChangeAction.SETTINGS_CHANGED,
    }
)
# And after it (the app is gone after an uninstall).
_READ_AFTER: Final = frozenset(
    {
        ChangeAction.INSTALLED,
        ChangeAction.UPDATED,
        ChangeAction.OPTIONS_CHANGED,
        ChangeAction.SETTINGS_CHANGED,
        ChangeAction.STARTED,
        ChangeAction.STOPPED,
        ChangeAction.RESTARTED,
        ChangeAction.REBUILT,
    }
)
_VERSIONS: Final = frozenset({ChangeAction.INSTALLED, ChangeAction.UPDATED})
_BACKGROUND: Final = "background"
_CONFIG: Final = frozenset(
    {ChangeAction.OPTIONS_CHANGED, ChangeAction.SETTINGS_CHANGED}
)
_REPOSITORY_ACTIONS: Final = frozenset(
    {ChangeAction.REPOSITORY_ADDED, ChangeAction.REPOSITORY_REMOVED}
)


@dataclass(frozen=True)
class AppOp:
    """An app change a Supervisor call makes.

    `keys` are the request's fields for an options or settings change;
    empty means unknown (decided from Supervisor's state instead).
    `background` is an install or update Supervisor answers once it has
    accepted the job, before it is done.
    """

    action: ChangeAction
    slug: str | None
    repository: str | None
    keys: tuple[str, ...]
    background: bool = False


def classify(endpoint: str, method: str, data: dict[str, Any] | None) -> AppOp | None:
    """Return the app change a Supervisor API call makes, or None.

    Pure. `endpoint` is Supervisor's path (leading slash, a trailing slash
    is ignored); `data` the JSON body, or None when unknown.
    """
    path = endpoint.rstrip("/") if len(endpoint) > 1 else endpoint
    verb = method.upper()
    if verb == "DELETE":
        if found := _REPOSITORY_ROUTE.fullmatch(path):
            return AppOp(ChangeAction.REPOSITORY_REMOVED, None, found[1], ())
        return None
    if verb != "POST":
        return None
    for pattern, action in _POST_ROUTES:
        if found := pattern.fullmatch(path):
            background = (
                action in _VERSIONS
                and data is not None
                and data.get(_BACKGROUND) is True
            )
            return AppOp(action, found[1], None, (), background)
    if found := _OPTIONS_ROUTE.fullmatch(path):
        if data is None:
            return AppOp(ChangeAction.OPTIONS_CHANGED, found[1], None, ())
        action = (
            ChangeAction.OPTIONS_CHANGED
            if OPTIONS in data
            else ChangeAction.SETTINGS_CHANGED
        )
        return AppOp(action, found[1], None, tuple(sorted(map(str, data))))
    if found := _SECURITY_ROUTE.fullmatch(path):
        keys = tuple(sorted(map(str, data))) if data is not None else ()
        return AppOp(ChangeAction.SETTINGS_CHANGED, found[1], None, keys)
    if path == _REPOSITORIES:
        if data is None:
            return AppOp(ChangeAction.REPOSITORY_ADDED, None, None, ())
        repository = data.get(_REPOSITORY)
        if isinstance(repository, str):
            return AppOp(ChangeAction.REPOSITORY_ADDED, None, repository, ())
    return None


def app_slug_from_unique_id(unique_id: str) -> str | None:
    """Return the app of a `hassio` update entity's unique ID, or None."""
    if not unique_id.endswith(APP_UPDATE_SUFFIX) or unique_id.startswith(
        _NOT_APP_PREFIX
    ):
        return None
    return unique_id.removesuffix(APP_UPDATE_SUFFIX) or None


def _state_keys(op: AppOp) -> tuple[str, ...]:
    """Return the app state fields an options or settings change compares."""
    known = tuple(key for key in op.keys if key in _STATE_KEYS)
    return known or _STATE_KEYS


def _plain(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _snapshot(info: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: _plain(getattr(info, key, None)) for key in keys}


def _password_fields(schema: Any) -> frozenset[str]:
    """Return the top-level option names whose schema type is a password.

    Supervisor's UI schema marks them `format: password` (`type: password`
    in older versions).
    """
    if not isinstance(schema, list):
        return frozenset()
    return frozenset(
        str(item["name"])
        for item in schema
        if isinstance(item, dict)
        and "name" in item
        and "password" in (item.get("format"), item.get("type"))
    )


def _mask_options(snapshot: dict[str, Any], secret: frozenset[str]) -> dict[str, Any]:
    """Return the snapshot with password-typed option values hidden."""
    options = snapshot.get(OPTIONS)
    if not secret or not isinstance(options, dict):
        return snapshot
    masked = {k: dict(HIDDEN) if k in secret else v for k, v in options.items()}
    return {**snapshot, OPTIONS: masked}


def _safe_url(url: str | None) -> str | None:
    """Return a repository URL with credentials and tokens masked."""
    if url is None:
        return None
    shown = redact_value(url)
    return shown if isinstance(shown, str) else None


type _Finish = Callable[[bool], None]


@dataclass
class _Before:
    """What was read from Supervisor before the call."""

    info: Any = None
    repositories: dict[str, str] | None = None


class AppCollector(BaseCollector):
    """Record app changes made through Home Assistant."""

    name = "apps"

    def __init__(self, hass: HomeAssistant, runtime: Runtime) -> None:
        """Initialise the collector."""
        super().__init__(hass, runtime)
        self._loaded_unsub: CALLBACK_TYPE | None = None
        self._update_wrapped = False
        self._rest_reasons: list[str] = []
        self._part_reasons: dict[str, list[str]] = {}

    def install(self) -> bool:
        """Install on Supervisor installs only; else return False, untouched.

        A failing rest proxy contract keeps the collector installed and
        raises its repair issue with the reasons (prefixed "rest proxy: ").
        A failing update entity or app manager contract skips only that
        source and raises that group's own repair issue.
        """
        if not is_hassio(self.hass):
            return False
        if not super().install():
            return False
        self._report_parts()
        return True

    def _report_parts(self) -> None:
        reasons = [f"rest proxy: {r}" for r in self._rest_reasons]
        if reasons:
            async_report_disabled_collector(self.hass, self.name, reasons)
        for group, label in _PART_LABELS.items():
            if part := self._part_reasons.get(group):
                async_report_disabled_collector(self.hass, group, part)
                reasons.extend(f"{label}: {r}" for r in part)
            else:
                async_clear_disabled_collector(self.hass, group)
        self.disabled_reason = reasons

    def _install_part(self, group: str, wrap: Callable[[], None]) -> None:
        """Wrap one source if its contracts hold; else note why not."""
        if reasons := check_contracts(self.hass, group):
            self._part_reasons[group] = reasons
            return
        try:
            wrap()
        except Exception as err:  # noqa: BLE001
            self._part_reasons[group] = [f"install raised {type(err).__name__}"]

    def _install(self) -> None:
        # The contracts passed: the commands and services are registered.
        self._handles.append(
            wrap_ws_command(self.hass, CMD_SUPERVISOR_API, self._wrap_api)
        )
        self._handles.append(
            wrap_ws_command(self.hass, CMD_UPDATE_ADDON, self._wrap_update_addon)
        )
        for service, (key, action) in APP_SERVICES.items():
            self._handles.append(
                wrap_service(
                    self.hass, HASSIO, service, self._wrap_app_service(key, action)
                )
            )
        self._part_reasons = {}
        self._install_part(MANAGER_CONTRACT_GROUP, self._wrap_addon_manager)
        if UPDATE in self.hass.config.components:
            self._install_part(UPDATE_CONTRACT_GROUP, self._wrap_update_install)
        else:
            self._loaded_unsub = self.hass.bus.async_listen(
                EVENT_COMPONENT_LOADED,
                self._on_component_loaded,
                event_filter=self._is_update,
            )
        self._rest_reasons = check_contracts(self.hass, REST_CONTRACT_GROUP)

    def _uninstall(self) -> None:
        self._update_wrapped = False
        if self._loaded_unsub is not None:
            unsub, self._loaded_unsub = self._loaded_unsub, None
            unsub()

    @callback
    def _is_update(self, event_data: EventComponentLoaded) -> bool:
        return event_data[ATTR_COMPONENT] == UPDATE

    @callback
    def _on_component_loaded(self, event: Event[EventComponentLoaded]) -> None:
        if self._loaded_unsub is not None:
            unsub, self._loaded_unsub = self._loaded_unsub, None
            unsub()
        if self._installed:
            self.guard.call(self._install_update_late)

    def _install_update_late(self) -> None:
        self._install_part(UPDATE_CONTRACT_GROUP, self._wrap_update_install)
        self._report_parts()

    def _wrap_update_install(self) -> None:
        if self._update_wrapped:
            return
        self._handles.append(
            wrap_service(self.hass, UPDATE, UPDATE_INSTALL, self._wrap_update_entity)
        )
        self._update_wrapped = True

    # WebSocket commands.

    def _wrap_api(self, original: WebSocketCommandHandler) -> WebSocketCommandHandler:
        def handler(
            hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
        ) -> None:
            op = self.guard.call(self._api_op, connection, msg)
            if op is None:
                return original(hass, connection, msg)
            return self._dispatch_ws(op, original, hass, connection, msg)

        return handler

    def _wrap_update_addon(
        self, original: WebSocketCommandHandler
    ) -> WebSocketCommandHandler:
        def handler(
            hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
        ) -> None:
            op = self.guard.call(self._update_addon_op, connection, msg)
            if op is None:
                return original(hass, connection, msg)
            return self._dispatch_ws(op, original, hass, connection, msg)

        return handler

    @staticmethod
    def _is_admin(connection: ActiveConnection) -> bool:
        # Core refuses the others; nothing to read or record for them.
        user = connection.user
        return user is not None and user.is_admin

    def _api_op(
        self, connection: ActiveConnection, msg: dict[str, Any]
    ) -> AppOp | None:
        if not self._is_admin(connection):
            return None
        data = msg.get("data")
        return classify(
            msg["endpoint"], msg["method"], data if isinstance(data, dict) else None
        )

    def _update_addon_op(
        self, connection: ActiveConnection, msg: dict[str, Any]
    ) -> AppOp | None:
        if not self._is_admin(connection):
            return None
        return AppOp(ChangeAction.UPDATED, str(msg["addon"]), None, ())

    def _dispatch_ws(
        self,
        op: AppOp,
        original: WebSocketCommandHandler,
        hass: HomeAssistant,
        connection: ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        """Call Core's handler with a result watch, reading first if needed."""
        actor = self.guard.call(ws_actor, connection)
        if actor is None:
            return original(hass, connection, msg)
        if not self._needs_before(op):
            target, finish = self._watched(op, actor, _Before(), connection, msg)
            try:
                return original(hass, target, msg)
            except Exception:
                self._finish(finish, success=False)
                raise
        task = self._read_then_call(op, actor, original, hass, connection, msg)
        try:
            hass.async_create_task(task, "electric_fence app change", eager_start=True)
        except Exception as err:  # noqa: BLE001
            task.close()
            self.guard.call(reraise, err)
            return original(hass, connection, msg)
        return None

    async def _read_then_call(
        self,
        op: AppOp,
        actor: Actor,
        original: WebSocketCommandHandler,
        hass: HomeAssistant,
        connection: ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        before = _Before()
        try:
            before = await self._read_before(op)
        finally:
            # Core's handler runs whatever the read did, and its errors are
            # answered as Core's dispatcher would.
            target, _finish = self._watched(op, actor, before, connection, msg)
            try:
                original(hass, target, msg)
            except Exception as err:  # noqa: BLE001
                target.async_handle_exception(msg, err)

    def _watched(
        self,
        op: AppOp,
        actor: Actor,
        before: _Before,
        connection: ActiveConnection,
        msg: dict[str, Any],
    ) -> tuple[ActiveConnection, _Finish | None]:
        """Return the connection to give Core's handler (a watch, if possible).

        Marks the call in flight; the watch ends the mark with Core's answer.
        """
        finish = self.guard.call(self._begin, op)

        def on_success() -> None:
            self._finish(finish, success=True)
            self._schedule_after(op, actor, before)

        def on_failure() -> None:
            self._finish(finish, success=False)

        watch = self.guard.call(
            ResultWatch, connection, msg["id"], on_success, self.guard, on_failure
        )
        if watch is None:
            # Core's answer can't be seen: nothing is recorded, nothing kept.
            self._finish(finish, success=False)
            return connection, None
        return cast(ActiveConnection, watch), finish

    def _begin(self, op: AppOp) -> _Finish | None:
        """Mark an app change in flight on the outside watcher.

        Returns the function that ends the mark (on that same watcher).
        """
        watcher = self.runtime.app_watcher
        if watcher is None or op.slug is None:
            return None
        mark = watcher.begin_core_change(op.slug, op.action)

        def finish(success: bool) -> None:
            watcher.end_core_change(mark, success=success)

        return finish

    def _finish(self, finish: _Finish | None, *, success: bool) -> None:
        if finish is not None:
            self.guard.call(finish, success)

    # Services.

    def _wrap_app_service(
        self, key: str, action: ChangeAction
    ) -> Callable[[ServiceHandler], ServiceHandler]:
        def make(original: ServiceHandler) -> ServiceHandler:
            async def handler(call: ServiceCall) -> Any:
                pending = self.guard.call(self._service_op, call, key, action)
                finish = (
                    None
                    if pending is None
                    else self.guard.call(self._begin, pending[0])
                )
                done = False
                try:
                    result = await original(call)
                    done = True
                finally:
                    self._finish(finish, success=done)
                if pending is not None:
                    self.guard.call(self._schedule_after, *pending, _Before())
                return result

            return handler

        return make

    def _service_op(
        self, call: ServiceCall, key: str, action: ChangeAction
    ) -> tuple[AppOp, Actor] | None:
        slug = call.data.get(key)
        if not isinstance(slug, str):
            return None
        return AppOp(action, slug, None, ()), call_actor(self.hass, call)

    def _wrap_update_entity(self, original: ServiceHandler) -> ServiceHandler:
        async def handler(call: ServiceCall) -> Any:
            pending: list[tuple[AppOp, Actor, _Before]] = []
            try:
                pending = await self._update_entity_before(call)
            except Exception as err:  # noqa: BLE001
                self.guard.call(reraise, err)
            finishes = [self.guard.call(self._begin, op) for op, _, _ in pending]
            done = False
            try:
                result = await original(call)
                done = True
            finally:
                for finish in finishes:
                    self._finish(finish, success=done)
            for op, actor, before in pending:
                self.guard.call(self._schedule_after, op, actor, before)
            return result

        return handler

    async def _update_entity_before(
        self, call: ServiceCall
    ) -> list[tuple[AppOp, Actor, _Before]]:
        """Read the versions of the apps whose update entities are targeted."""
        slugs = self._update_entity_slugs(
            call,
            set()
            if call.data.get(ATTR_ENTITY_ID) == ENTITY_MATCH_ALL
            else await async_extract_entity_ids(call),
        )
        if not slugs:
            return []
        actor = call_actor(self.hass, call)
        ops = [AppOp(ChangeAction.UPDATED, slug, None, ()) for slug in slugs]
        reads = [
            self.hass.async_create_task(
                self._read_before(op), "electric_fence app read", eager_start=True
            )
            for op in ops
        ]
        # One timeout for all: the user's update waits at most that long.
        _done, slow = await asyncio.wait(reads, timeout=READ_TIMEOUT)
        for task in slow:
            task.cancel()
        return [
            (op, actor, _Before() if read in slow else read.result())
            for op, read in zip(ops, reads, strict=True)
        ]

    def _update_entity_slugs(
        self, call: ServiceCall, entity_ids: set[str]
    ) -> list[str]:
        registry = er.async_get(self.hass)
        every = call.data.get(ATTR_ENTITY_ID) == ENTITY_MATCH_ALL
        entries = (
            [e for e in registry.entities.values() if e.domain == UPDATE]
            if every
            else [e for i in sorted(entity_ids) if (e := registry.async_get(i))]
        )
        # Core installs only on loaded, available entities with an update
        # (`on`); it skips the others and still succeeds.
        return [
            slug
            for entry in entries
            if entry.platform == HASSIO
            and entry.disabled_by is None
            and (state := self.hass.states.get(entry.entity_id)) is not None
            and state.state == STATE_ON
            and (slug := app_slug_from_unique_id(entry.unique_id)) is not None
        ]

    # Core's AddonManager.

    def _wrap_addon_manager(self) -> None:
        from homeassistant.components.hassio.addon_manager import AddonManager  # noqa: PLC0415

        # On the class: every integration's manager (and subclass) shares
        # these methods. Restored on uninstall like any other hook.
        for method, action in MANAGER_METHODS.items():
            self._handles.append(
                wrap_async_method(
                    AddonManager, method, self._wrap_manager_method(action)
                )
            )

    def _wrap_manager_method(
        self, action: ChangeAction
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def make(original: Callable[..., Any]) -> Callable[..., Any]:
            async def method(manager: Any, *args: Any, **kwargs: Any) -> Any:
                found = self.guard.call(self._manager_op, manager, action)
                if found is None:
                    return await original(manager, *args, **kwargs)
                op, actor = found
                before = (
                    await self._read_before(op) if self._needs_before(op) else _Before()
                )
                finish = self.guard.call(self._begin, op)
                done = False
                try:
                    result = await original(manager, *args, **kwargs)
                    done = True
                finally:
                    self._finish(finish, success=done)
                self.guard.call(self._schedule_after, op, actor, before)
                return result

            return method

        return make

    @staticmethod
    def _manager_op(manager: Any, action: ChangeAction) -> tuple[AppOp, Actor] | None:
        slug = getattr(manager, "addon_slug", None)
        if not isinstance(slug, str):
            return None
        keys = (OPTIONS,) if action == ChangeAction.OPTIONS_CHANGED else ()
        scope = credited_actor(ChangeKind.APP, action, slug)
        actor = (
            Actor(user_id=None, user_name=None, ip=None)
            if scope is None
            else Actor(user_id=scope.user_id, user_name=scope.user_name, ip=scope.ip)
        )
        return AppOp(action, slug, None, keys), actor

    # Reading Supervisor and recording.

    @staticmethod
    def _needs_before(op: AppOp) -> bool:
        return (
            op.action in _READ_BEFORE
            or op.action == ChangeAction.REPOSITORY_REMOVED
            or (op.action == ChangeAction.REPOSITORY_ADDED and op.repository is None)
        )

    def _client(self) -> Any:
        from homeassistant.components import hassio  # noqa: PLC0415

        return hassio.get_supervisor_client(self.hass)

    async def _info(self, slug: str | None) -> Any:
        """Return the app's info from Supervisor, or None."""
        if slug is None:
            return None
        try:
            async with asyncio.timeout(READ_TIMEOUT):
                return await self._client().addons.addon_info(slug)
        except Exception:  # noqa: BLE001
            # Not installed (before an install), or Supervisor can't answer.
            return None

    async def _repositories(self) -> dict[str, str] | None:
        """Return the store's repositories, slug to URL, or None."""
        try:
            async with asyncio.timeout(READ_TIMEOUT):
                found = await self._client().store.repositories_list()
            return {str(repo.slug): str(repo.url) for repo in found}
        except Exception:  # noqa: BLE001
            return None

    async def _read_before(self, op: AppOp) -> _Before:
        """Read what the details need from before the call. Never raises."""
        before = _Before()
        try:
            if op.action in _READ_BEFORE:
                before.info = await self._info(op.slug)
            elif op.action in _REPOSITORY_ACTIONS:
                before.repositories = await self._repositories()
        except Exception as err:  # noqa: BLE001
            self.guard.call(reraise, err)
        return before

    def _schedule_after(self, op: AppOp, actor: Actor, before: _Before) -> None:
        """Record in a task, so Core's answer isn't held up by our reads."""
        self.hass.async_create_task(
            self._after(op, actor, before),
            "electric_fence app change",
            eager_start=True,
        )

    async def _after(self, op: AppOp, actor: Actor, before: _Before) -> None:
        try:
            # A background job may not be done: its version isn't known yet.
            reads_after = op.action in _READ_AFTER and not op.background
            after = await self._info(op.slug) if reads_after else None
            repositories = (
                await self._repositories()
                if op.action == ChangeAction.REPOSITORY_ADDED and op.repository is None
                else None
            )
            self._record(op, actor, before, after, repositories)
        except Exception as err:  # noqa: BLE001
            self.guard.call(reraise, err)

    def _record(
        self,
        op: AppOp,
        actor: Actor,
        before: _Before,
        after: Any,
        repositories_after: dict[str, str] | None,
    ) -> None:
        action = op.action
        details: dict[str, Any] = {}
        if action in _REPOSITORY_ACTIONS:
            url = self._repository_url(op, before.repositories, repositories_after)
            target_id = target_name = _safe_url(url)
            details = {"repository": target_id}
        else:
            target_id = op.slug
            known = after if after is not None else before.info
            target_name = getattr(known, "name", None) or op.slug
            if action in _VERSIONS:
                version_before = getattr(before.info, "version", None)
                if op.background:
                    details = {"version_before": version_before, _BACKGROUND: True}
                else:
                    version_after = getattr(after, "version", None)
                    if (
                        action == ChangeAction.UPDATED
                        and version_before is not None
                        and version_before == version_after
                    ):
                        # Supervisor answered, but nothing was installed.
                        return
                    details = {
                        "version_before": version_before,
                        "version_after": version_after,
                    }
            elif action in _CONFIG:
                found = self._config_details(op, before.info, after)
                if found is None:
                    return
                action, details = found
        ts = dt_util.utcnow()
        self.runtime.worker.submit(
            SettingsChange(
                ts=ts,
                kind=ChangeKind.APP,
                action=action,
                target_id=target_id,
                target_name=target_name,
                details=details,
                actor_user_id=actor.user_id,
                actor_name=actor.user_name,
                actor_ip=actor.ip,
            )
        )
        if (watcher := self.runtime.app_watcher) is not None:
            # So the outside watcher doesn't record this change again.
            self.guard.call(
                watcher.note_core_change,
                target_id,
                action,
                ts,
                pending=op.background,
            )

    def _config_details(
        self, op: AppOp, before: Any, after: Any
    ) -> tuple[ChangeAction, dict[str, Any]] | None:
        """Return the action and details of an options or settings change.

        None when Supervisor's state shows nothing changed. Without both
        states, the request's fields are recorded, never values.
        """
        if before is None or after is None:
            return op.action, {"changed": sorted(op.keys)}
        keys = _state_keys(op)
        old, new = _snapshot(before, keys), _snapshot(after, keys)
        details = diff_details(old, new, include_values=False)
        changed = details["changed"]
        if changed and self.runtime.settings.record_values:
            secret = _password_fields(getattr(after, "schema", None)) | (
                _password_fields(getattr(before, "schema", None))
            )
            shown = diff_details(
                _mask_options(old, secret),
                _mask_options(new, secret),
                include_values=True,
            ).get("values", {})
            # A change only in a masked password shows as hidden.
            details["values"] = {
                name: shown.get(name, dict(HIDDEN)) for name in changed
            }
        if not changed:
            return None
        if op.keys:
            return op.action, details
        # No request body: options changed, or else only settings did.
        if OPTIONS in changed:
            return ChangeAction.OPTIONS_CHANGED, details
        return ChangeAction.SETTINGS_CHANGED, details

    @staticmethod
    def _repository_url(
        op: AppOp,
        before: dict[str, str] | None,
        after: dict[str, str] | None,
    ) -> str | None:
        if op.action == ChangeAction.REPOSITORY_REMOVED:
            slug = op.repository
            if before is not None and slug is not None and slug in before:
                return before[slug]
            return slug
        if op.repository is not None:
            return op.repository
        if before is None or after is None:
            return None
        added = sorted(set(after.values()) - set(before.values()))
        return added[0] if len(added) == 1 else None
