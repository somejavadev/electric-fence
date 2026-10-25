"""Tests for strict attribution (H9): action scopes around admin actions."""

# ruff: noqa: SLF001

import asyncio
from collections.abc import Iterator
import contextvars
from datetime import timedelta
from http import HTTPStatus
from typing import Any
from unittest.mock import Mock

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockUser,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.typing import (
    ClientSessionGenerator,
    WebSocketGenerator,
)

from custom_components.electric_fence import COLLECTOR_TYPES, attribution
from custom_components.electric_fence.attribution import (
    ACTION_CONFIG_FLOW,
    ACTION_ENTRY_DELETE,
    ACTION_OPTIONS_FLOW,
    ACTION_SUBENTRY_FLOW,
    ACTION_WS,
    ADMIN_WS_COMMANDS,
    HANDLE_REQUEST_QUALNAME,
    SCOPE,
    WS_TASK_PREFIX,
    ActionScope,
    AttributionCollector,
    credited_actor,
    current_actor,
    direct_request_actor,
    open_scope,
)
from custom_components.electric_fence.collectors.config_entries import (
    ConfigEntryCollector,
)
from custom_components.electric_fence.collectors.core_config import CoreConfigCollector
from custom_components.electric_fence.collectors.users import UserCollector
from custom_components.electric_fence.const import DOMAIN
from custom_components.electric_fence.models import ChangeAction, ChangeKind
from homeassistant import config_entries
from homeassistant.auth import EVENT_USER_ADDED
from homeassistant.auth.models import User
from homeassistant.components import person
from homeassistant.components.config import core as config_core
from homeassistant.components.websocket_api import DOMAIN as WEBSOCKET_API_DOMAIN
from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED,
    ConfigEntry,
    ConfigEntryChange,
)
from homeassistant.const import EVENT_COMPONENT_LOADED, EVENT_CORE_CONFIG_UPDATE
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.http import current_request
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from tests.helpers import DUMMY, PROBE, create_hass_user, dummy_entry, mock_dummy

_CHANGE = {
    ConfigEntryChange.ADDED: ChangeAction.ADDED,
    ConfigEntryChange.UPDATED: ChangeAction.UPDATED,
    ConfigEntryChange.REMOVED: ChangeAction.REMOVED,
}


@pytest.fixture(autouse=True)
def _only_attribution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set up without the settings collectors.

    These tests call `credited_actor` themselves, and a scope credits one
    change only (R25), so the collectors would use the credit up first.
    """
    monkeypatch.setattr(
        "custom_components.electric_fence.COLLECTOR_TYPES",
        [
            c
            for c in COLLECTOR_TYPES
            if c not in (ConfigEntryCollector, UserCollector, CoreConfigCollector)
        ],
    )


@pytest.fixture
async def fence(
    hass: HomeAssistant, dummy: None, config: None, setup_entry: MockConfigEntry
) -> AttributionCollector:
    """Return the installed attribution collector."""
    return _collector(hass)


def _collector(hass: HomeAssistant) -> AttributionCollector:
    entry = hass.config_entries.async_loaded_entries(DOMAIN)[0]
    return next(
        c for c in entry.runtime_data.collectors if isinstance(c, AttributionCollector)
    )


type _EntrySeen = tuple[
    ConfigEntryChange, ConfigEntry, ActionScope | None, ActionScope | None
]


OWNER_QUALNAMES: dict[int, str | None] = {}


def _qualname(task: asyncio.Task[Any] | None) -> str | None:
    assert task is not None
    return getattr(task.get_coro(), "__qualname__", None)


def _saved(entry: ConfigEntry) -> tuple[Any, ...]:
    """Return the saved fields of an entry (state changes leave them alone)."""
    return (
        entry.title,
        dict(entry.data),
        dict(entry.options),
        entry.disabled_by,
        entry.pref_disable_polling,
        entry.pref_disable_new_entities,
        tuple(entry.subentries),
    )


class _Seen(list[_EntrySeen]):
    """Seen changes, plus each entry's data at the time of the change."""

    def __init__(self) -> None:
        super().__init__()
        self.data: list[dict[str, Any]] = []


def _watch_entries(hass: HomeAssistant, *, dedupe: bool = True) -> _Seen:
    """Record `(change, entry, credited, current)` for each real entry change.

    Like the Task 10 listener, `credited_actor` is called once per real
    change only: UPDATEDs that leave the saved fields alone are skipped.
    Without `dedupe`, every dispatch is recorded.
    """
    seen = _Seen()
    saved = {e.entry_id: _saved(e) for e in hass.config_entries.async_entries()}

    @callback
    def _on_change(change: ConfigEntryChange, entry: ConfigEntry) -> None:
        if entry.domain != DUMMY:
            return
        now = _saved(entry)
        if (
            dedupe
            and change is ConfigEntryChange.UPDATED
            and saved.get(entry.entry_id) == now
        ):
            return
        saved[entry.entry_id] = now
        credited = credited_actor(
            ChangeKind.CONFIG_ENTRY, _CHANGE[change], entry.entry_id, domain=DUMMY
        )
        seen.append((change, entry, credited, current_actor()))
        seen.data.append(dict(entry.data))
        if credited is not None:
            # A finished task drops its coroutine: look while it runs.
            OWNER_QUALNAMES[id(credited)] = _qualname(asyncio.current_task())

    async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, _on_change)
    return seen


def _credited(seen: list[_EntrySeen], change: ConfigEntryChange) -> list[ActionScope]:
    return [c for ch, _, c, _ in seen if ch is change and c is not None]


def _assert_request_scope(scope: ActionScope, user: User, action: str) -> None:
    assert scope.user_id == user.id
    assert scope.user_name == user.name
    assert scope.ip == "127.0.0.1"
    assert scope.action == action
    assert scope.active is False
    # The flow wrapper's owner is the view's own aiohttp task.
    assert scope.owner is not None
    assert OWNER_QUALNAMES[id(scope)] == HANDLE_REQUEST_QUALNAME


def test_collector_registered() -> None:
    """The attribution collector is installed by setup."""
    assert AttributionCollector in COLLECTOR_TYPES
    assert AttributionCollector.name == "attribution"


@pytest.mark.parametrize("domain_loaded", [False, True])
async def test_scope_active_for_http_config_flow(
    hass: HomeAssistant,
    fence: AttributionCollector,
    hass_client: ClientSessionGenerator,
    hass_admin_user: MockUser,
    domain_loaded: bool,
) -> None:
    """The entry a config flow adds is credited to the admin."""
    if domain_loaded:
        assert await async_setup_component(hass, DUMMY, {})
    seen = _watch_entries(hass)
    client = await hass_client()

    resp = await client.post("/api/config/config_entries/flow", json={"handler": DUMMY})

    assert resp.status == HTTPStatus.OK
    assert (await resp.json())["type"] == "create_entry"
    await hass.async_block_till_done()
    (seen_added,) = _credited(seen, ConfigEntryChange.ADDED)
    assert seen_added.user_id == hass_admin_user.id
    assert seen_added.details["source"] == "user"
    assert seen_added.details["handler"] == DUMMY
    _assert_request_scope(seen_added, hass_admin_user, ACTION_CONFIG_FLOW)
    # A task spawned in the view inherits the request, but is no actor.
    assert PROBE.spawned["request"] is not None
    assert PROBE.spawned["direct_actor"] is None
    assert PROBE.spawned["scope"] is seen_added
    assert PROBE.spawned["actor"] is None


