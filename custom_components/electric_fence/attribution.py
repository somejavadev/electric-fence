"""Strict attribution (H9): action scopes around direct admin actions.

A change is credited to a person only while that person's own admin action
runs, and only for the change that action implies (`credited_actor`).
Everything else is System. Wrappers open a scope around the admin entry
points: the config, options and subentry flow managers, entry delete, and
the admin WebSocket commands (`ADMIN_WS_COMMANDS`, plus a user renaming
themselves with `person/update_own_profile`). Each command is wrapped once
the component that registers it (`config` or `person`) is loaded.

IP: HTTP scopes use `request.remote`. WebSocket scopes use
`ActiveConnection.remote`, which Core copies from the upgrade request's
`remote` at auth (after its forwarded-header handling, so it is the same
address as for HTTP); `current_request.remote` is the fallback.

Safety (spec §4): every wrapper calls Core's original and returns its result,
or lets its exception propagate, unchanged. Reading the actor and targets and
opening or closing the scope run inside `self.guard`; when any of that fails,
the call goes straight through with no scope.
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.auth.models import User
from homeassistant.auth.providers.homeassistant import HassAuthProvider
from homeassistant.components.http.const import KEY_HASS_USER
from homeassistant.components.websocket_api import ActiveConnection
from homeassistant.components.websocket_api.const import (
    DOMAIN as WEBSOCKET_API_DOMAIN,
    WebSocketCommandHandler,
)
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.const import EVENT_COMPONENT_LOADED
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.data_entry_flow import FlowManager, UnknownFlow
from homeassistant.helpers.http import current_request
from homeassistant.setup import ATTR_COMPONENT, EventComponentLoaded

from .collectors.base import BaseCollector
from .contracts import CONTRACTS
from .hooks import wrap_async_method, wrap_ws_command
from .models import ChangeAction, ChangeKind
from .repairs import async_report_disabled_collector

if TYPE_CHECKING:
    from .runtime import Runtime

ACTION_CONFIG_FLOW = "config_flow"
ACTION_OPTIONS_FLOW = "options_flow"
ACTION_SUBENTRY_FLOW = "subentry_flow"
ACTION_ENTRY_DELETE = "entry_delete"
ACTION_WS = "ws"

# aiohttp runs each view in a task with this coroutine (R10). Tasks spawned
# inside a view inherit `current_request` but run another coroutine.
HANDLE_REQUEST_QUALNAME = "RequestHandler._handle_request"
# `websocket_api.async_response` names its handler tasks with this prefix.
WS_TASK_PREFIX = "websocket_api.async:"
# The entry delete view runs `async_remove` in a task of this name.
_REMOVE_TASK_NAME = "config entry remove {entry_id}"
_ENTRY_PATH = "/api/config/config_entries/entry/{entry_id}"
_CONFIG = "config"
_PERSON = "person"

_CMD_CHANGE_PASSWORD = "config/auth_provider/homeassistant/change_password"
_CMD_HA_DELETE = "config/auth_provider/homeassistant/delete"
# Not admin only: renames the calling user (and their person).
_CMD_OWN_PROFILE = "person/update_own_profile"

ADMIN_WS_COMMANDS: tuple[str, ...] = (
    "config_entries/update",
    "config_entries/disable",
    "config_entries/subentries/update",
    "config_entries/subentries/delete",
    "config/auth/create",
    "config/auth/update",
    "config/auth/delete",
    "config/auth_provider/homeassistant/create",
    "config/auth_provider/homeassistant/delete",
    "config/auth_provider/homeassistant/change_password",
    "config/auth_provider/homeassistant/admin_change_password",
    "config/auth_provider/homeassistant/admin_change_username",
    "config/core/update",
    _CMD_OWN_PROFILE,
)


def command_component(command: str) -> str:
    """Return the component that registers a wrapped WebSocket command."""
    return _PERSON if command.startswith(f"{_PERSON}/") else _CONFIG


type _AsyncFn = Callable[..., Awaitable[Any]]


@dataclass(slots=True)
class ActionScope:
    """A running admin action, and who runs it."""

    user_id: str
    user_name: str | None
    ip: str | None
    action: str
    details: dict[str, Any]
    owner: asyncio.Task[Any] | None
    active: bool = True

    def close(self) -> None:
        """End the action: nothing is credited to it from now on."""
        self.active = False


SCOPE: ContextVar[ActionScope | None] = ContextVar("electric_fence_scope", default=None)


def current_actor() -> ActionScope | None:
    """Return the active scope, if seen from the task that owns it."""
    scope = SCOPE.get()
    if (
        scope is not None
        and scope.active
        and (scope.owner is None or scope.owner is asyncio.current_task())
    ):
        return scope
    return None


@contextmanager
def open_scope(
    *,
    user: User,
    ip: str | None,
    action: str,
    details: dict[str, Any],
    owner: asyncio.Task[Any] | None,
) -> Iterator[ActionScope]:
    """Set an action scope for the duration of the block."""
    scope = ActionScope(
        user_id=user.id,
        user_name=user.name,
        ip=ip,
        action=action,
        details=details,
        owner=owner,
    )
    token = SCOPE.set(scope)
    try:
        yield scope
    finally:
        try:
            scope.close()
        finally:
            SCOPE.reset(token)


def _coro_qualname(task: asyncio.Task[Any] | None) -> str | None:
    return getattr(task.get_coro(), "__qualname__", None) if task else None


def _request_user() -> tuple[Any, User] | None:
    """Return the current request and its authenticated user."""
    request = current_request.get()
    if request is None:
        return None
    user = request.get(KEY_HASS_USER)
    if not isinstance(user, User):
        return None
    return request, user


def direct_request_actor() -> tuple[User, str | None] | None:
    """Return the user and IP of the view running in this very task."""
    found = _request_user()
    if found is None:
        return None
    if _coro_qualname(asyncio.current_task()) != HANDLE_REQUEST_QUALNAME:
        return None
    request, user = found
    return user, request.remote


def _delete_request_actor(entry_id: str) -> tuple[User, str | None] | None:
    """Return the user and IP of the delete view removing `entry_id`.

    The view removes in its own named task (contract test), so that task is
    accepted besides the view's own `_handle_request` task.
    """
    found = _request_user()
    if found is None:
        return None
    request, user = found
    if request.method != "DELETE" or request.path != _ENTRY_PATH.format(
        entry_id=entry_id
    ):
        return None
    task = asyncio.current_task()
    in_view = _coro_qualname(task) == HANDLE_REQUEST_QUALNAME
    in_remove_task = task is not None and task.get_name() == (
        _REMOVE_TASK_NAME.format(entry_id=entry_id)
    )
    if not (in_view or in_remove_task):
        return None
    return user, request.remote


# Crediting (R22).


@dataclass(slots=True, frozen=True)
class _Rule:
    """The change an action implies."""

    kind: ChangeKind
    changes: frozenset[ChangeAction]
    # Details key the target must equal; None credits any target.
    target: str | None


_ENTRY_RULE = _Rule(
    ChangeKind.CONFIG_ENTRY,
    frozenset({ChangeAction.UPDATED, ChangeAction.ENABLED, ChangeAction.DISABLED}),
    "entry_id",
)
_USER_UPDATED_RULE = _Rule(
    ChangeKind.USER, frozenset({ChangeAction.UPDATED}), "user_id"
)
_PASSWORD_RULE = _Rule(
    ChangeKind.USER, frozenset({ChangeAction.PASSWORD_CHANGED}), "user_id"
)

_WS_RULES: dict[str, _Rule] = {
    "config_entries/update": _ENTRY_RULE,
    "config_entries/disable": _ENTRY_RULE,
    "config_entries/subentries/update": _ENTRY_RULE,
    "config_entries/subentries/delete": _ENTRY_RULE,
    "config/auth/create": _Rule(ChangeKind.USER, frozenset({ChangeAction.ADDED}), None),
    "config/auth/update": _USER_UPDATED_RULE,
    "config/auth_provider/homeassistant/admin_change_username": _USER_UPDATED_RULE,
    "config/auth_provider/homeassistant/create": _USER_UPDATED_RULE,
    _CMD_HA_DELETE: _USER_UPDATED_RULE,
    "config/auth/delete": _Rule(
        ChangeKind.USER, frozenset({ChangeAction.REMOVED}), "user_id"
    ),
    _CMD_CHANGE_PASSWORD: _PASSWORD_RULE,
    _CMD_OWN_PROFILE: _USER_UPDATED_RULE,
    "config/auth_provider/homeassistant/admin_change_password": _PASSWORD_RULE,
    "config/core/update": _Rule(
        ChangeKind.CORE_CONFIG, frozenset({ChangeAction.UPDATED}), None
    ),
}

_FLOW_RULES: dict[str, _Rule] = {
    ACTION_OPTIONS_FLOW: _Rule(
        ChangeKind.CONFIG_ENTRY,
        frozenset({ChangeAction.UPDATED, ChangeAction.OPTIONS_CHANGED}),
        "entry_id",
    ),
    ACTION_SUBENTRY_FLOW: _Rule(
        ChangeKind.CONFIG_ENTRY, frozenset({ChangeAction.UPDATED}), "entry_id"
    ),
    ACTION_ENTRY_DELETE: _Rule(
        ChangeKind.CONFIG_ENTRY, frozenset({ChangeAction.REMOVED}), "entry_id"
    ),
}
_RECONFIGURE_RULE = _Rule(
    ChangeKind.CONFIG_ENTRY,
    frozenset({ChangeAction.UPDATED, ChangeAction.RECONFIGURED}),
    "entry_id",
)


def _same(value: str | None, expected: Any) -> bool:
    return value is not None and value == expected


def _first(details: dict[str, Any]) -> bool:
    """Credit only once per scope (R25).

    A scope has one kind and one target, so this is once per (kind, target).
    Core dispatches the admin's own change before any follow-up change the
    handler makes itself (an inline setup or reload). It does not always
    dispatch it first: update listeners start as eager tasks before Core
    dispatches the change (`_async_save_and_notify`), so a listener's reload,
    up to the integration's own setup-time update, can be dispatched
    earlier. H4 never asks for credit for those: state changes are skipped
    (R26), and changes dispatched while the admin's change is still pending
    are recorded as System (see `collectors/config_entries.py`).
    """
    if details.get("credited"):
        return False
    details["credited"] = True
    return True


def _rule_for(scope: ActionScope) -> _Rule | None:
    details = scope.details
    if scope.action == ACTION_WS:
        command = details.get("command")
        return _WS_RULES.get(command) if isinstance(command, str) else None
    if scope.action == ACTION_CONFIG_FLOW and details.get("source") == (
        SOURCE_RECONFIGURE
    ):
        return _RECONFIGURE_RULE
    return _FLOW_RULES.get(scope.action)


def _implies(
    scope: ActionScope,
    kind: ChangeKind,
    change: ChangeAction,
    target_id: str | None,
    domain: str | None,
) -> bool:
    details = scope.details
    if scope.action == ACTION_CONFIG_FLOW and details.get("source") != (
        SOURCE_RECONFIGURE
    ):
        # A new entry of the flow's own domain; only the first one.
        return (
            kind == ChangeKind.CONFIG_ENTRY
            and change == ChangeAction.ADDED
            and _same(domain, details.get("handler"))
            and _first(details)
        )
    rule = _rule_for(scope)
    if rule is None or kind != rule.kind or change not in rule.changes:
        return False
    if rule.target is not None and not _same(target_id, details.get(rule.target)):
        return False
    return _first(details)


def credited_actor(
    kind: ChangeKind,
    change: ChangeAction,
    target_id: str | None,
    *,
    domain: str | None = None,
) -> ActionScope | None:
    """Return the current scope only if its action implies this change.

    A WebSocket scope credits only inside its command's handler task (R29).
    The first matching change uses up the scope's credit (R25): later ones,
    such as an integration's own update while it is set up again inline, are
    System. So call this exactly once per change, and only for a real change
    (saved fields changed), never for state-only updates.
    """
    scope = current_actor()
    if (
        scope is None
        or (scope.owner is None and not _in_ws_handler_task(scope))
        or not _implies(scope, kind, change, target_id, domain)
    ):
        return None
    return scope


def _in_ws_handler_task(scope: ActionScope) -> bool:
    """Return True in the `async_response` task that runs the scope's command.

    A WebSocket scope has no owner (`_ws_enter`), so tasks that inherit its
    context, such as an update listener's reload, would see it too (R29).
    """
    task = asyncio.current_task()
    return (
        task is not None
        and task.get_name().startswith(WS_TASK_PREFIX)
        and task.get_context().get(SCOPE) is scope
    )


# Targets captured when a scope opens.


def _arg(args: tuple[Any, ...], kwargs: dict[str, Any], i: int, name: str) -> Any:
    """Return a call argument by position or keyword, else None."""
    return args[i] if len(args) > i else kwargs.get(name)


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _flow_details(
    action: str, handler: Any, context: Any, *, init: bool
) -> dict[str, Any] | None:
    """Return the scope details of a flow step, or None for no scope.

    On init, only user and reconfigure flows (what the views start) get a
    scope; a flow started by other code during a request stays System.
    """
    if action == ACTION_CONFIG_FLOW:
        source = context.get("source") if isinstance(context, Mapping) else None
        if source is None or (init and source not in (SOURCE_USER, SOURCE_RECONFIGURE)):
            return None
        if source == SOURCE_RECONFIGURE:
            return {"source": source, "entry_id": _str(context.get("entry_id"))}
        return {"source": source, "handler": _str(handler)}
    if action == ACTION_OPTIONS_FLOW:
        # The options flow handler is the entry ID.
        return {"entry_id": _str(handler)}
    if action == ACTION_SUBENTRY_FLOW:
        # The subentry flow handler is `(entry_id, subentry_type)`.
        is_pair = isinstance(handler, (tuple, list)) and len(handler) == 2
        entry_id = handler[0] if is_pair else None
        return {"entry_id": _str(entry_id)}
    return None


def _ws_details(
    command: str, connection: ActiveConnection, msg: dict[str, Any]
) -> dict[str, Any]:
    """Return the scope details of a WebSocket command (R22)."""
    details: dict[str, Any] = {"command": command}
    rule = _WS_RULES.get(command)
    if rule is None or rule.target is None:
        return details
    if command in (_CMD_CHANGE_PASSWORD, _CMD_OWN_PROFILE):
        # The connection's own password, or its own name.
        details["user_id"] = connection.user.id
    elif command == _CMD_HA_DELETE:
        # The message names the username only (R24).
        details["user_id"] = hass_user_id(connection.hass, msg.get("username"))
    else:
        details[rule.target] = _str(msg.get(rule.target))
    return details


def auth_users(hass: HomeAssistant) -> Mapping[str, User] | None:
    """Return the auth store's users by ID, from memory.

    The auth store's private `_users` (pinned by a contract test); None when
    it isn't a mapping.
    """
    users = getattr(getattr(hass.auth, "_store", None), "_users", None)
    return users if isinstance(users, Mapping) else None


def hass_user_id(hass: HomeAssistant, username: Any) -> str | None:
    """Return the ID of the user with this `homeassistant` provider username.

    Memory only: the provider's credentials on each user, normalised on both
    sides as the provider does. The user list is the auth store's private
    `_users` (pinned by a contract test); anything unexpected gives None.
    """
    if not isinstance(username, str):
        return None
    provider = next(
        (p for p in hass.auth.auth_providers if isinstance(p, HassAuthProvider)),
        None,
    )
    if provider is None or provider.data is None:
        return None
    data = provider.data
    users = auth_users(hass)
    if users is None:
        return None
    name = data.normalize_username(username)
    for user in users.values():
        found = provider.async_get_username(user)
        if found is not None and data.normalize_username(found) == name:
            return str(user.id)
    return None


def _ws_ip(connection: ActiveConnection) -> str | None:
    """Return the WebSocket client's IP."""
    remote = _str(getattr(connection, "remote", None))
    if remote is not None:
        return remote
    request = current_request.get()
    return _str(request.remote) if request is not None else None


