"""Tests for username attack detection and slow-down."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

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
    LoginReason,
    LoginResult,
    SettingsChange,
    UserExists,
)
from custom_components.electric_fence.notifier import EVENT_USERNAME_ATTACK
from custom_components.electric_fence.protection import throttle as throttle_module
from custom_components.electric_fence.protection.throttle import (
    ACQUIRE_TIMEOUT,
    BASE_DELAY,
    EVENT_MAX_IPS,
    KNOWN_DEVICE_WINDOW,
    MAX_DELAY,
    MAX_WAITING,
    Refused,
    UsernameGuard,
)
from custom_components.electric_fence.runtime import Runtime
from custom_components.electric_fence.storage import FenceStore
from homeassistant.core import Event, HomeAssistant
from homeassistant.util import dt as dt_util
from tests.helpers import make_runtime

IPS = ("203.0.113.1", "203.0.113.2", "203.0.113.3")
IP = "198.51.100.7"
FAIL = (LoginResult.FAILED, LoginReason.WRONG_PASSWORD)
# Woken attempts return within one loop iteration.
STOP_ITERATIONS = 1


@pytest.fixture
def store(tmp_path: Path) -> Iterator[FenceStore]:
    """Return an open store."""
    s = FenceStore(tmp_path / "fence.db")
    s.open()
    yield s
    s.close()


@pytest.fixture
def runtime(hass: HomeAssistant, store: FenceStore) -> Runtime:
    """Return a runtime that records submissions and alerts."""
    rt = make_runtime(hass, store)
    rt.worker.submit = Mock(return_value=True)  # type: ignore[method-assign]
    rt.alerts = Mock()
    return rt


@pytest.fixture
def sleeps() -> Iterator[list[float]]:
    """Record the slow-down delays instead of sleeping."""
    durations: list[float] = []

    async def fake(delay: float) -> None:
        durations.append(delay)

    with patch.object(throttle_module, "_sleep", fake):
        yield durations


@pytest.fixture
async def guard(hass: HomeAssistant, runtime: Runtime) -> AsyncIterator[UsernameGuard]:
    """Return the guard under test; stopped at the end."""
    g = UsernameGuard(hass, runtime)
    yield g
    g.async_stop()
    await hass.async_block_till_done()


def _fail(
    guard: UsernameGuard,
    times: int,
    user: str = "admin",
    exists: UserExists = UserExists.YES,
) -> None:
    for i in range(times):
        guard.on_attempt(user, IPS[i % len(IPS)], *FAIL, exists)


def _changes(runtime: Runtime, action: ChangeAction) -> list[SettingsChange]:
    submit: Mock = runtime.worker.submit  # type: ignore[assignment]
    return [
        c.args[0]
        for c in submit.call_args_list
        if isinstance(c.args[0], SettingsChange)
        and c.args[0].kind == ChangeKind.LOGIN_PROTECTION
        and c.args[0].action == action
    ]


def _record(
    user: str | None = "admin",
    ip: str | None = IP,
    country: str | None = "NL",
    reason: LoginReason = LoginReason.WRONG_PASSWORD,
    result: LoginResult = LoginResult.FAILED,
) -> LoginAttempt:
    return LoginAttempt(
        ts=dt_util.utcnow(),
        result=result,
        reason=reason,
        username_typed=user,
        user_exists=UserExists.YES,
        user_id=None,
        provider=None,
        ip=ip,
        source=None,
        url=None,
        user_agent=None,
        client_id=None,
        country_code=country,
    )


async def _attacked(hass: HomeAssistant, guard: UsernameGuard) -> None:
    _fail(guard, 10)
    await hass.async_block_till_done()
    assert len(guard.active_attacks()) == 1


async def test_attack_starts(
    hass: HomeAssistant, runtime: Runtime, guard: UsernameGuard, store: FenceStore
) -> None:
    """The tenth failure from three addresses starts one attack."""
    events: list[Event] = async_capture_events(hass, EVENT_USERNAME_ATTACK)
    _fail(guard, 9)
    await hass.async_block_till_done()
    assert guard.active_attacks() == []
    assert _changes(runtime, ChangeAction.ATTACK_STARTED) == []

    _fail(guard, 1)
    assert len(guard.active_attacks()) == 1
    _fail(guard, 3)
    await hass.async_block_till_done()
    [view] = guard.active_attacks()
    assert view.username == "admin"
    assert view.failures == 13
    assert view.ip_count == 3
    assert view.countries == []
    [row] = store.query_attacks(active=True)
    assert view.attack_id == row.id
    assert row.username == "admin"
    [change] = _changes(runtime, ChangeAction.ATTACK_STARTED)
    assert change.target_name == "admin"
    assert change.actor_user_id is None
    [event] = events
    assert event.data == {
        "username": "admin",
        "username_masked": False,
        "ips": sorted(IPS),
        "ip_count": 3,
        "countries": [],
        "started_at": view.started_at.isoformat(),
        "state": "started",
    }
    runtime.alerts.username_attack.assert_called_once()  # type: ignore[union-attr]
    assert runtime.alerts.username_attack.call_args.args[0].username == "admin"  # type: ignore[union-attr]


async def test_unknown_username_masked_outside_panel(
    hass: HomeAssistant, runtime: Runtime, guard: UsernameGuard, store: FenceStore
) -> None:
    """A name no user has is masked in the event and alert; the store keeps it."""
    events: list[Event] = async_capture_events(hass, EVENT_USERNAME_ATTACK)
    _fail(guard, 10, "hostile-secret", UserExists.NO)
    await hass.async_block_till_done()
    [view] = guard.active_attacks()
    assert view.username == "hostile-secret"
    assert not view.user_exists
    [row] = store.query_attacks(active=True)
    assert row.username == "hostile-secret"
    [change] = _changes(runtime, ChangeAction.ATTACK_STARTED)
    assert change.target_name == "hostile-secret"
    assert change.details["user_exists"] is False
    [event] = events
    assert event.data["username"] == "ho•••"
    assert event.data["username_masked"] is True
    alert_view = runtime.alerts.username_attack.call_args.args[0]  # type: ignore[union-attr]
    assert alert_view.display() == ("ho•••", True)

    assert await guard.async_end("hostile-secret", "u1", "Ann")
    await hass.async_block_till_done()
    assert events[-1].data["username"] == "ho•••"
    [ended] = _changes(runtime, ChangeAction.SLOWDOWN_ENDED)
    assert ended.details["user_exists"] is False


async def test_existing_user_learned_later(
    hass: HomeAssistant, runtime: Runtime, guard: UsernameGuard
) -> None:
    """A later attempt or record that finds the user shows the name in full."""
    events: list[Event] = async_capture_events(hass, EVENT_USERNAME_ATTACK)
    _fail(guard, 10, "admin", UserExists.UNKNOWN)
    await hass.async_block_till_done()
    assert events[0].data["username"] == "ad•••"
    guard.on_record(_record())
    [view] = guard.active_attacks()
    assert view.user_exists
    assert await guard.async_end("admin", None, None)
    await hass.async_block_till_done()
    assert events[-1].data["username"] == "admin"
    assert events[-1].data["username_masked"] is False

    _fail(guard, 10, "bob", UserExists.UNKNOWN)
    _fail(guard, 1, "bob", UserExists.YES)
    [view] = guard.active_attacks()
    assert view.user_exists


async def test_needs_min_ips(hass: HomeAssistant, guard: UsernameGuard) -> None:
    """Twenty failures from two addresses are not an attack."""
    for i in range(20):
        guard.on_attempt("admin", IPS[i % 2], *FAIL)
    await hass.async_block_till_done()
    assert guard.active_attacks() == []


async def test_key_normalised(hass: HomeAssistant, guard: UsernameGuard) -> None:
    """Failures for "Admin " and "admin" count together."""
    assert UsernameGuard.key(" Admin ") == "admin"
    assert UsernameGuard.key("  ") is None
    assert UsernameGuard.key(None) is None
    assert UsernameGuard.key("ad\x01min") == "admin"
    _fail(guard, 5, "Admin ")
    _fail(guard, 5, "admin")
    await hass.async_block_till_done()
    [view] = guard.active_attacks()
    assert view.username == "admin"


async def test_ignores_exempt_uncounted_and_old(
    hass: HomeAssistant,
    runtime: Runtime,
    guard: UsernameGuard,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Loopback, uncounted and out-of-window failures never start an attack."""
    for _ in range(20):
        guard.on_attempt("admin", "127.0.0.1", *FAIL)
        guard.on_attempt("admin", None, *FAIL)
        guard.on_attempt("admin", IPS[0], LoginResult.FAILED, LoginReason.INVALID_TOKEN)
        guard.on_attempt("admin", IPS[1], LoginResult.SUCCESS, LoginReason.OK)
        guard.on_attempt(None, IPS[2], *FAIL)
    _fail(guard, 9)
    freezer.tick(timedelta(minutes=16))
    _fail(guard, 9)
    await hass.async_block_till_done()
    assert guard.active_attacks() == []

    runtime.settings = replace(runtime.settings, username_protection=False)
    _fail(guard, 1)
    await hass.async_block_till_done()
    assert guard.active_attacks() == []


