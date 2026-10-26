"""Tests for the system settings collector core (admin WebSocket commands)."""

# ruff: noqa: SLF001

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from unittest.mock import Mock

import probatio
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    MockUser,
    mock_integration,
)
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.electric_fence import COLLECTOR_TYPES, attribution
from custom_components.electric_fence.attribution import AttributionCollector
from custom_components.electric_fence.collectors.system_settings import (
    AREA_SPECS,
    ResultWatch,
    SystemSettingsCollector,
    WsSpec,
    request_params,
)
from custom_components.electric_fence.const import DOMAIN, MIN_HA_VERSION
from custom_components.electric_fence.contracts import CONTRACTS
from custom_components.electric_fence.failopen import FailOpen
from custom_components.electric_fence.models import ChangeAction, ChangeKind
from custom_components.electric_fence.settings import Settings
from custom_components.electric_fence.storage import ChangeRow
from homeassistant.components import websocket_api
from homeassistant.components.websocket_api import (
    DOMAIN as WEBSOCKET_API_DOMAIN,
    ActiveConnection,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.json import json_bytes
from homeassistant.setup import async_setup_component
from tests.helpers import change_rows, flush_worker

PRODUCTION_SPECS = SystemSettingsCollector.WS_SPECS

FAKE = "ef_sys_test"
DATA = f"{FAKE}_data"
IP = "127.0.0.1"

CMD_SYNC = f"{FAKE}/sync_set"
CMD_ASYNC = f"{FAKE}/async_set"
CMD_ERROR = f"{FAKE}/error"
CMD_RAISE = f"{FAKE}/raise"
CMD_SILENT = f"{FAKE}/silent"
CMD_DICT = f"{FAKE}/dict_result"
CMD_BYTES = f"{FAKE}/bytes_result"
CMD_DICT_ERROR = f"{FAKE}/dict_error"
CMD_LATE_ERROR = f"{FAKE}/result_then_error"
CMD_ACTION = f"{FAKE}/action"
CMD_ACTION_BEFORE = f"{FAKE}/action_before"

ALL_COMMANDS = (
    CMD_SYNC,
    CMD_ASYNC,
    CMD_ERROR,
    CMD_RAISE,
    CMD_SILENT,
    CMD_DICT,
    CMD_BYTES,
    CMD_DICT_ERROR,
    CMD_LATE_ERROR,
    CMD_ACTION,
    CMD_ACTION_BEFORE,
)


def _change(hass: HomeAssistant, msg: dict[str, Any]) -> None:
    hass.data[DATA].update(msg.get("values", {}))


def _read(hass: HomeAssistant, msg: dict[str, Any]) -> Mapping[str, Any] | None:
    data = hass.data.get(DATA)
    return dict(data) if data is not None else None


_SCHEMA: dict[Any, Any] = {
    probatio.Optional("values"): dict,
    probatio.Optional("target"): str,
    probatio.Optional("password"): str,
    probatio.Optional("nothing"): None,
}


def _cmd(name: str) -> dict[Any, Any]:
    return {probatio.Required("type"): name, **_SCHEMA}


@websocket_api.websocket_command(_cmd(CMD_SYNC))
@websocket_api.require_admin
@callback
def ws_sync(hass: HomeAssistant, connection: ActiveConnection, msg: dict) -> None:
    """Change the data and answer at once."""
    _change(hass, msg)
    connection.send_result(msg["id"], {"done": "sync"})


@websocket_api.websocket_command(_cmd(CMD_ASYNC))
@websocket_api.require_admin
@websocket_api.async_response
async def ws_async(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict
) -> None:
    """Change the data after an await, then answer from the handler task."""
    await asyncio.sleep(0)
    _change(hass, msg)
    connection.send_result(msg["id"], {"done": "async"})


@websocket_api.websocket_command(_cmd(CMD_ERROR))
@websocket_api.require_admin
@websocket_api.async_response
async def ws_error(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict
) -> None:
    """Change the data, then answer with an error."""
    _change(hass, msg)
    connection.send_error(msg["id"], "nope", "Not allowed")


@websocket_api.websocket_command(_cmd(CMD_RAISE))
@websocket_api.require_admin
@websocket_api.async_response
async def ws_raise(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict
) -> None:
    """Change the data, then raise."""
    _change(hass, msg)
    raise HomeAssistantError("boom")


@websocket_api.websocket_command(_cmd(CMD_SILENT))
@websocket_api.require_admin
@callback
def ws_silent(hass: HomeAssistant, connection: ActiveConnection, msg: dict) -> None:
    """Change the data and never answer."""
    _change(hass, msg)


@websocket_api.websocket_command(_cmd(CMD_DICT))
@websocket_api.require_admin
@callback
def ws_dict(hass: HomeAssistant, connection: ActiveConnection, msg: dict) -> None:
    """Answer with `send_message(result_message(...))`, as `logger` does."""
    _change(hass, msg)
    connection.send_message(websocket_api.messages.result_message(msg["id"]))


@websocket_api.websocket_command(_cmd(CMD_BYTES))
@websocket_api.require_admin
@callback
def ws_bytes(hass: HomeAssistant, connection: ActiveConnection, msg: dict) -> None:
    """Answer with an event, then a result already encoded as JSON bytes."""
    _change(hass, msg)
    connection.send_message(websocket_api.event_message(msg["id"], {"step": 1}))
    connection.send_message(json_bytes(websocket_api.result_message(msg["id"])))


@websocket_api.websocket_command(_cmd(CMD_DICT_ERROR))
@websocket_api.require_admin
@callback
def ws_dict_error(hass: HomeAssistant, connection: ActiveConnection, msg: dict) -> None:
    """Answer with `send_message(error_message(...))`."""
    _change(hass, msg)
    connection.send_message(
        websocket_api.messages.error_message(msg["id"], "nope", "Not allowed")
    )


@websocket_api.websocket_command(_cmd(CMD_LATE_ERROR))
@websocket_api.require_admin
@websocket_api.async_response
async def ws_late_error(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict
) -> None:
    """Send an error, then (wrongly) a result: the error counts."""
    _change(hass, msg)
    connection.async_handle_exception(msg, HomeAssistantError("first"))
    connection.send_result(msg["id"])


@websocket_api.websocket_command(_cmd(CMD_ACTION))
@websocket_api.require_admin
@callback
def ws_action(hass: HomeAssistant, connection: ActiveConnection, msg: dict) -> None:
    """Succeed without changing any setting (an action)."""
    connection.send_result(msg["id"])


@websocket_api.websocket_command(_cmd(CMD_ACTION_BEFORE))
@websocket_api.require_admin
@callback
def ws_action_before(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict
) -> None:
    """Answer an action with an error (or never, by restarting)."""
    hass.data[f"{DATA}_seen_records"] = len(hass.data[f"{DATA}_submitted"])
    connection.send_error(msg["id"], "nope", "Restore failed")


HANDLERS = (
    ws_sync,
    ws_async,
    ws_error,
    ws_raise,
    ws_silent,
    ws_dict,
    ws_bytes,
    ws_dict_error,
    ws_late_error,
    ws_action,
    ws_action_before,
)


def _spec(command: str, **kwargs: Any) -> WsSpec:
    fields: dict[str, Any] = {
        "command": command,
        "component": FAKE,
        "kind": ChangeKind.LOGGER,
        "action": ChangeAction.UPDATED,
        "target_name": "Test area",
        "read": _read,
    }
    fields.update(kwargs)
    return WsSpec(**fields)


TEST_SPECS: tuple[WsSpec, ...] = (
    *(_spec(c) for c in ALL_COMMANDS if c not in (CMD_ACTION, CMD_ACTION_BEFORE)),
    _spec(
        CMD_ACTION,
        kind=ChangeKind.BACKUP,
        action=ChangeAction.REMOVED,
        read=request_params,
        always_record=True,
    ),
    _spec(
        CMD_ACTION_BEFORE,
        kind=ChangeKind.BACKUP,
        action=ChangeAction.RESTORE_REQUESTED,
        read=request_params,
        record_before=True,
    ),
)


async def _async_setup_fake(hass: HomeAssistant, config: Any) -> bool:
    hass.data[DATA] = {"level": "info", "mode": "a"}
    for handler in HANDLERS:
        websocket_api.async_register_command(hass, handler)
    return True


def _mock_fake(hass: HomeAssistant) -> None:
    mock_integration(hass, MockModule(FAKE, async_setup=_async_setup_fake))


@pytest.fixture(autouse=True)
def _test_specs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SystemSettingsCollector, "WS_SPECS", TEST_SPECS)
    # The services are tested in their own module.
    monkeypatch.setattr(SystemSettingsCollector, "SERVICE_SPECS", ())