@pytest.mark.parametrize("domain_loaded", [False, True])
async def test_no_scope_for_update_made_by_setup_task(
    hass: HomeAssistant,
    fence: AttributionCollector,
    hass_client: ClientSessionGenerator,
    domain_loaded: bool,
) -> None:
    """The integration's own setup-time update is System."""
    if domain_loaded:
        assert await async_setup_component(hass, DUMMY, {})
    seen = _watch_entries(hass)
    client = await hass_client()

    resp = await client.post("/api/config/config_entries/flow", json={"handler": DUMMY})
    assert resp.status == HTTPStatus.OK
    await hass.async_block_till_done()

    setup_updates = [
        (credited, current)
        for change, entry, credited, current in seen
        if change is ConfigEntryChange.UPDATED and entry.data.get("touched")
    ]
    seen_update_from_setup, current = setup_updates[0]
    assert seen_update_from_setup is None
    # Inline setup runs inside the flow request (R11): a scope is current,
    # and only `credited_actor` keeps the update System.
    assert (current is not None) is domain_loaded


async def test_second_added_entry_in_one_flow_is_system(
    hass: HomeAssistant, fence: AttributionCollector, hass_admin_user: MockUser
) -> None:
    """Only the first ADDED of the flow's domain is credited."""
    with open_scope(
        user=hass_admin_user,
        ip=None,
        action=ACTION_CONFIG_FLOW,
        details={"source": "user", "handler": DUMMY},
        owner=asyncio.current_task(),
    ) as scope:
        added = ChangeKind.CONFIG_ENTRY, ChangeAction.ADDED
        assert credited_actor(*added, "x", domain="other") is None
        assert credited_actor(*added, "x", domain=None) is None
        assert credited_actor(*added, "a", domain=DUMMY) is scope
        assert credited_actor(*added, "b", domain=DUMMY) is None
        assert scope.details["credited"] is True