async def test_mapped_ipv6_counts_as_ipv4(
    hass: HomeAssistant, guard: UsernameGuard
) -> None:
    """IPv4-mapped IPv6 addresses count as their IPv4 address."""
    for i in range(10):
        ip = IPS[1] if i % 2 else IPS[0] if i % 4 else f"::ffff:{IPS[0]}"
        guard.on_attempt("admin", ip, *FAIL)
    await hass.async_block_till_done()
    assert guard.active_attacks() == []


async def test_delays_double_and_cap(
    hass: HomeAssistant, guard: UsernameGuard, sleeps: list[float]
) -> None:
    """Each further failure doubles the delay, up to the cap."""
    await _attacked(hass, guard)
    for _ in range(6):
        slot = await guard.async_enter("admin", IP)
        assert slot is not None
        slot.release()
        slot.release()
        guard.on_attempt("Admin", IP, *FAIL)
    assert sleeps == [2, 4, 8, 16, 30, 30]
    assert sleeps[0] == BASE_DELAY
    assert sleeps[-1] == MAX_DELAY


async def test_delay_never_overflows(
    hass: HomeAssistant, guard: UsernameGuard, sleeps: list[float]
) -> None:
    """A very long attack keeps the capped delay."""
    await _attacked(hass, guard)
    for _ in range(1100):
        guard.on_attempt("admin", IP, *FAIL)
    slot = await guard.async_enter("admin", IP)
    assert slot is not None
    slot.release()
    assert sleeps == [MAX_DELAY]


