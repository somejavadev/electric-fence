"""Contracts the app collector relies on, against Core's real Supervisor code.

Core's `supervisor/api` and `hassio/update/addon` commands, app services
and `update` component are registered for real; only the Supervisor
client is a stand-in.
"""

import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import probatio
import pytest

from custom_components.electric_fence.collectors.apps import (
    APP_UPDATE_SUFFIX,
    app_slug_from_unique_id,
)
from custom_components.electric_fence.contracts import CONTRACTS, check_contracts
from homeassistant.components.hassio import (
    entity as hassio_entity,
    http as hassio_http,
    update as hassio_update,
)
from homeassistant.components.websocket_api import DOMAIN as WEBSOCKET_API_DOMAIN
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.setup import async_setup_component
from tests.helpers import setup_fake_hassio, supervisor_client

NAMES = (
    "supervisor_ws_api",
    "hassio_update_addon_ws",
    "hassio_addon_services",
    "hassio_update_install",
    "supervisor_client",
)


def _check(hass: HomeAssistant, name: str) -> str | None:
    return CONTRACTS[name].check(hass)


@pytest.fixture
async def supervisor(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """Core's Supervisor commands, app services and the update component."""
    await setup_fake_hassio(hass, supervisor_client(), monkeypatch)
    assert await async_setup_component(hass, "update", {})


async def test_contracts_pass_against_core(
    hass: HomeAssistant, supervisor: None
) -> None:
    """Every contract passes against the installed Core."""
    for name in NAMES:
        assert CONTRACTS[name].collector == "apps", name
        assert _check(hass, name) is None, name
    assert check_contracts(hass, "apps") == []
    assert CONTRACTS["hassio_rest_proxy"].collector == "apps_rest_proxy"
    assert check_contracts(hass, "apps_rest_proxy") == []


async def test_update_contract_waits_for_update(hass: HomeAssistant) -> None:
    """Before `update` loads, its contract has nothing to check."""
    assert "update" not in hass.config.components
    assert _check(hass, "hassio_update_install") is None


def test_app_update_entity_unique_id() -> None:
    """Core's app update entity has the unique ID the collector parses."""
    addon = SimpleNamespace(addon=SimpleNamespace(slug="a0d7b954_vscode"))
    entity = hassio_update.SupervisorAddonUpdateEntity(
        coordinator=Mock(),
        entity_description=hassio_update.ENTITY_DESCRIPTION,
        addon=addon,  # type: ignore[arg-type]
    )
    assert entity.unique_id == f"a0d7b954_vscode{APP_UPDATE_SUFFIX}"
    assert app_slug_from_unique_id(entity.unique_id) == "a0d7b954_vscode"
    core = hassio_update.SupervisorCoreUpdateEntity(
        coordinator=Mock(), entity_description=hassio_update.ENTITY_DESCRIPTION
    )
    assert core.unique_id is not None
    assert app_slug_from_unique_id(core.unique_id) is None


def _replace_command(hass: HomeAssistant, command: str, schema: Any) -> None:
    handlers = hass.data[WEBSOCKET_API_DOMAIN]
    handlers[command] = (handlers[command][0], schema)


@pytest.mark.parametrize(
    ("name", "command", "fields", "reason"),
    [
        (
            "supervisor_ws_api",
            "supervisor/api",
            {"type": str, "endpoint": str, "method": str},
            "supervisor/api: message fields missing: data",
        ),
        (
            "hassio_update_addon_ws",
            "hassio/update/addon",
            {"type": str, "backup": bool},
            "hassio/update/addon: message fields missing: addon",
        ),
    ],
)
async def test_ws_contract_fails_on_wrong_schema(
    hass: HomeAssistant,
    supervisor: None,
    name: str,
    command: str,
    fields: dict[str, Any],
    reason: str,
) -> None:
    """A command whose schema lost a field fails with that field named."""
    _replace_command(
        hass,
        command,
        probatio.Schema({probatio.Required(k): v for k, v in fields.items()}),
    )
    assert _check(hass, name) == reason


@pytest.mark.parametrize(
    ("name", "command"),
    [
        ("supervisor_ws_api", "supervisor/api"),
        ("hassio_update_addon_ws", "hassio/update/addon"),
    ],
)
async def test_ws_contract_fails_when_missing(
    hass: HomeAssistant, supervisor: None, name: str, command: str
) -> None:
    """A command that is no longer registered fails the contract."""
    hass.data[WEBSOCKET_API_DOMAIN].pop(command)
    assert _check(hass, name) == f"WebSocket command not registered: {command}"


async def test_services_contract_fails_on_wrong_shape(
    hass: HomeAssistant, supervisor: None
) -> None:
    """A missing service, or one without its slug field, fails the contract."""

    async def handler(call: ServiceCall) -> None:
        """Stand in for Core's handler."""

    hass.services.async_register(
        "hassio", "addon_stop", handler, schema=probatio.Schema({"slug": str})
    )
    assert (
        _check(hass, "hassio_addon_services")
        == "hassio.addon_stop: service fields missing: addon"
    )
    hass.services.async_remove("hassio", "app_start")
    assert (
        _check(hass, "hassio_addon_services")
        == "services not registered: hassio.app_start"
    )


async def test_update_install_contract_fails(
    hass: HomeAssistant, supervisor: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing `update.install`, or a new unique ID shape, fails."""
    monkeypatch.setattr(
        hassio_update,
        "ENTITY_DESCRIPTION",
        SimpleNamespace(key="latest"),
    )
    assert _check(hass, "hassio_update_install") == (
        "hassio update entity key is not 'version_latest'"
    )
    monkeypatch.undo()
    monkeypatch.setattr(
        hassio_entity.HassioAddonEntity, "__init__", lambda self, *a, **k: None
    )
    assert _check(hass, "hassio_update_install") == (
        "HassioAddonEntity no longer builds its unique ID from slug and key"
    )
    monkeypatch.undo()
    hass.services.async_remove("update", "install")
    assert _check(hass, "hassio_update_install") == "update.install is not registered"


async def test_supervisor_client_contract_fails(
    hass: HomeAssistant, supervisor: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client without the reads the collector makes fails."""
    from aiohasupervisor.addons import AddonsClient  # noqa: PLC0415

    monkeypatch.delattr(AddonsClient, "addon_info")
    assert _check(hass, "supervisor_client") == (
        "aiohasupervisor AddonsClient.addon_info is missing"
    )


async def test_rest_proxy_contract_fails_when_open(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If Core's proxy lets admins reach app actions, the contract fails."""
    hass.config.components.add("hassio")
    monkeypatch.setattr(
        hassio_http, "PATHS_ADMIN", re.compile(r"^addons/[^/]+/restart$")
    )
    reason = _check(hass, "hassio_rest_proxy")
    assert reason == (
        "the /api/hassio proxy now lets admins reach app changes: addons/x/restart"
    )


async def test_contracts_pass_off_supervisor(hass: HomeAssistant) -> None:
    """Without Supervisor the app contracts don't apply, and import nothing."""
    for name in (*NAMES, "hassio_rest_proxy"):
        assert _check(hass, name) is None, name
