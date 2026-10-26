"""App changes made outside Home Assistant (Supervisor installs only).

A stand-in Supervisor client (`tests.helpers`) holds the apps and
repositories; each test changes them and polls.
"""

import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.electric_fence.collectors import apps_outside
from custom_components.electric_fence.collectors.apps_outside import (
    AppDiff,
    OutsideAppWatcher,
    diff_snapshots,
    snapshot_of,
)
from custom_components.electric_fence.const import DOMAIN, MIN_HA_VERSION
from custom_components.electric_fence.models import (
    OUTSIDE_ACTOR,
    ChangeAction,
    ChangeKind,
    SettingsChange,
)
from custom_components.electric_fence.settings import Settings
from custom_components.electric_fence.storage import (
    AppSnapshot,
    ChangeFilter,
    ChangeRow,
    FenceStore,
)
from homeassistant.components.hassio.const import EVENT_SUPERVISOR_EVENT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util
from tests.helpers import (
    change_rows,
    dump_db,
    flush_worker,
    setup_fake_hassio,
    supervisor_client,
)

A = ChangeAction
BUILTIN_REPO = "https://github.com/home-assistant/addons"


def _app(slug: str, **fields: Any) -> SimpleNamespace:
    """Return what `addons.addon_info(slug)` returns, as far as we read it."""
    found: dict[str, Any] = {
        "slug": slug,
        "name": f"App {slug}",
        "version": "1.0",
        "state": "started",
        "options": {},
        "boot": "auto",
        "watchdog": False,
        "auto_update": False,
        "ingress_panel": False,
        "protected": True,
        "network": None,
    }
    found.update(fields)
    return SimpleNamespace(**found)


class FakeSupervisor:
    """The apps and repositories the stand-in client reports."""

    def __init__(self, client: Mock) -> None:
        """Make `client` report these apps and repositories."""
        self.apps: dict[str, SimpleNamespace] = {}
        self.repos: list[str] = [BUILTIN_REPO]
        client.addons.list = AsyncMock(side_effect=self.list_apps)
        client.addons.addon_info = AsyncMock(side_effect=self.info)
        client.store.repositories_list = AsyncMock(side_effect=self.repos_list)
        self.client = client

    async def list_apps(self) -> list[SimpleNamespace]:
        """Stand in for `addons.list()`."""
        return [SimpleNamespace(slug=s) for s in self.apps]

    async def info(self, slug: str) -> SimpleNamespace:
        """Stand in for `addons.addon_info(slug)`."""
        return self.apps[slug]

    async def repos_list(self) -> list[SimpleNamespace]:
        """Stand in for `store.repositories_list()`."""
        return [
            SimpleNamespace(slug=f"r{i}", url=url) for i, url in enumerate(self.repos)
        ]

    def set(self, slug: str, **fields: Any) -> None:
        """Change one field or more of an app (adding it if new)."""
        current = self.apps.get(slug)
        self.apps[slug] = (
            _app(slug, **fields) if current is None else replace_ns(current, fields)
        )


def replace_ns(found: SimpleNamespace, fields: dict[str, Any]) -> SimpleNamespace:
    """Return a copy of `found` with `fields` changed."""
    return SimpleNamespace(**(vars(found) | fields))


@pytest.fixture
def client() -> Mock:
    """Return the Supervisor client Core and Electric Fence use."""
    return supervisor_client()


@pytest.fixture
def sup(client: Mock) -> FakeSupervisor:
    """Return the stand-in Supervisor state, with two apps."""
    found = FakeSupervisor(client)
    found.set("core_ssh", name="Terminal & SSH")
    found.set("core_mosquitto", name="Mosquitto broker")
    return found


@pytest.fixture
async def entry(
    hass: HomeAssistant,
    client: Mock,
    sup: FakeSupervisor,
    monkeypatch: pytest.MonkeyPatch,
) -> MockConfigEntry:
    """Electric Fence on a Supervisor install, its first snapshot taken."""
    await setup_fake_hassio(hass, client, monkeypatch)
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", MIN_HA_VERSION)
    found = MockConfigEntry(domain=DOMAIN, options=Settings().to_options())
    found.add_to_hass(hass)
    assert await hass.config_entries.async_setup(found.entry_id)
    await hass.async_block_till_done()
    return found


