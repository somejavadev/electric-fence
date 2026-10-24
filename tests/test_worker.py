"""Tests for the background worker, runtime and integration wiring."""

import asyncio
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import timedelta
from pathlib import Path
import sqlite3

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.electric_fence.collectors.base import BaseCollector
from custom_components.electric_fence.const import DOMAIN, MIN_HA_VERSION
from custom_components.electric_fence.models import (
    ChangeAction,
    ChangeKind,
    LoginAttempt,
    LoginReason,
    LoginResult,
    Record,
    SettingsChange,
    UserExists,
)
from custom_components.electric_fence.settings import Settings
from custom_components.electric_fence.storage import (
    ChangeFilter,
    FenceStore,
    LoginFilter,
    LoginRow,
)
from custom_components.electric_fence.worker import FenceWorker, NullEnricher
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from tests.helpers import flush_worker


def failed(
    *, ip: str = "203.0.113.7", user: str = "admin", seconds: float = 0.0
) -> LoginAttempt:
    """Return a failed attempt, `seconds` after now."""
    return LoginAttempt(
        ts=dt_util.utcnow() + timedelta(seconds=seconds),
        result=LoginResult.FAILED,
        reason=LoginReason.WRONG_PASSWORD,
        username_typed=user,
        user_exists=UserExists.YES,
        user_id=None,
        provider="homeassistant",
        ip=ip,
        source=None,
        url="/auth/login_flow",
        user_agent="test",
        client_id="http://127.0.0.1:8123/",
    )


def success(user_id: str = "user-1") -> LoginAttempt:
    """Return an unconfirmed successful attempt."""
    return LoginAttempt(
        ts=dt_util.utcnow(),
        result=LoginResult.SUCCESS,
        reason=LoginReason.OK,
        username_typed="admin",
        user_exists=UserExists.YES,
        user_id=user_id,
        provider="homeassistant",
        ip="192.168.1.10",
        source=None,
        url="/auth/login_flow",
        user_agent="test",
        client_id=None,
        confirmed=False,
    )


