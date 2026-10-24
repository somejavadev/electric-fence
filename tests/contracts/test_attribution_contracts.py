"""Contracts the attribution collector relies on (H9)."""

import asyncio
from http import HTTPStatus
from typing import Any

from aiohttp.web_protocol import RequestHandler
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import (
    ClientSessionGenerator,
    WebSocketGenerator,
)

from custom_components.electric_fence.attribution import (
    ADMIN_WS_COMMANDS,
    HANDLE_REQUEST_QUALNAME,
    WS_TASK_PREFIX,
)
from custom_components.electric_fence.contracts import CONTRACTS, check_contracts
from homeassistant.components.websocket_api import (
    DOMAIN as WEBSOCKET_API_DOMAIN,
    ActiveConnection,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.http import current_request
from homeassistant.setup import async_setup_component

_NAMES = (
    "flow_managers",
    "config_entries_remove",
    "request_handler_task",
    "ws_connection",
    "admin_ws_commands",
)


def _check(hass: HomeAssistant, name: str) -> str | None:
    return CONTRACTS[name].check(hass)


async def test_attribution_contracts_pass(hass: HomeAssistant) -> None:
    """Core matches every attribution contract, with `config` loaded."""
    assert await async_setup_component(hass, "config", {})
    for name in _NAMES:
        assert CONTRACTS[name].collector == "attribution"
        assert _check(hass, name) is None, name
    assert check_contracts(hass, "attribution") == []


async def test_admin_ws_commands_contract_waits_for_config(hass: HomeAssistant) -> None:
    """Before `config` loads, its commands can't be checked yet."""
    assert _check(hass, "admin_ws_commands") is None


async def test_admin_ws_commands_contract_names_missing_command(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing admin command fails the contract and is named."""
    assert await async_setup_component(hass, "config", {})
    monkeypatch.delitem(hass.data[WEBSOCKET_API_DOMAIN], "config/core/update")

    reason = _check(hass, "admin_ws_commands")

    assert reason is not None
    assert "config/core/update" in reason


async def _init_without_context(handler: Any, data: Any = None) -> None:
    """Async init without the keyword-only `context`."""


async def _configure_renamed(flow: str, user_input: Any = None) -> None:
    """Async configure with a renamed first parameter."""


async def _get_async(flow_id: str) -> None:
    """Coroutine lookup."""


@pytest.mark.parametrize("manager", ["flow", "options", "subentries"])
@pytest.mark.parametrize(
    ("attr", "replacement", "expected"),
    [
        ("async_init", _init_without_context, "async_init"),
        ("async_configure", _configure_renamed, "async_configure"),
        ("async_get", _get_async, "async_get is a coroutine function"),
        ("async_get", None, "async_get is missing"),
    ],
)
async def test_flow_managers_contract_fails_when_changed(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    manager: str,
    attr: str,
    replacement: Any,
    expected: str,
) -> None:
    """A changed flow manager method fails the contract, naming the manager."""
    monkeypatch.setattr(getattr(hass.config_entries, manager), attr, replacement)

    reason = _check(hass, "flow_managers")

    assert reason is not None
    assert f"hass.config_entries.{manager}.{expected}" in reason


async def test_flow_managers_contract_fails_without_manager(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing flow manager is reported."""
    monkeypatch.setattr(hass.config_entries, "subentries", None)

    reason = _check(hass, "flow_managers")

    assert reason == "hass.config_entries.subentries is missing"


async def _remove_renamed(entry: str) -> None:
    """Take a renamed parameter."""


def _remove_sync(entry_id: str) -> None:
    """Not a coroutine function."""


@pytest.mark.parametrize("replacement", [_remove_renamed, _remove_sync, None])
async def test_config_entries_remove_contract_fails_when_changed(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, replacement: Any
) -> None:
    """A changed `async_remove` fails the contract."""
    monkeypatch.setattr(hass.config_entries, "async_remove", replacement)

    reason = _check(hass, "config_entries_remove")

    assert reason is not None
    assert "hass.config_entries.async_remove" in reason


def _sync_handle_request() -> None:
    """Not a coroutine function."""


async def _renamed_handle_request() -> None:
    """Coroutine with another qualified name."""


@pytest.mark.parametrize(
    "replacement", [_sync_handle_request, _renamed_handle_request, None]
)
async def test_request_handler_task_contract_fails_when_changed(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, replacement: Any
) -> None:
    """A renamed aiohttp `_handle_request` fails the contract."""
    monkeypatch.setattr(RequestHandler, "_handle_request", replacement)

    reason = _check(hass, "request_handler_task")

    assert reason is not None
    assert HANDLE_REQUEST_QUALNAME in reason


@pytest.mark.parametrize("attr", ["user", "remote"])
async def test_ws_connection_contract_fails_without_attribute(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, attr: str
) -> None:
    """`ActiveConnection` must keep `user` and `remote`."""
    monkeypatch.delattr(ActiveConnection, attr)

    reason = _check(hass, "ws_connection")

    assert reason == f"ActiveConnection.{attr} is missing"


# Behaviour Electric Fence depends on (not checkable at runtime).


async def test_delete_view_removes_in_named_task_with_request(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """The entry delete view runs `async_remove` in its own named task.

    That task still carries the DELETE request, so the delete wrapper can
    accept it in place of the `_handle_request` task.
    """
    assert await async_setup_component(hass, "config", {})
    entry = MockConfigEntry(domain="ef_contract_delete")
    entry.add_to_hass(hass)
    seen: dict[str, Any] = {}
    original = hass.config_entries.async_remove

    async def _recording(entry_id: str) -> Any:
        task = asyncio.current_task()
        assert task is not None
        request = current_request.get()
        seen["name"] = task.get_name()
        seen["method"] = request.method if request else None
        seen["path"] = request.path if request else None
        return await original(entry_id)

    hass.config_entries.async_remove = _recording  # type: ignore[method-assign]
    client = await hass_client()

    resp = await client.delete(f"/api/config/config_entries/entry/{entry.entry_id}")

    assert resp.status == HTTPStatus.OK
    assert seen == {
        "name": f"config entry remove {entry.entry_id}",
        "method": "DELETE",
        "path": f"/api/config/config_entries/entry/{entry.entry_id}",
    }


async def test_admin_ws_commands_run_in_named_async_tasks(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """`async_response` handlers run in tasks named `websocket_api.async:...`."""
    assert await async_setup_component(hass, "config", {})
    seen: list[str] = []
    original = hass.config.async_update

    async def _recording(**kwargs: Any) -> None:
        task = asyncio.current_task()
        assert task is not None
        seen.append(task.get_name())
        await original(**kwargs)

    hass.config.async_update = _recording  # type: ignore[method-assign]
    ws = await hass_ws_client(hass)

    await ws.send_json_auto_id({"type": "config/core/update", "location_name": "x"})
    assert (await ws.receive_json())["success"]

    assert seen == [f"{WS_TASK_PREFIX}websocket_update_config"]
    assert "config/core/update" in ADMIN_WS_COMMANDS
