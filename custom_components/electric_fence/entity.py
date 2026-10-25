"""Shared base classes for the protection entities."""

import asyncio
from datetime import datetime, timedelta
import logging

from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_track_time_interval

from . import ElectricFenceConfigEntry
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

REFRESH_INTERVAL = timedelta(minutes=15)


class FenceEntity(Entity):
    """An entity on the Electric Fence service device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ElectricFenceConfigEntry, key: str) -> None:
        """Initialise the entity."""
        self._entry = entry
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Electric Fence",
            entry_type=DeviceEntryType.SERVICE,
        )

    @callback
    def _write(self, *_args: object) -> None:
        self.async_write_ha_state()

    @callback
    def _track_interval(self, action: CALLBACK_TYPE, interval: timedelta) -> None:
        @callback
        def tick(_now: datetime) -> None:
            action()

        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                tick,
                interval,
                name=f"electric_fence {self._attr_translation_key}",
                cancel_on_shutdown=True,
            )
        )


class AsyncFenceEntity(FenceEntity):
    """An entity whose value needs I/O: refreshed in a task, one at a time."""

    def __init__(self, entry: ElectricFenceConfigEntry, key: str) -> None:
        """Initialise the entity."""
        super().__init__(entry, key)
        self._task: asyncio.Task[None] | None = None
        self._again = False

    async def _async_update_value(self) -> None:
        """Compute the value; may raise, which keeps the previous value."""
        raise NotImplementedError

    async def _async_safe_update(self) -> None:
        try:
            await self._async_update_value()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Updating %s failed: %s", self._attr_translation_key, type(err).__name__
            )

    @callback
    def _cancel_task(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    @callback
    def _refresh(self, *_args: object) -> None:
        """Refresh soon; a refresh in progress runs once more afterwards."""
        if self._task is not None and not self._task.done():
            self._again = True
            return
        self._task = self._entry.async_create_task(
            self.hass,
            self._async_run(),
            f"electric_fence {self._attr_translation_key}",
        )

    async def _async_run(self) -> None:
        while True:
            self._again = False
            await self._async_safe_update()
            self.async_write_ha_state()
            if not self._again:
                return

    async def async_added_to_hass(self) -> None:
        """Compute the first value and keep it fresh."""
        self.async_on_remove(self._cancel_task)
        await self._async_safe_update()
        self._track_interval(self._refresh, REFRESH_INTERVAL)
