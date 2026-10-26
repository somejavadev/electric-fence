"""Tests for the failed logins sensor."""

import asyncio
from dataclasses import replace
from datetime import date, timedelta
from ipaddress import ip_address
from pathlib import Path
import sqlite3

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.electric_fence.const import DOMAIN
from custom_components.electric_fence.models import (
    ChangeAction,
    ChangeKind,
    LoginAttempt,
    LoginReason,
    LoginResult,
    SettingsChange,
    Source,
    UserExists,
)
from custom_components.electric_fence.sensor import (
    WRITE_INTERVAL,
    FailedLoginsSensor,
    merge_seed,
)
from custom_components.electric_fence.sensor_protection import (
    ActiveBansSensor,
    AttackedUsernamesSensor,
    Bans24hSensor,
    GeoipUpdatedSensor,
    LastBanSensor,
    LastNewCountryLoginSensor,
)
from custom_components.electric_fence.settings import Settings
from custom_components.electric_fence.storage import FenceStore
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import Entity
from homeassistant.util import dt as dt_util
from tests.helpers import create_hass_user, flush_worker

ENTITY_ID = "sensor.electric_fence_failed_logins"


def attempt(**kw: object) -> LoginAttempt:
    """Return a failed attempt for a user that does not exist."""
    base = LoginAttempt(
        ts=dt_util.utcnow(),
        result=LoginResult.FAILED,
        reason=LoginReason.NO_SUCH_USER,
        username_typed="admin",
        user_exists=UserExists.NO,
        user_id=None,
        provider="homeassistant",
        ip="203.0.113.7",
        source=Source.REMOTE,
        url="/auth/login_flow",
        user_agent="test",
        client_id="http://127.0.0.1:8123/",
        country_code="RU",
    )
    return replace(base, **kw)  # type: ignore[arg-type]


async def submit(hass: HomeAssistant, entry: MockConfigEntry, *records: object) -> None:
    """Submit records, wait until they are stored and the state is written."""
    for record in records:
        entry.runtime_data.worker.submit(record)
    await flush_worker(hass)
    await hass.async_block_till_done()
    # The sensor writes at most once a second; let a trailing write run.
    async_fire_time_changed(hass, dt_util.utcnow() + WRITE_INTERVAL)
    await hass.async_block_till_done()


def _writes(hass: HomeAssistant) -> list[str]:
    """Collect the sensor's written states."""
    writes: list[str] = []

    @callback
    def collect(event: Event[EventStateChangedData]) -> None:
        if event.data["entity_id"] == ENTITY_ID and event.data["new_state"]:
            writes.append(event.data["new_state"].state)

    hass.bus.async_listen(EVENT_STATE_CHANGED, collect)
    return writes


