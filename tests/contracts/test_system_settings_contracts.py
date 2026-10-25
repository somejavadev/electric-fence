"""Contracts the system settings areas rely on (backup, HTTP, network, ...)."""

from collections.abc import Iterator
import logging
from typing import Any
from unittest.mock import patch

import probatio
import pytest

from custom_components.electric_fence.collectors.system_settings import (
    SystemSettingsCollector,
)
from custom_components.electric_fence.contracts import (
    CONTRACTS,
    _message_keys_reason,
    _schema_keys,
    check_contracts,
)
from homeassistant.components.analytics import DATA_COMPONENT
from homeassistant.components.backup.const import DATA_MANAGER
from homeassistant.components.http import config as http_config
from homeassistant.components.http.config import DATA_STORE
from homeassistant.components.network.network import DATA_NETWORK
from homeassistant.components.websocket_api import DOMAIN as WEBSOCKET_API_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

AREAS = ("backup", "http", "network", "analytics", "logger")


def _check(hass: HomeAssistant) -> str | None:
    return CONTRACTS["system_settings_ws"].check(hass)


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """Put back the logger class and the root level the logger component changes."""
    klass = logging.getLoggerClass()
    level = logging.getLogger().level
    yield
    logging.setLoggerClass(klass)
    logging.getLogger().setLevel(level)


@pytest.fixture
async def areas(hass: HomeAssistant) -> None:
    """Set up every area component, as Core's own tests do."""
    with (
        patch("homeassistant.components.backup.is_hassio", return_value=False),
        patch("homeassistant.components.backup.backup.is_hassio", return_value=False),
        patch("homeassistant.components.backup.services.is_hassio", return_value=False),
        patch("homeassistant.components.analytics.analytics.Analytics.send_analytics"),
    ):
        for domain in AREAS:
            assert await async_setup_component(hass, domain, {})
        await hass.async_block_till_done(wait_background_tasks=True)


async def test_area_commands_registered(hass: HomeAssistant, areas: None) -> None:
    """Each command is registered once its component loads; the contract passes."""
    handlers = hass.data[WEBSOCKET_API_DOMAIN]
    specs = SystemSettingsCollector.WS_SPECS
    assert {spec.component for spec in specs} == set(AREAS)
    for spec in specs:
        assert spec.component in hass.config.components
        assert spec.command in handlers, spec.command
    assert CONTRACTS["system_settings_ws"].collector == "system_settings"
    assert _check(hass) is None
    assert check_contracts(hass, "system_settings") == []


async def test_area_internals(hass: HomeAssistant, areas: None) -> None:
    """The private state the readers use is where they expect it."""
    assert callable(hass.data[DATA_MANAGER].config.data.to_dict)
    store = hass.data[DATA_STORE]
    assert isinstance(store.stable, dict)
    assert store.pending is None
    assert isinstance(hass.data[DATA_NETWORK].configured_adapters, list)
    assert isinstance(hass.data[DATA_COMPONENT].preferences, dict)


async def test_contract_skips_components_not_loaded(hass: HomeAssistant) -> None:
    """Areas whose component is not loaded are checked when it loads."""
    assert _check(hass) is None


async def test_contract_fails_for_unregistered_area_command(
    hass: HomeAssistant, areas: None
) -> None:
    """A renamed command of a loaded component fails the contract."""
    del hass.data[WEBSOCKET_API_DOMAIN]["analytics/preferences"]
    assert _check(hass) == "WebSocket commands not registered: analytics/preferences"


@pytest.mark.parametrize(
    ("key", "reason"),
    [
        (DATA_MANAGER, "backup: the backup manager is missing"),
        (DATA_STORE, "http: the HTTP config store is missing"),
        (DATA_NETWORK, "network: the network object is missing"),
        (DATA_COMPONENT, "analytics: the analytics object is missing"),
    ],
)
async def test_contract_fails_without_area_object(
    hass: HomeAssistant, areas: None, key: Any, reason: str
) -> None:
    """A component that keeps its state elsewhere fails the contract."""
    hass.data.pop(key)
    assert _check(hass) == reason