async def test_not_attacked_goes_straight(
    hass: HomeAssistant,
    runtime: Runtime,
    guard: UsernameGuard,
    sleeps: list[float],
) -> None:
    """Without an attack, or with protection off, there is no slow-down."""
    assert await guard.async_enter("admin", IP) is None
    assert await guard.async_enter(None, IP) is None
    await _attacked(hass, guard)
    runtime.settings = replace(runtime.settings, username_protection=False)
    assert await guard.async_enter("admin", IP) is None
    assert await guard.async_enter("other", IP) is None
    assert sleeps == []


async def test_queue_refuses_fifth(
    hass: HomeAssistant, guard: UsernameGuard, sleeps: list[float]
) -> None:
    """One holds the slot, three wait, the fifth is refused."""
    await _attacked(hass, guard)
    holder = await guard.async_enter("admin", IP)
    assert holder is not None
    waiters = [
        hass.async_create_task(guard.async_enter("admin", IP))
        for _ in range(MAX_WAITING)
    ]
    await asyncio.sleep(0)
    assert not any(w.done() for w in waiters)
    with pytest.raises(Refused):
        await guard.async_enter("admin", IP)

    holder.release()
    for waiter in waiters:
        slot = await waiter
        assert slot is not None
        slot.release()
    assert len(sleeps) == 1 + MAX_WAITING


