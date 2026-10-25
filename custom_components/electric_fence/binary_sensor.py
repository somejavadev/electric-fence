"""The protection binary sensors."""

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ElectricFenceConfigEntry
from .entity import REFRESH_INTERVAL, AsyncFenceEntity, FenceEntity
from .models import ChangeKind, Record, SettingsChange
from .posture import async_check

PARALLEL_UPDATES = 0

_WARNING_LEVELS = frozenset({"warning", "error"})


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ElectricFenceConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the binary sensors."""
    async_add_entities(
        [
            LoginAttackBinarySensor(entry),
            SecurityWarningsBinarySensor(entry),
            AutoBanAvailableBinarySensor(entry),
        ]
    )


class LoginAttackBinarySensor(FenceEntity, BinarySensorEntity):
    """On while a username is under attack."""

    _attr_device_class = BinarySensorDeviceClass.SAFETY

    def __init__(self, entry: ElectricFenceConfigEntry) -> None:
        """Initialise the sensor."""
        super().__init__(entry, "login_attack")

    @property
    def available(self) -> bool:
        """Return whether the username guard exists."""
        return self._entry.runtime_data.username_guard is not None

    @property
    def is_on(self) -> bool | None:
        """Return whether any attack is active."""
        guard = self._entry.runtime_data.username_guard
        return bool(guard.active_attacks()) if guard is not None else None

    async def async_added_to_hass(self) -> None:
        """Follow the username guard."""
        if (guard := self._entry.runtime_data.username_guard) is not None:
            self.async_on_remove(guard.add_listener(self._write))


class SecurityWarningsBinarySensor(AsyncFenceEntity, BinarySensorEntity):
    """On while the security checklist has a warning or an error."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, entry: ElectricFenceConfigEntry) -> None:
        """Initialise the sensor."""
        super().__init__(entry, "security_warnings")
        self._on: bool | None = None

    @property
    def is_on(self) -> bool | None:
        """Return whether any check warns."""
        return self._on

    async def _async_update_value(self) -> None:
        runtime = self._entry.runtime_data
        items = await async_check(self.hass, runtime, runtime.store)
        self._on = any(item["level"] in _WARNING_LEVELS for item in items)

    @callback
    def _on_record(self, record: Record) -> None:
        if isinstance(record, SettingsChange) and record.kind is ChangeKind.USER:
            self._refresh()

    async def async_added_to_hass(self) -> None:
        """Re-run the checks when something they read changes."""
        runtime = self._entry.runtime_data
        self.async_on_remove(runtime.worker.add_listener(self._on_record))
        if runtime.bans is not None:
            self.async_on_remove(runtime.bans.add_listener(self._refresh))
        await super().async_added_to_hass()


class AutoBanAvailableBinarySensor(FenceEntity, BinarySensorEntity):
    """On while Electric Fence can ban addresses."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ElectricFenceConfigEntry) -> None:
        """Initialise the sensor."""
        super().__init__(entry, "auto_ban_available")

    @property
    def available(self) -> bool:
        """Return whether the ban manager exists."""
        return self._entry.runtime_data.bans is not None

    @property
    def is_on(self) -> bool | None:
        """Return whether bans can be applied."""
        bans = self._entry.runtime_data.bans
        return bans.available if bans is not None else None

    async def async_added_to_hass(self) -> None:
        """Follow the ban manager and re-check its health regularly."""
        if (bans := self._entry.runtime_data.bans) is not None:
            self.async_on_remove(bans.add_listener(self._write))
        self._track_interval(self.async_write_ha_state, REFRESH_INTERVAL)