def _watcher(entry: MockConfigEntry) -> OutsideAppWatcher:
    found = entry.runtime_data.app_watcher
    assert found is not None
    return found


async def _rows(hass: HomeAssistant, entry: MockConfigEntry) -> list[ChangeRow]:
    """Flush pending bursts and the worker; return the stored app changes."""
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    await flush_worker(hass)
    return [r for r in change_rows(hass) if r.kind == ChangeKind.APP]


def _snap(slug: str = "a", **fields: Any) -> AppSnapshot:
    base: dict[str, Any] = {
        "slug": slug,
        "name": f"App {slug}",
        "version": "1.0",
        "state": "started",
        "options_hash": "h1",
        "settings": {"boot": "auto", "watchdog": False},
    }
    base.update(fields)
    return AppSnapshot(**base)


async def test_first_snapshot_records_nothing(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """The first snapshot ever is stored and records nothing."""
    assert await _rows(hass, entry) == []
    store = entry.runtime_data.store
    snaps = await hass.async_add_executor_job(store.app_snapshot)
    assert set(snaps) == {"core_ssh", "core_mosquitto"}
    assert snaps["core_ssh"].name == "Terminal & SSH"
    repos = await hass.async_add_executor_job(store.app_repositories)
    assert repos == {BUILTIN_REPO}


_DIFFS: list[tuple[str, dict[str, AppSnapshot], dict[str, AppSnapshot], AppDiff]] = [
    (
        "installed",
        {},
        {"a": _snap(version="2.0")},
        AppDiff(
            "a", "App a", A.INSTALLED, {"version_before": None, "version_after": "2.0"}
        ),
    ),
    ("uninstalled", {"a": _snap()}, {}, AppDiff("a", "App a", A.UNINSTALLED, {})),
    (
        "updated",
        {"a": _snap()},
        {"a": _snap(version="2.0")},
        AppDiff(
            "a", "App a", A.UPDATED, {"version_before": "1.0", "version_after": "2.0"}
        ),
    ),
    (
        "started",
        {"a": _snap(state="stopped")},
        {"a": _snap(state="started")},
        AppDiff("a", "App a", A.STARTED, {}),
    ),
    (
        "stopped",
        {"a": _snap(state="started")},
        {"a": _snap(state="stopped")},
        AppDiff("a", "App a", A.STOPPED, {}),
    ),
    (
        "options",
        {"a": _snap()},
        {"a": _snap(options_hash="h2")},
        AppDiff(
            "a", "App a", A.OPTIONS_CHANGED, {"changed": ["options"], "outside": True}
        ),
    ),
    (
        "settings",
        {"a": _snap()},
        {"a": _snap(settings={"boot": "manual", "watchdog": True})},
        AppDiff(
            "a",
            "App a",
            A.SETTINGS_CHANGED,
            {"changed": ["boot", "watchdog"], "outside": True},
        ),
    ),
]


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [case[1:] for case in _DIFFS],
    ids=[case[0] for case in _DIFFS],
)
def test_diff_table(
    before: dict[str, AppSnapshot],
    after: dict[str, AppSnapshot],
    expected: AppDiff,
) -> None:
    """Each snapshot difference gives its one diff."""
    assert diff_snapshots(before, after) == [expected]


def test_diff_unchanged_and_transient_states() -> None:
    """No change, or a state other than started or stopped, gives nothing."""
    assert diff_snapshots({"a": _snap()}, {"a": _snap()}) == []
    assert diff_snapshots({"a": _snap()}, {"a": _snap(state="error")}) == []
    assert diff_snapshots({"a": _snap(state="startup")}, {"a": _snap()}) == [
        AppDiff("a", "App a", A.STARTED, {})
    ]


