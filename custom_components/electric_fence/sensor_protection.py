"""The protection sensors: bans, attacks, last events and diagnostics."""

from datetime import datetime, time, timedelta
import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.util import dt as dt_util

from . import ElectricFenceConfigEntry
from .attribution import auth_users
from .const import SIGNAL_FORWARDING_UPDATED
from .entity import REFRESH_INTERVAL, AsyncFenceEntity, FenceEntity
from .forwarding.forwarder import forwarding_state
from .models import ChangeKind, LoginAttempt, LoginResult, Record, SettingsChange
from .posture import async_admins_without_mfa
from .privacy import inline_text
from .storage import FAILED_OR_BLOCKED, FenceStore, LoginFilter

_LOGGER = logging.getLogger(__name__)

BANS_RECHECK = timedelta(minutes=1)
FORWARDING_REFRESH = timedelta(minutes=1)
_NAME_LIMIT = 64


class ActiveBansSensor(FenceEntity, SensorEntity):
    """The number of active IP bans."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({"last_ip", "last_country"})

    def __init__(self, entry: ElectricFenceConfigEntry) -> None:
        """Initialise the sensor."""
        super().__init__(entry, "active_bans")

    @property
    def available(self) -> bool:
        """Return whether the ban manager exists."""
        return self._entry.runtime_data.bans is not None

    @property
    def native_value(self) -> int | None:
        """Return the number of active bans."""
        bans = self._entry.runtime_data.bans
        return len(bans.active()) if bans is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Return the newest ban's address and country."""
        bans = self._entry.runtime_data.bans
        active = bans.active() if bans is not None else []
        if not active:
            return {}
        last = max(active, key=lambda view: view.banned_at)
        return {"last_ip": str(last.ip), "last_country": last.country_code}

    async def async_added_to_hass(self) -> None:
        """Follow the ban manager, and Core's own bans every minute."""
        if (bans := self._entry.runtime_data.bans) is not None:
            self.async_on_remove(bans.add_listener(self._write))
            # Core bans without telling anyone. Its lookup is in memory, and
            # an unchanged state writes nothing to the bus.
            self.async_on_remove(
                async_track_time_interval(
                    self.hass,
                    self._write,
                    BANS_RECHECK,
                    name="electric_fence active bans",
                    cancel_on_shutdown=True,
                )
            )


class Bans24hSensor(FenceEntity, SensorEntity):
    """The number of bans in the last 24 hours."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: ElectricFenceConfigEntry) -> None:
        """Initialise the sensor."""
        super().__init__(entry, "bans_24h")
        self._recheck: CALLBACK_TYPE | None = None

    @property
    def available(self) -> bool:
        """Return whether the ban manager exists."""
        return self._entry.runtime_data.bans is not None

    @property
    def native_value(self) -> int | None:
        """Return the number of bans in the window."""
        bans = self._entry.runtime_data.bans
        return bans.bans_24h() if bans is not None else None

    async def async_added_to_hass(self) -> None:
        """Follow the ban manager; old bans age out without an event."""
        self.async_on_remove(self._cancel_recheck)
        if (bans := self._entry.runtime_data.bans) is not None:
            self.async_on_remove(bans.add_listener(self._changed))
        self._arm()

    @callback
    def _cancel_recheck(self) -> None:
        if self._recheck is not None:
            self._recheck()
            self._recheck = None

    @callback
    def _arm(self) -> None:
        """Check again in a minute while there are bans in the window."""
        if self._recheck is None and (self.native_value or 0) > 0:
            self._recheck = async_call_later(self.hass, BANS_RECHECK, self._tick)

    @callback
    def _changed(self) -> None:
        self.async_write_ha_state()
        self._arm()

    @callback
    def _tick(self, _now: datetime) -> None:
        self._recheck = None
        self._changed()


class AttackedUsernamesSensor(FenceEntity, SensorEntity):
    """The number of usernames under attack."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: ElectricFenceConfigEntry) -> None:
        """Initialise the sensor."""
        super().__init__(entry, "attacked_usernames")

    @property
    def available(self) -> bool:
        """Return whether the username guard exists."""
        return self._entry.runtime_data.username_guard is not None

    @property
    def native_value(self) -> int | None:
        """Return the number of active attacks."""
        guard = self._entry.runtime_data.username_guard
        return len(guard.active_attacks()) if guard is not None else None

    async def async_added_to_hass(self) -> None:
        """Follow the username guard."""
        if (guard := self._entry.runtime_data.username_guard) is not None:
            self.async_on_remove(guard.add_listener(self._write))


