"""Tests for the alerter."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.electric_fence.alerts import Alerter
from custom_components.electric_fence.logbook import async_describe_events
from custom_components.electric_fence.models import (
    LoginAttempt,
    LoginReason,
    LoginResult,
    Source,
    UserExists,
)
from custom_components.electric_fence.notifier import (
    EVENT_IP_BANNED,
    EVENT_SETTINGS_CHANGED,
    EVENT_USERNAME_ATTACK,
    NOTIFICATION_ID,
    Notifier,
)
from custom_components.electric_fence.protection.backend import CoreBanBackend
from custom_components.electric_fence.protection.bans import BanManager, BanView
from custom_components.electric_fence.protection.network import normalise_ip
from custom_components.electric_fence.protection.throttle import AttackView
from custom_components.electric_fence.runtime import Runtime
from custom_components.electric_fence.settings import Settings
from custom_components.electric_fence.storage import FenceStore
from homeassistant.components.logbook import LOGBOOK_ENTRY_MESSAGE
from homeassistant.components.persistent_notification import (
    _async_get_or_create_notifications,
)
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from tests.helpers import flush_worker, make_runtime

IP = "203.0.113.9"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[FenceStore]:
    """Return an open store."""
    s = FenceStore(tmp_path / "fence.db")
    s.open()
    yield s
    s.close()


@pytest.fixture
async def runtime(hass: HomeAssistant, store: FenceStore) -> AsyncIterator[Runtime]:
    """Return a runtime with an alerter and a phone target."""
    assert await async_setup_component(hass, "persistent_notification", {})
    rt = make_runtime(hass, store)
    rt.settings = Settings(notify_targets=("notify.phone",))
    alerter = Alerter(hass, rt)
    rt.alerts = alerter
    hass.states.async_set("notify.phone", "unknown")
    yield rt
    alerter.async_stop()


@pytest.fixture
def sends(hass: HomeAssistant) -> list[ServiceCall]:
    """Capture notify.send_message calls."""
    return async_mock_service(hass, "notify", "send_message")


def ban_view(**kw: Any) -> BanView:
    """Return a ban view."""
    base = BanView(
        ip=normalise_ip(IP),  # type: ignore[arg-type]
        source="electric_fence",
        banned_at=dt_util.utcnow(),
        expires_at=dt_util.utcnow() + timedelta(hours=24),
        failures=5,
        ban_number=1,
        country_code="NL",
        hostname="host",
        ban_id=1,
    )
    return replace(base, **kw)


def bell(hass: HomeAssistant, notification_id: str) -> dict[str, str] | None:
    """Return a persistent notification."""
    return _async_get_or_create_notifications(hass).get(notification_id)


async def settle(hass: HomeAssistant) -> None:
    """Let the push background tasks finish."""
    await hass.async_block_till_done(wait_background_tasks=True)


async def settle_loaded(hass: HomeAssistant) -> None:
    """Let a loaded entry's work and the pushes finish.

    The worker runs as a background task for as long as the entry is loaded,
    so this can't wait for every background task.
    """
    for _ in range(3):
        await hass.async_block_till_done()
        await asyncio.sleep(0)


async def test_switch_off_stops_message_not_event(
    hass: HomeAssistant, runtime: Runtime, store: FenceStore, sends: list[ServiceCall]
) -> None:
    """A kind switched off sends nothing, but the event still fires."""
    assert await async_setup_component(hass, "http", {"http": {}})
    runtime.settings = Settings(
        notify_ip_banned=False, notify_targets=("notify.phone",)
    )
    events = async_capture_events(hass, EVENT_IP_BANNED)
    manager = BanManager(hass, runtime, CoreBanBackend(hass))
    await manager.async_start()
    assert await manager.async_ban(IP, 5, "NL", "host")
    await settle(hass)
    assert len(events) == 1
    assert bell(hass, "electric_fence_ip_banned") is None
    assert sends == []
    manager.async_stop()


async def test_ip_banned_message(
    hass: HomeAssistant, runtime: Runtime, sends: list[ServiceCall]
) -> None:
    """A ban reads as the brief says, with the expiry or Permanent."""
    assert runtime.alerts is not None
    runtime.alerts.ip_banned(ban_view())
    runtime.alerts.ip_banned(ban_view(expires_at=None, hostname=None))
    await settle(hass)
    note = bell(hass, "electric_fence_ip_banned")
    assert note is not None
    assert note["title"] == "Electric Fence: IP banned"
    assert (
        note["message"]
        == f"Banned `{IP}`, Netherlands, after 5 failed logins. Permanent."
    )
    first = sends[0].data["message"]
    assert first.startswith(f"Banned {IP}, Netherlands (host), after 5 failed logins.")
    assert "Expires " in first
    assert "`" not in first


async def test_targets_called(
    hass: HomeAssistant, runtime: Runtime, sends: list[ServiceCall]
) -> None:
    """A configured, existing target gets one send_message call."""
    assert runtime.alerts is not None
    runtime.alerts.ip_banned(ban_view())
    await settle(hass)
    [call] = sends
    assert call.data["entity_id"] == "notify.phone"
    assert call.data["title"] == "Electric Fence: IP banned"


async def test_missing_target_skipped(
    hass: HomeAssistant, runtime: Runtime, sends: list[ServiceCall]
) -> None:
    """A target that doesn't exist is skipped; the bell still shows."""
    assert runtime.alerts is not None
    runtime.settings = Settings(notify_targets=("notify.gone",))
    runtime.alerts.ip_banned(ban_view())
    await settle(hass)
    assert sends == []
    assert bell(hass, "electric_fence_ip_banned") is not None


