"""Tests for Electric Fence setup and unload."""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.electric_fence.const import DOMAIN
from custom_components.electric_fence.settings import Settings
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant


async def test_setup_and_unload(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """The entry loads and unloads cleanly."""
    assert setup_entry.state is ConfigEntryState.LOADED
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    assert setup_entry.state is ConfigEntryState.NOT_LOADED


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
    assert "requires Home Assistant 2026.11.0" in caplog.text