async def test_scope_for_reconfigure_flow(
    hass: HomeAssistant,
    fence: AttributionCollector,
    hass_client: ClientSessionGenerator,
    hass_admin_user: MockUser,
) -> None:
    """Reconfigure credits the update of its own entry, on configure."""
    entry = await dummy_entry(hass)
    seen = _watch_entries(hass)
    client = await hass_client()

    resp = await client.post(
        "/api/config/config_entries/flow",
        json={"handler": DUMMY, "entry_id": entry.entry_id},
    )
    form = await resp.json()
    assert form["step_id"] == "reconfigure"
    resp = await client.post(
        f"/api/config/config_entries/flow/{form['flow_id']}", json={}
    )
    assert (await resp.json())["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()

    (seen_update,) = _credited(seen, ConfigEntryChange.UPDATED)
    assert seen_update.details["source"] == "reconfigure"
    assert seen_update.details["entry_id"] == entry.entry_id
    _assert_request_scope(seen_update, hass_admin_user, ACTION_CONFIG_FLOW)
    assert entry.data["reconfigured"] is True


async def test_scope_for_options_flow(
    hass: HomeAssistant,
    fence: AttributionCollector,
    hass_client: ClientSessionGenerator,
    hass_admin_user: MockUser,
) -> None:
    """An options flow credits the options update of its entry."""
    entry = await dummy_entry(hass)
    seen = _watch_entries(hass)
    client = await hass_client()

    resp = await client.post(
        "/api/config/config_entries/options/flow", json={"handler": entry.entry_id}
    )
    form = await resp.json()
    resp = await client.post(
        f"/api/config/config_entries/options/flow/{form['flow_id']}", json={}
    )
    assert (await resp.json())["type"] == "create_entry"
    await hass.async_block_till_done()

    (seen_update,) = _credited(seen, ConfigEntryChange.UPDATED)
    assert seen_update.action == "options_flow"
    assert seen_update.details == {"entry_id": entry.entry_id, "credited": True}
    _assert_request_scope(seen_update, hass_admin_user, ACTION_OPTIONS_FLOW)
    assert entry.options == {"opt": True}


async def test_scope_for_subentry_flow(
    hass: HomeAssistant,
    fence: AttributionCollector,
    hass_client: ClientSessionGenerator,
    hass_admin_user: MockUser,
) -> None:
    """A subentry flow credits the update of its parent entry."""
    entry = await dummy_entry(hass)
    seen = _watch_entries(hass)
    client = await hass_client()

    resp = await client.post(
        "/api/config/config_entries/subentries/flow",
        json={"handler": [entry.entry_id, "thing"]},
    )
    assert (await resp.json())["type"] == "create_entry"
    await hass.async_block_till_done()

    (seen_update,) = _credited(seen, ConfigEntryChange.UPDATED)
    assert seen_update.details == {"entry_id": entry.entry_id, "credited": True}
    _assert_request_scope(seen_update, hass_admin_user, ACTION_SUBENTRY_FLOW)
    assert len(entry.subentries) == 1


async def test_scope_for_entry_delete_view(
    hass: HomeAssistant,
    fence: AttributionCollector,
    hass_client: ClientSessionGenerator,
    hass_admin_user: MockUser,
) -> None:
    """Deleting an entry credits only its REMOVED, not the unload updates."""
    entry = await dummy_entry(hass)
    seen = _watch_entries(hass)
    client = await hass_client()

    resp = await client.delete(f"/api/config/config_entries/entry/{entry.entry_id}")

    assert resp.status == HTTPStatus.OK
    await hass.async_block_till_done()
    (seen_removed,) = _credited(seen, ConfigEntryChange.REMOVED)
    assert seen_removed.action == "entry_delete"
    assert seen_removed.details == {"entry_id": entry.entry_id, "credited": True}
    assert seen_removed.user_id == hass_admin_user.id
    assert seen_removed.ip == "127.0.0.1"
    assert seen_removed.active is False
    # The view removes in its own task (R10 does not apply there).
    assert seen_removed.owner is not None
    assert seen_removed.owner.get_name() == f"config entry remove {entry.entry_id}"
    assert _credited(seen, ConfigEntryChange.UPDATED) == []


async def test_delete_of_unknown_entry_reraises_and_closes(
    hass: HomeAssistant,
    fence: AttributionCollector,
    hass_client: ClientSessionGenerator,
) -> None:
    """Core's UnknownEntry passes through unchanged (the view answers 404)."""
    client = await hass_client()

    resp = await client.delete("/api/config/config_entries/entry/nope")

    assert resp.status == HTTPStatus.NOT_FOUND
    assert SCOPE.get() is None
    assert fence.guard.errors == 0


async def test_direct_remove_without_request_has_no_scope(
    hass: HomeAssistant, fence: AttributionCollector
) -> None:
    """Removing an entry from code (no request) is System."""
    entry = await dummy_entry(hass)
    seen = _watch_entries(hass)

    await hass.config_entries.async_remove(entry.entry_id)

    assert [c for _, _, c, _ in seen] == [None] * len(seen)
    assert any(ch is ConfigEntryChange.REMOVED for ch, _, _, _ in seen)


async def test_remove_from_other_view_has_no_scope(
    hass: HomeAssistant, fence: AttributionCollector, hass_admin_user: MockUser
) -> None:
    """Only the delete view's own removal of its own entry is credited.

    Another entry, another method, or a task that is neither the view nor
    its remove task: System.
    """
    first = await dummy_entry(hass)
    second = await dummy_entry(hass)
    third = await dummy_entry(hass)
    seen = _watch_entries(hass)
    path = "/api/config/config_entries/entry/{}"
    request = Mock(method="DELETE", path=path.format(second.entry_id))
    request.get = {"hass_user": hass_admin_user}.get
    token = current_request.set(request)
    try:
        await hass.config_entries.async_remove(first.entry_id)
        request.method = "POST"
        await hass.config_entries.async_remove(second.entry_id)
        request.method = "DELETE"
        request.path = path.format(third.entry_id)
        # Right request and entry, but the test's task.
        await hass.config_entries.async_remove(third.entry_id)
    finally:
        current_request.reset(token)

    assert sum(ch is ConfigEntryChange.REMOVED for ch, _, _, _ in seen) == 3
    assert [c for _, _, c, _ in seen] == [None] * len(seen)


async def test_flows_from_code_have_no_scope(
    hass: HomeAssistant, fence: AttributionCollector
) -> None:
    """An options flow run by code (no request) is System."""
    entry = await dummy_entry(hass)
    seen = _watch_entries(hass)

    form = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(form["flow_id"], {})

    assert result["type"] == "create_entry"
    assert seen
    assert all(credited is None for _, _, credited, _ in seen)


async def test_scope_openers_ignore_odd_arguments(
    hass: HomeAssistant, fence: AttributionCollector, hass_admin_user: MockUser
) -> None:
    """Arguments that aren't IDs open no scope (Core then handles them)."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(attribution, "direct_request_actor", lambda: (hass_admin_user, None))
        mgr = hass.config_entries.options
        assert fence._configure_scope(mgr, ACTION_OPTIONS_FLOW, (3,), {}) is None
        assert fence._configure_scope(mgr, ACTION_OPTIONS_FLOW, (), {}) is None
    assert fence._remove_scope((3,), {}) is None
    assert fence._remove_scope((), {}) is None


async def test_scope_for_async_ws_command_closes_when_task_done(
    hass: HomeAssistant,
    fence: AttributionCollector,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
) -> None:
    """`config/auth/create` credits the new user; the scope closes after."""
    seen: list[ActionScope | None] = []

    @callback
    def _on_user_added(event: Event) -> None:
        seen.append(
            credited_actor(ChangeKind.USER, ChangeAction.ADDED, event.data["user_id"])
        )

    hass.bus.async_listen(EVENT_USER_ADDED, _on_user_added)
    ws = await hass_ws_client(hass)

    await ws.send_json_auto_id({"type": "config/auth/create", "name": "Carol"})
    msg = await ws.receive_json()

    assert msg["success"]
    await hass.async_block_till_done()
    (scope,) = seen
    assert scope is not None
    assert scope.user_id == hass_admin_user.id
    assert scope.ip == "127.0.0.1"
    assert scope.action == ACTION_WS
    assert scope.details["command"] == "config/auth/create"
    assert scope.owner is None
    assert scope.active is False


async def test_ws_scope_stays_open_until_handler_task_finishes(
    hass: HomeAssistant,
    fence: AttributionCollector,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
) -> None:
    """A handler that suspends keeps its scope until its task is done."""
    user = await create_hass_user(hass, "dave", "old-secret")
    observed: dict[str, Any] = {}
    original = hass.auth.async_link_user

    async def _link(*args: Any, **kwargs: Any) -> Any:
        observed["task"] = asyncio.current_task()
        observed["other_user"] = credited_actor(
            ChangeKind.USER, ChangeAction.UPDATED, hass_admin_user.id
        )
        observed["during"] = credited_actor(
            ChangeKind.USER, ChangeAction.UPDATED, user.id
        )
        return await original(*args, **kwargs)

    hass.auth.async_link_user = _link  # type: ignore[method-assign]
    await hass.auth.async_remove_credentials(user.credentials[0])
    ws = await hass_ws_client(hass)

    await ws.send_json_auto_id(
        {
            "type": "config/auth_provider/homeassistant/create",
            "user_id": user.id,
            "username": "dave2",
            "password": "new-secret",
        }
    )
    msg = await ws.receive_json()

    assert msg["success"], msg
    scope = observed["during"]
    assert scope is not None
    assert scope.details == {
        "command": "config/auth_provider/homeassistant/create",
        "user_id": user.id,
        "credited": True,
    }
    assert observed["other_user"] is None
    assert observed["task"].get_name().startswith("websocket_api.async:")
    await hass.async_block_till_done()
    assert scope.active is False


@pytest.mark.parametrize(
    ("command", "extra"),
    [
        ("config_entries/update", {"title": "New title"}),
        ("config_entries/disable", {"disabled_by": "user"}),
    ],
)
async def test_scope_for_entry_ws_commands(
    hass: HomeAssistant,
    fence: AttributionCollector,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
    command: str,
    extra: dict[str, Any],
) -> None:
    """Entry WebSocket commands credit the update of their own entry."""
    entry = await dummy_entry(hass)
    other = await dummy_entry(hass)
    seen = _watch_entries(hass)
    ws = await hass_ws_client(hass)

    await ws.send_json_auto_id({"type": command, "entry_id": entry.entry_id, **extra})
    msg = await ws.receive_json()

    assert msg["success"], msg
    await hass.async_block_till_done()
    (credited,) = [c for _, _, c, _ in seen if c is not None]
    assert credited.details == {
        "command": command,
        "entry_id": entry.entry_id,
        "credited": True,
    }
    assert credited.user_id == hass_admin_user.id
    assert [e for _, e, c, _ in seen if c is not None] == [entry]
    assert other.title != "New title"


def _credited_changes(seen: _Seen) -> list[tuple[bool, dict[str, Any]]]:
    """Return `(credited, data then)` for each real change, in order."""
    return [(c is not None, d) for (_, _, c, _), d in zip(seen, seen.data, strict=True)]


async def test_ws_reenable_credits_enable_not_setup_update(
    hass: HomeAssistant,
    fence: AttributionCollector,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Re-enabling sets the entry up inline; its own update is System (R25)."""
    entry = await dummy_entry(hass)
    await hass.config_entries.async_set_disabled_by(
        entry.entry_id, config_entries.ConfigEntryDisabler.USER
    )
    await hass.async_block_till_done()
    seen = _watch_entries(hass)
    ws = await hass_ws_client(hass)

    await ws.send_json_auto_id(
        {
            "type": "config_entries/disable",
            "entry_id": entry.entry_id,
            "disabled_by": None,
        }
    )
    msg = await ws.receive_json()

    assert msg["success"], msg
    await hass.async_block_till_done()
    assert entry.state is config_entries.ConfigEntryState.LOADED
    assert entry.data["setups"] == 2
    assert _credited_changes(seen) == [
        (True, {"touched": True, "setups": 1}),
        (False, {"touched": True, "setups": 2}),
    ]
    assert seen[0][2] is not None
    assert seen[0][2].active is False


async def test_ws_pref_disable_polling_credits_only_the_update(
    hass: HomeAssistant,
    fence: AttributionCollector,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """A polling change reloads inline; the setup's own update is System."""
    entry = await dummy_entry(hass)
    seen = _watch_entries(hass)
    ws = await hass_ws_client(hass)

    await ws.send_json_auto_id(
        {
            "type": "config_entries/update",
            "entry_id": entry.entry_id,
            "pref_disable_polling": True,
        }
    )
    msg = await ws.receive_json()

    assert msg["success"], msg
    await hass.async_block_till_done()
    assert entry.pref_disable_polling is True
    assert _credited_changes(seen) == [
        (True, {"touched": True, "setups": 1}),
        (False, {"touched": True, "setups": 2}),
    ]


async def test_ws_update_with_listener_reload_never_credits_listener(
    hass: HomeAssistant,
    fence: AttributionCollector,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """An update listener's eager reload inherits the scope, but is System."""
    mock_dummy(hass, reloading=True)
    entry = await dummy_entry(hass)
    seen = _watch_entries(hass)
    ws = await hass_ws_client(hass)

    # A title change doesn't reload; a WS options change isn't possible,
    # so the options change comes from code inside the command's scope.
    original = hass.config_entries.async_update_entry

    def _update(entry_: ConfigEntry, **kwargs: Any) -> bool:
        return original(entry_, **kwargs, options={"opt": "ws"})

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(hass.config_entries, "async_update_entry", _update)
        await ws.send_json_auto_id(
            {"type": "config_entries/update", "entry_id": entry.entry_id, "title": "T"}
        )
        msg = await ws.receive_json()
        assert msg["success"], msg
        await hass.async_block_till_done()

    assert entry.data["setups"] == 2
    # The reload ran in the listener's task ahead of the command's dispatch,
    # so this naive watcher sees the change there, where nothing is credited
    # (R29); H4 holds the change back for the command's own dispatch
    # (`test_ws_update_with_reloading_listener_credited`).
    assert [c for c, _ in _credited_changes(seen)] == [False, False]


async def test_options_flow_reload_credits_first_change_only(
    hass: HomeAssistant,
    fence: AttributionCollector,
    hass_client: ClientSessionGenerator,
) -> None:
    """The options change is credited once; the reload's own update is System.

    Every dispatch is recorded here: Core runs the update listener (an eager
    task that starts the reload) *before* it dispatches the options change,
    so the first dispatch showing the new options is the reload's state
    change, from the listener task (see the Task 9 fix report).
    """
    mock_dummy(hass, reloading=True)
    entry = await dummy_entry(hass)
    seen = _watch_entries(hass, dedupe=False)
    client = await hass_client()

    resp = await client.post(
        "/api/config/config_entries/options/flow", json={"handler": entry.entry_id}
    )
    form = await resp.json()
    resp = await client.post(
        f"/api/config/config_entries/options/flow/{form['flow_id']}", json={}
    )
    assert (await resp.json())["type"] == "create_entry"
    await hass.async_block_till_done()

    assert entry.options == {"opt": True}
    assert entry.data["setups"] == 2
    (credited,) = [c for _, _, c, _ in seen if c is not None]
    assert credited.action == ACTION_OPTIONS_FLOW
    # The setup's own update: the dispatch where `setups` turns 2.
    setups = [d["setups"] for d in seen.data]
    setup_update = setups.index(2)
    assert setups[setup_update - 1] == 1
    assert seen[setup_update][2] is None


async def test_scope_for_core_config_update(
    hass: HomeAssistant,
    fence: AttributionCollector,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
) -> None:
    """`config/core/update` credits the core config update."""
    seen: list[ActionScope | None] = []

    @callback
    def _on_core(event: Event) -> None:
        seen.append(credited_actor(ChangeKind.CORE_CONFIG, ChangeAction.UPDATED, None))

    hass.bus.async_listen(EVENT_CORE_CONFIG_UPDATE, _on_core)
    ws = await hass_ws_client(hass)

    await ws.send_json_auto_id({"type": "config/core/update", "location_name": "Hut"})
    msg = await ws.receive_json()

    assert msg["success"], msg
    await hass.async_block_till_done()
    assert seen
    assert all(s is not None and s.user_id == hass_admin_user.id for s in seen)


async def test_ws_command_from_non_admin_reraises_and_closes(
    hass: HomeAssistant,
    fence: AttributionCollector,
    hass_ws_client: WebSocketGenerator,
    hass_read_only_access_token: str,
) -> None:
    """`require_admin` raising Unauthorized passes through; the scope closes."""
    closed: list[ActionScope] = []
    original_close = ActionScope.close

    def _close(self: ActionScope) -> None:
        closed.append(self)
        original_close(self)

    ws = await hass_ws_client(hass, hass_read_only_access_token)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ActionScope, "close", _close)
        await ws.send_json_auto_id({"type": "config/auth/create", "name": "Eve"})
        msg = await ws.receive_json()

    assert not msg["success"]
    assert msg["error"]["code"] == "unauthorized"
    (scope,) = closed
    assert scope.active is False
    assert fence.guard.errors == 0


async def test_no_scope_for_timer_update(
    hass: HomeAssistant,
    fence: AttributionCollector,
    hass_admin_user: MockUser,
) -> None:
    """A timer set up during an action fires later, with no actor."""
    entry = await dummy_entry(hass)
    seen = _watch_entries(hass)

    @callback
    def _later(_now: Any) -> None:
        hass.config_entries.async_update_entry(entry, title="From timer")

    with open_scope(
        user=hass_admin_user,
        ip=None,
        action=ACTION_OPTIONS_FLOW,
        details={"entry_id": entry.entry_id},
        owner=asyncio.current_task(),
    ):
        async_call_later(hass, timedelta(seconds=5), _later)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=10))
    await hass.async_block_till_done()

    ((change, _, credited, current),) = seen
    assert change is ConfigEntryChange.UPDATED
    assert entry.title == "From timer"
    assert credited is None
    assert current is None


