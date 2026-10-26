"""Tests for the SIEM forwarder, with a fake transport."""

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
import json
import threading
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

import custom_components.electric_fence as fence
from custom_components.electric_fence import posture
from custom_components.electric_fence.const import DOMAIN, MIN_HA_VERSION
from custom_components.electric_fence.forwarding import forwarder as fwd
from custom_components.electric_fence.forwarding.format import event_dict
from custom_components.electric_fence.forwarding.forwarder import Forwarder
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
from custom_components.electric_fence.settings import Settings
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from tests.helpers import change_rows, create_hass_user, flush_worker, login_flow

SENSOR = "sensor.electric_fence_forwarding"


class Net:
    """The pretend network shared by every fake transport."""

    def __init__(self) -> None:
        """Start up."""
        self.down = False
        self.lines: list[bytes] = []
        self.made: list[FakeTransport] = []

    def events(self) -> list[dict[str, Any]]:
        """Return the JSON bodies sent so far."""
        return [json.loads(line.split(b": ", 1)[1]) for line in self.lines]


class FakeTransport:
    """Stands in for SyslogTransport."""

    def __init__(self, net: Net, port: int = 514) -> None:
        """Register on the network."""
        self.net = net
        self.port = port
        self.connected = False
        self.closed = False
        net.made.append(self)

    async def connect(self) -> None:
        """Connect unless the network is down."""
        if self.net.down:
            raise OSError("name does not resolve")
        self.connected = True

    async def send(self, line: bytes) -> None:
        """Send unless the network is down."""
        if self.net.down:
            self.connected = False
            raise ConnectionResetError
        self.net.lines.append(line)

    async def close(self) -> None:
        """Close."""
        self.connected = False
        self.closed = True


class Clock:
    """A clock the test moves; the event loop keeps its own time."""

    def __init__(self) -> None:
        """Start unshifted."""
        self.offset = timedelta()

    def now(self) -> datetime:
        """Return the shifted time."""
        return real_utcnow() + self.offset

    def tick(self, delta: timedelta) -> None:
        """Move forward."""
        self.offset += delta


real_utcnow = dt_util.utcnow


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """Shift the time the forwarder and posture see.

    freezegun would also freeze the loop's clock, and so every sleep.
    """
    clock = Clock()
    monkeypatch.setattr(fwd.dt_util, "utcnow", clock.now)
    monkeypatch.setattr(posture.dt_util, "utcnow", clock.now)
    return clock


@pytest.fixture
def net() -> Net:
    """Return a network that is up."""
    return Net()


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry in milliseconds."""
    monkeypatch.setattr(fwd, "BACKOFF_START", 0.01)
    monkeypatch.setattr(fwd, "BACKOFF_MAX", 0.02)


@pytest.fixture
def patched_transport(net: Net, monkeypatch: pytest.MonkeyPatch) -> Net:
    """Make setup create fake transports."""

    def make(host: str, port: int, protocol: str, ctx: object) -> FakeTransport:
        return FakeTransport(net, port)

    monkeypatch.setattr("custom_components.electric_fence.SyslogTransport", make)
    return net


def attempt(n: int) -> LoginAttempt:
    """Return a failed attempt for user n."""
    return LoginAttempt(
        ts=dt_util.utcnow(),
        result=LoginResult.FAILED,
        reason=LoginReason.NO_SUCH_USER,
        username_typed=f"user{n}",
        user_exists=UserExists.NO,
        user_id=None,
        provider="homeassistant",
        ip=f"203.0.113.{n % 250 + 1}",
        source=Source.REMOTE,
        url="/auth/login_flow",
        user_agent="test",
        client_id="http://127.0.0.1:8123/",
        country_code="RU",
    )


def make_forwarder(hass: HomeAssistant, entry: MockConfigEntry, net: Net) -> Forwarder:
    """Return a forwarder on the entry's runtime with a fake transport."""
    return Forwarder(hass, entry.runtime_data, lambda: FakeTransport(net))