def test_snapshot_of_hashes_options() -> None:
    """Options are kept as a sha256 of their canonical JSON only."""
    info = _app(
        "x", options={"b": 1, "a": [1, 2]}, boot="manual", network={"22/tcp": 22}
    )
    found = snapshot_of(info)
    assert (
        found.options_hash
        == snapshot_of(_app("x", options={"a": [1, 2], "b": 1})).options_hash
    )
    assert found.options_hash is not None
    assert len(found.options_hash) == 64
    assert found.settings == {
        "boot": "manual",
        "watchdog": False,
        "auto_update": False,
        "ingress_panel": False,
        "protected": True,
        "network": {"22/tcp": 22},
    }


async def test_options_change_hash_only(
    hass: HomeAssistant, entry: MockConfigEntry, sup: FakeSupervisor
) -> None:
    """An outside options change records no values, even with record_values on."""
    runtime = entry.runtime_data
    runtime.settings = replace(runtime.settings, record_values=True)
    sup.set("core_ssh", options={"password": "a"})
    await _watcher(entry).poll()
    sup.set("core_ssh", options={"password": "b"})
    await _watcher(entry).poll()
    rows = await _rows(hass, entry)
    assert [r.action for r in rows] == [A.OPTIONS_CHANGED] * 2
    row = rows[-1]
    assert row.target_id == "core_ssh"
    assert row.details == {"changed": ["options"], "outside": True}
    assert row.actor_user_id is None
    assert row.actor_name == OUTSIDE_ACTOR
    snaps = await hass.async_add_executor_job(runtime.store.app_snapshot)
    assert snaps["core_ssh"].options_hash is not None
    dump = dump_db(hass)
    assert "password" not in dump
    assert '"a"' not in dump
    assert '"b"' not in dump


async def test_core_change_suppresses_outside(
    hass: HomeAssistant, entry: MockConfigEntry, sup: FakeSupervisor
) -> None:
    """A restart Core recorded isn't recorded again as an outside start."""
    watcher = _watcher(entry)
    sup.set("core_ssh", state="stopped")
    watcher.note_core_change("core_ssh", A.RESTARTED, dt_util.utcnow())
    await watcher.poll()
    sup.set("core_ssh", state="started")
    await watcher.poll()
    assert await _rows(hass, entry) == []


async def test_core_change_suppresses_same_action_only(
    hass: HomeAssistant, entry: MockConfigEntry, sup: FakeSupervisor
) -> None:
    """A Core start doesn't hide an outside update of the same app."""
    watcher = _watcher(entry)
    watcher.note_core_change("core_ssh", A.STARTED, dt_util.utcnow())
    sup.set("core_ssh", version="2.0")
    await watcher.poll()
    [row] = await _rows(hass, entry)
    assert row.action == A.UPDATED


async def test_core_suppression_expires(
    hass: HomeAssistant, entry: MockConfigEntry, sup: FakeSupervisor
) -> None:
    """A Core change more than 120 s ago no longer suppresses."""
    watcher = _watcher(entry)
    watcher.note_core_change(
        "core_ssh", A.UPDATED, dt_util.utcnow() - timedelta(seconds=121)
    )
    sup.set("core_ssh", version="2.0")
    await watcher.poll()
    [row] = await _rows(hass, entry)
    assert row.action == A.UPDATED
    assert row.actor_name == OUTSIDE_ACTOR


async def test_collector_notes_core_changes(
    hass: HomeAssistant, entry: MockConfigEntry, sup: FakeSupervisor
) -> None:
    """A restart through Core's service is recorded once, by the collector."""
    await hass.services.async_call(
        "hassio", "addon_restart", {"addon": "core_ssh"}, blocking=True
    )
    await hass.async_block_till_done()
    sup.set("core_ssh", state="stopped")
    await _watcher(entry).poll()
    sup.set("core_ssh", state="started")
    await _watcher(entry).poll()
    [row] = await _rows(hass, entry)
    assert row.action == A.RESTARTED
    assert row.actor_name is None