async def test_loopback_not_throttled(
    hass: HomeAssistant, guard: UsernameGuard, sleeps: list[float]
) -> None:
    """Loopback goes straight to Core during an attack."""
    await _attacked(hass, guard)
    assert await guard.async_enter("admin", "127.0.0.1") is None
    assert await guard.async_enter("admin", None) is None
    assert sleeps == []


async def test_local_counted_and_throttled(
    hass: HomeAssistant,
    runtime: Runtime,
    guard: UsernameGuard,
    sleeps: list[float],
) -> None:
    """LAN and Tailscale addresses start attacks and wait like any other."""
    lan = ("192.168.1.2", "10.0.0.3", "100.101.102.103")
    for i in range(10):
        guard.on_attempt("admin", lan[i % 3], *FAIL)
    await hass.async_block_till_done()
    assert len(guard.active_attacks()) == 1
    slot = await guard.async_enter("admin", "192.168.1.50")
    assert slot is not None
    slot.release()
    assert len(sleeps) == 1

    runtime.settings = replace(runtime.settings, exempt_local=True)
    assert await guard.async_enter("admin", "192.168.1.50") is None
    assert len(sleeps) == 1


def _success_record(
    user: str = "admin", ip: str = IP, *, confirmed: bool = True
) -> LoginAttempt:
    record = _record(
        user=user, ip=ip, result=LoginResult.SUCCESS, reason=LoginReason.OK
    )
    record.confirmed = confirmed
    return record


async def test_known_device_skips_slowdown(
    hass: HomeAssistant, guard: UsernameGuard, sleeps: list[float]
) -> None:
    """A device that logged in as this username goes straight to Core.

    Even with the queue full: an attacker can't lock the owner out.
    """
    guard.on_record(_success_record("Admin"))
    await _attacked(hass, guard)
    guard._attacks["admin"].waiting = MAX_WAITING  # noqa: SLF001
    assert await guard.async_enter(" ADMIN", IP) is None
    assert sleeps == []
    with pytest.raises(Refused):
        await guard.async_enter("admin", "198.51.100.99")


async def test_known_device_is_per_username(
    hass: HomeAssistant, guard: UsernameGuard, sleeps: list[float]
) -> None:
    """A device known for another username waits like any other."""
    guard.on_record(_success_record("guest"))
    await _attacked(hass, guard)
    slot = await guard.async_enter("admin", IP)
    assert slot is not None
    slot.release()
    assert len(sleeps) == 1


async def test_known_device_per_prefix_and_expiry(
    hass: HomeAssistant,
    guard: UsernameGuard,
    sleeps: list[float],
    freezer: FrozenDateTimeFactory,
) -> None:
    """IPv6 is known per /64 (privacy addresses change); it lasts 30 days."""
    guard.on_record(_success_record(ip="2001:db8:1:2::aaaa"))
    await _attacked(hass, guard)
    assert await guard.async_enter("admin", "2001:db8:1:2::bbbb") is None
    freezer.tick(KNOWN_DEVICE_WINDOW + timedelta(seconds=1))
    _fail(guard, 1)  # keep the attack going
    slot = await guard.async_enter("admin", "2001:db8:1:2::bbbb")
    assert slot is not None
    slot.release()


async def test_unconfirmed_success_not_known(
    hass: HomeAssistant, guard: UsernameGuard, sleeps: list[float]
) -> None:
    """Only a confirmed success makes a device known."""
    guard.on_record(_success_record(confirmed=False))
    await _attacked(hass, guard)
    slot = await guard.async_enter("admin", IP)
    assert slot is not None
    slot.release()


async def test_seed_known(
    hass: HomeAssistant, guard: UsernameGuard, sleeps: list[float]
) -> None:
    """Successes from the store make devices known; bad rows are skipped."""
    now = dt_util.utcnow()
    guard.seed_known([(IP, "admin", now), ("nope", "admin", now), (IP, None, now)])
    await _attacked(hass, guard)
    assert await guard.async_enter("admin", IP) is None
    assert sleeps == []


