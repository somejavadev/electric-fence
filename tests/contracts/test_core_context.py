"""Contracts: which task and context Core runs listeners and handlers in (spike 10.1).

These tests characterise Home Assistant Core, not Electric Fence.
"""

import asyncio
from contextvars import ContextVar
from http import HTTPStatus
from typing import Any

from aiohttp import web
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)
from pytest_homeassistant_custom_component.typing import (
    ClientSessionGenerator,
    WebSocketGenerator,
)

from homeassistant import config_entries
from homeassistant.auth import EVENT_USER_ADDED
from homeassistant.components import websocket_api
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED,
    ConfigEntry,
    ConfigEntryChange,
    ConfigFlowResult,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.setup import async_setup_component

# The test HTTP and WebSocket clients need real loopback sockets.
pytestmark = pytest.mark.usefixtures("socket_enabled")

TEST_DOMAIN = "ef_contract_test"
TEST_COMMAND = "ef_contract_test/context"

SENTINEL = object()
TEST_VAR: ContextVar[object | None] = ContextVar("ef_contract_test_var", default=None)


async def test_callback_listener_runs_in_firing_task(hass: HomeAssistant) -> None:
    """`@callback` dispatcher and bus listeners run in the task that fired them."""
    entry = MockConfigEntry(domain=TEST_DOMAIN, title="before")
    entry.add_to_hass(hass)
    recorded: dict[str, asyncio.Task[Any] | None] = {}

    @callback
    def _on_entry_changed(change: ConfigEntryChange, changed: ConfigEntry) -> None:
        if change is ConfigEntryChange.UPDATED and changed.title == "after":
            recorded["entry_changed"] = asyncio.current_task()

    @callback
    def _on_user_added(event: Event) -> None:
        recorded["user_added"] = asyncio.current_task()

    unsub_signal = async_dispatcher_connect(
        hass, SIGNAL_CONFIG_ENTRY_CHANGED, _on_entry_changed
    )
    unsub_bus = hass.bus.async_listen(EVENT_USER_ADDED, _on_user_added)

    async def _fire() -> None:
        hass.config_entries.async_update_entry(entry, title="after")
        await hass.auth.async_create_user("bob")

    firing_task = asyncio.create_task(_fire())
    await firing_task
    unsub_signal()
    unsub_bus()

    for recorded_task in (recorded["entry_changed"], recorded["user_added"]):
        assert recorded_task is firing_task


HANDLE_REQUEST_QUALNAME = "RequestHandler._handle_request"