async def test_reboot_burst_collapses(
    hass: HomeAssistant, entry: MockConfigEntry, sup: FakeSupervisor
) -> None:
    """12 apps start; one was restarted from the panel (Review Focus 4)."""
    watcher = _watcher(entry)
    sup.apps.clear()
    for i in range(12):
        sup.set(f"app{i:02}", state="stopped")
    await watcher.poll()
    first = await _rows(hass, entry)
    # Task 5's record of the panel restart, and its note to the watcher.
    now = dt_util.utcnow()
    entry.runtime_data.worker.submit(
        SettingsChange(
            ts=now,
            kind=ChangeKind.APP,
            action=A.RESTARTED,
            target_id="app00",
            target_name="App app00",
            details={},
            actor_user_id="u1",
            actor_name="Paulus",
            actor_ip="127.0.0.1",
        )
    )
    watcher.note_core_change("app00", A.RESTARTED, now)
    for i in range(12):
        sup.set(f"app{i:02}", state="started")
    await watcher.poll()
    core, outside = (await _rows(hass, entry))[len(first) :]
    assert core.actor_name == "Paulus"
    assert core.target_id == "app00"
    assert outside.action == A.STARTED
    assert outside.target_id is None
    assert outside.target_name == "11 apps"
    assert outside.actor_user_id is None
    assert outside.actor_name == OUTSIDE_ACTOR
    assert outside.details == {
        "apps": [{"slug": f"app{i:02}", "name": f"App app{i:02}"} for i in range(1, 12)]
    }


async def test_burst_spans_polls(
    hass: HomeAssistant, entry: MockConfigEntry, sup: FakeSupervisor
) -> None:
    """Stops from two polls within 60 s collapse into one record."""
    watcher = _watcher(entry)
    sup.set("core_ssh", state="stopped")
    await watcher.poll()
    sup.set("core_mosquitto", state="stopped")
    await watcher.poll()
    [row] = await _rows(hass, entry)
    assert row.action == A.STOPPED
    assert row.target_name == "2 apps"


async def test_single_start_is_its_own_record(
    hass: HomeAssistant, entry: MockConfigEntry, sup: FakeSupervisor
) -> None:
    """One app starting alone is recorded with its own target."""
    sup.set("core_ssh", state="stopped")
    await _watcher(entry).poll()
    [row] = await _rows(hass, entry)
    assert row.action == A.STOPPED
    assert row.target_id == "core_ssh"
    assert row.target_name == "Terminal & SSH"
    assert row.actor_name == OUTSIDE_ACTOR


async def test_unload_flushes_pending_burst(
    hass: HomeAssistant, entry: MockConfigEntry, sup: FakeSupervisor
) -> None:
    """A burst still in its window is recorded on unload."""
    sup.set("core_ssh", state="stopped")
    sup.set("core_mosquitto", state="stopped")
    await _watcher(entry).poll()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    store = FenceStore(Path(hass.config.path(".storage", "electric_fence.db")))
    await hass.async_add_executor_job(store.open)
    try:
        rows = await hass.async_add_executor_job(
            store.query_changes, ChangeFilter(include_system=True)
        )
    finally:
        await hass.async_add_executor_job(store.close)
    assert [r.target_name for r in rows if r.kind == ChangeKind.APP] == ["2 apps"]


async def test_auto_update_is_system(
    hass: HomeAssistant, entry: MockConfigEntry, sup: FakeSupervisor
) -> None:
    """Supervisor's auto-update is recorded as System; a manual one Outside."""
    watcher = _watcher(entry)
    sup.set("core_ssh", auto_update=True)
    await watcher.poll()
    await _rows(hass, entry)
    sup.set("core_ssh", version="2.0")
    sup.set("core_mosquitto", version="2.0")
    await watcher.poll()
    rows = [r for r in await _rows(hass, entry) if r.action == A.UPDATED]
    by_slug = {r.target_id: r for r in rows}
    assert by_slug["core_ssh"].actor_user_id is None
    assert by_slug["core_ssh"].actor_name is None
    assert by_slug["core_ssh"].details == {
        "version_before": "1.0",
        "version_after": "2.0",
    }
    assert by_slug["core_mosquitto"].actor_name == OUTSIDE_ACTOR