async def until(predicate: Callable[[], bool]) -> None:
    """Wait for a condition."""
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.005)


async def test_forwards_in_order(
    hass: HomeAssistant, setup_entry: MockConfigEntry, net: Net
) -> None:
    """Three records are sent in order."""
    forwarder = make_forwarder(hass, setup_entry, net)
    forwarder.start()
    for n in (1, 2, 3):
        setup_entry.runtime_data.worker.submit(attempt(n))
    await flush_worker(hass)
    await until(lambda: len(net.lines) == 3)
    assert [e["username"] for e in net.events()] == ["user1", "user2", "user3"]
    assert net.lines[0].startswith(b"<")
    assert forwarder.stats().sent == 3
    await forwarder.stop()


async def test_down_at_startup_does_not_block(
    hass: HomeAssistant,
    patched_transport: Net,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SIEM that does not resolve never holds up setup or a login."""
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", MIN_HA_VERSION)
    patched_transport.down = True
    entry = MockConfigEntry(
        domain=DOMAIN,
        options=replace(
            Settings(), forward_enabled=True, forward_host="siem.invalid"
        ).to_options(),
    )
    entry.add_to_hass(hass)
    async with asyncio.timeout(5):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    await create_hass_user(hass, "alice", "right")
    result = await login_flow(await hass_client_no_auth(), "alice", "right")
    assert result["type"] == "create_entry"
    assert hass.states.get(SENSOR).state == "failing"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_queue_drops_oldest(
    hass: HomeAssistant, setup_entry: MockConfigEntry, net: Net
) -> None:
    """The queue keeps 1000 events and drops the oldest first."""
    net.down = True
    forwarder = make_forwarder(hass, setup_entry, net)
    forwarder.start()
    for n in range(1, 1006):
        forwarder.on_record(attempt(n))
    stats = forwarder.stats()
    assert (stats.queued, stats.dropped) == (1000, 5)
    net.down = False
    await until(lambda: len(net.lines) == 1000)
    assert net.events()[0]["username"] == "user6"
    assert forwarder.stats().dropped == 5
    await forwarder.stop()


async def test_failed_and_recovered_records(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    net: Net,
    clock: Clock,
) -> None:
    """Five minutes of failing give one record, and recovery one more."""
    net.down = True
    forwarder = make_forwarder(hass, setup_entry, net)
    forwarder.start()
    forwarder.on_record(attempt(1))
    await until(lambda: forwarder.stats().failing_since is not None)
    clock.tick(timedelta(minutes=6))
    await until(lambda: forwarder.stats().last_error == "OSError")
    await asyncio.sleep(0.1)
    await flush_worker(hass)

    def actions() -> list[ChangeAction]:
        return [c.action for c in change_rows(hass) if c.kind is ChangeKind.FORWARDING]

    assert actions() == [ChangeAction.FORWARDING_FAILED]
    assert change_rows(hass)[-1].details == {"error": "OSError"}
    net.down = False
    await until(lambda: forwarder.stats().failing_since is None)
    await flush_worker(hass)
    await until(lambda: len(net.lines) >= 2)
    await flush_worker(hass)
    assert actions() == [
        ChangeAction.FORWARDING_FAILED,
        ChangeAction.FORWARDING_RECOVERED,
    ]
    # The health records are forwarded like any other.
    kinds = {e.get("action") for e in net.events()}
    assert "forwarding_failed" in kinds
    assert [e["error"] for e in net.events() if "error" in e] == ["OSError"]
    await forwarder.stop()


async def test_reconfigure_restarts_forwarder(
    hass: HomeAssistant, patched_transport: Net
) -> None:
    """A new port gives a new transport, and the old one is closed."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        options=replace(
            Settings(), forward_enabled=True, forward_host="siem.example"
        ).to_options(),
    )
    entry.add_to_hass(hass)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("custom_components.electric_fence.HA_VERSION", MIN_HA_VERSION)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert [t.port for t in patched_transport.made] == [514]
    first = patched_transport.made[0]
    hass.config_entries.async_update_entry(
        entry,
        options=replace(
            Settings.from_options(entry.options), forward_port=6514
        ).to_options(),
    )
    await hass.async_block_till_done()
    assert [t.port for t in patched_transport.made] == [514, 6514]
    assert first.closed
    assert entry.runtime_data.forwarder is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert patched_transport.made[1].closed