async def test_failed_push_is_contained(
    hass: HomeAssistant, runtime: Runtime, sends: list[ServiceCall]
) -> None:
    """A send that fails is dropped without raising."""
    assert runtime.alerts is not None

    async def boom(call: ServiceCall) -> None:
        raise RuntimeError("x")

    hass.services.async_remove("notify", "send_message")
    hass.services.async_register("notify", "send_message", boom)
    runtime.alerts.ip_banned(ban_view())
    await settle(hass)
    assert bell(hass, "electric_fence_ip_banned") is not None


async def test_ban_ended(
    hass: HomeAssistant, runtime: Runtime, sends: list[ServiceCall]
) -> None:
    """Ban ended is off by default and reads well when on."""
    assert runtime.alerts is not None
    runtime.alerts.ban_ended(ban_view(), "expired")
    await settle(hass)
    assert bell(hass, "electric_fence_ban_ended") is None
    runtime.settings = Settings(notify_ban_ended=True, notify_targets=("notify.phone",))
    runtime.alerts.ban_ended(ban_view(), "unbanned")
    await settle(hass)
    note = bell(hass, "electric_fence_ban_ended")
    assert note is not None
    assert note["title"] == "Electric Fence: ban ended"
    assert "unbanned" in note["message"]
    assert sends[0].data["title"] == "Electric Fence: ban ended"


async def test_username_attack(
    hass: HomeAssistant, runtime: Runtime, sends: list[ServiceCall]
) -> None:
    """The attack message names the count, addresses and countries."""
    assert runtime.alerts is not None
    runtime.alerts.username_attack(
        AttackView("admin", dt_util.utcnow(), 12, 4, ["NL", "DE"], 1, user_exists=True)
    )
    await settle(hass)
    note = bell(hass, "electric_fence_username_attack")
    assert note is not None
    assert note["message"] == (
        "Username `admin` is under attack: 12 failed logins from 4 addresses "
        "(NL, DE). Logins for it are slowed down."
    )
    assert note["title"] == "Electric Fence: username under attack"
    assert sends[0].data["message"].startswith("Username admin is under attack")