@pytest.fixture
async def fake(hass: HomeAssistant) -> None:
    """Set up the fake component before Electric Fence."""
    _mock_fake(hass)
    assert await async_setup_component(hass, FAKE, {})


@pytest.fixture
async def fence(
    hass: HomeAssistant,
    fake: None,
    hass_admin_user: MockUser,
    setup_entry: MockConfigEntry,
) -> SystemSettingsCollector:
    """Return the installed system settings collector."""
    return _collector(hass)


def _collector(hass: HomeAssistant) -> SystemSettingsCollector:
    entry = hass.config_entries.async_loaded_entries(DOMAIN)[0]
    return next(
        c
        for c in entry.runtime_data.collectors
        if isinstance(c, SystemSettingsCollector)
    )


def _set_record_values(hass: HomeAssistant, value: bool) -> None:
    runtime = hass.config_entries.async_loaded_entries(DOMAIN)[0].runtime_data
    runtime.settings = replace(runtime.settings, record_values=value)


async def _rows(hass: HomeAssistant) -> list[ChangeRow]:
    await hass.async_block_till_done()
    await flush_worker(hass)
    return [
        r for r in change_rows(hass) if r.kind in (ChangeKind.LOGGER, ChangeKind.BACKUP)
    ]


async def _call(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    command: str,
    token: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    ws = await hass_ws_client(hass, token) if token else await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": command, **fields})
    response: dict[str, Any] = await ws.receive_json()
    while response["type"] == "event":
        response = await ws.receive_json()
    await ws.close()
    return response


def _by_admin(row: ChangeRow, admin: MockUser) -> bool:
    return (
        row.actor_user_id == admin.id
        and row.actor_name == admin.name
        and row.actor_ip == IP
    )


# Registration.


def test_collector_registered_after_attribution() -> None:
    """The collector is installed by setup, right after attribution."""
    assert SystemSettingsCollector.name == "system_settings"
    index = COLLECTOR_TYPES.index(SystemSettingsCollector)
    assert COLLECTOR_TYPES[index - 1] is AttributionCollector


def test_production_specs_are_the_areas() -> None:
    """The production specs are the area specs (tested in their own module)."""
    assert PRODUCTION_SPECS is AREA_SPECS
    assert PRODUCTION_SPECS


def test_ws_ip_is_public() -> None:
    """The WebSocket IP helper is public for this collector."""
    assert attribution.ws_ip is not None
    assert not hasattr(attribution, "_ws_ip")


# Recording.


@pytest.mark.parametrize("command", [CMD_SYNC, CMD_ASYNC, CMD_DICT, CMD_BYTES])
async def test_records_on_success_with_actor_and_ip(
    hass: HomeAssistant,
    fence: SystemSettingsCollector,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
    command: str,
) -> None:
    """A successful change is recorded with the admin, the IP and the names."""
    response = await _call(
        hass, hass_ws_client, command, values={"level": "debug", "new": 1}
    )

    assert response["success"] is True
    (row,) = await _rows(hass)
    assert row.kind is ChangeKind.LOGGER
    assert row.action is ChangeAction.UPDATED
    assert row.target_id is None
    assert row.target_name == "Test area"
    assert _by_admin(row, hass_admin_user)
    assert row.details == {"changed": ["level", "new"]}


async def test_records_values_when_option_on(
    hass: HomeAssistant,
    fence: SystemSettingsCollector,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
) -> None:
    """With `record_values` on, before and after values are recorded, redacted."""
    _set_record_values(hass, True)

    response = await _call(
        hass,
        hass_ws_client,
        CMD_ASYNC,
        values={"level": "debug", "password": "hunter2"},
    )

    assert response["success"] is True
    (row,) = await _rows(hass)
    assert _by_admin(row, hass_admin_user)
    assert row.details == {
        "changed": ["level", "password"],
        "values": {
            "level": {"before": "info", "after": "debug"},
            "password": {"hidden": True},
        },
    }


async def test_record_values_read_at_record_time(
    hass: HomeAssistant,
    fence: SystemSettingsCollector,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Turning the option off again drops values from new records at once."""
    _set_record_values(hass, True)
    await _call(hass, hass_ws_client, CMD_SYNC, values={"level": "debug"})
    _set_record_values(hass, False)
    await _call(hass, hass_ws_client, CMD_SYNC, values={"level": "warning"})

    first, second = await _rows(hass)
    assert "values" in first.details
    assert second.details == {"changed": ["level"]}


async def _expected_response(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    command: str,
    fields: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Return Core's own response, with Electric Fence's specs removed."""
    hass.data[DATA] = {"level": "info", "mode": "a"}
    handlers = hass.data[WEBSOCKET_API_DOMAIN]
    with monkeypatch.context() as m:
        m.setitem(
            handlers, command, (_unwrapped(handlers[command][0]), handlers[command][1])
        )
        return await _call(hass, hass_ws_client, command, **fields)


def _unwrapped(handler: Any) -> Any:
    while hasattr(handler, "__wrapped__") and handler not in HANDLERS:
        handler = handler.__wrapped__
    return handler


def _same_response(ours: dict[str, Any], core: dict[str, Any]) -> None:
    ours, core = dict(ours), dict(core)
    ours.pop("id")
    core.pop("id")
    assert ours == core


@pytest.mark.parametrize(
    ("command", "fields"),
    [
        (CMD_ERROR, {"values": {"level": "debug"}}),
        (CMD_RAISE, {"values": {"level": "debug"}}),
        (CMD_DICT_ERROR, {"values": {"level": "debug"}}),
        (CMD_LATE_ERROR, {"values": {"level": "debug"}}),
        (CMD_SYNC, {"values": {"level": "info"}}),
        (CMD_ASYNC, {}),
    ],
)
async def test_no_record_on_error_or_raise_or_no_change(
    hass: HomeAssistant,
    fence: SystemSettingsCollector,
    hass_ws_client: WebSocketGenerator,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    fields: dict[str, Any],
) -> None:
    """An error, a raise or a no-op success records nothing; Core's reply is kept."""
    ours = await _call(hass, hass_ws_client, command, **fields)
    assert await _rows(hass) == []
    core = await _expected_response(hass, hass_ws_client, command, fields, monkeypatch)

    _same_response(ours, core)
    if command in (CMD_SYNC, CMD_ASYNC):
        assert ours["success"] is True
    else:
        assert ours["success"] is False
        assert ours["error"]["code"] in ("nope", "home_assistant_error")
    assert fence.guard.errors == 0


async def test_no_record_when_handler_never_answers(
    hass: HomeAssistant,
    fence: SystemSettingsCollector,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """A handler that changes the data but never answers records nothing."""
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": CMD_SILENT, "values": {"level": "debug"}})
    await ws.send_json_auto_id({"type": CMD_SYNC, "values": {"mode": "b"}})
    response = await ws.receive_json()
    assert response["id"] == 2

    (row,) = await _rows(hass)
    assert row.details == {"changed": ["mode"]}
    assert hass.data[DATA]["level"] == "debug"


async def test_non_admin_unauthorized_records_nothing(
    hass: HomeAssistant,
    fence: SystemSettingsCollector,
    hass_ws_client: WebSocketGenerator,
    hass_read_only_access_token: str,
) -> None:
    """`require_admin` still rejects a non-admin, with Core's own error."""
    response = await _call(
        hass,
        hass_ws_client,
        CMD_SYNC,
        hass_read_only_access_token,
        values={"level": "debug"},
    )

    assert response["success"] is False
    assert response["error"]["code"] == "unauthorized"
    assert hass.data[DATA]["level"] == "info"
    assert await _rows(hass) == []


async def test_record_before_non_admin_records_nothing(
    hass: HomeAssistant,
    fence: SystemSettingsCollector,
    hass_ws_client: WebSocketGenerator,
    hass_read_only_access_token: str,
) -> None:
    """A non-admin's rejected record-before command is not recorded."""
    response = await _call(
        hass,
        hass_ws_client,
        CMD_ACTION_BEFORE,
        hass_read_only_access_token,
        target="b1",
    )

    assert response["success"] is False
    assert response["error"]["code"] == "unauthorized"
    assert f"{DATA}_seen_records" not in hass.data
    assert await _rows(hass) == []


async def test_action_spec_records_params_on_success(
    hass: HomeAssistant,
    fence: SystemSettingsCollector,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
) -> None:
    """An always-record spec records the request parameters, not a diff."""
    response = await _call(
        hass, hass_ws_client, CMD_ACTION, target="abc123", password="secret"
    )
    _set_record_values(hass, True)
    await _call(hass, hass_ws_client, CMD_ACTION, target="abc123", password="secret")

    assert response["success"] is True
    off, on = await _rows(hass)
    assert off.kind is ChangeKind.BACKUP
    assert off.action is ChangeAction.REMOVED
    assert _by_admin(off, hass_admin_user)
    assert off.details == {"changed": ["password", "target"]}
    assert on.details == {
        "changed": ["password", "target"],
        "values": {
            "password": {"hidden": True},
            "target": {"before": None, "after": "abc123"},
        },
    }


async def test_action_spec_records_nothing_on_error(
    hass: HomeAssistant,
    fake: None,
    hass_admin_user: MockUser,
    hass_ws_client: WebSocketGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An always-record spec still waits for Core's result."""
    monkeypatch.setattr(
        SystemSettingsCollector,
        "WS_SPECS",
        (_spec(CMD_ERROR, read=request_params, always_record=True),),
    )
    await _setup_fence(hass, monkeypatch)

    response = await _call(hass, hass_ws_client, CMD_ERROR, target="x")

    assert response["success"] is False
    assert await _rows(hass) == []


async def test_record_before_spec_records_even_when_handler_fails(
    hass: HomeAssistant,
    fence: SystemSettingsCollector,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A record-before spec records before Core's handler runs, whatever it does."""
    hass.data[f"{DATA}_submitted"] = submitted = []
    runtime = hass.config_entries.async_loaded_entries(DOMAIN)[0].runtime_data
    real_submit = runtime.worker.submit

    def _submit(record: Any) -> bool:
        submitted.append(record)
        return real_submit(record)

    monkeypatch.setattr(runtime.worker, "submit", _submit)

    response = await _call(
        hass, hass_ws_client, CMD_ACTION_BEFORE, target="b1", nothing=None
    )

    assert response["success"] is False
    assert response["error"]["code"] == "nope"
    assert hass.data[f"{DATA}_seen_records"] == 1
    (row,) = await _rows(hass)
    assert row.kind is ChangeKind.BACKUP
    assert row.action is ChangeAction.RESTORE_REQUESTED
    assert _by_admin(row, hass_admin_user)
    # A parameter given as None is not a parameter given.
    assert row.details == {"changed": ["target"]}


async def test_read_none_records_nothing(
    hass: HomeAssistant,
    fake: None,
    hass_admin_user: MockUser,
    hass_ws_client: WebSocketGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reader returning None (area not available) records nothing."""
    monkeypatch.setattr(
        SystemSettingsCollector,
        "WS_SPECS",
        (
            _spec(CMD_SYNC, read=lambda hass, msg: None),
            _spec(CMD_ACTION, read=lambda hass, msg: None, always_record=True),
            _spec(CMD_ACTION_BEFORE, read=lambda hass, msg: None, record_before=True),
        ),
    )
    await _setup_fence(hass, monkeypatch)
    hass.data[f"{DATA}_submitted"] = []

    for command in (CMD_SYNC, CMD_ACTION, CMD_ACTION_BEFORE):
        await _call(hass, hass_ws_client, command, values={"level": "x"})

    assert await _rows(hass) == []
    assert _collector(hass).guard.errors == 0


async def test_after_read_none_records_nothing(
    hass: HomeAssistant,
    fake: None,
    hass_admin_user: MockUser,
    hass_ws_client: WebSocketGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The area going away during the command records nothing."""
    reads: list[int] = []

    def _gone_after(hass: HomeAssistant, msg: dict[str, Any]) -> Mapping[str, Any]:
        reads.append(1)
        return {"level": "info"} if len(reads) == 1 else None  # type: ignore[return-value]

    monkeypatch.setattr(
        SystemSettingsCollector, "WS_SPECS", (_spec(CMD_SYNC, read=_gone_after),)
    )
    await _setup_fence(hass, monkeypatch)

    response = await _call(hass, hass_ws_client, CMD_SYNC, values={"level": "x"})

    assert response["success"] is True
    assert len(reads) == 2
    assert await _rows(hass) == []


async def _setup_fence(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> MockConfigEntry:
    """Set up Electric Fence with the specs patched in by the test."""
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", MIN_HA_VERSION)
    entry = MockConfigEntry(domain=DOMAIN, options=Settings().to_options())
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_guard_failure_passes_through(
    hass: HomeAssistant,
    fence: SystemSettingsCollector,
    hass_ws_client: WebSocketGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reader that raises: Core's result is unchanged and nothing is recorded."""

    def _boom(hass: HomeAssistant, msg: dict[str, Any]) -> Mapping[str, Any]:
        raise RuntimeError("reader broke")

    handlers = hass.data[WEBSOCKET_API_DOMAIN]
    _wrapper, schema = handlers[CMD_ASYNC]
    handlers[CMD_ASYNC] = (
        fence._wrap_ws(_spec(CMD_ASYNC, read=_boom))(ws_async),
        schema,
    )

    response = await _call(hass, hass_ws_client, CMD_ASYNC, values={"level": "debug"})

    assert response["success"] is True
    assert response["result"] == {"done": "async"}
    assert hass.data[DATA]["level"] == "debug"
    assert await _rows(hass) == []
    assert fence.guard.errors == 1


async def test_guard_failure_after_success_passes_through(
    hass: HomeAssistant,
    fence: SystemSettingsCollector,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """A reader that raises after the change: the result still reaches the client."""
    calls = 0

    def _second_fails(hass: HomeAssistant, msg: dict[str, Any]) -> Mapping[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("reader broke")
        return {}

    handlers = hass.data[WEBSOCKET_API_DOMAIN]
    _wrapper, schema = handlers[CMD_SYNC]
    handlers[CMD_SYNC] = (
        fence._wrap_ws(_spec(CMD_SYNC, read=_second_fails))(ws_sync),
        schema,
    )

    response = await _call(hass, hass_ws_client, CMD_SYNC, values={"level": "debug"})

    assert response["success"] is True
    assert response["result"] == {"done": "sync"}
    assert await _rows(hass) == []
    assert fence.guard.errors == 1


async def test_handler_exception_propagates_unchanged(
    hass: HomeAssistant, fence: SystemSettingsCollector
) -> None:
    """The wrapper calls Core's handler once and lets its exception through."""
    calls: list[Any] = []
    error = ValueError("core broke")

    def _raising(hass: HomeAssistant, connection: Any, msg: dict) -> None:
        calls.append(connection)
        raise error

    wrapped = fence._wrap_ws(_spec(CMD_SYNC))(_raising)
    connection = _fake_connection(hass)

    with pytest.raises(ValueError) as info:
        wrapped(hass, connection, {"id": 1, "type": CMD_SYNC})

    assert info.value is error
    assert len(calls) == 1
    assert isinstance(calls[0], ResultWatch)


async def test_handler_return_value_returned(
    hass: HomeAssistant, fence: SystemSettingsCollector
) -> None:
    """Whatever Core's handler returns, the wrapper returns."""
    sentinel = object()
    wrapped = fence._wrap_ws(_spec(CMD_SYNC))(lambda hass, conn, msg: sentinel)
    assert wrapped(hass, _fake_connection(hass), {"id": 1}) is sentinel


async def test_no_user_records_system(
    hass: HomeAssistant, fence: SystemSettingsCollector
) -> None:
    """A connection without a user (never for admin commands) records System."""

    def _handler(hass: HomeAssistant, connection: Any, msg: dict) -> None:
        _change(hass, msg)
        connection.send_result(msg["id"])

    wrapped = fence._wrap_ws(_spec(CMD_SYNC))(_handler)
    connection = _fake_connection(hass, user=None)
    wrapped(hass, connection, {"id": 1, "type": CMD_SYNC, "values": {"mode": "z"}})

    (row,) = await _rows(hass)
    assert row.actor_user_id is None
    assert row.actor_name is None
    assert row.actor_ip == "10.1.2.3"
    connection.send_result.assert_called_once_with(1, None)


# Wrapping and unwrapping.


async def test_wraps_commands_of_components_loaded_later(
    hass: HomeAssistant,
    hass_admin_user: MockUser,
    setup_entry: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """A component loading after Electric Fence gets its commands wrapped."""
    collector = _collector(hass)
    assert collector._waiting == {FAKE}
    assert collector._loaded_unsub is not None

    _mock_fake(hass)
    assert await async_setup_component(hass, FAKE, {})

    assert collector._waiting == set()
    assert collector._loaded_unsub is None
    assert collector.disabled_reason == []
    handlers = hass.data[WEBSOCKET_API_DOMAIN]
    for command, handler in zip(ALL_COMMANDS, HANDLERS, strict=True):
        assert handlers[command][0].__wrapped__ is handler
    response = await _call(hass, hass_ws_client, CMD_SYNC, values={"mode": "b"})
    assert response["success"] is True
    (row,) = await _rows(hass)
    assert _by_admin(row, hass_admin_user)


async def test_unrelated_component_loading_is_ignored(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Only the awaited components trigger wrapping."""
    collector = _collector(hass)
    mock_integration(hass, MockModule("ef_unrelated"))
    assert await async_setup_component(hass, "ef_unrelated", {})
    assert collector._waiting == {FAKE}


async def test_late_component_without_commands_disables(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """A component that loads without registering its commands fails the contract."""
    collector = _collector(hass)
    mock_integration(hass, MockModule(FAKE))
    assert await async_setup_component(hass, FAKE, {})

    assert not collector.is_installed
    assert collector.disabled_reason == [
        f"WebSocket commands not registered: {', '.join(ALL_COMMANDS)}"
    ]
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"collector_disabled_{collector.name}"
    )
    assert issue is not None


async def test_component_loading_after_uninstall_is_ignored(
    hass: HomeAssistant,
) -> None:
    """A late load for a collector no longer installed wraps nothing."""
    collector = SystemSettingsCollector(hass, Mock())
    assert collector.install()
    collector._installed = False

    _mock_fake(hass)
    assert await async_setup_component(hass, FAKE, {})

    assert hass.data[WEBSOCKET_API_DOMAIN][CMD_SYNC][0] is ws_sync
    assert collector._loaded_unsub is None


async def test_component_loaded_late_fails_open(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error while wrapping late is swallowed and counted."""
    collector = SystemSettingsCollector(hass, Mock())
    assert collector.install()

    def _boom() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(collector, "_wrap_ws_commands", _boom)
    _mock_fake(hass)
    assert await async_setup_component(hass, FAKE, {})

    assert collector.guard.errors == 1
    collector.uninstall()


async def test_uninstall_restores_handlers(
    hass: HomeAssistant, fence: SystemSettingsCollector, setup_entry: MockConfigEntry
) -> None:
    """Unloading puts Core's handlers and schemas back."""
    handlers = hass.data[WEBSOCKET_API_DOMAIN]
    wrapped = {c: handlers[c] for c in ALL_COMMANDS}
    for command, handler in zip(ALL_COMMANDS, HANDLERS, strict=True):
        assert wrapped[command][0].__wrapped__ is handler

    assert await hass.config_entries.async_unload(setup_entry.entry_id)

    for command, handler in zip(ALL_COMMANDS, HANDLERS, strict=True):
        assert handlers[command][0] is handler
        assert handlers[command][1] is wrapped[command][1]
    assert fence._wrapped == set()


async def test_uninstall_before_component_loads(hass: HomeAssistant) -> None:
    """Uninstalling while waiting removes the listener; nothing is wrapped later."""
    collector = SystemSettingsCollector(hass, Mock())
    assert collector.install()
    assert collector._loaded_unsub is not None

    collector.uninstall()

    assert collector._loaded_unsub is None
    _mock_fake(hass)
    assert await async_setup_component(hass, FAKE, {})
    assert hass.data[WEBSOCKET_API_DOMAIN][CMD_SYNC][0] is ws_sync


async def test_wrapping_is_order_independent(hass: HomeAssistant, fake: None) -> None:
    """Two wrappers of one command restore cleanly in either order."""
    handlers = hass.data[WEBSOCKET_API_DOMAIN]
    for order in ("inner_first", "outer_first"):
        inner = SystemSettingsCollector(hass, Mock())
        outer = SystemSettingsCollector(hass, Mock())
        assert inner.install()
        assert outer.install()
        assert handlers[CMD_SYNC][0].__wrapped__.__wrapped__ is ws_sync

        if order == "outer_first":
            outer.uninstall()
            inner.uninstall()
            assert handlers[CMD_SYNC][0] is ws_sync
        else:
            inner_wrapper = handlers[CMD_SYNC][0].__wrapped__
            inner.uninstall()
            outer.uninstall()
            # The inner wrapper stays, as a passthrough to Core's handler.
            assert handlers[CMD_SYNC][0] is inner_wrapper
            assert inner_wrapper.__wrapped__ is ws_sync
            handlers[CMD_SYNC] = (ws_sync, handlers[CMD_SYNC][1])


# Contract.


async def test_contract_passes_with_no_specs(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no specs, only the connection's methods are checked."""
    monkeypatch.setattr(SystemSettingsCollector, "WS_SPECS", ())
    assert CONTRACTS["system_settings_ws"].collector == "system_settings"
    assert CONTRACTS["system_settings_ws"].check(hass) is None


@pytest.mark.parametrize(
    "attr",
    [
        "user",
        "remote",
        "send_result",
        "send_error",
        "send_message",
        "async_handle_exception",
    ],
)
async def test_contract_fails_without_connection_attr(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, attr: str
) -> None:
    """The watch relies on these `ActiveConnection` members."""
    monkeypatch.delattr(ActiveConnection, attr)
    assert CONTRACTS["system_settings_ws"].check(hass) == (
        f"ActiveConnection.{attr} is missing"
    )


async def test_contract_fails_for_unregistered_command(
    hass: HomeAssistant, fake: None
) -> None:
    """A loaded component missing a command fails the contract."""
    assert CONTRACTS["system_settings_ws"].check(hass) is None
    del hass.data[WEBSOCKET_API_DOMAIN][CMD_SYNC]
    assert CONTRACTS["system_settings_ws"].check(hass) == (
        f"WebSocket commands not registered: {CMD_SYNC}"
    )


# The result watch.


def _fake_connection(hass: HomeAssistant, *, user: Any = "admin") -> Mock:
    connection = Mock(spec=ActiveConnection)
    connection.hass = hass
    connection.remote = "10.1.2.3"
    if user == "admin":
        connection.user = Mock(id="u1", name="Admin")
        connection.user.name = "Admin"
    else:
        connection.user = user
    return connection


def _watch(on_success: Any = None) -> tuple[ResultWatch, Mock, Mock]:
    connection = Mock(spec=ActiveConnection)
    on_success = on_success or Mock()
    return (
        ResultWatch(connection, 5, on_success, FailOpen("t")),
        connection,
        on_success,
    )


def test_watch_forwards_attributes() -> None:
    """Reads, writes and deletes go to the real connection."""
    watch, connection, _ = _watch()
    connection.user = "someone"
    assert watch.user == "someone"
    watch.can_coalesce = True
    assert connection.can_coalesce is True
    del watch.can_coalesce
    assert not hasattr(connection, "can_coalesce")
    assert repr(watch) == repr(connection)


def test_watch_passes_isinstance() -> None:
    """Code that type-checks the connection still sees an `ActiveConnection`."""
    watch, _, _ = _watch()
    assert isinstance(watch, ActiveConnection)


def test_watch_success_once_then_forwards() -> None:
    """`send_result` for our ID calls `on_success` once, before forwarding."""
    order: list[str] = []
    watch, connection, _ = _watch(lambda: order.append("success"))
    connection.send_result.side_effect = lambda *a: order.append("forward")

    watch.send_result(5, {"a": 1})
    watch.send_result(5)

    assert order == ["success", "forward", "forward"]
    assert connection.send_result.call_args_list[0].args == (5, {"a": 1})
    assert connection.send_result.call_args_list[1].args == (5, None)


def test_watch_ignores_other_ids() -> None:
    """Results, errors and messages for other IDs decide nothing."""
    watch, _connection, on_success = _watch()
    watch.send_result(6)
    watch.send_error(6, "x", "y")
    watch.async_handle_exception({"id": 6}, ValueError())
    watch.send_message({"id": 6, "type": "result", "success": False})
    watch.send_message(b"not json")
    watch.send_message("[1]")
    watch.send_message(lambda: "{}")  # type: ignore[arg-type]
    watch.send_result(5)
    on_success.assert_called_once_with()


@pytest.mark.parametrize(
    "fail",
    [
        lambda w: w.send_error(5, "x", "y", translation_key="k"),
        lambda w: w.async_handle_exception({"id": 5}, ValueError()),
        lambda w: w.send_message({"id": 5, "type": "result", "success": False}),
        lambda w: w.send_message(
            json_bytes({"id": 5, "type": "result", "success": False})
        ),
    ],
)
def test_watch_error_marks_failure(fail: Any) -> None:
    """An error for our ID means a later result is not a success."""
    watch, connection, on_success = _watch()
    fail(watch)
    watch.send_message({"id": 5, "type": "result", "success": True})
    watch.send_result(5)
    on_success.assert_not_called()
    connection.send_result.assert_called_once_with(5, None)


def test_watch_error_forwards_arguments() -> None:
    """Error calls reach the connection with every argument."""
    watch, connection, _ = _watch()
    err = ValueError()
    watch.send_error(5, "x", "y", translation_key="k")
    watch.async_handle_exception({"id": 5}, err)
    connection.send_error.assert_called_once_with(5, "x", "y", translation_key="k")
    connection.async_handle_exception.assert_called_once_with({"id": 5}, err)


def test_watch_success_failing_still_forwards() -> None:
    """`on_success` raising is caught; the result is still sent."""
    guard = FailOpen("t")
    connection = Mock(spec=ActiveConnection)
    watch = ResultWatch(connection, 5, Mock(side_effect=RuntimeError), guard)
    watch.send_result(5)
    connection.send_result.assert_called_once_with(5, None)
    assert guard.errors == 1


def test_watch_send_message_success_variants() -> None:
    """A result message sent as dict, str or bytes counts as success."""
    for message in (
        {"id": 5, "type": "result", "success": True},
        '{"id": 5, "type": "result", "success": true}',
        json_bytes({"id": 5, "type": "result", "success": True, "result": None}),
    ):
        watch, connection, on_success = _watch()
        watch.send_message({"id": 5, "type": "event", "event": {}})
        watch.send_message(message)  # type: ignore[arg-type]
        on_success.assert_called_once_with()
        assert connection.send_message.call_count == 2