async def _observe_view(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> dict[str, Any]:
    """Serve one request from a test view and record its tasks."""
    seen: dict[str, Any] = {}

    async def _spawned() -> None:
        seen["spawned_qualname"] = _coro_qualname(asyncio.current_task())

    class _TaskView(HomeAssistantView):
        url = "/api/ef_contract_test/task"
        name = "api:ef_contract_test:task"

        async def get(self, request: web.Request) -> web.Response:
            seen["request_task"] = request.task
            seen["current_task"] = asyncio.current_task()
            seen["current_qualname"] = _coro_qualname(seen["current_task"])
            await hass.async_create_task(_spawned())
            return self.json({})

    assert await async_setup_component(hass, "http", {})
    hass.http.register_view(_TaskView())
    client = await hass_client()

    resp = await client.get("/api/ef_contract_test/task")

    assert resp.status == HTTPStatus.OK
    return seen


def _coro_qualname(task: asyncio.Task[Any] | None) -> str | None:
    """Return the qualified name of a task's coroutine."""
    assert task is not None
    return getattr(task.get_coro(), "__qualname__", None)


async def test_request_task_is_connection_task_not_handler_task(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """`request.task` is aiohttp's connection task, not the view's task.

    aiohttp (web_protocol.py) builds the request with the connection's
    `RequestHandler.start()` task, then runs the handler in a new task.
    """
    seen = await _observe_view(hass, hass_client)

    assert seen["request_task"] is not seen["current_task"]


async def test_view_runs_in_aiohttp_handle_request_task(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """A view runs in aiohttp's per-request `_handle_request` task."""
    seen = await _observe_view(hass, hass_client)

    assert seen["current_qualname"] == HANDLE_REQUEST_QUALNAME


async def test_task_spawned_in_view_is_not_handle_request_task(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """A task created inside a view runs a different coroutine."""
    seen = await _observe_view(hass, hass_client)

    assert seen["spawned_qualname"] is not None
    assert seen["spawned_qualname"] != HANDLE_REQUEST_QUALNAME


async def test_async_response_ws_handler_runs_in_task_with_copied_context(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """An `@async_response` handler runs in its own task with a copied context.

    A value set around the `hass.data["websocket_api"]` entry, and reset as soon
    as the entry returns, is still seen by the handler's coroutine.
    """
    seen: dict[str, Any] = {}

    @websocket_api.websocket_command({"type": TEST_COMMAND})
    @websocket_api.async_response
    async def _handler(
        hass: HomeAssistant,
        connection: websocket_api.ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        await asyncio.sleep(0)
        seen["var"] = TEST_VAR.get()
        seen["task"] = asyncio.current_task()
        connection.send_result(msg["id"])

    assert await async_setup_component(hass, "websocket_api", {})
    websocket_api.async_register_command(hass, _handler)
    handlers = hass.data[websocket_api.DOMAIN]
    original, schema = handlers[TEST_COMMAND]

    @callback
    def _wrapper(
        hass: HomeAssistant,
        connection: websocket_api.ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        seen["reader_task"] = asyncio.current_task()
        token = TEST_VAR.set(SENTINEL)
        try:
            original(hass, connection, msg)
        finally:
            TEST_VAR.reset(token)

    handlers[TEST_COMMAND] = (_wrapper, schema)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": TEST_COMMAND})
    msg = await client.receive_json()

    assert msg["success"]
    assert seen["var"] is SENTINEL and seen["task"] is not seen["reader_task"]


async def _observe_flow_setup_update(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    *,
    domain_loaded: bool,
) -> dict[str, Any]:
    """Finish a config flow over HTTP and record the setup-time update's task.

    The new entry's `async_setup_entry` calls `async_update_entry`. A wrapper
    on `hass.config_entries.flow.async_init` records the request task, and a
    `SIGNAL_CONFIG_ENTRY_CHANGED` listener records the update's task.
    """
    seen: dict[str, Any] = {}

    async def _async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
        hass.config_entries.async_update_entry(entry, data={"touched": True})
        return True

    class _FlowHandler(config_entries.ConfigFlow):
        VERSION = 1

        async def async_step_user(
            self, user_input: dict[str, Any] | None = None
        ) -> ConfigFlowResult:
            return self.async_create_entry(title="Contract", data={})

    mock_integration(
        hass, MockModule(TEST_DOMAIN, async_setup_entry=_async_setup_entry)
    )
    mock_platform(hass, f"{TEST_DOMAIN}.config_flow", None)
    assert await async_setup_component(hass, "config", {})
    if domain_loaded:
        assert await async_setup_component(hass, TEST_DOMAIN, {})

    flow_mgr = hass.config_entries.flow
    original_init = flow_mgr.async_init

    async def _recording_init(*args: Any, **kwargs: Any) -> Any:
        seen["request_task"] = asyncio.current_task()
        return await original_init(*args, **kwargs)

    flow_mgr.async_init = _recording_init  # type: ignore[method-assign]

    @callback
    def _on_entry_changed(change: ConfigEntryChange, entry: ConfigEntry) -> None:
        if (
            change is ConfigEntryChange.UPDATED
            and entry.data.get("touched")
            and "update_task" not in seen
        ):
            seen["update_task"] = asyncio.current_task()

    unsub = async_dispatcher_connect(
        hass, SIGNAL_CONFIG_ENTRY_CHANGED, _on_entry_changed
    )
    client = await hass_client()

    with mock_config_flow(TEST_DOMAIN, _FlowHandler):
        resp = await client.post(
            "/api/config/config_entries/flow", json={"handler": TEST_DOMAIN}
        )
        assert resp.status == HTTPStatus.OK
        assert (await resp.json())["type"] == "create_entry"
    await hass.async_block_till_done()
    unsub()
    return seen


async def test_task_spawned_during_flow_runs_in_other_task(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """On the domain's first setup, the setup-time update runs in another task.

    `async_setup_component` sets up the entries in eager tasks (setup.py).
    """
    seen = await _observe_flow_setup_update(hass, hass_client, domain_loaded=False)

    update_task = seen["update_task"]
    request_task = seen["request_task"]
    assert update_task is not request_task


async def test_inline_setup_when_domain_loaded_runs_in_request_task(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """With the domain already loaded, the setup-time update runs inline.

    `ConfigEntries.async_setup` awaits `entry.async_setup` directly when the
    domain is in `hass.config.components` (config_entries.py), so the update
    happens in the flow request's own task.
    """
    seen = await _observe_flow_setup_update(hass, hass_client, domain_loaded=True)

    update_task = seen["update_task"]
    request_task = seen["request_task"]
    assert update_task is request_task
