"""Tests for the ban manager."""

import asyncio
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from ipaddress import IPv4Address, ip_address
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.electric_fence.models import (
    ChangeAction,
    ChangeKind,
    LoginAttempt,
    SettingsChange,
)
from custom_components.electric_fence.notifier import EVENT_IP_BANNED
from custom_components.electric_fence.protection.backend import CoreBanBackend
from custom_components.electric_fence.protection.bans import BanManager
from custom_components.electric_fence.runtime import Runtime
from custom_components.electric_fence.storage import FenceStore
from homeassistant.components.http.ban import KEY_BAN_MANAGER, IpBanManager
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from tests.helpers import make_runtime

IP = "203.0.113.9"
ADDR = ip_address(IP)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[FenceStore]:
    """Return an open store."""
    s = FenceStore(tmp_path / "fence.db")
    s.open()
    yield s
    s.close()


@pytest.fixture
def runtime(hass: HomeAssistant, store: FenceStore) -> Runtime:
    """Return a runtime whose worker records what is submitted."""
    rt = make_runtime(hass, store)
    rt.worker.submit = Mock(return_value=True)  # type: ignore[method-assign]
    return rt


async def _http(hass: HomeAssistant, enabled: bool = True) -> None:
    assert await async_setup_component(
        hass, "http", {"http": {"ip_ban_enabled": enabled}}
    )
    await hass.async_block_till_done()


def _core(hass: HomeAssistant) -> IpBanManager:
    return hass.http.app[KEY_BAN_MANAGER]


async def _manager(
    hass: HomeAssistant, runtime: Runtime, *, enabled: bool = True
) -> BanManager:
    await _http(hass, enabled)
    manager = BanManager(hass, runtime, CoreBanBackend(hass))
    await manager.async_start()
    await hass.async_block_till_done()
    return manager


def _changes(runtime: Runtime, action: ChangeAction) -> list[SettingsChange]:
    submit: Mock = runtime.worker.submit  # type: ignore[assignment]
    return [
        c.args[0]
        for c in submit.call_args_list
        if isinstance(c.args[0], SettingsChange)
        and c.args[0].kind == ChangeKind.IP_BAN
        and c.args[0].action == action
    ]


def _write_bans_file(hass: HomeAssistant, ip: str, banned_at: str) -> None:
    Path(hass.config.path("ip_bans.yaml")).write_text(
        f'"{ip}":\n  banned_at: "{banned_at}"\n', encoding="utf8"
    )


async def test_ban_applies_immediately(
    hass: HomeAssistant, runtime: Runtime, store: FenceStore
) -> None:
    """The ban is in Core's lookup as soon as async_ban returns."""
    manager = await _manager(hass, runtime)
    assert manager.available
    assert manager.unavailable_reason is None
    assert await manager.async_ban(IP, 5, "NL", None) is True
    assert ip_address(IP) in _core(hass).ip_bans_lookup
    [row] = store.active_bans()
    assert row.ip == IP
    assert row.failures == 5
    assert row.country_code == "NL"
    assert row.ban_number == 1
    assert row.expires_at == row.banned_at + timedelta(hours=24)
    [view] = manager.active()
    assert view.source == "electric_fence"
    assert view.ban_id == row.id
    assert manager.bans_24h() == 1
    assert manager.last_ban_at == row.banned_at
    [change] = _changes(runtime, ChangeAction.BANNED)
    assert change.target_id == IP
    assert change.actor_user_id is None
    assert change.details == {
        "failures": 5,
        "expires_at": row.expires_at.isoformat(),
        "ban_number": 1,
    }
    manager.async_stop()


async def test_ban_mapped_ipv6_is_ipv4(hass: HomeAssistant, runtime: Runtime) -> None:
    """An IPv4-mapped IPv6 address is banned as its IPv4 address."""
    manager = await _manager(hass, runtime)
    assert await manager.async_ban("::ffff:203.0.113.9", 5, None, None)
    assert IPv4Address(IP) in _core(hass).ip_bans_lookup
    assert [v.ip for v in manager.active()] == [IPv4Address(IP)]
    assert await manager.async_ban(IP, 5, None, None) is False
    manager.async_stop()


