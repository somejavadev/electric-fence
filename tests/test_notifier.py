"""Tests for the notifier: events and the persistent notification."""

import asyncio
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.electric_fence import notifier as notifier_module
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
from custom_components.electric_fence.notifier import (
    NOTIFICATION_ID,
    Notifier,
    build_message,
    country_text,
)
from custom_components.electric_fence.settings import Settings
from custom_components.electric_fence.storage import FenceStore
from homeassistant.components.persistent_notification import (
    _async_get_or_create_notifications,
)
from homeassistant.const import MATCH_ALL
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from tests.helpers import create_hass_user, flush_worker, make_runtime

TYPED = "admin-secret-pw"


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
        hostname="host.example.net",
    )
    return replace(base, **kw)  # type: ignore[arg-type]


@pytest.fixture
def store(tmp_path: Path) -> Iterator[FenceStore]:
    """Return an open store."""
    s = FenceStore(tmp_path / "fence.db")
    s.open()
    yield s
    s.close()


@pytest.fixture
async def notifier(hass: HomeAssistant, store: FenceStore) -> Notifier:
    """Return a notifier on a real store, with notifications set up."""
    assert await async_setup_component(hass, "persistent_notification", {})
    return Notifier(hass, make_runtime(hass, store))


def notification(hass: HomeAssistant) -> str | None:
    """Return the message of the Electric Fence notification."""
    found = _async_get_or_create_notifications(hass).get(NOTIFICATION_ID)
    return None if found is None else found["message"]


async def deliver(
    hass: HomeAssistant, notifier: Notifier, store: FenceStore, *records: LoginAttempt
) -> None:
    """Store the records, then hand them to the notifier like the worker."""
    store.insert(records)
    for rec in records:
        notifier.on_record(rec)
    await hass.async_block_till_done()


async def test_failed_event_masks_unknown_username(
    hass: HomeAssistant, notifier: Notifier
) -> None:
    """A username that does not exist is masked in the event."""
    events = async_capture_events(hass, "electric_fence_login_failed")
    notifier.on_record(attempt(count=3))
    await hass.async_block_till_done()
    assert len(events) == 1
    data = events[0].data
    assert data["username"] == "ad•••"
    assert data["username_masked"] is True
    assert data["user_exists"] is False
    assert data["user_id"] is None
    assert data["reason"] == "no_such_user"
    assert data["ip"] == "203.0.113.7"
    assert data["country"] == "RU"
    assert data["hostname"] == "host.example.net"
    assert data["source"] == "remote"
    assert data["client"] == "http://127.0.0.1:8123/"
    assert data["count"] == 3


async def test_failed_event_full_name_for_existing_user(
    hass: HomeAssistant, notifier: Notifier
) -> None:
    """An existing user is shown in full, with the user ID."""
    events = async_capture_events(hass, "electric_fence_login_failed")
    notifier.on_record(
        attempt(
            reason=LoginReason.WRONG_PASSWORD,
            user_exists=UserExists.YES,
            user_id="u1",
            client_name="Companion",
        )
    )
    await hass.async_block_till_done()
    data = events[0].data
    assert data["username"] == "admin"
    assert data["username_masked"] is False
    assert data["user_exists"] is True
    assert data["user_id"] == "u1"
    assert data["client"] == "Companion"


async def test_token_failure_event_has_no_username(
    hass: HomeAssistant, notifier: Notifier
) -> None:
    """A token failure has no username and is not masked."""
    events = async_capture_events(hass, "electric_fence_login_failed")
    notifier.on_record(
        attempt(
            reason=LoginReason.INVALID_TOKEN,
            username_typed=None,
            user_exists=UserExists.UNKNOWN,
        )
    )
    await hass.async_block_till_done()
    data = events[0].data
    assert data["username"] is None
    assert data["username_masked"] is False
    assert data["user_exists"] is None


async def test_blocked_event_uses_blocked_reason(
    hass: HomeAssistant, notifier: Notifier
) -> None:
    """A blocked attempt fires the failed event with the blocked reason."""
    events = async_capture_events(hass, "electric_fence_login_failed")
    notifier.on_record(
        attempt(
            result=LoginResult.BLOCKED,
            reason=LoginReason.BLOCKED_LOCAL_ONLY,
            user_exists=UserExists.YES,
            user_id="u1",
        )
    )
    await hass.async_block_till_done()
    assert events[0].data["reason"] == "blocked_local_only"
    assert events[0].data["user_id"] == "u1"


