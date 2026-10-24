"""Shared fixtures for Electric Fence tests."""

from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_config_flow,
    mock_platform,
)

from custom_components.electric_fence.const import DOMAIN, MIN_HA_VERSION
from custom_components.electric_fence.geo.updater import GeoUpdater
from custom_components.electric_fence.settings import Settings
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from tests.helpers import DUMMY, PROBE, DummyFlow, mock_dummy


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading of custom integrations in every test."""


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Use a temporary config folder, so the database never lands in the repo."""
    return hass_tmp_config_dir


@pytest.fixture
async def setup_entry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> MockConfigEntry:
    """Add and set up an Electric Fence config entry."""
    # Independent of the installed Home Assistant version.
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", MIN_HA_VERSION)
    entry = MockConfigEntry(domain=DOMAIN, options=Settings().to_options())
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
def dummy(hass: HomeAssistant) -> Iterator[None]:
    """Register the `ef_dummy` test integration with its config flow."""
    PROBE.spawned = {}
    mock_dummy(hass)
    mock_platform(hass, f"{DUMMY}.config_flow", None)
    with mock_config_flow(DUMMY, DummyFlow):
        yield


@pytest.fixture
async def config(hass: HomeAssistant) -> None:
    """Set up `config` (and `http`) before Electric Fence."""
    assert await async_setup_component(hass, "config", {})


@pytest.fixture(autouse=True)
def geo_start() -> Iterator[AsyncMock]:
    """Never download a GeoIP database during setup; expose the start call."""
    start = AsyncMock()

    class NoStartUpdater(GeoUpdater):
        async_start = start  # type: ignore[assignment]

    with patch("custom_components.electric_fence.GeoUpdater", NoStartUpdater):
        yield start
