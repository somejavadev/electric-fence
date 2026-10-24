"""Registration of the Electric Fence admin panel."""

from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN

PANEL_URL = "/electric_fence_static"
PANEL_PATH = "electric-fence"
PANEL_ELEMENT = "electric-fence-panel"
FRONTEND_DIR = Path(__file__).parent / "frontend_dist"

# Static paths cannot be removed again, so they are registered once per run.
DATA_STATIC_REGISTERED: HassKey[bool] = HassKey(f"{DOMAIN}_static_registered")


async def async_register_panel(hass: HomeAssistant, version: str) -> None:
    """Serve the built panel and add it to the sidebar (admins only)."""
    if not hass.data.get(DATA_STATIC_REGISTERED):
        await hass.http.async_register_static_paths(
            [StaticPathConfig(PANEL_URL, str(FRONTEND_DIR), cache_headers=False)]
        )
        hass.data[DATA_STATIC_REGISTERED] = True
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_PATH,
        webcomponent_name=PANEL_ELEMENT,
        sidebar_title="Electric Fence",
        sidebar_icon="mdi:fence",
        module_url=f"{PANEL_URL}/{PANEL_ELEMENT}.js?v={version}",
        require_admin=True,
        embed_iframe=False,
    )


def async_unregister_panel(hass: HomeAssistant) -> None:
    """Remove the panel from the sidebar."""
    frontend.async_remove_panel(hass, PANEL_PATH, warn_if_unknown=False)