async def test_success_event_uses_real_username(
    hass: HomeAssistant, notifier: Notifier
) -> None:
    """The success event carries the user's real name, not the typed text."""
    user = await create_hass_user(hass, "alice", "pw-123456")
    events = async_capture_events(hass, "electric_fence_login_succeeded")
    notifier.on_record(
        attempt(
            result=LoginResult.SUCCESS,
            reason=LoginReason.OK,
            username_typed="ALICE-typed-secret",
            user_exists=UserExists.YES,
            user_id=user.id,
            mfa_module="totp",
            client_name="Companion",
        )
    )
    await hass.async_block_till_done()
    data = events[0].data
    assert data["user_id"] == user.id
    assert data["username"] == "alice"
    assert data["mfa_used"] is True
    assert data["client"] == "Companion"
    assert data["country"] == "RU"
    assert data["source"] == "remote"
    assert "typed-secret" not in str(data)
    assert not notification(hass)


async def test_success_event_unknown_user_and_unconfirmed(
    hass: HomeAssistant, notifier: Notifier
) -> None:
    """An unknown user ID gives no name; an unconfirmed success fires nothing."""
    events = async_capture_events(hass, "electric_fence_login_succeeded")
    ok = attempt(
        result=LoginResult.SUCCESS,
        reason=LoginReason.OK,
        user_exists=UserExists.YES,
        user_id="missing",
    )
    notifier.on_record(ok)
    notifier.on_record(replace(ok, confirmed=False))
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data["username"] is None
    assert events[0].data["mfa_used"] is False


async def test_settings_changed_event_system_has_null_actor(
    hass: HomeAssistant, notifier: Notifier
) -> None:
    """A change by the system has no actor."""
    events = async_capture_events(hass, "electric_fence_settings_changed")
    notifier.on_record(
        SettingsChange(
            ts=dt_util.utcnow(),
            kind=ChangeKind.CONFIG_ENTRY,
            action=ChangeAction.ADDED,
            target_id="e1",
            target_name="Thing",
            details={"secret": "x"},
            actor_user_id=None,
            actor_name=None,
            actor_ip=None,
        )
    )
    notifier.on_record(
        SettingsChange(
            ts=dt_util.utcnow(),
            kind=ChangeKind.USER,
            action=ChangeAction.PASSWORD_CHANGED,
            target_id="u2",
            target_name="Bob",
            details={},
            actor_user_id="u1",
            actor_name="Admin",
            actor_ip="192.168.1.5",
        )
    )
    await hass.async_block_till_done()
    assert events[0].data == {
        "kind": "config_entry",
        "action": "added",
        "target": "e1",
        "target_name": "Thing",
        "changed_by_user_id": None,
        "changed_by_name": None,
        "ip": None,
        "changed": [],
    }
    assert events[1].data["changed_by_name"] == "Admin"
    assert events[1].data["ip"] == "192.168.1.5"
    assert events[1].data["kind"] == "user"
    assert notification(hass) is None


async def test_notification_text_masks_and_counts(
    hass: HomeAssistant, notifier: Notifier, store: FenceStore
) -> None:
    """The notification masks the name, names the country and counts 24 hours."""
    old = attempt(ts=dt_util.utcnow() - timedelta(hours=25), count=40)
    earlier = attempt(count=15)
    blocked = attempt(result=LoginResult.BLOCKED, reason=LoginReason.BLOCKED_INACTIVE)
    last = attempt(count=1, username_typed="admin")
    store.insert([old])
    await deliver(hass, notifier, store, earlier, blocked, last)
    message = notification(hass)
    assert message is not None
    assert "`ad•••`" in message
    assert "Russia" in message
    assert "no such user" in message
    assert "`203.0.113.7`" in message
    assert "(`host.example.net`)" in message
    assert "17 failed logins in the last 24 hours" in message
    assert message.endswith("[Open Electric Fence](/electric-fence)")
    persistent = _async_get_or_create_notifications(hass)[NOTIFICATION_ID]
    assert persistent["title"] == "Electric Fence: failed login"


