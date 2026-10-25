"""The failed logins sensor."""

from collections import deque
from datetime import datetime, timedelta
import logging

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.util import dt as dt_util

from . import ElectricFenceConfigEntry
from .const import DOMAIN
from .models import LoginAttempt, LoginResult, Record
from .notifier import display_username
from .privacy import inline_text
from .sensor_protection import (
    ActiveBansSensor,
    AdminsWithoutMfaSensor,
    AttackedUsernamesSensor,
    Bans24hSensor,
    GeoipUpdatedSensor,
    LastBanSensor,
    LastFailedLoginSensor,
    LastNewCountryLoginSensor,
)
from .storage import FenceStore

_LOGGER = logging.getLogger(__name__)

WINDOW = timedelta(hours=24)
EXPIRY_INTERVAL = timedelta(minutes=1)
ATTRIBUTE_LIMIT = 64
# The state is written at most this often; a trailing write keeps it current.
WRITE_INTERVAL = timedelta(seconds=1)

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
    async_add_entities(
        [
            FailedLoginsSensor(entry),
            ActiveBansSensor(entry),
            Bans24hSensor(entry),
            AttackedUsernamesSensor(entry),
            LastFailedLoginSensor(entry),
            LastBanSensor(entry),
            LastNewCountryLoginSensor(entry),
            AdminsWithoutMfaSensor(entry),
            GeoipUpdatedSensor(entry),
        ]
    )


class FailedLoginsSensor(SensorEntity):
    """Failed and blocked login attempts in the last 24 hours."""

    _attr_has_entity_name = True
    _attr_translation_key = "failed_logins"
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "attempts"
    _unrecorded_attributes = frozenset({"last_username", "last_ip", "last_country"})

    def __init__(self, entry: ElectricFenceConfigEntry) -> None:
        """Initialise the sensor."""
        self._entry = entry
        self._events: deque[tuple[datetime, int]] = deque()
        self._total = 0
        self._last: LoginAttempt | None = None
        self._buffer: list[LoginAttempt] | None = None
        self._last_write: datetime | None = None
        self._pending_write: CALLBACK_TYPE | None = None
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
        self.async_on_remove(self._cancel_write)
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
        for ts, count in merge_seed(
            seeded, [(r.ts, r.count) for r in buffered], cutoff
        ):
            self._add(ts, count)
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

    @callback
    def _cancel_write(self) -> None:
        """Drop a pending write: a removed entity can't write any more."""
        if self._pending_write is not None:
            self._pending_write()
            self._pending_write = None

    @callback
    def _schedule_write(self) -> None:
        """Write now, or once WRITE_INTERVAL has passed since the last write."""
        if self._pending_write is not None:
            return
        now = dt_util.utcnow()
        last = self._last_write
        if last is None or now - last >= WRITE_INTERVAL:
            self._write()
            return
        self._pending_write = async_call_later(
            self.hass, last + WRITE_INTERVAL - now, self._write_later
        )

    @callback
    def _write_later(self, _now: datetime) -> None:
        self._pending_write = None
        self._write()

    @callback
    def _write(self) -> None:
        self._last_write = dt_util.utcnow()
        self.async_write_ha_state()

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
        self._schedule_write()


def merge_seed(
    seeded: list[tuple[datetime, int]],
    buffered: list[tuple[datetime, int]],
    cutoff: datetime,
) -> list[tuple[datetime, int]]:
    """Return the seed plus the buffered records newer than the query, by time.

    A buffered record at or before `cutoff` (when the query started) is taken
    to be in the seed already.

    Accepted edge cases: a record created before the cutoff but stored after
    the query read the store is lost. The window is up to `enrich_timeout`
    (5 s) plus the worker's `flush_interval` (1 s), about 6 s in the worst
    case and longer under a backlog. A record newer than the cutoff that was
    stored before the query read the store counts twice. Both involve one
    record and fade out of the 24 hour window.
    """
    kept = [item for item in buffered if item[0] > cutoff]
    return sorted([*seeded, *kept], key=lambda item: item[0])


def _seed(store: FenceStore, since: datetime) -> list[tuple[datetime, int]]:
    return store.failed_buckets_since(since, include_blocked=True)