async def test_discovery_flow_without_request_has_no_scope(
    hass: HomeAssistant, fence: AttributionCollector
) -> None:
    """A discovery flow started by code (no request) is System."""
    seen = _watch_entries(hass)

    result = await hass.config_entries.flow.async_init(
        DUMMY, context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY}, data={}
    )
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert seen
    assert all(credited is None and current is None for _, _, credited, current in seen)


async def test_discovery_flow_started_inside_request_has_no_scope(
    hass: HomeAssistant, fence: AttributionCollector, hass_admin_user: MockUser
) -> None:
    """Only user and reconfigure flows start a scope on init."""
    seen = _watch_entries(hass)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            attribution, "direct_request_actor", lambda: (hass_admin_user, "10.0.0.1")
        )
        await hass.config_entries.flow.async_init(
            DUMMY, context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY}
        )
    await hass.async_block_till_done()

    assert all(credited is None for _, _, credited, _ in seen)


async def test_configure_unknown_flow_reraises(
    hass: HomeAssistant,
    fence: AttributionCollector,
    hass_client: ClientSessionGenerator,
) -> None:
    """An unknown flow ID still reaches Core, which answers 404."""
    client = await hass_client()

    resp = await client.post("/api/config/config_entries/flow/nope", json={})

    assert resp.status == HTTPStatus.NOT_FOUND
    assert fence.guard.errors == 0