def _ws_enter(
    command: str, connection: ActiveConnection, msg: dict[str, Any]
) -> tuple[ActionScope, Token[ActionScope | None]] | None:
    """Set the scope of a WebSocket command (owner None: see `_ws_exit`)."""
    user = connection.user
    if user is None:
        return None
    scope = ActionScope(
        user_id=user.id,
        user_name=user.name,
        ip=_ws_ip(connection),
        action=ACTION_WS,
        details=_ws_details(command, connection, msg),
        owner=None,
    )
    return scope, SCOPE.set(scope)


def _find_ws_task(scope: ActionScope) -> asyncio.Task[Any] | None:
    """Return the `async_response` task that carries `scope`, if still running."""
    for task in asyncio.all_tasks():
        if (
            task.get_name().startswith(WS_TASK_PREFIX)
            and task.get_context().get(SCOPE) is scope
        ):
            return task
    return None


class AttributionCollector(BaseCollector):
    """Open action scopes around direct admin actions."""

    name = "attribution"

    def __init__(self, hass: HomeAssistant, runtime: Runtime) -> None:
        """Initialise the collector."""
        super().__init__(hass, runtime)
        # Components whose commands wait for them to load, and the listener.
        self._waiting: set[str] = set()
        self._loaded_unsub: CALLBACK_TYPE | None = None
        self._wrapped: set[str] = set()

    def _install(self) -> None:
        entries = self.hass.config_entries
        managers: tuple[tuple[FlowManager[Any, Any, Any], str], ...] = (
            (entries.flow, ACTION_CONFIG_FLOW),
            (entries.options, ACTION_OPTIONS_FLOW),
            (entries.subentries, ACTION_SUBENTRY_FLOW),
        )
        for mgr, action in managers:
            self._handles.append(
                wrap_async_method(mgr, "async_init", self._wrap_init(action))
            )
            self._handles.append(
                wrap_async_method(
                    mgr, "async_configure", self._wrap_configure(mgr, action)
                )
            )
        self._handles.append(
            wrap_async_method(entries, "async_remove", self._wrap_remove)
        )
        # `config` and `person` register the commands, and may load after us.
        # The contract already passed for those that are loaded.
        self._wrap_ws_commands()
        self._waiting = {
            component
            for component in map(command_component, ADMIN_WS_COMMANDS)
            if component not in self.hass.config.components
        }
        if self._waiting:
            self._loaded_unsub = self.hass.bus.async_listen(
                EVENT_COMPONENT_LOADED,
                self._on_component_loaded,
                event_filter=self._is_waiting,
            )

    def _uninstall(self) -> None:
        self._waiting.clear()
        self._wrapped.clear()
        if self._loaded_unsub is not None:
            unsub, self._loaded_unsub = self._loaded_unsub, None
            unsub()

    @callback
    def _is_waiting(self, event_data: EventComponentLoaded) -> bool:
        return event_data[ATTR_COMPONENT] in self._waiting

    @callback
    def _on_component_loaded(self, event: Event[EventComponentLoaded]) -> None:
        """Wrap a component's WebSocket commands once it has registered them."""
        self._waiting.discard(event.data[ATTR_COMPONENT])
        if not self._waiting and self._loaded_unsub is not None:
            unsub, self._loaded_unsub = self._loaded_unsub, None
            unsub()
        if self._installed:
            self.guard.call(self._install_ws_late)

    def _install_ws_late(self) -> None:
        reason = CONTRACTS["admin_ws_commands"].check(self.hass)
        if reason is None:
            self._wrap_ws_commands()
            return
        # Same outcome as a contract failing at install.
        self.uninstall()
        self.disabled_reason = [reason]
        async_report_disabled_collector(self.hass, self.name, [reason])

    def _wrap_ws_commands(self) -> None:
        """Wrap the registered commands of loaded components, each once."""
        handlers = self.hass.data.get(WEBSOCKET_API_DOMAIN, {})
        loaded = self.hass.config.components
        for command in ADMIN_WS_COMMANDS:
            if (
                command not in self._wrapped
                and command_component(command) in loaded
                and command in handlers
            ):
                self._handles.append(
                    wrap_ws_command(self.hass, command, self._wrap_ws(command))
                )
                self._wrapped.add(command)

    # Wrappers. Core's call runs untouched; ours never raises.

    async def _call_in_scope(
        self,
        scope_cm: AbstractContextManager[ActionScope] | None,
        original: _AsyncFn,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        if scope_cm is not None and self.guard.call(scope_cm.__enter__) is None:
            # Not opened: nothing to close.
            scope_cm = None
        try:
            return await original(*args, **kwargs)
        finally:
            if scope_cm is not None:
                self.guard.call(scope_cm.__exit__, None, None, None)

    def _wrap_init(self, action: str) -> Callable[[_AsyncFn], _AsyncFn]:
        def make(original: _AsyncFn) -> _AsyncFn:
            async def async_init(*args: Any, **kwargs: Any) -> Any:
                scope_cm = self.guard.call(self._init_scope, action, args, kwargs)
                return await self._call_in_scope(scope_cm, original, args, kwargs)

            return async_init

        return make

    def _wrap_configure(
        self, mgr: FlowManager[Any, Any, Any], action: str
    ) -> Callable[[_AsyncFn], _AsyncFn]:
        def make(original: _AsyncFn) -> _AsyncFn:
            async def async_configure(*args: Any, **kwargs: Any) -> Any:
                scope_cm = self.guard.call(
                    self._configure_scope, mgr, action, args, kwargs
                )
                return await self._call_in_scope(scope_cm, original, args, kwargs)

            return async_configure

        return make

    def _wrap_remove(self, original: _AsyncFn) -> _AsyncFn:
        async def async_remove(*args: Any, **kwargs: Any) -> Any:
            scope_cm = self.guard.call(self._remove_scope, args, kwargs)
            return await self._call_in_scope(scope_cm, original, args, kwargs)

        return async_remove

    def _wrap_ws(
        self, command: str
    ) -> Callable[[WebSocketCommandHandler], WebSocketCommandHandler]:
        def make(original: WebSocketCommandHandler) -> WebSocketCommandHandler:
            def handler(
                hass: HomeAssistant,
                connection: ActiveConnection,
                msg: dict[str, Any],
            ) -> None:
                entered = self.guard.call(_ws_enter, command, connection, msg)
                try:
                    return original(hass, connection, msg)
                finally:
                    if entered is not None:
                        self.guard.call(self._ws_exit, *entered)

            return handler

        return make

    def _ws_exit(self, scope: ActionScope, token: Token[ActionScope | None]) -> None:
        """Reset our context; close the scope when the handler task is done.

        `async_response` runs the handler in its own task with a copy of our
        context, so the scope (owner None) lives on there until it finishes.
        """
        try:
            SCOPE.reset(token)
        finally:
            task = self.guard.call(_find_ws_task, scope)
            if task is None:
                # Not async, finished eagerly, or not found: done now.
                scope.close()
            else:
                task.add_done_callback(lambda _task: self.guard.call(scope.close))

    # Scope openers (inside the guard).

    def _scope(
        self, actor: tuple[User, str | None] | None, action: str, details: Any
    ) -> AbstractContextManager[ActionScope] | None:
        if actor is None or details is None:
            return None
        user, ip = actor
        return open_scope(
            user=user,
            ip=ip,
            action=action,
            details=details,
            owner=asyncio.current_task(),
        )

    def _init_scope(
        self, action: str, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> AbstractContextManager[ActionScope] | None:
        """Scope for `async_init(handler, *, context=None, data=None)`."""
        actor = direct_request_actor()
        if actor is None:
            return None
        details = _flow_details(
            action, _arg(args, kwargs, 0, "handler"), kwargs.get("context"), init=True
        )
        return self._scope(actor, action, details)

    def _configure_scope(
        self,
        mgr: FlowManager[Any, Any, Any],
        action: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> AbstractContextManager[ActionScope] | None:
        """Scope for `async_configure(flow_id, user_input=None)`."""
        actor = direct_request_actor()
        flow_id = _arg(args, kwargs, 0, "flow_id")
        if actor is None or not isinstance(flow_id, str):
            return None
        try:
            flow = mgr.async_get(flow_id)
        except UnknownFlow, IndexError:
            # Core raises it to the caller in a moment.
            return None
        details = _flow_details(
            action, flow.get("handler"), flow.get("context"), init=False
        )
        return self._scope(actor, action, details)

    def _remove_scope(
        self, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> AbstractContextManager[ActionScope] | None:
        """Scope for `async_remove(entry_id)` called by the delete view."""
        entry_id = _arg(args, kwargs, 0, "entry_id")
        if not isinstance(entry_id, str):
            return None
        return self._scope(
            _delete_request_actor(entry_id),
            ACTION_ENTRY_DELETE,
            {"entry_id": entry_id},
        )