async def test_overlapping_restarts_leave_one_forwarder(
    hass: HomeAssistant, patched_transport: Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two option updates that overlap never orphan a running forwarder."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        options=replace(
            Settings(), forward_enabled=True, forward_host="siem.example"
        ).to_options(),
    )
    entry.add_to_hass(hass)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("custom_components.electric_fence.HA_VERSION", MIN_HA_VERSION)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    runtime = entry.runtime_data
    assert runtime.forwarder is not None
    real_build = fence._async_build_forwarder  # noqa: SLF001
    gate = asyncio.Event()

    async def slow_build(hass: HomeAssistant, runtime: Any) -> Forwarder | None:
        # Stands in for the TLS context load in the executor.
        await gate.wait()
        return await real_build(hass, runtime)

    monkeypatch.setattr(fence, "_async_build_forwarder", slow_build)
    restarts = [
        asyncio.create_task(
            fence._async_restart_forwarder(hass, runtime)  # noqa: SLF001
        )
        for _ in range(2)
    ]
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(*restarts)
    current = runtime.forwarder
    assert current is not None
    assert len(patched_transport.made) == 3
    assert [t.closed for t in patched_transport.made] == [True, True, False]
    assert current._task is not None  # noqa: SLF001
    runtime.worker.submit(attempt(1))
    await flush_worker(hass)
    await until(lambda: len(patched_transport.lines) == 1)
    await asyncio.sleep(0.05)
    assert len(patched_transport.lines) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_forwarding_off_no_posture_item(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """With forwarding off there is no posture item and the sensor is off."""
    runtime = setup_entry.runtime_data
    assert runtime.forwarder is None
    items = await posture.async_check(hass, runtime, runtime.store)
    assert not [i for i in items if i["id"] == "forwarding_failing"]
    assert hass.states.get(SENSOR).state == "off"


async def test_forwarding_sensor_states(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    net: Net,
    clock: Clock,
) -> None:
    """The sensor shows off, connected and failing; posture warns after 5 min."""
    runtime = setup_entry.runtime_data
    assert hass.states.get(SENSOR).state == "off"
    net.down = True
    forwarder = make_forwarder(hass, setup_entry, net)
    runtime.forwarder = forwarder
    forwarder.start()
    await until(lambda: forwarder.stats().failing_since is not None)
    await hass.async_block_till_done()
    assert hass.states.get(SENSOR).state == "failing"

    items = await posture.async_check(hass, runtime, runtime.store)
    assert [i["level"] for i in items if i["id"] == "forwarding_failing"] == ["ok"]
    clock.tick(timedelta(minutes=6))
    items = await posture.async_check(hass, runtime, runtime.store)
    assert [i["level"] for i in items if i["id"] == "forwarding_failing"] == ["warning"]
    net.down = False
    await until(lambda: forwarder.stats().connected)
    await hass.async_block_till_done()
    state = hass.states.get(SENSOR)
    assert state.state == "connected"
    assert set(state.attributes) >= {"queued", "dropped", "sent"}
    await forwarder.stop()
    runtime.forwarder = None


async def test_worker_health_record_is_a_settings_change(
    hass: HomeAssistant, setup_entry: MockConfigEntry, net: Net
) -> None:
    """A forwarded health record is formatted like other settings changes."""
    forwarder = make_forwarder(hass, setup_entry, net)
    forwarder.start()
    forwarder.on_record(
        SettingsChange(
            ts=dt_util.utcnow(),
            kind=ChangeKind.FORWARDING,
            action=ChangeAction.FORWARDING_FAILED,
            target_id=None,
            target_name=None,
            details={"error": "OSError"},
            actor_user_id=None,
            actor_name=None,
            actor_ip=None,
        )
    )
    await until(lambda: len(net.lines) == 1)
    assert net.lines[0].startswith(b"<132>")
    await forwarder.stop()


class SlowTransport(FakeTransport):
    """A transport whose send waits until the test lets it finish."""

    def __init__(self, net: Net, gate: asyncio.Event, started: asyncio.Event) -> None:
        """Register on the network."""
        super().__init__(net)
        self.gate = gate
        self.started = started

    async def send(self, line: bytes) -> None:
        """Wait for the gate, then send."""
        self.started.set()
        await self.gate.wait()
        await super().send(line)


async def test_adopt_during_send_neither_duplicates_nor_goes_negative(
    hass: HomeAssistant, setup_entry: MockConfigEntry, net: Net
) -> None:
    """Adopting the old queue while a send is in flight sends each event once."""
    gate, started = asyncio.Event(), asyncio.Event()
    forwarder = Forwarder(
        hass, setup_entry.runtime_data, lambda: SlowTransport(net, gate, started)
    )
    forwarder.start()
    forwarder.on_record(attempt(3))
    await asyncio.wait_for(started.wait(), 5)
    forwarder.adopt(
        [event_dict(attempt(1), values=False), event_dict(attempt(2), values=False)]
    )
    gate.set()
    await until(lambda: len(net.lines) == 3)
    await asyncio.sleep(0.05)
    assert sorted(e["username"] for e in net.events()) == ["user1", "user2", "user3"]
    stats = forwarder.stats()
    assert (stats.dropped, stats.queued, stats.sent) == (0, 0, 3)
    await forwarder.stop()


async def test_unformattable_event_is_dropped_not_retried(
    hass: HomeAssistant, setup_entry: MockConfigEntry, net: Net
) -> None:
    """A bad event is counted and skipped; the ones behind it still go out."""
    forwarder = make_forwarder(hass, setup_entry, net)
    forwarder.start()
    forwarder.on_record(attempt(1))
    await until(lambda: len(net.lines) == 1)
    bad = attempt(2)
    bad.username_typed = object()  # type: ignore[assignment]
    forwarder.on_record(bad)
    forwarder.on_record(attempt(3))
    await until(lambda: len(net.lines) == 2)
    assert [e["username"] for e in net.events()] == ["user1", "user3"]
    stats = forwarder.stats()
    assert (stats.dropped, stats.queued, stats.last_error) == (1, 0, None)
    assert net.made[0].connected
    await forwarder.stop()


async def test_restart_with_tls_loses_no_record(
    hass: HomeAssistant,
    patched_transport: Net,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Records stored while the new TLS context is built are all delivered."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        options=replace(
            Settings(), forward_enabled=True, forward_host="siem.example"
        ).to_options(),
    )
    entry.add_to_hass(hass)
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", MIN_HA_VERSION)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    net = patched_transport
    net.down = True
    runtime = entry.runtime_data
    release = threading.Event()

    def slow_context(verify: bool, ca: str) -> None:
        release.wait(5)

    monkeypatch.setattr(
        "custom_components.electric_fence.build_ssl_context", slow_context
    )
    hass.config_entries.async_update_entry(
        entry,
        options=replace(
            Settings.from_options(entry.options), forward_protocol="tls"
        ).to_options(),
    )
    await asyncio.sleep(0.05)
    for n in (1, 2, 3):
        runtime.worker.submit(attempt(n))
    await flush_worker(hass)
    release.set()
    await hass.async_block_till_done()
    net.down = False
    await until(lambda: [e.get("username") for e in net.events()].count("user3") == 1)
    await asyncio.sleep(0.1)
    sent = [e["username"] for e in net.events() if "username" in e]
    assert sent == ["user1", "user2", "user3"]
    assert await hass.config_entries.async_unload(entry.entry_id)