def change(target: str = "entry-1") -> SettingsChange:
    """Return a settings change."""
    return SettingsChange(
        ts=dt_util.utcnow(),
        kind=ChangeKind.CONFIG_ENTRY,
        action=ChangeAction.ADDED,
        target_id=target,
        target_name="Thing",
        details={},
        actor_user_id="user-1",
        actor_name="Admin",
        actor_ip=None,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[FenceStore]:
    """Return an open store in a temporary folder."""
    s = FenceStore(tmp_path / "fence.db")
    s.open()
    yield s
    s.close()


@pytest.fixture
async def worker(hass: HomeAssistant, store: FenceStore) -> AsyncIterator[FenceWorker]:
    """Return a started worker; stopped after the test."""
    w = FenceWorker(hass, store, NullEnricher())
    await w.async_start()
    yield w
    await w.async_stop()


def logins(store: FenceStore) -> list[LoginRow]:
    """Return all stored login rows, oldest first."""
    return list(reversed(store.query_logins(LoginFilter(), limit=500)))


async def test_submit_never_blocks_and_counts_drops(
    hass: HomeAssistant, store: FenceStore
) -> None:
    """A full queue drops and counts records instead of blocking."""
    w = FenceWorker(hass, store, NullEnricher(), max_queue=2)
    for i in range(5):
        w.submit(failed(user=f"u{i}"))
    assert w.dropped == 3
    assert w.queued == 2
    # A confirmation on a full queue is dropped quietly too.
    w.confirm_success("user-1", None, None)
    assert w.queued == 2
    await w.async_start()
    await w.async_stop()
    assert len(logins(store)) == 2
    assert w.queued == 0


async def test_records_stored_and_listeners_called_in_order(
    hass: HomeAssistant, store: FenceStore, worker: FenceWorker
) -> None:
    """Records are stored, then each listener sees each record in order."""
    calls: list[tuple[str, Record, int]] = []

    def first(rec: Record) -> None:
        calls.append(("first", rec, store.stats().logins + store.stats().changes))

    def second(rec: Record) -> None:
        calls.append(("second", rec, store.stats().logins + store.stats().changes))

    worker.add_listener(first)
    remove = worker.add_listener(second)
    a = failed(user="a")
    b = change()
    worker.submit(a)
    worker.submit(b)
    await worker.async_flush()

    assert [(name, rec) for name, rec, _ in calls] == [
        ("first", a),
        ("second", a),
        ("first", b),
        ("second", b),
    ]
    # Listeners run only after the write.
    assert all(stored == 2 for _, _, stored in calls)
    assert [r.username_typed for r in logins(store)] == ["a"]
    assert len(store.query_changes(ChangeFilter(), limit=10)) == 1

    remove()
    remove()
    worker.submit(failed(user="c"))
    await worker.async_flush()
    assert len(calls) == 5
    assert calls[-1][0] == "first"


async def test_identical_failures_merged_within_window(
    hass: HomeAssistant, store: FenceStore, worker: FenceWorker
) -> None:
    """Identical failures within 1 s become one row with a count."""
    for seconds in (0.0, 0.4, 0.8):
        worker.submit(failed(seconds=seconds))
    # Different IP, username or reason: separate rows.
    worker.submit(failed(ip="198.51.100.1", seconds=0.5))
    worker.submit(failed(user="other", seconds=0.5))
    other_reason = failed(seconds=0.5)
    other_reason.reason = LoginReason.NO_SUCH_USER
    worker.submit(other_reason)
    # 2 s after the last merged attempt: a new row.
    worker.submit(failed(seconds=2.9))
    await worker.async_flush()

    rows = store.query_logins(
        LoginFilter(ip="203.0.113.7", username_contains="admin"), limit=10
    )
    rows = [r for r in rows if r.reason is LoginReason.WRONG_PASSWORD]
    assert sorted(r.count for r in rows) == [1, 3]
    assert len(logins(store)) == 5
    assert store.count_failed_since(dt_util.utcnow() - timedelta(minutes=1)) == 7


async def test_merge_never_touches_stored_rows(
    hass: HomeAssistant, store: FenceStore, worker: FenceWorker
) -> None:
    """A failure after the batch was written starts a new row."""
    worker.submit(failed())
    await worker.async_flush()
    worker.submit(failed(seconds=0.1))
    await worker.async_flush()
    assert [r.count for r in logins(store)] == [1, 1]


async def test_success_held_until_confirmed(
    hass: HomeAssistant, store: FenceStore, worker: FenceWorker
) -> None:
    """A success is stored once confirmed, with the client of the token."""
    worker.submit(success("user-1"))
    worker.submit(success("user-2"))
    await worker.async_flush()
    assert logins(store) == []

    worker.confirm_success("user-1", "https://app.example/", "App")
    worker.confirm_success("nobody", None, None)
    await worker.async_flush()
    rows = logins(store)
    assert len(rows) == 1
    assert rows[0].user_id == "user-1"
    assert rows[0].confirmed is True
    assert rows[0].client_id == "https://app.example/"
    assert rows[0].client_name == "App"

    # Stopping stores what is still held, unconfirmed.
    await worker.async_stop()
    rows = logins(store)
    assert [(r.user_id, r.confirmed) for r in rows] == [
        ("user-1", True),
        ("user-2", False),
    ]


async def test_unconfirmed_success_stored_after_timeout(
    hass: HomeAssistant, store: FenceStore
) -> None:
    """Without confirmation, a success is stored unconfirmed after the timeout."""
    w = FenceWorker(
        hass, store, NullEnricher(), confirm_timeout=0.1, flush_interval=0.05
    )
    await w.async_start()
    try:
        w.submit(success())
        await w.async_flush()
        assert logins(store) == []
        for _ in range(100):
            await asyncio.sleep(0.02)
            if logins(store):
                break
        rows = logins(store)
        assert len(rows) == 1
        assert rows[0].confirmed is False
    finally:
        await w.async_stop()


async def test_worker_survives_storage_failure(
    hass: HomeAssistant,
    store: FenceStore,
    worker: FenceWorker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing database drops the batch, counts it, and the worker carries on."""
    original = store.insert
    broken = True

    def insert(records: Sequence[Record]) -> None:
        if broken:
            raise sqlite3.OperationalError("database is locked")
        original(records)

    monkeypatch.setattr(store, "insert", insert)
    listened: list[Record] = []
    worker.add_listener(listened.append)

    worker.submit(failed(user="lost"))
    await worker.async_flush()
    assert worker.storage_errors == 1
    assert worker.dropped == 1
    assert worker.last_storage_error == "OperationalError: database is locked"
    assert worker.last_storage_error_at is not None
    assert listened == []

    broken = False
    worker.submit(failed(user="kept"))
    await worker.async_flush()
    assert [r.username_typed for r in logins(store)] == ["kept"]
    assert worker.storage_errors == 1
    assert len(listened) == 1


async def test_listener_exception_does_not_stop_worker(
    hass: HomeAssistant, store: FenceStore, worker: FenceWorker
) -> None:
    """A raising listener is guarded; later listeners and records still run."""
    seen: list[Record] = []

    def boom(rec: Record) -> None:
        raise RuntimeError("listener bug")

    worker.add_listener(boom)
    worker.add_listener(seen.append)
    worker.submit(failed(user="a"))
    await worker.async_flush()
    worker.submit(failed(user="b"))
    await worker.async_flush()
    assert [r.username_typed for r in seen if isinstance(r, LoginAttempt)] == [
        "a",
        "b",
    ]
    assert len(logins(store)) == 2


async def test_enricher_exception_still_stores(
    hass: HomeAssistant, store: FenceStore
) -> None:
    """A raising enricher is guarded and the record is stored anyway."""

    class Broken:
        async def async_enrich(self, record: LoginAttempt) -> None:
            raise RuntimeError("enrich bug")

    w = FenceWorker(hass, store, Broken())
    await w.async_start()
    w.submit(failed())
    await w.async_flush()
    await w.async_stop()
    await w.async_stop()
    assert len(logins(store)) == 1


async def test_flush_size_triggers_write(
    hass: HomeAssistant, store: FenceStore
) -> None:
    """A full batch is written without waiting for the interval."""
    w = FenceWorker(hass, store, NullEnricher(), flush_size=2, flush_interval=3600)
    await w.async_start()
    try:
        w.submit(failed(user="a"))
        w.submit(failed(user="b"))
        w.submit(failed(user="c"))
        for _ in range(100):
            await asyncio.sleep(0)
            if len(logins(store)) == 2:
                break
        assert len(logins(store)) == 2
    finally:
        await w.async_stop()
    assert len(logins(store)) == 3


async def test_submit_after_stop_is_dropped(
    hass: HomeAssistant, store: FenceStore
) -> None:
    """Records submitted after stop are counted, never raised."""
    w = FenceWorker(hass, store, NullEnricher())
    await w.async_flush()
    await w.async_start()
    await w.async_stop()
    w.submit(failed())
    w.confirm_success("user-1", None, None)
    assert w.dropped == 1
    assert w.queued == 0


async def test_held_successes_are_bounded(
    hass: HomeAssistant, store: FenceStore
) -> None:
    """Past max_queue held successes, the oldest is stored unconfirmed."""
    w = FenceWorker(hass, store, NullEnricher(), max_queue=2)
    await w.async_start()
    await w.async_start()
    for i in range(3):
        w.submit(success(f"user-{i}"))
        await w.async_flush()
    assert [(r.user_id, r.confirmed) for r in logins(store)] == [("user-0", False)]
    await w.async_stop()
    assert len(logins(store)) == 3


async def test_worker_survives_internal_error(
    hass: HomeAssistant,
    store: FenceStore,
    worker: FenceWorker,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unexpected error in the pipeline is logged and the loop carries on."""
    original = worker._process  # noqa: SLF001
    calls = 0

    async def process(item: Record) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("secret-username")
        await original(item)

    monkeypatch.setattr(worker, "_process", process)
    worker.submit(failed(user="lost"))
    worker.submit(failed(user="kept"))
    await worker.async_flush()
    assert [r.username_typed for r in logins(store)] == ["kept"]
    assert "Electric Fence worker error: ValueError" in caplog.text
    assert "secret-username" not in caplog.text


async def test_listener_may_submit(
    hass: HomeAssistant, store: FenceStore, worker: FenceWorker
) -> None:
    """A record submitted from a listener is stored too."""

    def resubmit(rec: Record) -> None:
        if isinstance(rec, LoginAttempt):
            worker.submit(change())

    worker.add_listener(resubmit)
    worker.submit(failed())
    await worker.async_flush()
    await worker.async_flush()
    assert len(store.query_changes(ChangeFilter(), limit=10)) == 1


async def test_collectors_installed_and_uninstalled_in_reverse(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setup installs COLLECTOR_TYPES in order; unload uninstalls in reverse."""
    events: list[str] = []

    def make(name: str) -> type[BaseCollector]:
        class Collector(BaseCollector):
            def _install(self) -> None:
                events.append(f"install {self.name}")

            def _uninstall(self) -> None:
                events.append(f"uninstall {self.name}")

        Collector.name = name
        return Collector

    monkeypatch.setattr(
        "custom_components.electric_fence.COLLECTOR_TYPES", [make("a"), make("b")]
    )
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", MIN_HA_VERSION)
    entry = MockConfigEntry(domain=DOMAIN, options=Settings().to_options())
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert [c.name for c in entry.runtime_data.collectors] == ["a", "b"]
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert events == ["install a", "install b", "uninstall b", "uninstall a"]


async def test_setup_wires_runtime(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Setup builds the runtime and a running worker writing to the database."""
    runtime = setup_entry.runtime_data
    assert runtime.collectors == []
    assert runtime.notifier is None
    runtime.worker.submit(failed())
    await flush_worker(hass)
    rows = await hass.async_add_executor_job(logins, runtime.store)
    assert len(rows) == 1
    assert Path(hass.config.path(".storage", "electric_fence.db")).is_file()
    assert await hass.config_entries.async_unload(setup_entry.entry_id)


async def test_daily_purge_uses_retention_days(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The first purge runs after 5 minutes, then daily, using retention_days."""
    store = setup_entry.runtime_data.store
    old = failed(user="old")
    old.ts = dt_util.utcnow() - timedelta(days=90, minutes=1)
    recent = failed(user="recent")
    recent.ts = dt_util.utcnow() - timedelta(days=89)
    await hass.async_add_executor_job(store.insert, [old, recent])

    freezer.tick(timedelta(minutes=5, seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    rows = await hass.async_add_executor_job(logins, store)
    assert [r.username_typed for r in rows] == ["recent"]

    # A day later the 89-day-old row is past 90 days and goes too.
    freezer.tick(timedelta(days=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert await hass.async_add_executor_job(logins, store) == []
    assert await hass.config_entries.async_unload(setup_entry.entry_id)


async def test_purge_failure_is_logged(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing purge logs a warning and does not raise."""

    def purge(cutoff: object) -> int:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(setup_entry.runtime_data.store, "purge_older_than", purge)
    freezer.tick(timedelta(minutes=5, seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert "Electric Fence purge failed: OperationalError" in caplog.text
    assert await hass.config_entries.async_unload(setup_entry.entry_id)


async def test_shutdown_flushes_worker(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Stopping Home Assistant stores what is queued and closes the store."""
    runtime = setup_entry.runtime_data
    runtime.worker.submit(failed(user="last"))
    await hass.async_stop(force=True)
    store = FenceStore(Path(hass.config.path(".storage", "electric_fence.db")))
    store.open()
    try:
        assert [r.username_typed for r in logins(store)] == ["last"]
    finally:
        store.close()


async def test_remove_entry_deletes_database(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Removing the integration deletes the database and the GeoIP folder."""
    db = Path(hass.config.path(".storage", "electric_fence.db"))
    geoip = Path(hass.config.path(".storage", "electric_fence", "geoip"))
    await hass.async_add_executor_job(lambda: geoip.mkdir(parents=True))
    await hass.async_add_executor_job((geoip / "country.mmdb").write_bytes, b"x")
    assert db.is_file()

    assert await hass.config_entries.async_remove(setup_entry.entry_id)
    await hass.async_block_till_done()

    assert not db.exists()
    assert not Path(f"{db}-wal").exists()
    assert not Path(f"{db}-shm").exists()
    assert not geoip.parent.exists()