async def test_known_devices_bounded(
    hass: HomeAssistant,
    guard: UsernameGuard,
    sleeps: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The oldest known devices are dropped first."""
    monkeypatch.setattr(throttle_module, "MAX_TRACKED", 2)
    for n in (1, 2, 3):
        guard.on_record(_success_record(ip=f"198.51.100.{n}"))
    await _attacked(hass, guard)
    slot = await guard.async_enter("admin", "198.51.100.1")
    assert slot is not None
    slot.release()
    assert await guard.async_enter("admin", "198.51.100.3") is None


async def test_ends_when_quiet(
    hass: HomeAssistant,
    runtime: Runtime,
    guard: UsernameGuard,
    store: FenceStore,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A window without counted failures ends the attack."""
    events: list[Event] = async_capture_events(hass, EVENT_USERNAME_ATTACK)
    listener = Mock()
    remove = guard.add_listener(listener)
    await _attacked(hass, guard)
    freezer.tick(timedelta(minutes=10))
    _fail(guard, 1)
    freezer.tick(timedelta(minutes=10))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(guard.active_attacks()) == 1

    freezer.tick(timedelta(minutes=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert guard.active_attacks() == []
    [change] = _changes(runtime, ChangeAction.ATTACK_ENDED)
    assert change.target_name == "admin"
    assert change.details["reason"] == "quiet"
    assert [e.data["state"] for e in events] == ["started", "ended"]
    [row] = store.query_attacks(active=False)
    assert row.end_reason == "quiet"
    assert row.failures == 11
    assert row.ip_count == 3
    assert listener.call_count == 2
    remove()
    remove()


async def test_end_by_user(
    hass: HomeAssistant,
    runtime: Runtime,
    guard: UsernameGuard,
    store: FenceStore,
    sleeps: list[float],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Ending the slow-down is audited and stops the delays."""
    await _attacked(hass, guard)
    assert await guard.async_end("admin", "u1", "Ann") is True
    assert await guard.async_end("admin", "u1", "Ann") is False
    await hass.async_block_till_done()
    assert guard.active_attacks() == []
    [change] = _changes(runtime, ChangeAction.SLOWDOWN_ENDED)
    assert change.target_name == "admin"
    assert change.actor_user_id == "u1"
    assert change.actor_name == "Ann"
    [row] = store.query_attacks(active=False)
    assert row.end_reason == "ended_by_user"
    assert row.ended_by_user_id == "u1"
    assert row.ended_by_name == "Ann"

    assert await guard.async_enter("admin", IP) is None
    assert sleeps == []
    # The quiet timer was cancelled: no second end.
    freezer.tick(timedelta(minutes=20))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _changes(runtime, ChangeAction.ATTACK_ENDED) == []


async def test_end_releases_waiters(
    hass: HomeAssistant, guard: UsernameGuard, sleeps: list[float]
) -> None:
    """Attempts waiting when the attack ends go straight to Core."""
    await _attacked(hass, guard)
    holder = await guard.async_enter("admin", IP)
    assert holder is not None
    waiters = [hass.async_create_task(guard.async_enter("admin", IP)) for _ in range(2)]
    await asyncio.sleep(0)
    assert await guard.async_end("admin", None, None)
    await hass.async_block_till_done()
    assert [w.result() for w in waiters] == [None, None]
    holder.release()


async def test_turn_comes_as_attack_ends(
    hass: HomeAssistant, guard: UsernameGuard, sleeps: list[float]
) -> None:
    """An attempt whose turn comes as the attack ends goes straight to Core."""
    await _attacked(hass, guard)
    holder = await guard.async_enter("admin", IP)
    assert holder is not None
    waiter = hass.async_create_task(guard.async_enter("admin", IP))
    await asyncio.sleep(0)
    holder.release()
    assert await guard.async_end("admin", None, None)
    assert await waiter is None
    assert sleeps == [BASE_DELAY]


async def test_unload_releases_waiters(
    hass: HomeAssistant, guard: UsernameGuard, sleeps: list[float]
) -> None:
    """On stop, every waiting attempt returns None at once; no task is left."""
    await _attacked(hass, guard)
    holder = await guard.async_enter("admin", IP)
    assert holder is not None
    before = asyncio.all_tasks()
    waiters = [
        hass.async_create_task(guard.async_enter("admin", IP))
        for _ in range(MAX_WAITING)
    ]
    await asyncio.sleep(0)
    assert not any(w.done() for w in waiters)

    guard.async_stop()
    for _ in range(STOP_ITERATIONS):
        await asyncio.sleep(0)
    assert all(w.done() for w in waiters)
    await hass.async_block_till_done()
    assert [w.result() for w in waiters] == [None] * MAX_WAITING
    assert guard.active_attacks() == []
    assert await guard.async_enter("admin", IP) is None
    holder.release()
    guard.on_attempt("admin", IP, *FAIL)
    _fail(guard, 10)
    assert guard.active_attacks() == []
    assert asyncio.all_tasks() - before == set()


async def test_unload_wakes_sleeper(hass: HomeAssistant, guard: UsernameGuard) -> None:
    """An attempt in its delay goes on to Core when stopped."""
    await _attacked(hass, guard)
    started = asyncio.Event()

    async def forever(_delay: float) -> None:
        started.set()
        await asyncio.Event().wait()

    with patch.object(throttle_module, "_sleep", forever):
        task = hass.async_create_task(guard.async_enter("admin", IP))
        await started.wait()
        guard.async_stop()
        for _ in range(STOP_ITERATIONS):
            await asyncio.sleep(0)
        assert task.done()
    assert task.result() is None


async def test_enter_fails_open(
    hass: HomeAssistant, guard: UsernameGuard, sleeps: list[float]
) -> None:
    """An internal error lets the attempt go straight to Core."""
    await _attacked(hass, guard)
    with patch.object(throttle_module, "is_exempt", side_effect=RuntimeError):
        assert await guard.async_enter("admin", IP) is None

    async def broken(_delay: float) -> None:
        raise RuntimeError

    with patch.object(throttle_module, "_sleep", broken):
        assert await guard.async_enter("admin", IP) is None
    # The lock was released: the next attempt is only delayed.
    slot = await guard.async_enter("admin", IP)
    assert slot is not None
    slot.release()


async def test_caller_cancelled_releases_lock(
    hass: HomeAssistant, guard: UsernameGuard, sleeps: list[float]
) -> None:
    """A cancelled login attempt never keeps the lock."""
    await _attacked(hass, guard)
    holder = await guard.async_enter("admin", IP)
    assert holder is not None
    waiter = hass.async_create_task(guard.async_enter("admin", IP))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    holder.release()

    started = asyncio.Event()

    async def forever(_delay: float) -> None:
        started.set()
        await asyncio.Event().wait()

    with patch.object(throttle_module, "_sleep", forever):
        sleeper = hass.async_create_task(guard.async_enter("admin", IP))
        await started.wait()
        sleeper.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sleeper
    slot = await guard.async_enter("admin", IP)
    assert slot is not None
    slot.release()


async def test_store_errors_keep_attack(
    hass: HomeAssistant,
    runtime: Runtime,
    guard: UsernameGuard,
    store: FenceStore,
    sleeps: list[float],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A store that fails never stops the attack or its slow-down."""
    with (
        patch.object(store, "add_attack", side_effect=OSError),
        patch.object(store, "end_attack", side_effect=OSError) as end,
    ):
        await _attacked(hass, guard)
        [view] = guard.active_attacks()
        assert view.attack_id is None
        guard.on_record(_record())
        slot = await guard.async_enter("admin", IP)
        assert slot is not None
        slot.release()
        assert await guard.async_end("admin", None, None)
        await hass.async_block_till_done()
    end.assert_not_called()
    assert store.query_attacks(active=True) == []

    with (
        patch.object(store, "update_attack", side_effect=OSError),
        patch.object(store, "end_attack", side_effect=OSError),
    ):
        await _attacked(hass, guard)
        guard.on_record(_record())
        assert await guard.async_end("admin", None, None)
        await hass.async_block_till_done()
    assert len(store.query_attacks(active=True)) == 1


async def test_countries_from_records(
    hass: HomeAssistant,
    guard: UsernameGuard,
    store: FenceStore,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Enriched records add countries; the store is written at most every 10 s."""
    guard.on_record(_record())
    await _attacked(hass, guard)
    with patch.object(store, "update_attack", wraps=store.update_attack) as update:
        guard.on_record(_record(country="NL"))
        await hass.async_block_till_done()
        assert update.call_count == 1
        guard.on_record(_record(user="Admin", ip="198.51.100.8", country="DE"))
        guard.on_record(_record(country=None, ip="127.0.0.1"))
        guard.on_record(_record(country="FR", reason=LoginReason.INVALID_TOKEN))
        guard.on_record(_record(country="FR", result=LoginResult.SUCCESS))
        guard.on_record(_record(user="other", country="FR"))
        guard.on_record(
            SettingsChange(
                ts=dt_util.utcnow(),
                kind=ChangeKind.LOGIN_PROTECTION,
                action=ChangeAction.ATTACK_STARTED,
                target_id=None,
                target_name="admin",
                details={},
                actor_user_id=None,
                actor_name=None,
                actor_ip=None,
            )
        )
        await hass.async_block_till_done()
        assert update.call_count == 1
        freezer.tick(timedelta(seconds=10))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert update.call_count == 2
    [view] = guard.active_attacks()
    assert view.countries == ["DE", "NL"]
    assert view.ip_count == 5
    [row] = store.query_attacks(active=True)
    assert row.countries == ["DE", "NL"]
    assert row.ip_count == 5

    # An update still waiting when the attack ends is dropped; the end
    # writes the final totals.
    guard.on_record(_record(country="BE"))
    assert await guard.async_end("admin", None, None)
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=10))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    [row] = store.query_attacks(active=False)
    assert row.countries == ["BE", "DE", "NL"]


async def test_record_hooks_fail_open(
    hass: HomeAssistant, runtime: Runtime, guard: UsernameGuard
) -> None:
    """Errors in on_attempt, on_record and alerts never raise."""
    runtime.alerts.username_attack.side_effect = RuntimeError  # type: ignore[union-attr]
    listener = Mock(side_effect=RuntimeError)
    guard.add_listener(listener)
    await _attacked(hass, guard)
    listener.assert_called_once()
    with patch.object(throttle_module, "normalise_ip", side_effect=RuntimeError):
        guard.on_attempt("admin", IP, *FAIL)
        guard.on_record(_record())
    with patch.object(guard, "_end", side_effect=RuntimeError):
        assert await guard.async_end("admin", None, None) is False


async def test_no_alerts_when_unset(
    hass: HomeAssistant, runtime: Runtime, guard: UsernameGuard
) -> None:
    """An attack starts without an alerter."""
    runtime.alerts = None
    await _attacked(hass, guard)


async def test_tracked_usernames_bounded(
    hass: HomeAssistant, guard: UsernameGuard
) -> None:
    """Only a bounded number of usernames is tracked."""
    with patch.object(throttle_module, "MAX_TRACKED", 2):
        _fail(guard, 9, "a")
        _fail(guard, 1, "b")
        _fail(guard, 1, "c")
        _fail(guard, 1, "a")
        _fail(guard, 9, "c")
    await hass.async_block_till_done()
    assert [v.username for v in guard.active_attacks()] == ["c"]


async def test_wired_into_setup(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Setup creates the guard; unload stops it before the collectors go."""
    runtime: Runtime = setup_entry.runtime_data
    guard = runtime.username_guard
    assert isinstance(guard, UsernameGuard)
    collectors = list(runtime.collectors)
    seen: list[bool] = []
    original = collectors[0].uninstall

    def uninstall() -> None:
        seen.append(guard._stopped)  # noqa: SLF001
        original()

    with patch.object(collectors[0], "uninstall", uninstall):
        assert await hass.config_entries.async_unload(setup_entry.entry_id)
    assert seen == [True]


async def test_key_sanitised(hass: HomeAssistant, guard: UsernameGuard) -> None:
    """Raw and sanitised usernames count together; the raw form is never kept."""
    _fail(guard, 5, "ad\x01min")
    _fail(guard, 5, "admin")
    await hass.async_block_till_done()
    [view] = guard.active_attacks()
    assert view.username == "admin"


async def test_stalled_holder_times_out(
    hass: HomeAssistant,
    guard: UsernameGuard,
    sleeps: list[float],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A slot never released doesn't lock the username out: waiters give up."""
    await _attacked(hass, guard)
    holder = await guard.async_enter("admin", IP)
    assert holder is not None
    waiter = hass.async_create_task(guard.async_enter("admin", IP))
    await asyncio.sleep(0)
    freezer.tick(timedelta(seconds=ACQUIRE_TIMEOUT - 1))
    async_fire_time_changed(hass)
    await asyncio.sleep(0)
    assert not waiter.done()
    freezer.tick(timedelta(seconds=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert waiter.result() is None
    assert len(sleeps) == 1
    # The next attempt queues again rather than being refused.
    waiters = [
        hass.async_create_task(guard.async_enter("admin", IP))
        for _ in range(MAX_WAITING)
    ]
    await asyncio.sleep(0)
    assert not any(w.done() for w in waiters)
    holder.release()
    for w in waiters:
        slot = await w
        assert slot is not None
        slot.release()


async def test_start_closes_stale_rows(
    hass: HomeAssistant, runtime: Runtime, guard: UsernameGuard, store: FenceStore
) -> None:
    """Attacks left open by an earlier run end as quiet at start, silently."""
    events: list[Event] = async_capture_events(hass, EVENT_USERNAME_ATTACK)
    store.add_attack("admin", dt_util.utcnow() - timedelta(hours=1))
    await guard.async_start()
    assert store.query_attacks(active=True) == []
    [row] = store.query_attacks(active=False)
    assert row.end_reason == "quiet"
    assert row.ended_at is not None
    runtime.worker.submit.assert_not_called()  # type: ignore[attr-defined]
    assert events == []

    with patch.object(store, "end_open_attacks", side_effect=OSError):
        await guard.async_start()


async def test_attacks_count_toward_tracking_cap(
    hass: HomeAssistant, guard: UsernameGuard
) -> None:
    """Active attacks count toward the tracked-username cap."""
    with patch.object(throttle_module, "MAX_TRACKED", 2):
        _fail(guard, 10, "a")
        _fail(guard, 9, "b")
        _fail(guard, 1, "c")
        _fail(guard, 1, "b")
        assert [v.username for v in guard.active_attacks()] == ["a"]
        _fail(guard, 10, "c")
        _fail(guard, 10, "d")
        assert [v.username for v in guard.active_attacks()] == ["a", "c"]
        _fail(guard, 10, "e")
    await hass.async_block_till_done()
    assert [v.username for v in guard.active_attacks()] == ["a", "c"]


async def test_event_ips_capped(hass: HomeAssistant, guard: UsernameGuard) -> None:
    """Events list a bounded number of addresses; the count stays exact."""
    events: list[Event] = async_capture_events(hass, EVENT_USERNAME_ATTACK)
    await _attacked(hass, guard)
    for i in range(30):
        guard.on_attempt("admin", f"198.51.100.{i + 10}", *FAIL)
    assert await guard.async_end("admin", None, None)
    await hass.async_block_till_done()
    ended = events[-1].data
    assert ended["state"] == "ended"
    assert len(ended["ips"]) == EVENT_MAX_IPS
    assert ended["ip_count"] == 33
