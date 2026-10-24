"""Tests for the panel registration."""

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.frontend import DATA_PANELS
from homeassistant.core import HomeAssistant


async def test_panel_registered_admin_only(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """The panel is in the sidebar, admin only, versioned by the manifest."""
    panel = hass.data[DATA_PANELS]["electric-fence"]
    assert panel.require_admin is True
    assert panel.sidebar_title == "Electric Fence"
    assert panel.sidebar_icon == "mdi:fence"
    assert panel.config["_panel_custom"]["module_url"].endswith("?v=0.1.0")
    assert panel.config["_panel_custom"]["module_url"].startswith(
        "/electric_fence_static/electric-fence-panel.js"
    )


async def test_panel_removed_on_unload(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Unloading removes the panel, and loading again brings it back."""
    assert "electric-fence" in hass.data[DATA_PANELS]
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    assert "electric-fence" not in hass.data[DATA_PANELS]
    assert await hass.config_entries.async_setup(setup_entry.entry_id)
    assert "electric-fence" in hass.data[DATA_PANELS]