async def test_flow_wrapper_fails_open(
    hass: HomeAssistant,
    fence: AttributionCollector,
    hass_client: ClientSessionGenerator,
) -> None:
    """A failing actor lookup only means no scope; the flow still works."""

    def _boom() -> None:
        raise RuntimeError("boom")

    seen = _watch_entries(hass)
    client = await hass_client()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(attribution, "direct_request_actor", _boom)
        resp = await client.post(
            "/api/config/config_entries/flow", json={"handler": DUMMY}
        )

    assert (await resp.json())["type"] == "create_entry"
    await hass.async_block_till_done()
    assert fence.guard.errors == 1
    assert all(credited is None for _, _, credited, _ in seen)


async def test_scope_enter_failure_calls_through(
    hass: HomeAssistant,
    fence: AttributionCollector,
    hass_client: ClientSessionGenerator,
) -> None:
    """A scope that can't be opened is skipped, and never closed."""

    def _broken_scope(**kwargs: Any) -> Any:
        scope = Mock()
        scope.__enter__ = Mock(side_effect=RuntimeError("boom"))
        return scope

    client = await hass_client()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(attribution, "open_scope", _broken_scope)
        resp = await client.post(
            "/api/config/config_entries/flow", json={"handler": DUMMY}
        )

    assert (await resp.json())["type"] == "create_entry"
    assert fence.guard.errors == 1


