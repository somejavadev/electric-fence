"""The failed logins sensor."""

from collections import deque
from datetime import datetime, timedelta
import logging

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from . import ElectricFenceConfigEntry
from .const import DOMAIN
from .models import LoginAttempt, LoginResult, Record
from .notifier import display_username
from .privacy import inline_text
from .storage import FenceStore

_LOGGER = logging.getLogger(__name__)

WINDOW = timedelta(hours=24)
EXPIRY_INTERVAL = timedelta(minutes=1)
ATTRIBUTE_LIMIT = 64

PARALLEL_UPDATES = 0


def _attribute(value: object) -> str | None:
    """Return an attacker-controlled value as short single-line text."""
    return None if value is None else inline_text(str(value), ATTRIBUTE_LIMIT)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ElectricFenceConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensor."""
    async_add_entities([FailedLoginsSensor(entry)])


class FailedLoginsSensor(SensorEntity):
    """Failed and blocked login attempts in the last 24 hours."""

    _attr_has_entity_name = True
    _attr_translation_key = "failed_logins"
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "attempts"
    _unrecorded_attributes = frozenset({"last_username", "last_ip"})

    def __init__(self, entry: ElectricFenceConfigEntry) -> None:
        """Initialise the sensor."""
        self._entry = entry
        self._events: deque[tuple[datetime, int]] = deque()
        self._total = 0
        self._last: LoginAttempt | None = None
        self._buffer: list[LoginAttempt] | None = None
        self._attr_unique_id = f"{entry.entry_id}_failed_logins"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Electric Fence",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> int:
        """Return the number of attempts in the window."""
        return self._total

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Return the last failed attempt, masked and shortened."""
        last = self._last
        if last is None:
            return {}
        return {
            "last_username": _attribute(display_username(last)[0]),
            "last_ip": _attribute(last.ip),
            "last_country": _attribute(last.country_code),
            "last_reason": _attribute(last.reason),
        }

    async def async_added_to_hass(self) -> None:
        """Follow the worker, seed from the store, then merge what arrived."""
        runtime = self._entry.runtime_data
        # The listener comes first and buffers while the seed query runs, so
        # no record falls in the gap.
        self._buffer = []
        self.async_on_remove(runtime.worker.add_listener(self._on_record))
        cutoff = dt_util.utcnow()
        try:
            seeded = await self.hass.async_add_executor_job(
                _seed, runtime.store, cutoff - WINDOW
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Electric Fence could not read past failures: %s", type(err).__name__
            )
            seeded = []
        buffered, self._buffer = self._buffer, None
        # A buffered record older than the query is already in the seed.
        # One newer than the query may be too, and then counts twice: a
        # rare overcount of a record, rather than losing one.
        for ts, count in seeded:
            self._add(ts, count)
        for record in buffered:
            if record.ts > cutoff:
                self._add(record.ts, record.count)
        self._expire(dt_util.utcnow())
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._expire,
                EXPIRY_INTERVAL,
                name="electric_fence failed logins expiry",
                cancel_on_shutdown=True,
            )
        )

    def _add(self, ts: datetime, count: int) -> None:
        self._events.append((ts, count))
        self._total += count

    @callback
    def _on_record(self, record: Record) -> None:
        if not isinstance(record, LoginAttempt):
            return
        if record.result not in (LoginResult.FAILED, LoginResult.BLOCKED):
            return
        self._last = record
        if self._buffer is not None:
            self._buffer.append(record)
            return
        self._add(record.ts, record.count)
        self._expire(dt_util.utcnow())

    @callback
    def _expire(self, now: datetime) -> None:
        cutoff = now - WINDOW
        while self._events and self._events[0][0] <= cutoff:
            self._total -= self._events.popleft()[1]
        self.async_write_ha_state()


# Seeding edge cases, accepted: the listener buffers while the query runs and
# a buffered record is dropped when its ts is at or before the query start
# (it is taken to be in the seed). A record created before the query start
# but stored after the query read the store is therefore lost; that window is
# about one worker flush interval (1 s). A record with a ts after the query
# start that was stored before the query read the store counts twice. Both
# are rare, one record, and fade out of the 24 hour window.
def _seed(store: FenceStore, since: datetime) -> list[tuple[datetime, int]]:
    return store.failed_buckets_since(since, include_blocked=True)
