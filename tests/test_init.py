"""Tests for Electric Fence setup and unload."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.electric_fence.collectors.base import BaseCollector
from custom_components.electric_fence.const import DOMAIN, MIN_HA_VERSION
from custom_components.electric_fence.geo.enrich import GeoEnricher
from custom_components.electric_fence.runtime import Runtime
from custom_components.electric_fence.settings import Settings
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant


async def test_setup_and_unload(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """The entry loads and unloads cleanly."""
    assert setup_entry.state is ConfigEntryState.LOADED
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    assert setup_entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_and_unload_return_none(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setup and unload return None, as HA 2026.11 expects (no bool)."""
    report_usage = Mock()
    monkeypatch.setattr(config_entries, "report_usage", report_usage)
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", MIN_HA_VERSION)
    entry = MockConfigEntry(domain=DOMAIN, options=Settings().to_options())
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    assert await hass.config_entries.async_unload(entry.entry_id)

    assert entry.state is ConfigEntryState.NOT_LOADED
    report_usage.assert_not_called()


async def test_refuses_old_home_assistant(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Setup is refused with a log error on an older Home Assistant."""
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", "2026.10.3")
    entry = MockConfigEntry(domain=DOMAIN, options=Settings().to_options())
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert "requires Home Assistant 2026.11.0" in caplog.text
    # Raised as ConfigEntryError, not returned as False.
    assert "returns a boolean" not in caplog.text
    assert entry.reason is not None
    assert "requires Home Assistant 2026.11.0" in entry.reason


async def test_failed_setup_rolls_back(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure partway through setup rolls back what was started.

    The worker stops, the store closes, and installed collectors are removed.
    """
    events: list[str] = []
    tasks: list[asyncio.Task[Any]] = []
    original = hass.async_create_background_task

    def spy(target: Any, name: str, eager_start: bool = True) -> asyncio.Task[Any]:
        task = original(target, name, eager_start)
        tasks.append(task)
        return task

    monkeypatch.setattr(hass, "async_create_background_task", spy)

    class Good(BaseCollector):
        name = "good"

        def _install(self) -> None:
            events.append("install good")

        def _uninstall(self) -> None:
            events.append("uninstall good")

    class Broken(BaseCollector):
        name = "broken"

        def __init__(self, hass: HomeAssistant, runtime: Runtime) -> None:
            raise RuntimeError("constructor bug")

        def _install(self) -> None:
            pass

        def _uninstall(self) -> None:
            pass

    monkeypatch.setattr(
        "custom_components.electric_fence.COLLECTOR_TYPES", [Good, Broken]
    )
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", MIN_HA_VERSION)
    entry = MockConfigEntry(domain=DOMAIN, options=Settings().to_options())
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert events == ["install good", "uninstall good"]
    assert [t.get_name() for t in tasks] == ["electric_fence worker"]
    assert all(t.done() for t in tasks)
    with pytest.raises(RuntimeError, match="not open"):
        entry.runtime_data.store.stats()


async def test_geo_wired_and_started(
    hass: HomeAssistant, setup_entry: MockConfigEntry, geo_start: AsyncMock
) -> None:
    """Setup uses the real enricher and starts the GeoIP updater."""
    runtime = setup_entry.runtime_data
    assert isinstance(runtime.enricher, GeoEnricher)
    assert runtime.enricher.runtime is runtime
    assert runtime.geo_updater is not None
    assert runtime.geo_status is not None
    assert runtime.geo_status.provider == "dbip_lite"
    geo_start.assert_awaited_once()
    assert await hass.config_entries.async_unload(setup_entry.entry_id)


async def test_geo_start_waits_for_started(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, geo_start: AsyncMock
) -> None:
    """While Home Assistant is still starting, the updater starts on STARTED."""
    hass.set_state(CoreState.starting)
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", MIN_HA_VERSION)
    entry = MockConfigEntry(domain=DOMAIN, options=Settings().to_options())
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    geo_start.assert_not_awaited()
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    geo_start.assert_awaited_once()