async def test_notification_shows_blocked_reason_and_full_existing_name(
    hass: HomeAssistant, notifier: Notifier, store: FenceStore
) -> None:
    """A blocked attempt for an existing user is shown in full."""
    await deliver(
        hass,
        notifier,
        store,
        attempt(
            result=LoginResult.BLOCKED,
            reason=LoginReason.BLOCKED_LOCAL_ONLY,
            user_exists=UserExists.YES,
            country_code=None,
            hostname=None,
        ),
    )
    message = notification(hass)
    assert message is not None
    assert "Failed login for `admin` (blocked: local only) from `203.0.113.7`." in (
        message
    )


async def test_notification_neutralises_markdown(
    hass: HomeAssistant, notifier: Notifier, store: FenceStore
) -> None:
    """Attacker-controlled text stays inside inline code."""
    await deliver(
        hass,
        notifier,
        store,
        attempt(
            hostname="`](javascript:alert(1))\n x",
            ip="1.2.3.4`](javascript:evil)",
            username_typed="[x](javascript:y)` z",
            user_exists=UserExists.YES,
        ),
    )
    message = notification(hass)
    assert message is not None
    outside = ""
    inside = False
    for ch in message:
        if ch == "`":
            inside = not inside
        elif not inside:
            outside += ch
    assert "](javascript:" not in outside
    assert "\n" not in message
    assert " " not in message
    assert " " not in message


async def test_token_failures_grouped_per_ip(
    hass: HomeAssistant, notifier: Notifier, store: FenceStore
) -> None:
    """Token failures from one IP in the last hour are counted together."""

    def token(ip: str, **kw: object) -> LoginAttempt:
        return attempt(
            reason=LoginReason.INVALID_TOKEN,
            username_typed=None,
            user_exists=UserExists.UNKNOWN,
            ip=ip,
            **kw,
        )

    other = token("192.168.1.99")
    stale = token("192.168.1.30", ts=dt_util.utcnow() - timedelta(hours=2), count=50)
    mine = [token("192.168.1.30") for _ in range(11)] + [token("192.168.1.30", count=1)]
    store.insert([other, stale])
    await deliver(hass, notifier, store, *mine)
    message = notification(hass)
    assert message is not None
    assert "12 invalid token attempts from `192.168.1.30`" in message
    assert "Failed login for" not in message
    assert "63 failed logins in the last 24 hours" in message


async def test_notification_off_creates_nothing(
    hass: HomeAssistant, store: FenceStore
) -> None:
    """With notifications off, events still fire and no notification exists."""
    assert await async_setup_component(hass, "persistent_notification", {})
    runtime = make_runtime(hass, store)
    runtime.settings = Settings(notify_failed_login=False)
    notifier = Notifier(hass, runtime)
    events = async_capture_events(hass, "electric_fence_login_failed")
    await deliver(hass, notifier, store, attempt())
    assert len(events) == 1
    assert notification(hass) is None
    # The setting applies live.
    runtime.settings = Settings(notify_failed_login=True)
    await deliver(hass, notifier, store, attempt())
    assert notification(hass) is not None


async def test_notification_keeps_last_count_when_query_fails(
    hass: HomeAssistant, notifier: Notifier, store: FenceStore
) -> None:
    """A failing count query keeps the notification, with the last count."""
    await deliver(hass, notifier, store, attempt(count=4))
    assert "4 failed logins" in (notification(hass) or "")
    with patch.object(store, "count_failed_since", side_effect=RuntimeError("db")):
        await deliver(hass, notifier, store, attempt(username_typed="zed"))
    message = notification(hass)
    assert message is not None
    assert "`ze•••`" in message
    assert "4 failed logins in the last 24 hours" in message


async def test_notification_without_count_when_never_counted(
    hass: HomeAssistant, notifier: Notifier, store: FenceStore
) -> None:
    """With no count ever known, the count line is left out."""
    with patch.object(store, "count_failed_since", side_effect=RuntimeError("db")):
        await deliver(hass, notifier, store, attempt(ip=None, hostname=None))
    message = notification(hass)
    assert message is not None
    assert "failed logins in the last" not in message
    assert "from an unknown address" in message


async def test_burst_is_coalesced_to_the_newest(
    hass: HomeAssistant, notifier: Notifier, store: FenceStore
) -> None:
    """Many records in one tick give one update, showing the newest."""
    recs = [attempt(username_typed=f"user{i:02d}") for i in range(30)]
    await deliver(hass, notifier, store, *recs)
    message = notification(hass)
    assert message is not None
    assert "`us•••`" in message
    assert "30 failed logins" in message


