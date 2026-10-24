"""Tests for the failed logins sensor."""

import asyncio
from dataclasses import replace
from datetime import timedelta
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
    LoginAttempt,
    LoginReason,
    LoginResult,
    Source,
    UserExists,
)
from custom_components.electric_fence.settings import Settings
from custom_components.electric_fence.storage import FenceStore
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from tests.helpers import flush_worker

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
    """Submit records to the worker, and wait until they are stored."""
    for record in records:
        entry.runtime_data.worker.submit(record)
    await flush_worker(hass)
    await hass.async_block_till_done()


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


async def test_sensor_records_during_seed_are_kept(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record flushed and notified while the seed query runs is counted."""
    real = FenceStore.failed_buckets_since
    holder: list[MockConfigEntry] = []

    async def store_one() -> None:
        worker = holder[0].runtime_data.worker
        worker.submit(attempt(ts=dt_util.utcnow()))
        # Stored, and the listeners called, before this returns. (The entry
        # is still setting up, so `flush_worker` would not find it.)
        await worker.async_flush()

    def slow(self: FenceStore, *args: object, **kwargs: object) -> object:
        # Runs in the executor, so the seed is still pending while the record
        # is stored and the live listener is called.
        result = real(self, *args, **kwargs)
        asyncio.run_coroutine_threadsafe(store_one(), hass.loop).result(10)
        return result

    monkeypatch.setattr(FenceStore, "failed_buckets_since", slow)
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", "2026.11.0")
    entry = MockConfigEntry(domain=DOMAIN, options=Settings().to_options())
    holder.append(entry)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # Stored after the query read the store, so only the buffer has it.
    assert hass.states.get(ENTITY_ID).state == "1"


async def test_sensor_hides_identifying_attributes_from_recorder(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """The username and IP are not written to the recorder history."""
    entity = hass.data["entity_components"]["sensor"].get_entity(ENTITY_ID)
    assert entity._unrecorded_attributes >= {"last_username", "last_ip"}  # noqa: SLF001
    assert "last_reason" not in entity._unrecorded_attributes  # noqa: SLF001


async def test_sensor_removed_on_unload(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Unloading removes the listener and the state."""
    worker = setup_entry.runtime_data.worker
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    assert hass.states.get(ENTITY_ID).state == "unavailable"
    assert not worker._listeners  # noqa: SLF001