@pytest.mark.parametrize(
    ("key", "reason"),
    [
        (DATA_MANAGER, "backup: BackupManager.config.data.to_dict is missing"),
        (DATA_STORE, "http: HTTPConfigStore.stable is missing"),
        (DATA_NETWORK, "network: Network.configured_adapters is missing"),
        (DATA_COMPONENT, "analytics: Analytics.preferences is missing"),
    ],
)
async def test_contract_fails_without_area_member(
    hass: HomeAssistant, areas: None, key: Any, reason: str
) -> None:
    """A component object without the member the reader uses fails the contract."""
    hass.data[key] = object()
    assert _check(hass) == reason


async def test_contract_fails_without_http_pending(
    hass: HomeAssistant, areas: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The HTTP store must have both slots."""
    monkeypatch.delattr(http_config.HTTPConfigStore, "pending")
    assert _check(hass) == "http: HTTPConfigStore.pending is missing"


@pytest.mark.parametrize(
    "name", ["HTTP_CONFIG_CREATED_AT", "HTTP_CONFIG_ERROR", "HTTP_CONFIG_ERROR_MESSAGE"]
)
async def test_contract_fails_without_http_meta_keys(
    hass: HomeAssistant, areas: None, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """The HTTP config's metadata keys are stripped by name."""
    monkeypatch.delattr(http_config, name)
    assert _check(hass) == f"http: config.{name} is missing"


# Message fields the readers use.

_KEYED = [s for s in SystemSettingsCollector.WS_SPECS if s.message_keys]


def test_keyed_commands() -> None:
    """The commands whose readers use message fields declare them."""
    assert {s.command: s.message_keys for s in _KEYED} == {
        "backup/delete": ("backup_id",),
        "backup/restore": (
            "backup_id",
            "agent_id",
            "restore_database",
            "restore_folders",
            "restore_homeassistant",
            "restore_addons",
        ),
        "http/config/configure": ("config",),
        "logger/log_level": ("module",),
        "logger/integration_log_level": ("integration",),
    }


@pytest.mark.parametrize("spec", _KEYED, ids=lambda s: s.command)
async def test_message_keys_in_core_schema(
    hass: HomeAssistant, areas: None, spec: Any
) -> None:
    """Core's registered schema has every field the reader uses."""
    keys = _schema_keys(hass.data[WEBSOCKET_API_DOMAIN][spec.command][1])
    assert keys is not None
    assert set(spec.message_keys) <= keys
    assert (
        _message_keys_reason(spec, hass.data[WEBSOCKET_API_DOMAIN][spec.command][1])
        is None
    )
    assert _check(hass) is None


@pytest.mark.parametrize(
    ("spec", "key"),
    [(s, k) for s in _KEYED for k in s.message_keys],
    ids=lambda v: getattr(v, "command", v),
)
async def test_contract_fails_for_renamed_message_field(
    hass: HomeAssistant, areas: None, spec: Any, key: str
) -> None:
    """A field renamed in Core's schema fails the contract with its name."""
    handlers = hass.data[WEBSOCKET_API_DOMAIN]
    handler, schema = handlers[spec.command]
    handlers[spec.command] = (
        handler,
        probatio.Schema({k: v for k, v in schema.schema.items() if str(k) != key}),
    )
    assert _check(hass) == f"{spec.command}: message fields missing: {key}"


async def test_contract_fails_for_schema_without_names(
    hass: HomeAssistant, areas: None
) -> None:
    """A schema whose fields can't be read fails the contract."""
    handlers = hass.data[WEBSOCKET_API_DOMAIN]
    handlers["logger/log_level"] = (handlers["logger/log_level"][0], object())
    assert _check(hass) == "logger/log_level: the message schema has no field names"


class _NoValidators:
    validators: list[Any] = []


def test_schema_keys_shapes() -> None:
    """Core's three schema shapes, and shapes without names."""
    fields = probatio.Schema({probatio.Required("module"): str})
    assert _schema_keys(False) == set()
    assert _schema_keys(fields) == {"module"}
    assert _schema_keys(probatio.All(fields, lambda v: v)) == {"module"}
    assert _schema_keys(_NoValidators()) is None
    assert _schema_keys(object()) is None