async def test_repository_added_outside(
    hass: HomeAssistant, entry: MockConfigEntry, sup: FakeSupervisor
) -> None:
    """A repository added outside is recorded, its credentials masked."""
    sup.repos.append("https://user:s3cret@github.com/x/apps")
    await _watcher(entry).poll()
    [row] = await _rows(hass, entry)
    assert row.action == A.REPOSITORY_ADDED
    assert row.target_id == "https://•••@github.com/x/apps"
    assert row.details == {"repository": "https://•••@github.com/x/apps"}
    assert row.actor_name == OUTSIDE_ACTOR
    store = entry.runtime_data.store
    repos = await hass.async_add_executor_job(store.app_repositories)
    assert repos == {BUILTIN_REPO, "https://•••@github.com/x/apps"}
    assert "s3cret" not in dump_db(hass)


async def test_repository_removed_outside_and_core_suppression(
    hass: HomeAssistant, entry: MockConfigEntry, sup: FakeSupervisor
) -> None:
    """A Core-noted repository change hides the same outside one."""
    watcher = _watcher(entry)
    sup.repos.append("https://github.com/x/apps")
    watcher.note_core_change(
        "https://github.com/x/apps", A.REPOSITORY_ADDED, dt_util.utcnow()
    )
    await watcher.poll()
    sup.repos.remove(BUILTIN_REPO)
    await watcher.poll()
    [row] = await _rows(hass, entry)
    assert row.action == A.REPOSITORY_REMOVED
    assert row.target_id == BUILTIN_REPO


async def test_poll_failure_keeps_snapshot(
    hass: HomeAssistant, entry: MockConfigEntry, sup: FakeSupervisor
) -> None:
    """A failing Supervisor read keeps the old snapshot and records nothing."""
    watcher = _watcher(entry)
    store = entry.runtime_data.store
    before = await hass.async_add_executor_job(store.app_snapshot)
    sup.set("core_ssh", version="2.0")
    sup.client.addons.list.side_effect = RuntimeError("down")
    await watcher.poll()
    assert await _rows(hass, entry) == []
    assert await hass.async_add_executor_job(store.app_snapshot) == before
    sup.client.addons.list.side_effect = sup.list_apps
    sup.client.addons.addon_info.side_effect = RuntimeError("down")
    await watcher.poll()
    assert await _rows(hass, entry) == []
    sup.client.addons.addon_info.side_effect = sup.info
    await watcher.poll()
    [row] = await _rows(hass, entry)
    assert row.details == {"version_before": "1.0", "version_after": "2.0"}


async def test_changes_while_down_recorded_on_startup(
    hass: HomeAssistant, entry: MockConfigEntry, sup: FakeSupervisor
) -> None:
    """A later startup compares with the stored snapshot."""
    sup.set("core_ssh", version="3.0")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    [row] = await _rows(hass, entry)
    assert row.action == A.UPDATED
    assert row.actor_name == OUTSIDE_ACTOR


async def test_supervisor_events_poll_debounced(
    hass: HomeAssistant, entry: MockConfigEntry, sup: FakeSupervisor
) -> None:
    """App and store events poll once, 5 s after the last; others don't."""
    calls = sup.client.addons.list
    calls.reset_mock()
    async_dispatcher_send(hass, EVENT_SUPERVISOR_EVENT, {"event": "health_changed"})
    async_dispatcher_send(
        hass, EVENT_SUPERVISOR_EVENT, {"event": "job", "data": {"name": "backup_x"}}
    )
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done()
    assert calls.await_count == 0
    async_dispatcher_send(
        hass,
        EVENT_SUPERVISOR_EVENT,
        {"event": "addon", "slug": "x", "state": "started"},
    )
    async_dispatcher_send(hass, EVENT_SUPERVISOR_EVENT, {"event": "store_reloaded"})
    async_dispatcher_send(
        hass,
        EVENT_SUPERVISOR_EVENT,
        {"event": "job", "data": {"name": "app_update", "done": True}},
    )
    await hass.async_block_till_done()
    assert calls.await_count == 0
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done()
    assert calls.await_count == 1