class _TimestampSensor(FenceEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP


class LastBanSensor(_TimestampSensor):
    """When the last IP ban happened."""

    def __init__(self, entry: ElectricFenceConfigEntry) -> None:
        """Initialise the sensor."""
        super().__init__(entry, "last_ban")

    @property
    def available(self) -> bool:
        """Return whether the ban manager exists."""
        return self._entry.runtime_data.bans is not None

    @property
    def native_value(self) -> datetime | None:
        """Return the time of the last ban."""
        bans = self._entry.runtime_data.bans
        return bans.last_ban_at if bans is not None else None

    async def async_added_to_hass(self) -> None:
        """Follow the ban manager."""
        if (bans := self._entry.runtime_data.bans) is not None:
            self.async_on_remove(bans.add_listener(self._write))


class _LastLoginSensor(_TimestampSensor):
    """A timestamp that follows login records and is seeded from the store."""

    _filter: LoginFilter

    def __init__(self, entry: ElectricFenceConfigEntry, key: str) -> None:
        """Initialise the sensor."""
        super().__init__(entry, key)
        self._ts: datetime | None = None
        self._user_id: str | None = None
        self._country: str | None = None

    def _matches(self, record: LoginAttempt) -> bool:
        raise NotImplementedError

    @property
    def native_value(self) -> datetime | None:
        """Return the time of the last matching login."""
        return self._ts

    @callback
    def _on_record(self, record: Record) -> None:
        if not isinstance(record, LoginAttempt) or not self._matches(record):
            return
        if self._ts is None or record.ts >= self._ts:
            self._ts = record.ts
            self._user_id = record.user_id
            self._country = record.country_code
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Follow the worker, then seed from the store."""
        runtime = self._entry.runtime_data
        self.async_on_remove(runtime.worker.add_listener(self._on_record))
        try:
            found = await self.hass.async_add_executor_job(
                _newest_row, runtime.store, self._filter
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Seeding %s failed: %s", self._attr_translation_key, type(err).__name__
            )
            return
        if found is not None and (self._ts is None or found[0] > self._ts):
            self._ts, self._user_id, self._country = found


def _newest_row(
    store: FenceStore, flt: LoginFilter
) -> tuple[datetime, str | None, str | None] | None:
    rows = store.query_logins(flt, limit=1)
    return (rows[0].ts, rows[0].user_id, rows[0].country_code) if rows else None


class LastFailedLoginSensor(_LastLoginSensor):
    """When the last failed or blocked login happened."""

    _filter = LoginFilter(result=FAILED_OR_BLOCKED)

    def __init__(self, entry: ElectricFenceConfigEntry) -> None:
        """Initialise the sensor."""
        super().__init__(entry, "last_failed_login")

    def _matches(self, record: LoginAttempt) -> bool:
        return record.result in (LoginResult.FAILED, LoginResult.BLOCKED)


class LastNewCountryLoginSensor(_LastLoginSensor):
    """When the last login from a new country happened."""

    _filter = LoginFilter(new_country=True)
    _unrecorded_attributes = frozenset({"user", "country"})

    def __init__(self, entry: ElectricFenceConfigEntry) -> None:
        """Initialise the sensor."""
        super().__init__(entry, "last_new_country_login")

    def _matches(self, record: LoginAttempt) -> bool:
        return (
            record.new_country
            and record.result is LoginResult.SUCCESS
            and record.confirmed
        )

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Return the user's name and the country."""
        if self._ts is None:
            return {}
        users = auth_users(self.hass)
        user = users.get(self._user_id) if users and self._user_id else None
        name = inline_text(user.name, _NAME_LIMIT) if user and user.name else None
        return {"user": name, "country": self._country}


class AdminsWithoutMfaSensor(AsyncFenceEntity, SensorEntity):
    """The number of active administrators without MFA."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: ElectricFenceConfigEntry) -> None:
        """Initialise the sensor."""
        super().__init__(entry, "admins_without_mfa")
        self._count: int | None = None

    @property
    def native_value(self) -> int | None:
        """Return the number of such administrators."""
        return self._count

    async def _async_update_value(self) -> None:
        self._count = len(await async_admins_without_mfa(self.hass))

    @callback
    def _on_record(self, record: Record) -> None:
        if isinstance(record, SettingsChange) and record.kind is ChangeKind.USER:
            self._refresh()

    async def async_added_to_hass(self) -> None:
        """Follow user changes."""
        self.async_on_remove(
            self._entry.runtime_data.worker.add_listener(self._on_record)
        )
        await super().async_added_to_hass()


class GeoipUpdatedSensor(FenceEntity, SensorEntity):
    """The date of the GeoIP database in use."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ElectricFenceConfigEntry) -> None:
        """Initialise the sensor."""
        super().__init__(entry, "geoip_updated")

    @property
    def available(self) -> bool:
        """Return whether GeoIP status exists."""
        return self._entry.runtime_data.geo_status is not None

    @property
    def native_value(self) -> datetime | None:
        """Return the database date as midnight UTC, None when unknown."""
        status = self._entry.runtime_data.geo_status
        if status is None or status.db_date is None:
            return None
        return datetime.combine(status.db_date, time.min, tzinfo=dt_util.UTC)

    async def async_added_to_hass(self) -> None:
        """Re-read the status regularly; the updater has no signal."""
        self._track_interval(self.async_write_ha_state, REFRESH_INTERVAL)


class ForwardingSensor(FenceEntity, SensorEntity):
    """The state of the log forwarding to the SIEM."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["connected", "failing", "off"]
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ElectricFenceConfigEntry) -> None:
        """Initialise the sensor."""
        super().__init__(entry, "forwarding")

    @property
    def native_value(self) -> str:
        """Return connected, failing or off."""
        return forwarding_state(self._entry.runtime_data.forwarder)

    @property
    def extra_state_attributes(self) -> dict[str, int]:
        """Return the queue and the counts."""
        forwarder = self._entry.runtime_data.forwarder
        if forwarder is None:
            return {}
        stats = forwarder.stats()
        return {"queued": stats.queued, "dropped": stats.dropped, "sent": stats.sent}

    async def async_added_to_hass(self) -> None:
        """Follow the link state and the options; refresh the counts."""
        self._listen_settings(self._write)
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_FORWARDING_UPDATED, self._write)
        )
        self._track_interval(self.async_write_ha_state, FORWARDING_REFRESH)