async def test_ws_wrapper_fails_open(
    hass: HomeAssistant,
    fence: AttributionCollector,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """A failing target lookup only means no scope; the command still works."""

    def _boom(*args: Any) -> None:
        raise RuntimeError("boom")

    ws = await hass_ws_client(hass)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(attribution, "_ws_details", _boom)
        await ws.send_json_auto_id({"type": "config/auth/create", "name": "Fay"})
        msg = await ws.receive_json()

    assert msg["success"]
    assert fence.guard.errors == 1
    assert SCOPE.get() is None


async def test_ws_task_lookup_failure_closes_scope(
    hass: HomeAssistant,
    fence: AttributionCollector,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """If the handler task can't be found, the scope closes at once."""
    seen: list[ActionScope | None] = []

    @callback
    def _on_user_added(event: Event) -> None:
        seen.append(SCOPE.get())

    def _boom(*args: Any) -> None:
        raise RuntimeError("boom")

    hass.bus.async_listen(EVENT_USER_ADDED, _on_user_added)
    ws = await hass_ws_client(hass)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(attribution, "_find_ws_task", _boom)
        await ws.send_json_auto_id({"type": "config/auth/create", "name": "Gil"})
        msg = await ws.receive_json()

    assert msg["success"]
    await hass.async_block_till_done()
    (scope,) = seen
    assert scope is not None
    assert scope.active is False
    assert fence.guard.errors == 1


async def test_ws_without_user_has_no_scope(hass: HomeAssistant) -> None:
    """A connection without a user opens no scope."""
    connection = Mock(user=None)
    assert attribution._ws_enter("config/core/update", connection, {}) is None


async def test_unload_restores_flow_managers_and_ws_handlers(
    hass: HomeAssistant, fence: AttributionCollector, setup_entry: MockConfigEntry
) -> None:
    """Unloading puts every original back."""
    entries = hass.config_entries
    handlers = hass.data[WEBSOCKET_API_DOMAIN]
    for mgr in (entries.flow, entries.options, entries.subentries):
        assert "async_init" in vars(mgr)
        assert "async_configure" in vars(mgr)
    assert "async_remove" in vars(entries)
    # `person` is not loaded here: its command is covered in test_unload_reload.
    wrapped = {c: handlers[c] for c in ADMIN_WS_COMMANDS if c in handlers}
    assert len(wrapped) == len(ADMIN_WS_COMMANDS) - 1

    assert await hass.config_entries.async_unload(setup_entry.entry_id)

    for mgr in (entries.flow, entries.options, entries.subentries):
        assert "async_init" not in vars(mgr)
        assert "async_configure" not in vars(mgr)
    assert "async_remove" not in vars(entries)
    for command, entry in wrapped.items():
        assert handlers[command] is not entry
        assert handlers[command][1] is entry[1]
        assert handlers[command][0] is entry[0].__wrapped__


def _loaded_listeners(hass: HomeAssistant) -> int:
    return hass.bus.async_listeners().get(EVENT_COMPONENT_LOADED, 0)


@pytest.fixture
def late(hass: HomeAssistant) -> Iterator[AttributionCollector]:
    """Return an attribution collector installed before `config` loads.

    Electric Fence's own dependencies load `config` first, so this is the
    collector alone.
    """
    assert "config" not in hass.config.components
    collector = AttributionCollector(hass, Mock())
    assert collector.install()
    yield collector
    collector.uninstall()


def _core_update_handler(hass: HomeAssistant) -> Any:
    return hass.data[WEBSOCKET_API_DOMAIN]["config/core/update"][0]


async def test_ws_wrapping_waits_for_config(
    hass: HomeAssistant, late: AttributionCollector
) -> None:
    """Without `config` and `person`, their commands are wrapped as they load."""
    assert late._waiting == {"config", "person"}
    assert late._loaded_unsub is not None

    assert await async_setup_component(hass, "config", {})

    assert late._waiting == {"person"}
    assert _core_update_handler(hass) is not config_core.websocket_update_config
    assert len(late._handles) == 7 + len(ADMIN_WS_COMMANDS) - 1
    assert late.disabled_reason == []

    assert await async_setup_component(hass, "person", {})

    assert late._waiting == set()
    assert late._loaded_unsub is None
    own = hass.data[WEBSOCKET_API_DOMAIN]["person/update_own_profile"][0]
    assert own.__wrapped__ is person.ws_update_own_profile
    assert len(late._handles) == 7 + len(ADMIN_WS_COMMANDS)
    assert late.disabled_reason == []


async def test_person_command_component() -> None:
    """Each wrapped command belongs to the component that registers it."""
    assert attribution.command_component("person/update_own_profile") == "person"
    assert attribution.command_component("config/core/update") == "config"


async def test_uninstall_cancels_pending_config_listener(
    hass: HomeAssistant, late: AttributionCollector
) -> None:
    """Uninstalling before `config` loads removes the pending listener."""
    before = _loaded_listeners(hass)

    late.uninstall()

    assert _loaded_listeners(hass) == before - 1
    assert await async_setup_component(hass, "config", {})
    assert _core_update_handler(hass) is config_core.websocket_update_config


async def test_config_loading_after_uninstall_is_ignored(
    hass: HomeAssistant, late: AttributionCollector
) -> None:
    """A late `config` load for a collector no longer installed wraps nothing."""
    late._installed = False

    assert await async_setup_component(hass, "config", {})

    assert _core_update_handler(hass) is config_core.websocket_update_config


async def test_late_config_with_missing_command_disables_collector(
    hass: HomeAssistant, late: AttributionCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command missing once `config` loads turns the collector off."""
    monkeypatch.setattr(
        attribution, "ADMIN_WS_COMMANDS", (*ADMIN_WS_COMMANDS, "config/renamed")
    )

    assert await async_setup_component(hass, "config", {})

    assert not late._installed
    assert "config/renamed" in late.disabled_reason[0]
    assert "async_init" not in vars(hass.config_entries.flow)
    assert _core_update_handler(hass) is config_core.websocket_update_config
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "collector_disabled_attribution")
    assert issue is not None


async def test_config_loaded_late_fails_open(
    hass: HomeAssistant, late: AttributionCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error while wrapping late is swallowed and counted."""

    def _boom() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(late, "_wrap_ws_commands", _boom)

    assert await async_setup_component(hass, "config", {})

    assert late.guard.errors == 1


# Unit tests: scope, actor and the crediting table.


async def test_current_actor_requires_active_scope_in_owner_task(
    hass_admin_user: MockUser,
) -> None:
    """`current_actor` returns only an active scope seen from its owner."""
    assert current_actor() is None
    other: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(0))
    with open_scope(
        user=hass_admin_user, ip="1.2.3.4", action=ACTION_WS, details={}, owner=None
    ) as scope:
        assert current_actor() is scope
        scope.owner = other
        assert current_actor() is None
        scope.owner = asyncio.current_task()
        assert current_actor() is scope
        scope.close()
        assert current_actor() is None
    assert SCOPE.get() is None
    await other


async def test_scope_reset_even_if_close_raises(
    hass_admin_user: MockUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing close still resets the context variable (no leaked scope)."""

    def _boom(self: ActionScope) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(ActionScope, "close", _boom)
    cm = open_scope(
        user=hass_admin_user, ip=None, action=ACTION_WS, details={}, owner=None
    )
    cm.__enter__()
    assert SCOPE.get() is not None
    with pytest.raises(RuntimeError):
        cm.__exit__(None, None, None)
    assert SCOPE.get() is None


async def test_direct_request_actor_needs_request_user_and_view_task(
    hass_admin_user: MockUser,
) -> None:
    """No request, or a request without a user, gives no actor."""
    assert direct_request_actor() is None
    request = Mock(remote="1.2.3.4")
    request.get = {}.get
    token = current_request.set(request)
    try:
        assert direct_request_actor() is None
        request.get = {"hass_user": hass_admin_user}.get
        # The test task is not aiohttp's `_handle_request` task.
        assert direct_request_actor() is None
    finally:
        current_request.reset(token)


_CE = ChangeKind.CONFIG_ENTRY
_USER = ChangeKind.USER
_CORE = ChangeKind.CORE_CONFIG
A = ChangeAction

_ENTRY_WS = (
    "config_entries/update",
    "config_entries/disable",
    "config_entries/subentries/update",
    "config_entries/subentries/delete",
)
_HA_DELETE = "config/auth_provider/homeassistant/delete"
_USER_UPDATE_WS = (
    "config/auth/update",
    "config/auth_provider/homeassistant/admin_change_username",
    "config/auth_provider/homeassistant/create",
    "config/auth_provider/homeassistant/delete",
)

# (action, details, kind, change, target, credited?)
_TABLE: list[tuple[str, dict[str, Any], ChangeKind, ChangeAction, str | None, bool]] = [
    (ACTION_CONFIG_FLOW, {"source": "reconfigure", "entry_id": "e"}, _CE, A.UPDATED, "e", True),
    (ACTION_CONFIG_FLOW, {"source": "reconfigure", "entry_id": "e"}, _CE, A.RECONFIGURED, "e", True),
    (ACTION_CONFIG_FLOW, {"source": "reconfigure", "entry_id": "e"}, _CE, A.UPDATED, "f", False),
    (ACTION_CONFIG_FLOW, {"source": "reconfigure", "entry_id": "e"}, _CE, A.ADDED, "e", False),
    (ACTION_CONFIG_FLOW, {"source": "reconfigure", "entry_id": None}, _CE, A.UPDATED, None, False),
    (ACTION_CONFIG_FLOW, {"source": "user", "handler": "d"}, _CE, A.UPDATED, "e", False),
    (ACTION_CONFIG_FLOW, {"source": "user", "handler": "d"}, _USER, A.ADDED, "e", False),
    (ACTION_OPTIONS_FLOW, {"entry_id": "e"}, _CE, A.UPDATED, "e", True),
    (ACTION_OPTIONS_FLOW, {"entry_id": "e"}, _CE, A.OPTIONS_CHANGED, "e", True),
    (ACTION_OPTIONS_FLOW, {"entry_id": "e"}, _CE, A.OPTIONS_CHANGED, "f", False),
    (ACTION_OPTIONS_FLOW, {"entry_id": "e"}, _CE, A.REMOVED, "e", False),
    (ACTION_SUBENTRY_FLOW, {"entry_id": "e"}, _CE, A.UPDATED, "e", True),
    (ACTION_SUBENTRY_FLOW, {"entry_id": "e"}, _CE, A.OPTIONS_CHANGED, "e", False),
    (ACTION_SUBENTRY_FLOW, {"entry_id": "e"}, _CE, A.UPDATED, "f", False),
    (ACTION_ENTRY_DELETE, {"entry_id": "e"}, _CE, A.REMOVED, "e", True),
    (ACTION_ENTRY_DELETE, {"entry_id": "e"}, _CE, A.REMOVED, "f", False),
    (ACTION_ENTRY_DELETE, {"entry_id": "e"}, _CE, A.UPDATED, "e", False),
    *[
        (ACTION_WS, {"command": c, "entry_id": "e"}, _CE, change, "e", True)
        for c in _ENTRY_WS
        for change in (A.UPDATED, A.ENABLED, A.DISABLED)
    ],
    *[(ACTION_WS, {"command": c, "entry_id": "e"}, _CE, A.UPDATED, "f", False) for c in _ENTRY_WS],
    *[(ACTION_WS, {"command": c, "entry_id": "e"}, _CE, A.REMOVED, "e", False) for c in _ENTRY_WS],
    *[(ACTION_WS, {"command": c, "user_id": "u"}, _USER, A.UPDATED, "u", True) for c in _USER_UPDATE_WS],
    *[(ACTION_WS, {"command": c, "user_id": "u"}, _USER, A.UPDATED, "v", False) for c in _USER_UPDATE_WS],
    *[(ACTION_WS, {"command": c, "user_id": "u"}, _USER, A.PASSWORD_CHANGED, "u", False) for c in _USER_UPDATE_WS],
    (ACTION_WS, {"command": "config/auth_provider/homeassistant/delete", "user_id": None}, _USER, A.UPDATED, None, False),
    (ACTION_WS, {"command": "config/auth/delete", "user_id": "u"}, _USER, A.REMOVED, "u", True),
    (ACTION_WS, {"command": "config/auth/delete", "user_id": "u"}, _USER, A.REMOVED, "v", False),
    (ACTION_WS, {"command": "config/auth/delete", "user_id": "u"}, _USER, A.UPDATED, "u", False),
    (ACTION_WS, {"command": "config/auth_provider/homeassistant/change_password", "user_id": "u"}, _USER, A.PASSWORD_CHANGED, "u", True),
    (ACTION_WS, {"command": "config/auth_provider/homeassistant/admin_change_password", "user_id": "u"}, _USER, A.PASSWORD_CHANGED, "u", True),
    (ACTION_WS, {"command": "config/auth_provider/homeassistant/admin_change_password", "user_id": "u"}, _USER, A.PASSWORD_CHANGED, "v", False),
    (ACTION_WS, {"command": "config/auth_provider/homeassistant/admin_change_password", "user_id": "u"}, _USER, A.UPDATED, "u", False),
    (ACTION_WS, {"command": "config/auth/create"}, _USER, A.UPDATED, "u", False),
    (ACTION_WS, {"command": "config/auth/create"}, _CE, A.ADDED, "u", False),
    (ACTION_WS, {"command": "config/core/update"}, _CORE, A.UPDATED, None, True),
    (ACTION_WS, {"command": "config/core/update"}, _CORE, A.UPDATED, "x", True),
    (ACTION_WS, {"command": "config/core/update"}, _USER, A.UPDATED, "u", False),
    (ACTION_WS, {"command": "config/core/update"}, _CORE, A.ADDED, None, False),
    (ACTION_WS, {"command": "config_entries/flow/progress"}, _CE, A.UPDATED, "e", False),
    (ACTION_WS, {}, _CORE, A.UPDATED, None, False),
    ("unknown", {"entry_id": "e"}, _CE, A.UPDATED, "e", False),
]  # fmt: skip


@pytest.mark.parametrize(
    ("action", "details", "kind", "change", "target", "expected"), _TABLE
)
async def test_credited_actor_table(
    hass_admin_user: MockUser,
    action: str,
    details: dict[str, Any],
    kind: ChangeKind,
    change: ChangeAction,
    target: str | None,
    expected: bool,
) -> None:
    """Each scope credits only the change its action implies, on its target."""
    with open_scope(
        user=hass_admin_user,
        ip=None,
        action=action,
        details=dict(details),
        owner=asyncio.current_task(),
    ) as scope:
        result = credited_actor(kind, change, target, domain="d")
    assert (result is scope) is expected
    assert result is None or result is scope


@pytest.mark.parametrize(
    ("action", "details", "kind", "change", "target"),
    [row[:5] for row in _TABLE if row[5]],
)
async def test_credited_actor_credits_once_per_scope(
    hass_admin_user: MockUser,
    action: str,
    details: dict[str, Any],
    kind: ChangeKind,
    change: ChangeAction,
    target: str | None,
) -> None:
    """A scope credits only its first matching change (R25)."""
    with open_scope(
        user=hass_admin_user,
        ip=None,
        action=action,
        details=dict(details),
        owner=asyncio.current_task(),
    ) as scope:
        assert credited_actor(kind, change, target, domain="d") is scope
        assert credited_actor(kind, change, target, domain="d") is None


async def test_non_matching_change_does_not_use_up_the_credit(
    hass_admin_user: MockUser,
) -> None:
    """Changes to other targets leave the credit for the scope's own change."""
    with open_scope(
        user=hass_admin_user,
        ip=None,
        action=ACTION_ENTRY_DELETE,
        details={"entry_id": "e"},
        owner=asyncio.current_task(),
    ) as scope:
        assert credited_actor(_CE, A.UPDATED, "e") is None
        assert credited_actor(_CE, A.REMOVED, "f") is None
        assert credited_actor(_CE, A.REMOVED, "e") is scope


async def test_ha_delete_resolves_username_to_user(
    hass: HomeAssistant,
    fence: AttributionCollector,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
) -> None:
    """Deleting a username's credentials credits that user's update (R24)."""
    user = await create_hass_user(hass, "dave", "secret", admin=False)
    observed: dict[str, Any] = {}
    original = hass.auth.async_remove_credentials

    async def _remove(*args: Any, **kwargs: Any) -> Any:
        observed["credited"] = credited_actor(_USER, A.UPDATED, user.id)
        return await original(*args, **kwargs)

    hass.auth.async_remove_credentials = _remove  # type: ignore[method-assign]
    ws = await hass_ws_client(hass)

    await ws.send_json_auto_id({"type": _HA_DELETE, "username": "dave"})
    msg = await ws.receive_json()

    assert msg["success"], msg
    scope = observed["credited"]
    assert scope is not None
    assert scope.user_id == hass_admin_user.id
    assert scope.details == {
        "command": _HA_DELETE,
        "user_id": user.id,
        "credited": True,
    }
    assert user.credentials == []


async def test_hass_user_id_lookup(hass: HomeAssistant) -> None:
    """The username matches normalised; anything unknown is None."""
    user = await create_hass_user(hass, "dave", "secret")
    await create_hass_user(hass, "erin", "secret")
    lookup = attribution.hass_user_id

    assert lookup(hass, " DAVE ") == user.id
    assert lookup(hass, "nobody") is None
    assert lookup(hass, None) is None
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(hass.auth._store, "_users", None)
        assert lookup(hass, "dave") is None
    provider = hass.auth.get_auth_provider("homeassistant", None)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(provider, "data", None)
        assert lookup(hass, "dave") is None
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(hass.auth, "_providers", {})
        assert lookup(hass, "dave") is None


async def test_credited_actor_first_added_user_only(hass_admin_user: MockUser) -> None:
    """`config/auth/create` credits only the first user added."""

    async def _in_handler() -> list[ActionScope | None]:
        return [
            credited_actor(_USER, A.ADDED, "u1"),
            credited_actor(_USER, A.ADDED, "u2"),
        ]

    with open_scope(
        user=hass_admin_user,
        ip=None,
        action=ACTION_WS,
        details={"command": "config/auth/create"},
        owner=None,
    ) as scope:
        first, second = await asyncio.create_task(
            _in_handler(), name=f"{WS_TASK_PREFIX}websocket_create"
        )
        assert first is scope
        assert second is None


async def test_ws_scope_credits_only_in_its_handler_task(
    hass_admin_user: MockUser,
) -> None:
    """A WebSocket scope credits only in its `async_response` task (R29).

    Not in the task that opened it, not in a task inheriting its context
    (an update listener), and not where the scope isn't the task's own.
    """
    details = {"command": "config/core/update"}
    core = ChangeKind.CORE_CONFIG, A.UPDATED, None

    async def _credit() -> ActionScope | None:
        return credited_actor(*core)

    async def _handler_spawning_listener() -> ActionScope | None:
        # Created inside the handler: inherits the scope, other name.
        return await asyncio.create_task(_credit(), name="config entry update listener")

    with open_scope(
        user=hass_admin_user, ip=None, action=ACTION_WS, details=details, owner=None
    ) as scope:
        assert current_actor() is scope
        assert credited_actor(*core) is None
        assert (
            await asyncio.create_task(
                _handler_spawning_listener(), name=f"{WS_TASK_PREFIX}handler"
            )
            is None
        )
        other = ActionScope("x", None, None, ACTION_WS, dict(details), owner=None)

        async def _other_scope_inside() -> ActionScope | None:
            # The task's own context still holds `scope`.
            def _run() -> ActionScope | None:
                SCOPE.set(other)
                return credited_actor(*core)

            return contextvars.copy_context().run(_run)

        assert (
            await asyncio.create_task(
                _other_scope_inside(), name=f"{WS_TASK_PREFIX}handler"
            )
            is None
        )
        assert "credited" not in details
        assert (
            await asyncio.create_task(_credit(), name=f"{WS_TASK_PREFIX}handler")
            is scope
        )


async def test_credited_actor_without_scope_is_system() -> None:
    """No scope means System."""
    assert credited_actor(_CORE, A.UPDATED, None) is None


def _conn(user_id: str = "admin", remote: str | None = "10.0.0.2") -> Mock:
    connection = Mock(remote=remote)
    connection.user.id = user_id
    connection.hass.auth.auth_providers = []
    return connection


@pytest.mark.parametrize(
    ("command", "msg", "expected"),
    [
        *[(c, {"entry_id": "e", "x": 1}, {"entry_id": "e"}) for c in _ENTRY_WS],
        *[(c, {"user_id": "u"}, {"user_id": "u"}) for c in _USER_UPDATE_WS if c != _HA_DELETE],
        (_HA_DELETE, {"username": "x"}, {"user_id": None}),
        (_HA_DELETE, {"user_id": "u"}, {"user_id": None}),
        ("config/auth/delete", {"user_id": "u"}, {"user_id": "u"}),
        ("config/auth_provider/homeassistant/admin_change_password", {"user_id": "u"}, {"user_id": "u"}),
        ("config/auth_provider/homeassistant/change_password", {"user_id": "u"}, {"user_id": "admin"}),
        ("person/update_own_profile", {"name": "n", "user_id": "u"}, {"user_id": "admin"}),
        ("config/auth/create", {"name": "n"}, {}),
        ("config/core/update", {"location_name": "n"}, {}),
        ("config_entries/update", {}, {"entry_id": None}),
    ],
)  # fmt: skip
def test_ws_details_capture_targets(
    command: str, msg: dict[str, Any], expected: dict[str, Any]
) -> None:
    """The WebSocket wrapper reads the target from the message (R22)."""
    assert attribution._ws_details(command, _conn(), msg) == {
        "command": command,
        **expected,
    }


def test_ws_details_cover_every_admin_command() -> None:
    """Every wrapped command has a crediting rule."""
    assert set(attribution._WS_RULES) == set(ADMIN_WS_COMMANDS)


async def test_ws_ip_falls_back_to_request() -> None:
    """Without `connection.remote`, the upgrade request gives the IP."""
    assert attribution.ws_ip(_conn(remote="10.0.0.2")) == "10.0.0.2"
    assert attribution.ws_ip(_conn(remote=None)) is None
    token = current_request.set(Mock(remote="10.0.0.3"))
    try:
        assert attribution.ws_ip(_conn(remote=None)) == "10.0.0.3"
    finally:
        current_request.reset(token)


@pytest.mark.parametrize(
    ("action", "handler", "context", "expected"),
    [
        (ACTION_CONFIG_FLOW, "d", {"source": "user"}, {"source": "user", "handler": "d"}),
        (ACTION_CONFIG_FLOW, 3, {"source": "user"}, {"source": "user", "handler": None}),
        (ACTION_CONFIG_FLOW, "d", {"source": "reconfigure", "entry_id": "e"}, {"source": "reconfigure", "entry_id": "e"}),
        (ACTION_CONFIG_FLOW, "d", {"source": "zeroconf"}, None),
        (ACTION_CONFIG_FLOW, "d", None, None),
        (ACTION_OPTIONS_FLOW, "e", None, {"entry_id": "e"}),
        (ACTION_OPTIONS_FLOW, 3, None, {"entry_id": None}),
        (ACTION_SUBENTRY_FLOW, ("e", "thing"), None, {"entry_id": "e"}),
        (ACTION_SUBENTRY_FLOW, "e", None, {"entry_id": None}),
        ("unknown", "e", None, None),
    ],
)  # fmt: skip
def test_flow_details_on_init(
    action: str, handler: Any, context: Any, expected: dict[str, Any] | None
) -> None:
    """Flow scopes capture their target; init only starts user and reconfigure."""
    assert attribution._flow_details(action, handler, context, init=True) == expected


def test_flow_details_on_configure_keep_any_source() -> None:
    """An admin confirming a discovered flow is credited on configure."""
    assert attribution._flow_details(
        ACTION_CONFIG_FLOW, "d", {"source": "zeroconf"}, init=False
    ) == {"source": "zeroconf", "handler": "d"}


async def test_install_wraps_only_registered_commands(
    hass: HomeAssistant, dummy: None, config: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command that is not registered is skipped by the wrapper."""
    monkeypatch.delitem(hass.data[WEBSOCKET_API_DOMAIN], "config/core/update")
    collector = AttributionCollector(hass, Mock())
    collector._installed = True

    collector._wrap_ws_commands()

    assert "config/core/update" not in hass.data[WEBSOCKET_API_DOMAIN]
    # Minus the missing one, and `person`'s (not loaded).
    assert len(collector._handles) == len(ADMIN_WS_COMMANDS) - 2
    collector.uninstall()
