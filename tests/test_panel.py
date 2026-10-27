"""Tests for the panel registration."""

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL, DATA_PANELS
from homeassistant.core import HomeAssistant

ICONS_URL = "/electric_fence_static/electric-fence-icons.js?v=0.3.0"


async def test_panel_registered_admin_only(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """The panel is in the sidebar, admin only, versioned by the manifest."""
    panel = hass.data[DATA_PANELS]["electric-fence"]
    assert panel.require_admin is True
    assert panel.sidebar_title == "Electric Fence"
    assert panel.sidebar_icon == "electric-fence:logo"
    assert panel.config["_panel_custom"]["module_url"].endswith("?v=0.3.0")
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


def _icon_modules(hass: HomeAssistant) -> list[str]:
    """Return the extra module URLs that are Electric Fence's icon module."""
    return [
        url
        for url in hass.data[DATA_EXTRA_MODULE_URL].urls
        if "electric-fence-icons" in url
    ]


async def test_icon_module_loaded_on_every_page(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """The sidebar icon set is loaded on every page, versioned by the manifest."""
    assert _icon_modules(hass) == [ICONS_URL]


async def test_icon_module_removed_on_unload_and_not_duplicated_on_reload(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Unloading removes the icon module; loading again adds it exactly once."""
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    assert _icon_modules(hass) == []
    assert await hass.config_entries.async_setup(setup_entry.entry_id)
    assert _icon_modules(hass) == [ICONS_URL]