async def test_never_bans_exempt(hass: HomeAssistant, runtime: Runtime) -> None:
    """Private and unparsable addresses are never banned."""
    manager = await _manager(hass, runtime)
    before = dict(_core(hass).ip_bans_lookup)
    assert await manager.async_ban("192.168.1.9", 5, None, None) is False
    assert await manager.async_ban("not an ip", 5, None, None) is False
    assert _core(hass).ip_bans_lookup == before
    manager.async_stop()


async def test_already_banned(hass: HomeAssistant, runtime: Runtime) -> None:
    """A second ban of the same address is refused."""
    manager = await _manager(hass, runtime)
    assert await manager.async_ban(IP, 5, None, None)
    assert await manager.async_ban(IP, 6, None, None) is False
    assert len(_changes(runtime, ChangeAction.BANNED)) == 1
    manager.async_stop()


async def test_expires(
    hass: HomeAssistant,
    runtime: Runtime,
    store: FenceStore,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A ban ends when its time is up."""
    runtime.settings = replace(runtime.settings, ban_hours=1)
    alerts = MagicMock()
    runtime.alerts = alerts
    manager = await _manager(hass, runtime)
    changed = Mock()
    manager.add_listener(changed)
    assert await manager.async_ban(IP, 5, None, None)
    freezer.tick(timedelta(minutes=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert ADDR not in _core(hass).ip_bans_lookup
    assert store.active_bans() == []
    [ended] = store.query_bans(active=False)
    assert ended.end_reason == "expired"
    assert len(_changes(runtime, ChangeAction.EXPIRED)) == 1
    alerts.ip_banned.assert_called_once()
    [(view, how)] = [c.args for c in alerts.ban_ended.call_args_list]
    assert how == "expired"
    assert view.ip == ADDR
    assert changed.call_count == 2
    assert manager.active() == []
    manager.async_stop()


async def test_permanent_ban_never_expires(
    hass: HomeAssistant,
    runtime: Runtime,
    store: FenceStore,
    freezer: FrozenDateTimeFactory,
) -> None:
    """With ban_hours 0 a ban has no end."""
    runtime.settings = replace(runtime.settings, ban_hours=0)
    manager = await _manager(hass, runtime)
    assert await manager.async_ban(IP, 5, None, None)
    freezer.tick(timedelta(days=400))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    [row] = store.active_bans()
    assert row.expires_at is None
    assert ADDR in _core(hass).ip_bans_lookup
    manager.async_stop()


async def test_expired_while_off(
    hass: HomeAssistant, runtime: Runtime, store: FenceStore
) -> None:
    """A ban that ran out while Home Assistant was off ends at startup."""
    hass.set_state(CoreState.not_running)
    now = dt_util.utcnow()
    banned_at = now - timedelta(hours=30)
    store.add_ban(IP, banned_at, now - timedelta(hours=6), 5, 1, None, None)
    _write_bans_file(hass, IP, banned_at.isoformat())
    manager = await _manager(hass, runtime)
    await _core(hass).async_load()
    assert ADDR in _core(hass).ip_bans_lookup
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert ADDR not in _core(hass).ip_bans_lookup
    assert store.active_bans() == []
    [ended] = store.query_bans(active=False)
    assert ended.end_reason == "expired"
    assert len(_changes(runtime, ChangeAction.EXPIRED)) == 1
    manager.async_stop()


async def test_reconcile_after_core_load(
    hass: HomeAssistant, runtime: Runtime, store: FenceStore
) -> None:
    """Reconcile waits for Core to load its bans, so it does not see them gone."""
    hass.set_state(CoreState.not_running)
    now = dt_util.utcnow()
    store.add_ban(IP, now, now + timedelta(hours=10), 5, 1, None, None)
    gone = "198.51.100.7"
    store.add_ban(gone, now, now + timedelta(hours=10), 5, 1, None, None)
    _write_bans_file(hass, IP, now.isoformat())
    manager = await _manager(hass, runtime)
    # Before Core's ban_startup the lookup is empty; nothing may be ended yet.
    assert _core(hass).ip_bans_lookup == {}
    assert len(store.active_bans()) == 2
    await _core(hass).async_load()
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    [row] = store.active_bans()
    assert row.ip == IP
    [ended] = store.query_bans(active=False)
    assert (ended.ip, ended.end_reason) == (gone, "removed")
    [removed] = _changes(runtime, ChangeAction.REMOVED)
    assert (removed.target_id, removed.actor_user_id) == (gone, None)
    assert ADDR in _core(hass).ip_bans_lookup
    assert [v.ip for v in manager.active()] == [ADDR]
    manager.async_stop()


async def test_unban(hass: HomeAssistant, runtime: Runtime, store: FenceStore) -> None:
    """Unbanning an Electric Fence ban ends its row with who did it."""
    alerts = MagicMock()
    runtime.alerts = alerts
    manager = await _manager(hass, runtime)
    assert await manager.async_ban(IP, 5, None, None)
    assert await manager.async_unban("::ffff:203.0.113.9", "uid", "Admin") is True
    assert ADDR not in _core(hass).ip_bans_lookup
    [ended] = store.query_bans(active=False)
    assert (ended.end_reason, ended.ended_by_user_id, ended.ended_by_name) == (
        "unbanned",
        "uid",
        "Admin",
    )
    [change] = _changes(runtime, ChangeAction.UNBANNED)
    assert (change.target_id, change.actor_user_id, change.actor_name) == (
        IP,
        "uid",
        "Admin",
    )
    assert alerts.ban_ended.call_args.args[1] == "unbanned"
    assert await manager.async_unban(IP, "uid", "Admin") is False
    assert await manager.async_unban("nonsense", "uid", "Admin") is False
    manager.async_stop()


async def test_unban_other(
    hass: HomeAssistant, runtime: Runtime, store: FenceStore
) -> None:
    """A ban Electric Fence did not make shows as other and can be unbanned."""
    _write_bans_file(hass, IP, "2026-01-02T03:04:05+00:00")
    await _http(hass)
    await _core(hass).async_load()
    manager = BanManager(hass, runtime, CoreBanBackend(hass))
    await manager.async_start()
    await hass.async_block_till_done()
    [view] = manager.active()
    assert view.source == "other"
    assert view.expires_at is None
    assert view.ban_id is None
    assert await manager.async_unban(IP, "uid", "Admin") is True
    assert ADDR not in _core(hass).ip_bans_lookup
    assert manager.active() == []
    [change] = _changes(runtime, ChangeAction.UNBANNED)
    assert change.target_id == IP
    assert change.actor_name == "Admin"
    assert store.query_bans(active=False) == []
    manager.async_stop()


async def test_repeat_offender(
    hass: HomeAssistant,
    runtime: Runtime,
    store: FenceStore,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Each further ban within 30 days doubles; the fourth is permanent."""
    runtime.settings = replace(runtime.settings, ban_hours=1, ban_repeat_offenders=True)
    manager = await _manager(hass, runtime)
    lengths: list[timedelta | None] = []
    for _ in range(4):
        assert await manager.async_ban(IP, 5, None, None)
        [row] = store.active_bans()
        lengths.append(
            row.expires_at - row.banned_at if row.expires_at is not None else None
        )
        assert await manager.async_unban(IP, None, None)
        freezer.tick(timedelta(days=1))
    assert lengths == [
        timedelta(hours=1),
        timedelta(hours=2),
        timedelta(hours=4),
        None,
    ]
    numbers = [c.details["ban_number"] for c in _changes(runtime, ChangeAction.BANNED)]
    assert numbers == [1, 2, 3, 4]
    manager.async_stop()


async def test_repeat_count_resets_after_30_days(
    hass: HomeAssistant,
    runtime: Runtime,
    store: FenceStore,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Bans older than 30 days do not count, and repeat-off keeps ban_hours."""
    runtime.settings = replace(runtime.settings, ban_hours=1)
    manager = await _manager(hass, runtime)
    assert await manager.async_ban(IP, 5, None, None)
    assert await manager.async_unban(IP, None, None)
    assert await manager.async_ban(IP, 5, None, None)
    [row] = store.active_bans()
    assert row.ban_number == 2
    assert row.expires_at == row.banned_at + timedelta(hours=1)
    assert await manager.async_unban(IP, None, None)
    runtime.settings = replace(runtime.settings, ban_repeat_offenders=True)
    freezer.tick(timedelta(days=31))
    assert await manager.async_ban(IP, 5, None, None)
    [row] = store.active_bans()
    assert row.ban_number == 1
    assert manager.bans_24h() == 1
    manager.async_stop()


async def test_unavailable_when_ip_ban_disabled(
    hass: HomeAssistant, runtime: Runtime
) -> None:
    """Without Core's ban manager nothing is banned, and nothing raises."""
    manager = await _manager(hass, runtime, enabled=False)
    assert manager.available is False
    assert manager.unavailable_reason == "ip_ban_disabled"
    assert await manager.async_ban(IP, 5, None, None) is False
    assert await manager.async_unban(IP, None, None) is False
    assert manager.active() == []
    manager.async_stop()


async def test_unavailable_when_contracts_fail(
    hass: HomeAssistant, runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken contract disables bans and raises a repair issue."""
    monkeypatch.setattr(
        "custom_components.electric_fence.protection.bans.check_contracts",
        lambda hass, collector: ["IpBan is missing"],
    )
    report = Mock()
    monkeypatch.setattr(
        "custom_components.electric_fence.protection.bans."
        "async_report_disabled_collector",
        report,
    )
    manager = await _manager(hass, runtime)
    report.assert_called_once_with(hass, "protection", ["IpBan is missing"])
    assert manager.available is False
    assert manager.unavailable_reason == "contracts"
    assert await manager.async_ban(IP, 5, None, None) is False
    manager.async_stop()


async def test_event_payload(hass: HomeAssistant, runtime: Runtime) -> None:
    """A ban fires an event describing it."""
    events = async_capture_events(hass, EVENT_IP_BANNED)
    manager = await _manager(hass, runtime)
    assert await manager.async_ban(IP, 7, "NL", "host.example")
    await hass.async_block_till_done()
    [event] = events
    assert event.data["ip"] == IP
    assert event.data["country"] == "NL"
    assert event.data["hostname"] == "host.example"
    assert event.data["failures"] == 7
    assert event.data["ban_number"] == 1
    assert isinstance(event.data["expires_at"], str)
    manager.async_stop()


async def test_enriches_missing_country(
    hass: HomeAssistant, runtime: Runtime, store: FenceStore
) -> None:
    """Country and host name come from the enricher when not given."""

    async def enrich(record: LoginAttempt) -> None:
        record.country_code = "DE"
        record.hostname = "probe.example"

    runtime.enricher = Mock(async_enrich=enrich)
    manager = await _manager(hass, runtime)
    assert await manager.async_ban(IP, 5, None, None)
    [row] = store.active_bans()
    assert (row.country_code, row.hostname) == ("DE", "probe.example")
    manager.async_stop()


async def test_enrich_timeout_and_error_leave_none(
    hass: HomeAssistant,
    runtime: Runtime,
    store: FenceStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow or failing enricher leaves country and host name empty."""
    monkeypatch.setattr(
        "custom_components.electric_fence.protection.bans.ENRICH_TIMEOUT", 0.01
    )

    async def slow(record: LoginAttempt) -> None:
        await asyncio.sleep(1)

    runtime.enricher = Mock(async_enrich=slow)
    manager = await _manager(hass, runtime)
    assert await manager.async_ban(IP, 5, None, None)
    runtime.enricher = Mock(async_enrich=AsyncMock(side_effect=RuntimeError))
    assert await manager.async_ban("198.51.100.7", 5, None, None)
    assert [(r.country_code, r.hostname) for r in store.active_bans()] == [
        (None, None),
        (None, None),
    ]
    manager.async_stop()


async def test_backend_errors_fail_open(
    hass: HomeAssistant,
    runtime: Runtime,
    store: FenceStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A raising backend never reaches the caller."""
    manager = await _manager(hass, runtime)
    backend: Any = manager.backend
    backend.async_add = AsyncMock(side_effect=RuntimeError("secret 203.0.113.9"))
    assert await manager.async_ban(IP, 5, None, None) is False
    assert store.active_bans() == []
    backend.async_add = AsyncMock()
    backend.list_ips = Mock(side_effect=RuntimeError)
    assert manager.active() == []
    del backend.list_ips
    assert await manager.async_ban(IP, 5, None, None)
    backend.async_remove = AsyncMock(side_effect=RuntimeError)
    assert await manager.async_unban(IP, None, None) is False
    assert len(store.active_bans()) == 1
    backend.health = Mock(side_effect=RuntimeError)
    assert manager.available is False
    assert "secret" not in caplog.text
    manager.async_stop()


async def test_failing_side_effects_do_not_break_ban(
    hass: HomeAssistant, runtime: Runtime, store: FenceStore
) -> None:
    """Raising alerts and listeners are guarded."""
    runtime.alerts = Mock(
        ip_banned=Mock(side_effect=RuntimeError),
        ban_ended=Mock(side_effect=RuntimeError),
    )
    manager = await _manager(hass, runtime)
    unsub = manager.add_listener(Mock(side_effect=RuntimeError))
    assert await manager.async_ban(IP, 5, None, None)
    assert await manager.async_unban(IP, None, None)
    unsub()
    assert store.query_bans(active=False)[0].end_reason == "unbanned"
    manager.async_stop()


async def test_store_errors_fail_open(
    hass: HomeAssistant,
    runtime: Runtime,
    store: FenceStore,
    monkeypatch: pytest.MonkeyPatch,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A ban that could not be stored still applies, and still ends on time."""
    runtime.settings = replace(runtime.settings, ban_hours=1)
    manager = await _manager(hass, runtime)
    monkeypatch.setattr(store, "add_ban", Mock(side_effect=OSError))
    monkeypatch.setattr(store, "count_ip_bans_since", Mock(side_effect=OSError))
    end_ban = Mock()
    monkeypatch.setattr(store, "end_ban", end_ban)
    assert await manager.async_ban(IP, 5, None, None) is True
    assert ADDR in _core(hass).ip_bans_lookup
    [view] = manager.active()
    assert view.source == "electric_fence"
    assert view.ban_id is None
    assert view.expires_at == view.banned_at + timedelta(hours=1)
    freezer.tick(timedelta(minutes=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert ADDR not in _core(hass).ip_bans_lookup
    assert manager.active() == []
    end_ban.assert_not_called()
    assert len(_changes(runtime, ChangeAction.EXPIRED)) == 1
    manager.async_stop()


async def test_start_with_broken_store(
    hass: HomeAssistant,
    runtime: Runtime,
    store: FenceStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store that cannot be read at start leaves an empty cache."""
    monkeypatch.setattr(store, "active_bans", Mock(side_effect=OSError))
    manager = await _manager(hass, runtime)
    assert manager.active() == []
    assert manager.bans_24h() == 0
    assert manager.last_ban_at is None
    manager.async_stop()


async def test_concurrent_ban_and_unban(
    hass: HomeAssistant, runtime: Runtime, store: FenceStore
) -> None:
    """A ban racing an unban never leaves an active row for a lifted ban."""
    manager = await _manager(hass, runtime)
    await asyncio.gather(
        manager.async_ban(IP, 5, None, None), manager.async_unban(IP, None, None)
    )
    assert store.active_bans() == []
    assert ADDR not in _core(hass).ip_bans_lookup
    manager.async_stop()


async def test_loads_history_at_start(
    hass: HomeAssistant, runtime: Runtime, store: FenceStore
) -> None:
    """The 24-hour count and last ban time survive a restart."""
    now = dt_util.utcnow()
    old = store.add_ban("198.51.100.1", now - timedelta(days=2), None, 5, 1, None, None)
    store.end_ban(old, now - timedelta(days=1), "unbanned", None, None)
    recent = store.add_ban(
        "198.51.100.2", now - timedelta(hours=2), None, 5, 1, None, None
    )
    store.end_ban(recent, now - timedelta(hours=1), "unbanned", None, None)
    store.add_ban(IP, now - timedelta(hours=1), None, 5, 1, None, None)
    manager = await _manager(hass, runtime)
    assert manager.bans_24h() == 2
    assert manager.last_ban_at == now - timedelta(hours=1)
    manager.async_stop()


async def test_wired_into_setup(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Setup creates and starts the ban manager; unload stops it."""
    runtime: Runtime = setup_entry.runtime_data
    assert isinstance(runtime.bans, BanManager)
    assert runtime.bans.available
    assert await hass.config_entries.async_unload(setup_entry.entry_id)


async def test_loads_history_in_pages(
    hass: HomeAssistant,
    runtime: Runtime,
    store: FenceStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ended bans are read page by page until older than 24 hours."""
    monkeypatch.setattr("custom_components.electric_fence.protection.bans._PAGE", 2)
    now = dt_util.utcnow()
    for hours in (30, 5, 4, 3, 2):
        ban_id = store.add_ban(
            f"198.51.100.{hours}", now - timedelta(hours=hours), None, 5, 1, None, None
        )
        store.end_ban(ban_id, now, "unbanned", None, None)
    manager = await _manager(hass, runtime)
    assert manager.bans_24h() == 4
    manager.async_stop()


async def test_unexpected_errors_fail_open(
    hass: HomeAssistant,
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected error inside ban or unban is logged, not raised."""
    manager = await _manager(hass, runtime)
    monkeypatch.setattr(
        "custom_components.electric_fence.protection.bans.normalise_ip",
        Mock(side_effect=RuntimeError),
    )
    assert await manager.async_ban(IP, 5, None, None) is False
    assert await manager.async_unban(IP, None, None) is False
    manager.async_stop()


async def test_expiry_retried_when_removal_fails(
    hass: HomeAssistant,
    runtime: Runtime,
    store: FenceStore,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A ban whose removal fails stays and is retried a minute later."""
    runtime.settings = replace(runtime.settings, ban_hours=1)
    manager = await _manager(hass, runtime)
    assert await manager.async_ban(IP, 5, None, None)
    backend: Any = manager.backend
    real_remove = backend.async_remove
    backend.async_remove = AsyncMock(side_effect=RuntimeError)
    freezer.tick(timedelta(minutes=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(store.active_bans()) == 1
    backend.async_remove = real_remove
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert store.active_bans() == []
    assert ADDR not in _core(hass).ip_bans_lookup
    manager.async_stop()


async def test_end_ban_store_error(
    hass: HomeAssistant,
    runtime: Runtime,
    store: FenceStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing store write on unban still lifts the ban."""
    manager = await _manager(hass, runtime)
    assert await manager.async_ban(IP, 5, None, None)
    monkeypatch.setattr(store, "end_ban", Mock(side_effect=OSError))
    assert await manager.async_unban(IP, None, None) is True
    assert ADDR not in _core(hass).ip_bans_lookup
    assert manager.active() == []
    manager.async_stop()


async def test_no_timer_after_stop(
    hass: HomeAssistant,
    runtime: Runtime,
    store: FenceStore,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A ban made after stop does not arm the expiry timer."""
    manager = await _manager(hass, runtime)
    manager.async_stop()
    assert await manager.async_ban(IP, 5, None, None)
    freezer.tick(timedelta(hours=25))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(store.active_bans()) == 1


async def test_concurrent_bans_do_not_wait_for_each_other(
    hass: HomeAssistant, runtime: Runtime
) -> None:
    """Two bans with a slow enricher both apply before either enrichment ends."""
    release = asyncio.Event()
    started = 0

    async def slow(record: LoginAttempt) -> None:
        nonlocal started
        started += 1
        await release.wait()

    runtime.enricher = Mock(async_enrich=slow)
    manager = await _manager(hass, runtime)
    other = "198.51.100.7"
    tasks = [
        hass.async_create_task(manager.async_ban(IP, 5, None, None)),
        hass.async_create_task(manager.async_ban(other, 5, None, None)),
    ]
    for _ in range(50):
        if started == 2:
            break
        await asyncio.sleep(0)
    assert started == 2
    lookup = _core(hass).ip_bans_lookup
    assert ADDR in lookup
    assert ip_address(other) in lookup
    release.set()
    assert await asyncio.gather(*tasks) == [True, True]
    manager.async_stop()


async def test_reconcile_keeps_rows_when_backend_unreadable(
    hass: HomeAssistant,
    runtime: Runtime,
    store: FenceStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the backend can't list its bans, reconcile ends nothing as removed."""
    hass.set_state(CoreState.not_running)
    now = dt_util.utcnow()
    store.add_ban(IP, now, now + timedelta(hours=10), 5, 1, None, None)
    manager = await _manager(hass, runtime)
    monkeypatch.setattr(manager.backend, "list_ips", Mock(side_effect=RuntimeError))
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert len(store.active_bans()) == 1
    assert _changes(runtime, ChangeAction.REMOVED) == []
    manager.async_stop()


async def test_rebans_after_outside_removal(
    hass: HomeAssistant, runtime: Runtime, store: FenceStore
) -> None:
    """A ban lifted outside Electric Fence ends as removed; a new ban applies."""
    manager = await _manager(hass, runtime)
    assert await manager.async_ban(IP, 5, None, None)
    _core(hass).ip_bans_lookup.clear()
    assert await manager.async_ban(IP, 6, None, None) is True
    assert ADDR in _core(hass).ip_bans_lookup
    [ended] = store.query_bans(active=False)
    assert ended.end_reason == "removed"
    [row] = store.active_bans()
    assert (row.failures, row.ban_number) == (6, 2)
    [removed] = _changes(runtime, ChangeAction.REMOVED)
    assert removed.target_id == IP
    assert len(_changes(runtime, ChangeAction.BANNED)) == 2
    manager.async_stop()


async def test_does_not_take_over_other_ban(
    hass: HomeAssistant, runtime: Runtime, store: FenceStore
) -> None:
    """An address another ban already holds is not banned again."""
    await _http(hass)
    await _core(hass).async_add_ban(ADDR)
    manager = BanManager(hass, runtime, CoreBanBackend(hass))
    await manager.async_start()
    assert await manager.async_ban(IP, 5, None, None) is False
    assert store.active_bans() == []
    manager.async_stop()


async def test_expiry_leaves_later_bans(
    hass: HomeAssistant,
    runtime: Runtime,
    store: FenceStore,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Only the bans that are due end; the timer re-arms for the next one."""
    runtime.settings = replace(runtime.settings, ban_hours=1)
    manager = await _manager(hass, runtime)
    assert await manager.async_ban(IP, 5, None, None)
    runtime.settings = replace(runtime.settings, ban_hours=2)
    later = "198.51.100.7"
    assert await manager.async_ban(later, 5, None, None)
    freezer.tick(timedelta(minutes=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert [r.ip for r in store.active_bans()] == [later]
    freezer.tick(timedelta(minutes=60))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert store.active_bans() == []
    manager.async_stop()