async def test_periodic_poll(
    hass: HomeAssistant, entry: MockConfigEntry, sup: FakeSupervisor
) -> None:
    """Supervisor is polled every 15 minutes."""
    sup.set("core_ssh", version="2.0")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=15, seconds=1))
    await hass.async_block_till_done()
    [row] = await _rows(hass, entry)
    assert row.action == A.UPDATED


async def test_one_poll_at_a_time(
    hass: HomeAssistant, entry: MockConfigEntry, sup: FakeSupervisor
) -> None:
    """Polls asked for while one runs don't run alongside it."""
    watcher = _watcher(entry)
    running = 0
    peak = 0
    real = sup.list_apps

    async def slow() -> list[SimpleNamespace]:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        for _ in range(5):
            await asyncio.sleep(0)
        running -= 1
        return await real()

    sup.client.addons.list.side_effect = slow
    sup.client.addons.list.reset_mock()
    await asyncio.gather(watcher.poll(), watcher.poll(), watcher.poll())
    assert peak == 1
    # The two asked for during the first run once, together, after it.
    assert sup.client.addons.list.await_count == 2


async def test_not_loaded_without_supervisor(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Without Supervisor there is no watcher."""
    assert setup_entry.runtime_data.app_watcher is None


def test_event_names_match_core() -> None:
    """The signal and event names the watcher uses are Core's."""
    from homeassistant.components.hassio import const  # noqa: PLC0415

    assert apps_outside.SIGNAL_SUPERVISOR_EVENT == const.EVENT_SUPERVISOR_EVENT
    assert apps_outside.EVENT_JOB == const.EVENT_JOB
    assert {const.EVENT_ADDON, const.EVENT_STORE_RELOADED} <= apps_outside.POLL_EVENTS


async def test_load_failure_still_watches(
    hass: HomeAssistant, entry: MockConfigEntry, sup: FakeSupervisor
) -> None:
    """A store that can't be read at start records nothing, then keeps watching."""
    watcher = OutsideAppWatcher(hass, entry.runtime_data)
    store = Mock(app_snapshot=Mock(side_effect=OSError("disk")))
    watcher.runtime = SimpleNamespace(store=store, worker=entry.runtime_data.worker)  # type: ignore[assignment]
    store.save_app_snapshot = Mock()
    store.save_app_repositories = Mock()
    await watcher.start()
    sup.set("core_ssh", version="2.0")
    await watcher.poll()
    await watcher.stop()
    [row] = await _rows(hass, entry)
    assert row.action == A.UPDATED
    assert store.save_app_snapshot.call_count == 2


async def test_background_core_update_pending(
    hass: HomeAssistant, entry: MockConfigEntry, sup: FakeSupervisor
) -> None:
    """A Core background update suppresses its diff past 120 s, once."""
    watcher = _watcher(entry)
    accepted = dt_util.utcnow() - timedelta(seconds=200)
    watcher.note_core_change("core_ssh", A.UPDATED, accepted, pending=True)
    sup.set("core_ssh", version="2.0")
    await watcher.poll()
    assert await _rows(hass, entry) == []
    sup.set("core_ssh", version="3.0")
    await watcher.poll()
    [row] = await _rows(hass, entry)
    assert row.details == {"version_before": "2.0", "version_after": "3.0"}
    assert row.actor_name == OUTSIDE_ACTOR


async def test_background_core_install_pending(
    hass: HomeAssistant, entry: MockConfigEntry, sup: FakeSupervisor
) -> None:
    """A Core background install finishing late isn't recorded again."""
    watcher = _watcher(entry)
    accepted = dt_util.utcnow() - timedelta(minutes=10)
    watcher.note_core_change("core_new", A.INSTALLED, accepted, pending=True)
    sup.set("core_new")
    await watcher.poll()
    assert await _rows(hass, entry) == []


async def test_pending_note_expires_after_an_hour(
    hass: HomeAssistant, entry: MockConfigEntry, sup: FakeSupervisor
) -> None:
    """A pending note older than an hour no longer suppresses."""
    watcher = _watcher(entry)
    accepted = dt_util.utcnow() - timedelta(hours=1, seconds=1)
    watcher.note_core_change("core_ssh", A.UPDATED, accepted, pending=True)
    sup.set("core_ssh", version="2.0")
    await watcher.poll()
    [row] = await _rows(hass, entry)
    assert row.action == A.UPDATED
    assert row.actor_name == OUTSIDE_ACTOR


async def test_collector_passes_background_as_pending(
    hass: HomeAssistant, entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The collector notes a background update as pending."""
    from custom_components.electric_fence.collectors.apps import (  # noqa: PLC0415
        AppCollector,
        AppOp,
        _Before,
    )
    from custom_components.electric_fence.collectors.system_settings import Actor  # noqa: PLC0415

    noted = Mock()
    monkeypatch.setattr(_watcher(entry), "note_core_change", noted)
    collector = next(
        c for c in entry.runtime_data.collectors if isinstance(c, AppCollector)
    )
    op = AppOp(A.UPDATED, "core_ssh", None, (), background=True)
    collector._record(op, Actor(None, None, None), _Before(), None, None)  # noqa: SLF001
    [call] = noted.call_args_list
    assert call.args[:2] == ("core_ssh", A.UPDATED)
    assert call.kwargs == {"pending": True}


async def test_options_with_core_update_skipped(
    hass: HomeAssistant, entry: MockConfigEntry, sup: FakeSupervisor
) -> None:
    """New option defaults from a Core update aren't an outside change."""
    watcher = _watcher(entry)
    watcher.note_core_change("core_ssh", A.UPDATED, dt_util.utcnow())
    sup.set("core_ssh", version="2.0", options={"new_default": 1})
    await watcher.poll()
    assert await _rows(hass, entry) == []


async def test_options_with_outside_update_recorded(
    hass: HomeAssistant, entry: MockConfigEntry, sup: FakeSupervisor
) -> None:
    """Options changed alongside an outside update are recorded, hash only."""
    sup.set("core_ssh", version="2.0", options={"password": "x"})
    await _watcher(entry).poll()
    rows = await _rows(hass, entry)
    by_action = {r.action: r for r in rows}
    assert set(by_action) == {A.UPDATED, A.OPTIONS_CHANGED}
    assert by_action[A.OPTIONS_CHANGED].details == {
        "changed": ["options"],
        "outside": True,
        "with_update": True,
    }
    assert by_action[A.OPTIONS_CHANGED].actor_name == OUTSIDE_ACTOR


async def test_auto_update_turned_on_with_update_is_outside(
    hass: HomeAssistant, entry: MockConfigEntry, sup: FakeSupervisor
) -> None:
    """System needs auto-update on before and after the update."""
    sup.set("core_ssh", version="2.0", auto_update=True)
    await _watcher(entry).poll()
    [row] = [r for r in await _rows(hass, entry) if r.action == A.UPDATED]
    assert row.actor_name == OUTSIDE_ACTOR


async def test_stop_waits_for_poll_in_flight(
    hass: HomeAssistant, entry: MockConfigEntry, sup: FakeSupervisor
) -> None:
    """A poll whose read ends after stop leaves the stored snapshot as it was."""
    watcher = _watcher(entry)
    store = entry.runtime_data.store
    before = await hass.async_add_executor_job(store.app_snapshot)
    release = asyncio.Event()
    real = sup.list_apps

    async def held() -> list[SimpleNamespace]:
        await release.wait()
        return await real()

    sup.client.addons.list.side_effect = held
    sup.set("core_ssh", version="2.0")
    polling = asyncio.ensure_future(watcher.poll())
    await asyncio.sleep(0)
    stopping = asyncio.ensure_future(watcher.stop())
    await asyncio.sleep(0)
    assert not stopping.done()
    release.set()
    await asyncio.gather(polling, stopping)
    assert await hass.async_add_executor_job(store.app_snapshot) == before
    await watcher.poll()
    assert await _rows(hass, entry) == []


def test_is_poll_event_ignores_non_dicts() -> None:
    """Anything but a dict is not a Supervisor event we poll for."""
    assert apps_outside.is_poll_event("addon") is False
    assert apps_outside.is_poll_event(None) is False


async def test_start_after_stop_does_nothing(
    hass: HomeAssistant, entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A watcher stopped before it started takes no snapshot."""
    watcher = _watcher(entry)
    poll = AsyncMock()
    monkeypatch.setattr(watcher, "poll", poll)
    watcher._stopped = True  # noqa: SLF001
    await watcher.start()
    poll.assert_not_awaited()


async def test_start_survives_listen_failure(
    hass: HomeAssistant, entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If listening can't start, start gives up quietly without polling."""
    watcher = _watcher(entry)
    poll = AsyncMock()
    monkeypatch.setattr(watcher, "poll", poll)
    monkeypatch.setattr(watcher, "_listen", Mock(side_effect=RuntimeError))
    await watcher.start()
    poll.assert_not_awaited()


async def test_listen_after_stop_does_nothing(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """A stopped watcher doesn't start listening."""
    watcher = _watcher(entry)
    await watcher.stop()
    watcher._listen()  # noqa: SLF001
    assert watcher._debouncer is None  # noqa: SLF001
    assert watcher._unsubs == []  # noqa: SLF001


async def test_stop_gives_up_on_a_poll_that_will_not_finish(
    hass: HomeAssistant, entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poll still running at stop is not waited for forever."""
    watcher = _watcher(entry)
    monkeypatch.setattr(apps_outside, "READ_TIMEOUT", 0.01)
    await watcher._lock.acquire()  # noqa: SLF001
    await watcher.stop()
    assert watcher._lock.locked()  # noqa: SLF001
    watcher._lock.release()  # noqa: SLF001


async def test_core_change_without_app_is_ignored(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """A Core change with no app slug (a repository) suppresses nothing."""
    watcher = _watcher(entry)
    watcher.note_core_change(None, A.REPOSITORY_ADDED, dt_util.utcnow())
    assert watcher._core == {}  # noqa: SLF001
    diff = AppDiff(None, None, A.REPOSITORY_ADDED, {})
    assert watcher._suppressed(diff, dt_util.utcnow()) is False  # noqa: SLF001


async def test_event_handling_errors_are_contained(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """An error while handling a Supervisor event is swallowed."""
    watcher = _watcher(entry)
    watcher._debouncer = Mock(  # type: ignore[assignment]  # noqa: SLF001
        async_schedule_call=Mock(side_effect=RuntimeError)
    )
    watcher._on_supervisor_event({"event": "addon"})  # noqa: SLF001


async def test_snapshot_save_failure_does_not_stop_recording(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    sup: FakeSupervisor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the snapshot can't be stored, the change is still recorded."""
    watcher = _watcher(entry)
    monkeypatch.setattr(watcher, "_save", Mock(side_effect=OSError))
    sup.set("core_ssh", version="2.0")
    await watcher.poll()
    [row] = await _rows(hass, entry)
    assert row.action == "updated"


async def test_worker_failure_does_not_escape(
    hass: HomeAssistant, entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the worker refuses a record, nothing is raised."""
    watcher = _watcher(entry)
    monkeypatch.setattr(
        watcher.runtime.worker, "submit", Mock(side_effect=RuntimeError)
    )
    watcher._submit(A.STARTED, "a", "App a", {}, dt_util.utcnow(), False)  # noqa: SLF001