async def test_username_attack_masks_unknown_name(
    hass: HomeAssistant, runtime: Runtime, sends: list[ServiceCall]
) -> None:
    """A username that belongs to no user is masked on the bell and the phone."""
    assert runtime.alerts is not None
    runtime.alerts.username_attack(
        AttackView("hostile-secret", dt_util.utcnow(), 12, 4, [], 1)
    )
    await settle(hass)
    note = bell(hass, "electric_fence_username_attack")
    assert note is not None
    assert "hostile-secret" not in note["message"]
    assert note["message"].startswith("Username `ho•••` is under attack")
    assert "hostile-secret" not in sends[0].data["message"]
    assert sends[0].data["message"].startswith("Username ho••• is under attack")


def success(**kw: Any) -> LoginAttempt:
    """Return a confirmed success from a new country."""
    base = LoginAttempt(
        ts=dt_util.utcnow(),
        result=LoginResult.SUCCESS,
        reason=LoginReason.OK,
        username_typed="ann",
        user_exists=UserExists.YES,
        user_id=None,
        provider="homeassistant",
        ip=IP,
        source=Source.REMOTE,
        url="/auth/login_flow",
        user_agent="t",
        client_id="c",
        country_code="DE",
        confirmed=True,
        new_country=True,
    )
    return replace(base, **kw)


async def test_new_country(
    hass: HomeAssistant, runtime: Runtime, sends: list[ServiceCall]
) -> None:
    """The user's name comes from the auth store, else 'A user'."""
    assert runtime.alerts is not None
    user = await hass.auth.async_create_user("Ann")
    runtime.alerts.new_country(success(user_id=user.id))
    await settle(hass)
    note = bell(hass, "electric_fence_new_country")
    assert note is not None
    assert (
        note["message"] == f"`Ann` logged in from Germany for the first time (`{IP}`)."
    )
    assert note["title"] == "Electric Fence: new country login"
    assert sends[0].data["message"] == (
        f"Ann logged in from Germany for the first time ({IP})."
    )
    runtime.alerts.new_country(success(user_id=None))
    assert (bell(hass, "electric_fence_new_country") or {})["message"].startswith(
        "A user logged in"
    )


async def test_new_country_off(
    hass: HomeAssistant, runtime: Runtime, sends: list[ServiceCall]
) -> None:
    """The switch applies live."""
    assert runtime.alerts is not None
    runtime.settings = Settings(notify_new_country=False)
    runtime.alerts.new_country(success())
    await settle(hass)
    assert bell(hass, "electric_fence_new_country") is None