async def test_sensor_writes_at_most_once_a_second(
    hass: HomeAssistant, setup_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """A burst of records is one trailing write with the final value."""
    writes = _writes(hass)
    worker = setup_entry.runtime_data.worker
    for i in range(5):
        worker.submit(attempt(ip=f"203.0.113.{i}"))
        await flush_worker(hass)
    await hass.async_block_till_done()
    # Set up less than a second ago: the write waits.
    assert writes == []

    freezer.tick(WRITE_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert writes == ["5"]

    # After a quiet second, the next record is written at once.
    freezer.tick(timedelta(seconds=2))
    worker.submit(attempt())
    await flush_worker(hass)
    await hass.async_block_till_done()
    assert writes == ["5", "6"]
    worker.submit(attempt(ip="198.51.100.1"))
    await flush_worker(hass)
    await hass.async_block_till_done()
    assert writes == ["5", "6"]

    # Then the trailing write, with the final value.
    freezer.tick(WRITE_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert writes == ["5", "6", "7"]


async def test_sensor_unload_cancels_pending_write(
    hass: HomeAssistant, setup_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """A write still pending at unload is dropped, never run on a removed entity."""
    writes = _writes(hass)
    setup_entry.runtime_data.worker.submit(attempt())
    await flush_worker(hass)
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    freezer.tick(WRITE_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert writes == ["unavailable"]


async def test_sensor_counts_failures_in_24h(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Failed and blocked attempts count; successes don't."""
    assert hass.states.get(ENTITY_ID).state == "0"
    await submit(
        hass,
        setup_entry,
        attempt(),
        attempt(result=LoginResult.BLOCKED, reason=LoginReason.BLOCKED_LOCAL_ONLY),
        attempt(result=LoginResult.SUCCESS, reason=LoginReason.OK),
    )
    state = hass.states.get(ENTITY_ID)
    assert state.state == "2"
    assert state.attributes["unit_of_measurement"] == "attempts"
    assert state.attributes["state_class"] == "measurement"
    assert state.attributes["friendly_name"] == "Electric Fence Failed logins"


async def test_sensor_registry_entry(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """The entity has a stable unique id."""
    entity = er.async_get(hass).async_get(ENTITY_ID)
    assert entity is not None
    assert entity.unique_id == f"{setup_entry.entry_id}_failed_logins"
    assert entity.translation_key == "failed_logins"


async def test_sensor_expires_after_24h(
    hass: HomeAssistant, setup_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """Attempts leave the count after 24 hours."""
    await submit(hass, setup_entry, attempt(), attempt())
    assert hass.states.get(ENTITY_ID).state == "2"

    freezer.tick(timedelta(hours=23, minutes=59))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == "2"

    freezer.tick(timedelta(minutes=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == "0"


async def test_sensor_attributes_masked(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """The last attempt shows a masked username, and short single-line text."""
    await submit(hass, setup_entry, attempt())
    attrs = hass.states.get(ENTITY_ID).attributes
    assert attrs["last_username"] == "ad•••"
    assert attrs["last_ip"] == "203.0.113.7"
    assert attrs["last_country"] == "RU"
    assert attrs["last_reason"] == "no_such_user"

    await submit(
        hass,
        setup_entry,
        attempt(ip="1.2.3.4\n" + "x" * 200, username_typed="a\x00b " + "z" * 200),
    )
    attrs = hass.states.get(ENTITY_ID).attributes
    assert attrs["last_username"] == "ab•••"
    assert len(attrs["last_ip"]) == 64
    assert "\n" not in attrs["last_ip"]


async def test_sensor_existing_user_shown_in_full(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """A username of an existing user is not masked, but still shortened."""
    await submit(
        hass,
        setup_entry,
        attempt(username_typed="u" * 100, user_exists=UserExists.YES),
    )
    assert hass.states.get(ENTITY_ID).attributes["last_username"] == "u" * 64


async def test_sensor_counts_merged_rows(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """A merged record adds its count."""
    await submit(hass, setup_entry, attempt(count=3))
    assert hass.states.get(ENTITY_ID).state == "3"


async def _setup_with_rows(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, rows: list[LoginAttempt]
) -> MockConfigEntry:
    """Store rows, then set up an entry (a restart)."""
    path = Path(hass.config.path(".storage", "electric_fence.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    store = FenceStore(path)
    store.open()
    store.insert(rows)
    store.close()

    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", "2026.11.0")
    entry = MockConfigEntry(domain=DOMAIN, options=Settings().to_options())
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_sensor_seeded_from_store_on_restart(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failed and blocked attempts stored before a restart count again."""
    await _setup_with_rows(
        hass,
        monkeypatch,
        [
            attempt(count=2),
            attempt(result=LoginResult.BLOCKED, reason=LoginReason.BLOCKED_LOCAL_ONLY),
            attempt(ts=dt_util.utcnow() - timedelta(hours=25)),
            attempt(result=LoginResult.SUCCESS, reason=LoginReason.OK),
        ],
    )
    assert hass.states.get(ENTITY_ID).state == "3"


async def test_sensor_seed_keeps_row_times(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Seeded attempts expire at their own time, not 24h after the restart."""
    await _setup_with_rows(
        hass,
        monkeypatch,
        [
            attempt(ts=dt_util.utcnow() - timedelta(hours=23), count=2),
            attempt(ts=dt_util.utcnow() - timedelta(hours=1)),
        ],
    )
    assert hass.states.get(ENTITY_ID).state == "3"

    freezer.tick(timedelta(hours=1, minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == "1"


async def test_sensor_seed_failure_starts_empty(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing seed query is logged by type only, and the sensor still works."""

    def boom(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("secret detail")

    monkeypatch.setattr(FenceStore, "failed_buckets_since", boom)
    entry = await _setup_with_rows(hass, monkeypatch, [])
    assert hass.states.get(ENTITY_ID).state == "0"
    assert "OperationalError" in caplog.text
    assert "secret detail" not in caplog.text
    await submit(hass, entry, attempt())
    assert hass.states.get(ENTITY_ID).state == "1"


def test_merge_seed_drops_buffered_at_or_before_cutoff() -> None:
    """A buffered record at or before the query start is already in the seed."""
    cutoff = dt_util.utcnow()
    seeded = [(cutoff - timedelta(hours=1), 2)]
    buffered = [(cutoff - timedelta(seconds=1), 1), (cutoff, 4)]
    assert merge_seed(seeded, buffered, cutoff) == seeded


def test_merge_seed_keeps_buffered_after_cutoff_in_order() -> None:
    """Newer buffered records are kept, and the result is ascending."""
    cutoff = dt_util.utcnow()
    early = (cutoff - timedelta(hours=2), 1)
    late = (cutoff - timedelta(minutes=5), 2)
    newer = (cutoff + timedelta(seconds=1), 3)
    newest = (cutoff + timedelta(seconds=2), 4)
    assert merge_seed([late, early], [newest, newer], cutoff) == [
        early,
        late,
        newer,
        newest,
    ]


async def test_sensor_buffers_records_while_seeding(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record notified while the seed query runs is buffered, then merged."""
    real = FenceStore.failed_buckets_since
    holder: list[MockConfigEntry] = []
    adds_during_seed: list[int] = []
    adds: list[int] = []
    real_add = FailedLoginsSensor._add  # noqa: SLF001

    def spy_add(self: FailedLoginsSensor, ts: object, count: int) -> None:
        adds.append(count)
        real_add(self, ts, count)  # type: ignore[arg-type]

    async def store_one() -> None:
        worker = holder[0].runtime_data.worker
        worker.submit(attempt(ts=dt_util.utcnow()))
        # Stored, and the listeners called, before this returns. (The entry
        # is still setting up, so `flush_worker` would not find it.)
        await worker.async_flush()
        adds_during_seed.append(len(adds))

    def slow(self: FenceStore, *args: object, **kwargs: object) -> object:
        # Runs in the executor, so the seed is still pending while the record
        # is stored and the live listener is called.
        result = real(self, *args, **kwargs)
        asyncio.run_coroutine_threadsafe(store_one(), hass.loop).result(10)
        return result

    monkeypatch.setattr(FailedLoginsSensor, "_add", spy_add)
    monkeypatch.setattr(FenceStore, "failed_buckets_since", slow)
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", "2026.11.0")
    entry = MockConfigEntry(domain=DOMAIN, options=Settings().to_options())
    holder.append(entry)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # Nothing was counted while seeding: the record went to the buffer.
    assert adds_during_seed == [0]
    # It was stored after the query read the store, so the merge keeps it.
    assert hass.states.get(ENTITY_ID).state == "1"


async def test_sensor_hides_identifying_attributes_from_recorder(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """The username and IP are not written to the recorder history."""
    entity = hass.data["entity_components"]["sensor"].get_entity(ENTITY_ID)
    unrecorded = entity._unrecorded_attributes  # noqa: SLF001
    assert unrecorded >= {"last_username", "last_ip", "last_country"}
    assert "last_reason" not in entity._unrecorded_attributes  # noqa: SLF001


async def test_sensor_removed_on_unload(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Unloading removes the listener and the state."""
    worker = setup_entry.runtime_data.worker
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    assert hass.states.get(ENTITY_ID).state == "unavailable"
    assert not worker._listeners  # noqa: SLF001


SENSOR = "sensor.electric_fence_"
BINARY = "binary_sensor.electric_fence_"
BAN_IP = "203.0.113.9"


async def settle(hass: HomeAssistant) -> None:
    """Let listeners and tasks run."""
    await hass.async_block_till_done()


async def test_entities_created(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """All eleven entities exist and failed_logins is still there."""
    for entity_id in (
        "sensor.electric_fence_failed_logins",
        f"{SENSOR}active_bans",
        f"{SENSOR}bans_in_the_last_24_hours",
        f"{SENSOR}attacked_usernames",
        f"{SENSOR}last_failed_login",
        f"{SENSOR}last_ban",
        f"{SENSOR}last_new_country_login",
        f"{SENSOR}admins_without_mfa",
        f"{SENSOR}geoip_updated",
        f"{BINARY}login_attack",
        f"{BINARY}security_warnings",
        f"{BINARY}auto_ban_available",
    ):
        assert hass.states.get(entity_id) is not None, entity_id
    assert hass.states.get(ENTITY_ID).state == "0"


async def test_active_bans_updates_on_ban(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """A ban raises active_bans and bans_24h and sets last_ban."""
    bans = setup_entry.runtime_data.bans
    assert bans is not None
    assert hass.states.get(f"{SENSOR}active_bans").state == "0"
    assert hass.states.get(f"{SENSOR}last_ban").state == "unknown"

    assert await bans.async_ban(BAN_IP, 5, "RU", None)
    await settle(hass)

    state = hass.states.get(f"{SENSOR}active_bans")
    assert state.state == "1"
    assert state.attributes["last_ip"] == BAN_IP
    assert state.attributes["last_country"] == "RU"
    assert hass.states.get(f"{SENSOR}bans_in_the_last_24_hours").state == "1"
    assert hass.states.get(f"{SENSOR}last_ban").state == bans.last_ban_at.isoformat(
        timespec="seconds"
    )

    assert await bans.async_unban(BAN_IP, None, None)
    await settle(hass)
    assert hass.states.get(f"{SENSOR}active_bans").state == "0"
    assert hass.states.get(f"{SENSOR}bans_in_the_last_24_hours").state == "1"


async def test_active_bans_follow_core_bans(
    hass: HomeAssistant, setup_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """A ban Core makes itself shows within a minute, without a ban event."""
    bans = setup_entry.runtime_data.bans
    assert bans is not None
    assert hass.states.get(f"{SENSOR}active_bans").state == "0"
    await bans.backend.async_add(ip_address("198.51.100.20"))
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass)
    await settle(hass)
    state = hass.states.get(f"{SENSOR}active_bans")
    assert state.state == "1"
    assert state.attributes["last_ip"] == "198.51.100.20"


async def test_bans_24h_reevaluated_each_minute(
    hass: HomeAssistant, setup_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """The count falls after 24 hours without a further ban event."""
    bans = setup_entry.runtime_data.bans
    assert bans is not None
    assert await bans.async_ban(BAN_IP, 5, "RU", None)
    await settle(hass)
    assert hass.states.get(f"{SENSOR}bans_in_the_last_24_hours").state == "1"

    freezer.tick(timedelta(hours=24, minutes=1))
    async_fire_time_changed(hass)
    await settle(hass)
    assert hass.states.get(f"{SENSOR}bans_in_the_last_24_hours").state == "0"


async def test_state_never_contains_ip(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Counts are numbers, and identifying attributes are unrecorded."""
    bans = setup_entry.runtime_data.bans
    assert bans is not None
    assert await bans.async_ban(BAN_IP, 5, "RU", None)
    await settle(hass)

    assert hass.states.get(f"{SENSOR}active_bans").state.isdigit()
    assert {"last_ip", "last_country"} <= ActiveBansSensor._unrecorded_attributes  # noqa: SLF001
    assert {"user", "country"} <= LastNewCountryLoginSensor._unrecorded_attributes  # noqa: SLF001
    assert BAN_IP not in "".join(
        s.state
        for s in hass.states.async_all()
        if s.domain in ("sensor", "binary_sensor")
    )


async def test_attacked_usernames_and_login_attack(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """An attack on a username turns login_attack on and counts it."""
    guard = setup_entry.runtime_data.username_guard
    assert guard is not None
    assert hass.states.get(f"{BINARY}login_attack").state == "off"
    for i in range(10):
        guard.on_attempt(
            "admin",
            f"198.51.100.{i % 3 + 1}",
            LoginResult.FAILED,
            LoginReason.WRONG_PASSWORD,
        )
    await settle(hass)
    assert hass.states.get(f"{SENSOR}attacked_usernames").state == "1"
    assert hass.states.get(f"{BINARY}login_attack").state == "on"
    assert "admin" not in hass.states.get(f"{BINARY}login_attack").state


async def test_last_failed_login_tracks_and_seeds(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """A failed login sets the timestamp; a restart seeds it from the store."""
    ts = dt_util.utcnow().replace(microsecond=0)
    assert hass.states.get(f"{SENSOR}last_failed_login").state == "unknown"
    await submit(hass, setup_entry, attempt(ts=ts))
    assert hass.states.get(f"{SENSOR}last_failed_login").state == ts.isoformat()

    assert await hass.config_entries.async_reload(setup_entry.entry_id)
    await settle(hass)
    assert hass.states.get(f"{SENSOR}last_failed_login").state == ts.isoformat()


async def test_last_new_country_login(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """A new-country login sets the timestamp, user and country."""
    user = await create_hass_user(hass, "alice", "pw")
    ts = dt_util.utcnow().replace(microsecond=0)
    login = attempt(
        result=LoginResult.SUCCESS,
        reason=LoginReason.OK,
        user_id=user.id,
        user_exists=UserExists.YES,
    )
    await submit(hass, setup_entry, replace(login, ts=ts, country_code="US"))
    assert hass.states.get(f"{SENSOR}last_new_country_login").state == "unknown"
    await submit(hass, setup_entry, replace(login, ts=ts, country_code="DE"))
    state = hass.states.get(f"{SENSOR}last_new_country_login")
    assert state.state == ts.isoformat()
    assert state.attributes["user"] == "alice"
    assert state.attributes["country"] == "DE"
    assert hass.states.get(f"{SENSOR}last_failed_login").state == "unknown"

    assert await hass.config_entries.async_reload(setup_entry.entry_id)
    await settle(hass)
    state = hass.states.get(f"{SENSOR}last_new_country_login")
    assert state.state == ts.isoformat(timespec="seconds")
    assert state.attributes["user"] == "alice"


async def test_admins_without_mfa(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """The count follows user changes."""
    assert hass.states.get(f"{SENSOR}admins_without_mfa").state == "0"
    await create_hass_user(hass, "alice", "pw")
    setup_entry.runtime_data.worker.submit(
        SettingsChange(
            ts=dt_util.utcnow(),
            kind=ChangeKind.USER,
            action=ChangeAction.ADDED,
            target_id=None,
            target_name=None,
            details={},
            actor_user_id=None,
            actor_name=None,
            actor_ip=None,
        )
    )
    await flush_worker(hass)
    await settle(hass)
    assert hass.states.get(f"{SENSOR}admins_without_mfa").state == "1"


async def test_geoip_updated(
    hass: HomeAssistant, setup_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """The database date shows as midnight UTC, unknown without one."""
    status = setup_entry.runtime_data.geo_status
    assert status is not None
    status.db_date = None
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=16))
    await settle(hass)
    assert hass.states.get(f"{SENSOR}geoip_updated").state == "unknown"

    status.db_date = date(2026, 10, 1)
    freezer.tick(timedelta(minutes=16))
    async_fire_time_changed(hass)
    await settle(hass)
    assert (
        hass.states.get(f"{SENSOR}geoip_updated").state == "2026-10-01T00:00:00+00:00"
    )


async def test_unavailable_without_manager(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Entities whose manager is missing report unavailable instead of raising."""
    runtime = setup_entry.runtime_data
    runtime.bans = None
    assert ActiveBansSensor(setup_entry).available is False


@pytest.mark.parametrize(
    "factory",
    [
        ActiveBansSensor,
        Bans24hSensor,
        AttackedUsernamesSensor,
        LastBanSensor,
        GeoipUpdatedSensor,
    ],
)
async def test_unavailable_without_manager_parametrised(
    setup_entry: MockConfigEntry, factory: type[Entity]
) -> None:
    """Manager-dependent sensors report unavailable instead of raising."""
    runtime = setup_entry.runtime_data
    runtime.bans = None
    runtime.username_guard = None
    runtime.geo_status = None
    assert factory(setup_entry).available is False