async def test_stop_finishes_pending_update(
    hass: HomeAssistant, notifier: Notifier, store: FenceStore
) -> None:
    """Stopping lets a pending update finish: the last record still notifies."""
    store.insert([attempt()])
    notifier.on_record(attempt())
    await notifier.async_stop()
    assert notification(hass) is not None
    await notifier.async_stop()


async def test_stop_cancels_hanging_update(
    hass: HomeAssistant, notifier: Notifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An update that hangs past the stop timeout is cancelled."""
    monkeypatch.setattr(notifier_module, "STOP_TIMEOUT", 0.01)

    async def hang(record: LoginAttempt) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(notifier, "_async_update", hang)
    notifier.on_record(attempt())
    task = notifier._task  # noqa: SLF001
    assert task is not None
    await notifier.async_stop()
    await hass.async_block_till_done()
    assert task.cancelled()
    assert notifier._task is None  # noqa: SLF001
    assert notification(hass) is None


async def test_raw_typed_username_not_in_event_or_notification(
    hass: HomeAssistant, notifier: Notifier, store: FenceStore
) -> None:
    """The raw typed text never reaches an event or the notification."""
    events: list[Event] = []

    @callback
    def collect(event: Event) -> None:
        events.append(event)

    hass.bus.async_listen(MATCH_ALL, collect)
    for exists in (UserExists.NO, UserExists.UNKNOWN):
        await deliver(
            hass, notifier, store, attempt(username_typed=TYPED, user_exists=exists)
        )
    assert len(events) == 2
    assert TYPED not in repr([e.data for e in events])
    assert TYPED not in (notification(hass) or "")
    assert "ad•••" in repr(events[0].data)


def test_country_text() -> None:
    """Known codes give names; unknown codes are shown as they are."""
    assert country_text("ru") == "Russia"
    assert country_text("ZZ") == "ZZ"
    assert country_text("") is None
    assert country_text(None) is None


async def test_setup_registers_notifier_on_worker(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """A stored record reaches the notifier; unloading detaches it."""
    events = async_capture_events(hass, "electric_fence_login_failed")
    runtime = setup_entry.runtime_data
    runtime.worker.submit(attempt())
    await flush_worker(hass)
    await hass.async_block_till_done()
    assert len(events) == 1
    assert "1 failed logins in the last 24 hours" in (notification(hass) or "")
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    assert not runtime.worker._listeners  # noqa: SLF001


def test_wrong_code_reason_text() -> None:
    """A wrong two-factor code reads as such (R43), never as "2FA"."""
    message = build_message(
        attempt(
            reason=LoginReason.WRONG_2FA_CODE,
            username_typed="alice",
            user_exists=UserExists.YES,
        ),
        1,
        None,
    )
    assert "(wrong two-factor code)" in message
    assert "2FA" not in message


async def test_event_has_changed_names_never_values(
    hass: HomeAssistant, notifier: Notifier
) -> None:
    """The event lists the setting names, never a value or before/after."""
    events = async_capture_events(hass, "electric_fence_settings_changed")
    notifier.on_record(
        SettingsChange(
            ts=dt_util.utcnow(),
            kind=ChangeKind.CORE_CONFIG,
            action=ChangeAction.UPDATED,
            target_id="core",
            target_name="Core",
            details={
                "changed": ["location_name"],
                "values": {"location_name": {"before": "OldHome", "after": "NewHome"}},
            },
            actor_user_id="u1",
            actor_name="Admin",
            actor_ip=None,
        )
    )
    notifier.on_record(
        SettingsChange(
            ts=dt_util.utcnow(),
            kind=ChangeKind.USER,
            action=ChangeAction.ADDED,
            target_id="u2",
            target_name="Bob",
            details={},
            actor_user_id=None,
            actor_name=None,
            actor_ip=None,
        )
    )
    await hass.async_block_till_done()
    assert events[0].data["changed"] == ["location_name"]
    assert "values" not in events[0].data
    text = repr(events[0].data)
    for word in ("OldHome", "NewHome", "before", "after"):
        assert word not in text
    assert events[1].data["changed"] == []