async def test_failed_login_push_rate_limited(
    hass: HomeAssistant,
    runtime: Runtime,
    sends: list[ServiceCall],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Two failures a minute apart: two bell updates, one push."""
    assert runtime.alerts is not None
    runtime.alerts.failed_login("first")
    await settle(hass)
    freezer.tick(timedelta(minutes=1))
    runtime.alerts.failed_login("second")
    await settle(hass)
    note = bell(hass, NOTIFICATION_ID)
    assert note is not None
    assert note["message"] == "second"
    assert len(sends) == 1
    freezer.tick(timedelta(minutes=10))
    runtime.alerts.failed_login("third")
    await settle(hass)
    assert len(sends) == 2


async def test_ban_push_rate_limited(
    hass: HomeAssistant,
    runtime: Runtime,
    sends: list[ServiceCall],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A flood of bans: every ban on the bell, one push, then one summary."""
    assert runtime.alerts is not None
    runtime.alerts.ip_banned(ban_view())
    await settle(hass)
    assert len(sends) == 1
    assert sends[0].data["message"].startswith(f"Banned {IP}")
    for i in range(20):
        freezer.tick(timedelta(seconds=10))
        runtime.alerts.ip_banned(ban_view(ip=normalise_ip(f"198.51.100.{i}")))
    await settle(hass)
    note = bell(hass, "electric_fence_ip_banned")
    assert note is not None
    assert note["message"].startswith("Banned `198.51.100.19`")
    assert len(sends) == 1

    freezer.tick(timedelta(minutes=10))
    async_fire_time_changed(hass)
    await settle(hass)
    assert len(sends) == 2
    assert sends[1].data["title"] == "Electric Fence: IP banned"
    assert sends[1].data["message"] == "20 addresses banned since the last message."

    # The summary counts as a push: the next ban waits for the interval.
    freezer.tick(timedelta(minutes=1))
    runtime.alerts.ip_banned(ban_view())
    await settle(hass)
    assert len(sends) == 2
    freezer.tick(timedelta(minutes=10))
    async_fire_time_changed(hass)
    await settle(hass)
    assert sends[2].data["message"] == "1 address banned since the last message."


async def test_ban_push_folds_waiting_bans(
    hass: HomeAssistant,
    runtime: Runtime,
    sends: list[ServiceCall],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A ban pushed while a summary still waits carries the waiting count."""
    assert runtime.alerts is not None
    runtime.alerts.ip_banned(ban_view())
    freezer.tick(timedelta(minutes=1))
    runtime.alerts.ip_banned(ban_view())
    freezer.tick(timedelta(minutes=10))
    # Before the summary timer runs.
    runtime.alerts.ip_banned(ban_view())
    await settle(hass)
    assert len(sends) == 2
    assert (
        sends[1]
        .data["message"]
        .startswith(f"2 addresses banned since the last message. Latest: Banned {IP}")
    )
    async_fire_time_changed(hass)
    await settle(hass)
    assert len(sends) == 2


async def test_ban_summary_skipped_when_switched_off(
    hass: HomeAssistant,
    runtime: Runtime,
    sends: list[ServiceCall],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Turning the kind off before the summary is due sends nothing."""
    assert runtime.alerts is not None
    runtime.alerts.ip_banned(ban_view())
    runtime.alerts.ip_banned(ban_view())
    runtime.settings = Settings(
        notify_ip_banned=False, notify_targets=("notify.phone",)
    )
    freezer.tick(timedelta(minutes=10))
    async_fire_time_changed(hass)
    await settle(hass)
    assert len(sends) == 1


async def test_failed_login_off(
    hass: HomeAssistant, runtime: Runtime, sends: list[ServiceCall]
) -> None:
    """With the switch off, nothing is sent."""
    assert runtime.alerts is not None
    runtime.settings = Settings(notify_failed_login=False)
    runtime.alerts.failed_login("x")
    await settle(hass)
    assert bell(hass, NOTIFICATION_ID) is None
    assert sends == []


async def test_hostile_username_inert(
    hass: HomeAssistant,
    runtime: Runtime,
    store: FenceStore,
    sends: list[ServiceCall],
) -> None:
    """A hostile username is code in the bell and plain text in the push."""
    hostile = '"<img src=x>` *b*'
    notifier = Notifier(hass, runtime)
    rec = LoginAttempt(
        ts=dt_util.utcnow(),
        result=LoginResult.FAILED,
        reason=LoginReason.WRONG_PASSWORD,
        username_typed=hostile,
        user_exists=UserExists.YES,
        user_id="u",
        provider="homeassistant",
        ip=IP,
        source=Source.REMOTE,
        url="/auth/login_flow",
        user_agent="t",
        client_id="c",
    )
    store.insert([rec])
    notifier.on_record(rec)
    await settle(hass)
    note = bell(hass, NOTIFICATION_ID)
    assert note is not None
    assert '`"<img src=x>ˋ *b*`' in note["message"]
    [call] = sends
    assert '"<img src=x>ˋ *b*' in call.data["message"]
    assert "`" not in call.data["message"]
    assert "Open Electric Fence" not in call.data["message"]


async def test_never_raises(hass: HomeAssistant, runtime: Runtime) -> None:
    """A broken settings object doesn't escape the callbacks."""
    assert runtime.alerts is not None
    runtime.settings = None  # type: ignore[assignment]
    runtime.alerts.ip_banned(ban_view())
    runtime.alerts.failed_login("x")


async def test_ban_end_to_end_notifies(
    hass: HomeAssistant, setup_entry: MockConfigEntry, sends: list[ServiceCall]
) -> None:
    """Failures from one address ban it, ring the bell and push to the phone."""
    runtime = setup_entry.runtime_data
    runtime.settings = replace(
        runtime.settings, notify_ip_banned=True, notify_targets=("notify.phone",)
    )
    hass.states.async_set("notify.phone", "unknown")
    policy = runtime.ip_policy
    assert policy is not None
    for _ in range(runtime.settings.ban_threshold):
        policy.on_attempt(IP, LoginResult.FAILED, LoginReason.WRONG_PASSWORD)
    await settle_loaded(hass)
    assert runtime.bans is not None
    assert [str(v.ip) for v in runtime.bans.active()] == [IP]
    note = bell(hass, "electric_fence_ip_banned")
    assert note is not None
    assert note["message"].startswith(f"Banned `{IP}`")
    [call] = sends
    assert call.data["entity_id"] == "notify.phone"
    assert call.data["message"].startswith(f"Banned {IP}")


async def test_attack_end_to_end_masks_unknown_username(
    hass: HomeAssistant, setup_entry: MockConfigEntry, sends: list[ServiceCall]
) -> None:
    """A hostile username stays masked in every sink outside the panel."""
    runtime = setup_entry.runtime_data
    runtime.settings = replace(runtime.settings, notify_targets=("notify.phone",))
    hass.states.async_set("notify.phone", "unknown")
    attack_events = async_capture_events(hass, EVENT_USERNAME_ATTACK)
    change_events = async_capture_events(hass, EVENT_SETTINGS_CHANGED)
    guard = runtime.username_guard
    assert guard is not None
    hostile = "hostile-secret"
    for i in range(10):
        guard.on_attempt(
            hostile,
            f"198.51.100.{i % 3 + 1}",
            LoginResult.FAILED,
            LoginReason.NO_SUCH_USER,
            UserExists.NO,
        )
    assert await guard.async_end(hostile, None, "Ann")
    await settle_loaded(hass)
    await flush_worker(hass)
    await settle_loaded(hass)

    describe = {}

    def register(domain: str, event_type: str, fn: Any) -> None:
        describe[event_type] = fn

    async_describe_events(hass, register)
    texts = [str(e.data) for e in attack_events + change_events]
    texts += [
        describe[EVENT_SETTINGS_CHANGED](e)[LOGBOOK_ENTRY_MESSAGE]
        for e in change_events
    ]
    note = bell(hass, "electric_fence_username_attack")
    assert note is not None
    texts += [note["message"], *(c.data["message"] for c in sends)]
    assert len(change_events) == 2
    assert all(hostile not in t for t in texts)
    assert any("ho•••" in t for t in texts)
    assert attack_events[0].data["username_masked"] is True
    # The panel (and the store behind it) keeps the full name.
    assert [a.username for a in runtime.store.query_attacks(active=False)] == [hostile]


def attack(name: str = "admin") -> AttackView:
    """Return an attack on a username that exists."""
    return AttackView(name, dt_util.utcnow(), 12, 4, [], 1, user_exists=True)


async def test_ban_ended_push_rate_limited(
    hass: HomeAssistant,
    runtime: Runtime,
    sends: list[ServiceCall],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Many bans ending at once: one push now, one summary later."""
    assert runtime.alerts is not None
    runtime.settings = Settings(notify_ban_ended=True, notify_targets=("notify.phone",))
    for i in range(30):
        runtime.alerts.ban_ended(
            ban_view(ip=normalise_ip(f"198.51.100.{i}")), "expired"
        )
    await settle(hass)
    assert len(sends) == 1
    note = bell(hass, "electric_fence_ban_ended")
    assert note is not None
    assert "198.51.100.29" in note["message"]

    freezer.tick(timedelta(minutes=10))
    async_fire_time_changed(hass)
    await settle(hass)
    assert len(sends) == 2
    assert sends[1].data["title"] == "Electric Fence: ban ended"
    assert sends[1].data["message"] == "29 bans ended since the last message."


async def test_username_attack_push_rate_limited(
    hass: HomeAssistant,
    runtime: Runtime,
    sends: list[ServiceCall],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Attacks on many usernames: one push now, one summary later."""
    assert runtime.alerts is not None
    for i in range(50):
        runtime.alerts.username_attack(attack(f"user{i}"))
    await settle(hass)
    assert len(sends) == 1

    freezer.tick(timedelta(minutes=10))
    async_fire_time_changed(hass)
    await settle(hass)
    assert len(sends) == 2
    assert sends[1].data["message"] == (
        "49 usernames came under attack since the last message."
    )


async def test_new_country_push_rate_limited(
    hass: HomeAssistant,
    runtime: Runtime,
    sends: list[ServiceCall],
    freezer: FrozenDateTimeFactory,
) -> None:
    """New-country logins are limited like the other kinds."""
    assert runtime.alerts is not None
    runtime.alerts.new_country(success())
    runtime.alerts.new_country(success(country_code="FR"))
    await settle(hass)
    assert len(sends) == 1
    freezer.tick(timedelta(minutes=10))
    async_fire_time_changed(hass)
    await settle(hass)
    assert sends[1].data["message"] == ("1 new-country login since the last message.")


async def test_pushes_capped_per_hour(
    hass: HomeAssistant,
    runtime: Runtime,
    sends: list[ServiceCall],
    freezer: FrozenDateTimeFactory,
) -> None:
    """At most 6 pushes an hour over all kinds; then one note of what was held."""
    assert runtime.alerts is not None
    alerts = runtime.alerts
    runtime.settings = Settings(notify_ban_ended=True, notify_targets=("notify.phone",))

    def burst() -> None:
        alerts.failed_login("failed")
        alerts.ip_banned(ban_view())
        alerts.ban_ended(ban_view(), "expired")
        alerts.username_attack(attack())
        alerts.new_country(success())

    burst()
    await settle(hass)
    assert len(sends) == 5
    freezer.tick(timedelta(minutes=10))
    burst()
    await settle(hass)
    # One more fits under the cap; the other four are held back.
    assert len(sends) == 6
    assert sends[5].data["message"] == "failed"

    freezer.tick(timedelta(minutes=49))
    async_fire_time_changed(hass)
    await settle(hass)
    assert len(sends) == 6

    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass)
    await settle(hass)
    assert len(sends) == 7
    assert sends[6].data["title"] == "Electric Fence"
    assert sends[6].data["message"] == (
        "4 more notifications were not sent to your phone, to keep it quiet "
        "during a flood. Open Home Assistant to see them."
    )


async def test_held_note_skipped_after_stop(
    hass: HomeAssistant,
    runtime: Runtime,
    sends: list[ServiceCall],
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stopping cancels the note about held-back pushes."""
    monkeypatch.setattr("custom_components.electric_fence.alerts.PUSH_HOURLY_LIMIT", 1)
    assert runtime.alerts is not None
    runtime.alerts.failed_login("first")
    freezer.tick(timedelta(minutes=10))
    runtime.alerts.failed_login("held")
    await settle(hass)
    assert len(sends) == 1
    runtime.alerts.async_stop()
    freezer.tick(timedelta(hours=1))
    async_fire_time_changed(hass)
    await settle(hass)
    assert len(sends) == 1


async def test_username_attack_off(
    hass: HomeAssistant, runtime: Runtime, sends: list[ServiceCall]
) -> None:
    """With the switch off, an attack sends nothing."""
    assert runtime.alerts is not None
    runtime.settings = Settings(
        notify_username_attack=False, notify_targets=("notify.phone",)
    )
    runtime.alerts.username_attack(attack())
    await settle(hass)
    assert bell(hass, "electric_fence_username_attack") is None
    assert sends == []
