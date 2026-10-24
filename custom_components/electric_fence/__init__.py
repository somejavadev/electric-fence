"""The Electric Fence integration."""

import logging

from awesomeversion import AwesomeVersion

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant

from .const import MIN_HA_VERSION
from .runtime import Runtime
from .settings import Settings

_LOGGER = logging.getLogger(__name__)

type ElectricFenceConfigEntry = ConfigEntry[Runtime]


async def async_setup_entry(
    hass: HomeAssistant, entry: ElectricFenceConfigEntry
) -> bool:
    """Set up Electric Fence from a config entry."""
    if AwesomeVersion(HA_VERSION) < MIN_HA_VERSION:
        _LOGGER.error(
            "Electric Fence requires Home Assistant %s or newer (running %s)",
            MIN_HA_VERSION,
            HA_VERSION,
        )
        return False
    entry.runtime_data = Runtime(settings=Settings.from_options(entry.options))
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: ElectricFenceConfigEntry
) -> bool:
    """Unload a config entry."""
    return True
