"""Tests for the admin WebSocket API."""

# ruff: noqa: SLF001

from dataclasses import fields, replace
from datetime import date, timedelta
from ipaddress import ip_address
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser
from pytest_homeassistant_custom_component.typing import (
    ClientSessionGenerator,
    MockHAClientWebSocket,
    WebSocketGenerator,
)

from custom_components.electric_fence import websocket_api as fence_ws
from custom_components.electric_fence.models import (
    ChangeAction,
    ChangeKind,
    LoginAttempt,
    LoginReason,
    LoginResult,
    Record,
    SettingsChange,
    Source,
    UserExists,
)
from custom_components.electric_fence.protection.throttle import AttackView
from custom_components.electric_fence.storage import ChangeFilter, ChangeRow, LoginRow
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from tests.helpers import create_hass_user, flush_worker, login_flow

# Stored fields plus `user_name`, resolved when the item is built.
LOGIN_KEYS = {f.name for f in fields(LoginRow)} | {"user_name"}
CHANGE_KEYS = {f.name for f in fields(ChangeRow)}
COMMANDS = (
    "electric_fence/overview",
    "electric_fence/logins",
    "electric_fence/changes",
    "electric_fence/users",
    "electric_fence/status",
    "electric_fence/subscribe",
    "electric_fence/bans",
    "electric_fence/unban",
    "electric_fence/attacks",
    "electric_fence/end_slowdown",
    "electric_fence/clear_countries",
)
ARGS: dict[str, dict[str, Any]] = {
    "electric_fence/bans": {"state": "active"},
    "electric_fence/unban": {"ip": "203.0.113.9"},
    "electric_fence/attacks": {"state": "active"},
    "electric_fence/end_slowdown": {"username": "admin"},
}


def attempt(**kw: Any) -> LoginAttempt:
    """Return a failed login attempt."""
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
    return replace(base, **kw)


def change(**kw: Any) -> SettingsChange:
    """Return a settings change made by a user."""
    base = SettingsChange(
        ts=dt_util.utcnow(),
        kind=ChangeKind.CONFIG_ENTRY,
        action=ChangeAction.ADDED,
        target_id="abc",
        target_name="Tuya",
        details={"domain": "tuya"},
        actor_user_id="u1",
        actor_name="Alice",
        actor_ip="192.168.1.5",
    )
    return replace(base, **kw)


async def store(hass: HomeAssistant, entry: MockConfigEntry, *recs: Record) -> None:
    """Insert records straight into the store, in order."""
    await hass.async_add_executor_job(entry.runtime_data.store.insert, list(recs))


async def call(client: MockHAClientWebSocket, cmd: str, **kw: Any) -> dict[str, Any]:
    """Send a command and return the response message."""
    await client.send_json_auto_id({"type": cmd, **kw})
    msg: dict[str, Any] = await client.receive_json()
    return msg


async def result(client: MockHAClientWebSocket, cmd: str, **kw: Any) -> Any:
    """Send a command and return its successful result."""
    msg = await call(client, cmd, **kw)
    assert msg["success"], msg
    return msg["result"]


async def subscribe(client: MockHAClientWebSocket) -> int:
    """Subscribe to live records and return the subscription ID."""
    msg = await call(client, "electric_fence/subscribe")
    assert msg["success"], msg
    assert msg["result"] is None
    sub_id: int = msg["id"]
    return sub_id


@pytest.fixture
async def ws(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
) -> MockHAClientWebSocket:
    """Return an admin WebSocket client with Electric Fence loaded."""
    return await hass_ws_client(hass)


async def test_non_admin_rejected(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
) -> None:
    """Every command refuses a user who is not an admin."""
    hass_admin_user.groups = []
    client = await hass_ws_client(hass)
    for cmd in COMMANDS:
        msg = await call(client, cmd, **ARGS.get(cmd, {}))
        assert not msg["success"], cmd
        assert msg["error"]["code"] == "unauthorized", cmd


async def test_not_loaded(hass: HomeAssistant, ws: MockHAClientWebSocket) -> None:
    """Every command answers `not_loaded` when no entry is loaded."""
    for entry in hass.config_entries.async_entries("electric_fence"):
        assert await hass.config_entries.async_unload(entry.entry_id)
    for cmd in COMMANDS:
        msg = await call(ws, cmd, **ARGS.get(cmd, {}))
        assert not msg["success"], cmd
        assert msg["error"]["code"] == "not_loaded", cmd


async def test_overview_shape_and_counts(
    hass: HomeAssistant, setup_entry: MockConfigEntry, ws: MockHAClientWebSocket
) -> None:
    """The overview has four cards with counts and the newest five items."""
    now = dt_util.utcnow()
    old = now - timedelta(days=2)
    await store(
        hass,
        setup_entry,
        attempt(ts=old, ip="203.0.113.99"),
        *(
            attempt(ts=now - timedelta(minutes=10 - i), ip=f"203.0.113.{i}")
            for i in range(6)
        ),
        attempt(ts=now - timedelta(minutes=5), ip="203.0.113.50", count=3),
        attempt(
            ts=now - timedelta(seconds=30),
            result=LoginResult.BLOCKED,
            reason=LoginReason.BLOCKED_INACTIVE,
            ip="203.0.113.60",
        ),
        change(ts=now - timedelta(days=8)),
        *(change(ts=now - timedelta(hours=6 - i), target_id=str(i)) for i in range(6)),
        *(
            attempt(
                ts=now - timedelta(minutes=20 - i),
                result=LoginResult.SUCCESS,
                reason=LoginReason.OK,
                user_exists=UserExists.YES,
                user_id=f"user{i}",
            )
            for i in range(6)
        ),
    )

    res = await result(ws, "electric_fence/overview")

    assert set(res) == {
        "failed_24h",
        "changes_7d",
        "recent_success",
        "posture",
        "protection",
    }
    failed = res["failed_24h"]
    # 6 single rows, one merged row of 3, one blocked; the 2-day-old row is out.
    assert failed["count"] == 10
    assert len(failed["items"]) == 5
    assert failed["items"][0]["result"] == "blocked"
    assert failed["items"][0]["reason"] == "blocked_inactive_user"
    assert {"203.0.113.99"}.isdisjoint(i["ip"] for i in failed["items"])
    for item in failed["items"]:
        assert set(item) == LOGIN_KEYS
        assert isinstance(item["id"], int)
        assert item["ts"].endswith("+00:00")
    ts = [i["ts"] for i in failed["items"]]
    assert ts == sorted(ts, reverse=True)

    changes = res["changes_7d"]
    assert changes["count"] == 6
    assert [i["target_id"] for i in changes["items"]] == ["5", "4", "3", "2", "1"]
    for item in changes["items"]:
        assert set(item) == CHANGE_KEYS
        assert item["kind"] == "config_entry"
        assert item["details"] == {"domain": "tuya"}

    recent = res["recent_success"]
    assert set(recent) == {"items"}
    assert [i["user_id"] for i in recent["items"]] == [
        "user5",
        "user4",
        "user3",
        "user2",
        "user1",
    ]

    posture = res["posture"]
    assert {p["id"] for p in posture} >= {
        "admins_without_mfa",
        "auto_ban_off",
        "proxy_suspected",
        "geoip_missing",
        "collector_disabled",
        "storage_error",
    }
    for item in posture:
        assert item["level"] in ("ok", "warning", "error")
        assert isinstance(item["params"], dict)
    json.dumps(res)


async def test_overview_hides_system_changes(
    hass: HomeAssistant, setup_entry: MockConfigEntry, ws: MockHAClientWebSocket
) -> None:
    """Changes nobody made (System) are not counted or shown."""
    await store(
        hass,
        setup_entry,
        change(target_id="user-made"),
        change(target_id="system-made", actor_user_id=None, actor_name=None),
    )

    res = await result(ws, "electric_fence/overview")

    assert res["changes_7d"]["count"] == 1
    assert [i["target_id"] for i in res["changes_7d"]["items"]] == ["user-made"]


async def test_overview_counts_changes_past_one_page(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    ws: MockHAClientWebSocket,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The change count pages through the store, so it is never capped."""
    monkeypatch.setattr(fence_ws, "_COUNT_PAGE", 2)
    await store(hass, setup_entry, *(change(target_id=str(i)) for i in range(5)))

    res = await result(ws, "electric_fence/overview")

    assert res["changes_7d"]["count"] == 5


async def test_logins_filters_and_paging(
    hass: HomeAssistant, setup_entry: MockConfigEntry, ws: MockHAClientWebSocket
) -> None:
    """Login filters map to the store, and paging walks newest first."""
    now = dt_util.utcnow()
    await store(
        hass,
        setup_entry,
        attempt(
            ts=now - timedelta(hours=3), username_typed="mallory", ip="198.51.100.1"
        ),
        attempt(ts=now - timedelta(hours=2), username_typed="bob", country_code="DE"),
        attempt(
            ts=now - timedelta(hours=1),
            result=LoginResult.SUCCESS,
            reason=LoginReason.OK,
            username_typed="alice",
            user_id="alice-id",
        ),
        attempt(ts=now - timedelta(minutes=30), username_typed="<b>evil</b>"),
    )

    async def logins(**kw: Any) -> list[str]:
        res = await result(ws, "electric_fence/logins", **kw)
        return [i["username_typed"] for i in res["items"]]

    assert await logins() == ["<b>evil</b>", "alice", "bob", "mallory"]
    assert await logins(result="success") == ["alice"]
    assert await logins(user_id="alice-id") == ["alice"]
    assert await logins(username="ALL") == ["mallory"]
    assert await logins(ip="198.51.100.1") == ["mallory"]
    assert await logins(country="DE") == ["bob"]
    assert await logins(
        since=(now - timedelta(hours=2, minutes=30)).isoformat(),
        until=(now - timedelta(minutes=45)).isoformat(),
    ) == ["alice", "bob"]

    page = await result(ws, "electric_fence/logins", limit=3)
    assert [i["username_typed"] for i in page["items"]] == [
        "<b>evil</b>",
        "alice",
        "bob",
    ]
    assert page["next_before_id"] == page["items"][-1]["id"]
    page = await result(
        ws, "electric_fence/logins", limit=3, before_id=page["next_before_id"]
    )
    assert [i["username_typed"] for i in page["items"]] == ["mallory"]
    assert page["next_before_id"] is None

    assert await logins(result="failed_or_blocked") == ["<b>evil</b>", "bob", "mallory"]

    for bad in ({"limit": 201}, {"limit": 0}, {"result": "maybe"}, {"since": "x"}):
        msg = await call(ws, "electric_fence/logins", **bad)
        assert not msg["success"], bad
        assert msg["error"]["code"] == "invalid_format", bad


async def test_logins_user_filter_finds_failures(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    ws: MockHAClientWebSocket,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """Filtering by user returns that user's failed logins too."""
    alice = await create_hass_user(hass, "alice", "right")
    await login_flow(await hass_client_no_auth(), "alice", "wrong")
    await login_flow(await hass_client_no_auth(), "mallory", "wrong")
    await flush_worker(hass)

    res = await result(ws, "electric_fence/logins", user_id=alice.id)

    assert [(i["username_typed"], i["reason"]) for i in res["items"]] == [
        ("alice", "wrong_password")
    ]


async def test_logins_naive_time_is_utc(
    hass: HomeAssistant, setup_entry: MockConfigEntry, ws: MockHAClientWebSocket
) -> None:
    """A time filter without an offset is read as UTC."""
    now = dt_util.utcnow()
    await store(hass, setup_entry, attempt(ts=now - timedelta(hours=1)))
    naive = (now - timedelta(hours=2)).replace(tzinfo=None).isoformat()

    res = await result(ws, "electric_fence/logins", since=naive)

    assert len(res["items"]) == 1


async def test_changes_include_system_toggle(
    hass: HomeAssistant, setup_entry: MockConfigEntry, ws: MockHAClientWebSocket
) -> None:
    """System changes appear only with `include_system`; filters apply."""
    now = dt_util.utcnow()
    await store(
        hass,
        setup_entry,
        change(ts=now - timedelta(hours=3), target_id="a"),
        change(
            ts=now - timedelta(hours=2),
            target_id="b",
            kind=ChangeKind.USER,
            actor_user_id="u2",
        ),
        change(ts=now - timedelta(hours=1), target_id="c", actor_user_id=None),
    )

    async def changes(**kw: Any) -> list[str]:
        res = await result(ws, "electric_fence/changes", **kw)
        return [i["target_id"] for i in res["items"]]

    assert await changes() == ["b", "a"]
    assert await changes(include_system=True) == ["c", "b", "a"]
    assert await changes(kind="user") == ["b"]
    assert await changes(actor_user_id="u1") == ["a"]
    assert await changes(
        include_system=True, since=(now - timedelta(minutes=150)).isoformat()
    ) == ["c", "b"]

    page = await result(ws, "electric_fence/changes", include_system=True, limit=2)
    assert [i["target_id"] for i in page["items"]] == ["c", "b"]
    page = await result(
        ws,
        "electric_fence/changes",
        include_system=True,
        limit=2,
        before_id=page["next_before_id"],
    )
    assert [i["target_id"] for i in page["items"]] == ["a"]
    assert page["next_before_id"] is None

    msg = await call(ws, "electric_fence/changes", kind="nope")
    assert msg["error"]["code"] == "invalid_format"


async def test_users_lists_sessions_without_tokens(
    hass: HomeAssistant, setup_entry: MockConfigEntry, ws: MockHAClientWebSocket
) -> None:
    """Users come with username, MFA, last login and sessions, never tokens."""
    alice = await create_hass_user(hass, "alice", "secret-pw")
    token = await hass.auth.async_create_refresh_token(
        alice, client_id="http://phone/", client_name=None
    )
    token.last_used_at = dt_util.utcnow()
    token.last_used_ip = "192.168.1.9"
    lla = await hass.auth.async_create_refresh_token(
        alice,
        client_name="Script",
        token_type="long_lived_access_token",
        access_token_expiration=timedelta(days=10),
    )
    now = dt_util.utcnow()
    await store(
        hass,
        setup_entry,
        attempt(
            ts=now - timedelta(hours=1),
            result=LoginResult.SUCCESS,
            reason=LoginReason.OK,
            user_exists=UserExists.YES,
            username_typed="alice",
            user_id=alice.id,
        ),
    )

    res = await result(ws, "electric_fence/users")

    items = {u["id"]: u for u in res["items"]}
    item = items[alice.id]
    assert item["name"] == "alice"
    assert item["username"] == "alice"
    assert item["is_admin"] is True
    assert item["is_owner"] is False
    assert item["is_active"] is True
    assert item["local_only"] is False
    assert item["mfa_modules"] == []
    assert item["last_login"]["user_id"] == alice.id
    assert item["last_login"]["user_name"] == "alice"
    assert set(item["last_login"]) == LOGIN_KEYS
    sessions = {s["id"]: s for s in item["sessions"]}
    assert set(sessions) == {token.id, lla.id}
    session = sessions[token.id]
    assert set(session) == {
        "id",
        "client_name",
        "client_id",
        "token_type",
        "created_at",
        "last_used_at",
        "last_used_ip",
    }
    assert session["client_id"] == "http://phone/"
    assert session["token_type"] == "normal"
    assert session["last_used_ip"] == "192.168.1.9"
    assert session["created_at"].endswith("+00:00")
    assert session["last_used_at"].endswith("+00:00")
    assert sessions[lla.id]["token_type"] == "long_lived_access_token"
    assert sessions[lla.id]["last_used_at"] is None

    dumped = json.dumps(res)
    for user in await hass.auth.async_get_users():
        for rt in user.refresh_tokens.values():
            assert rt.token not in dumped
            assert rt.jwt_key not in dumped
    assert "secret-pw" not in dumped
    for user_item in res["items"]:
        for s in user_item["sessions"]:
            assert "token" not in s
            assert "jwt_key" not in s


async def test_users_reports_mfa_modules(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    ws: MockHAClientWebSocket,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enabled MFA module IDs are listed per user."""
    alice = await create_hass_user(hass, "alice", "pw")

    async def enabled(user: Any) -> dict[str, str]:
        return {"totp": "Authenticator app"} if user.id == alice.id else {}

    monkeypatch.setattr(hass.auth, "async_get_enabled_mfa", enabled)

    res = await result(ws, "electric_fence/users")

    item = next(u for u in res["items"] if u["id"] == alice.id)
    assert item["mfa_modules"] == ["totp"]
    assert item["last_login"] is None


async def test_status_redacts_license_key(
    hass: HomeAssistant, setup_entry: MockConfigEntry, ws: MockHAClientWebSocket
) -> None:
    """Status shows settings without the licence key, geo, collectors, worker."""
    runtime = setup_entry.runtime_data
    runtime.settings = replace(
        runtime.settings,
        geoip_provider="maxmind",
        maxmind_account_id="123456",
        maxmind_license_key="SUPER-SECRET-KEY",
    )
    runtime.worker.dropped = 2

    res = await result(ws, "electric_fence/status")

    assert "SUPER-SECRET-KEY" not in json.dumps(res)
    assert "maxmind_license_key" not in res["settings"]
    assert res["settings"]["maxmind_license_key_set"] is True
    assert res["settings"]["maxmind_account_id"] == "123456"
    assert res["settings"]["retention_days"] == 90
    assert res["version"] == "0.1.1"
    assert set(res["geo"]) == {"provider", "db_date", "last_error", "attribution"}
    names = [c["name"] for c in res["collectors"]]
    assert names == [c.name for c in runtime.collectors]
    for item in res["collectors"]:
        assert item["enabled"] is True
        assert item["reason"] == []
    assert res["worker"] == {"queued": 0, "dropped": 2, "storage_errors": 0}


async def test_subscribe_pushes_new_records(
    hass: HomeAssistant, setup_entry: MockConfigEntry, ws: MockHAClientWebSocket
) -> None:
    """Each stored record is pushed as a login or change event."""
    sub_id = await subscribe(ws)
    worker = setup_entry.runtime_data.worker

    worker.submit(attempt(username_typed="<script>x</script>"))
    worker.submit(change())
    await flush_worker(hass)

    msg = await ws.receive_json()
    assert msg["id"] == sub_id
    assert msg["type"] == "event"
    assert msg["event"]["type"] == "login"
    item = msg["event"]["item"]
    assert set(item) == LOGIN_KEYS
    assert item["id"] is None
    assert item["username_typed"] == "<script>x</script>"
    assert item["result"] == "failed"
    msg = await ws.receive_json()
    assert msg["event"]["type"] == "change"
    assert set(msg["event"]["item"]) == CHANGE_KEYS
    assert msg["event"]["item"]["action"] == "added"


async def test_subscribe_survives_bad_record(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    ws: MockHAClientWebSocket,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A record that can't be turned into JSON is skipped; later ones arrive."""
    await result(ws, "electric_fence/subscribe")
    worker = setup_entry.runtime_data.worker
    original = fence_ws.record_event
    calls = 0

    def flaky(rec: Record, *args: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("bad")
        return original(rec, *args)

    monkeypatch.setattr(fence_ws, "record_event", flaky)
    worker.submit(attempt(ip="203.0.113.1"))
    worker.submit(attempt(ip="203.0.113.2"))
    await flush_worker(hass)

    msg = await ws.receive_json()
    assert msg["event"]["item"]["ip"] == "203.0.113.2"


async def test_subscribe_unsubscribe_removes_listener(
    hass: HomeAssistant, setup_entry: MockConfigEntry, ws: MockHAClientWebSocket
) -> None:
    """Unsubscribing, closing the socket and unloading all remove the listener."""
    worker = setup_entry.runtime_data.worker
    before = len(worker._listeners)

    sub_id = await subscribe(ws)
    assert len(worker._listeners) == before + 1
    assert await result(ws, "unsubscribe_events", subscription=sub_id) is None
    assert len(worker._listeners) == before
    assert not hass.data[fence_ws.DATA_SUBSCRIPTIONS][setup_entry.entry_id]

    # Unloading the entry ends the subscription and tells the client.
    sub_id = await subscribe(ws)
    assert len(worker._listeners) == before + 1
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    msg = await ws.receive_json()
    assert msg == {"id": sub_id, "type": "event", "event": {"type": "unloaded"}}
    assert len(worker._listeners) == 0
    assert setup_entry.entry_id not in hass.data[fence_ws.DATA_SUBSCRIPTIONS]
    msg = await call(ws, "unsubscribe_events", subscription=sub_id)
    assert msg["error"]["code"] == "not_found"


async def test_subscribe_removed_when_socket_closes(
    hass: HomeAssistant, setup_entry: MockConfigEntry, ws: MockHAClientWebSocket
) -> None:
    """Closing the WebSocket removes the listener."""
    worker = setup_entry.runtime_data.worker
    before = len(worker._listeners)
    await result(ws, "electric_fence/subscribe")
    assert len(worker._listeners) == before + 1

    await ws.close()
    await hass.async_block_till_done()

    assert len(worker._listeners) == before
    assert not hass.data[fence_ws.DATA_SUBSCRIPTIONS][setup_entry.entry_id]


async def test_status_geo_date_and_missing_status(
    hass: HomeAssistant, setup_entry: MockConfigEntry, ws: MockHAClientWebSocket
) -> None:
    """The database date is `YYYY-MM-DD`; no GeoIP status gives null."""
    runtime = setup_entry.runtime_data
    assert runtime.geo_status is not None
    runtime.geo_status.db_date = date(2026, 10, 1)

    res = await result(ws, "electric_fence/status")
    assert res["geo"]["db_date"] == "2026-10-01"
    assert res["settings"]["maxmind_license_key_set"] is False

    runtime.geo_status = None
    res = await result(ws, "electric_fence/status")
    assert res["geo"] is None


async def test_login_items_name_the_user(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    ws: MockHAClientWebSocket,
) -> None:
    """A login item names its user from the auth store, not the typed text."""
    alice = await create_hass_user(hass, "alice", "pw")
    await hass.auth.async_update_user(alice, name="Alice Liddell")
    trusted = attempt(
        result=LoginResult.SUCCESS,
        reason=LoginReason.OK,
        username_typed=None,
        user_exists=UserExists.YES,
        user_id=alice.id,
        provider="trusted_networks",
    )
    await store(hass, setup_entry, trusted, attempt(user_id="gone"))

    overview = await result(ws, "electric_fence/overview")
    page = await result(ws, "electric_fence/logins")

    (success,) = overview["recent_success"]["items"]
    assert (success["username_typed"], success["user_name"]) == (
        None,
        "Alice Liddell",
    )
    assert [i["user_name"] for i in page["items"]] == [None, "Alice Liddell"]

    await flush_worker(hass)  # the rename's own change record
    sub_id = await subscribe(ws)
    setup_entry.runtime_data.worker.submit(replace(trusted, confirmed=True))
    await flush_worker(hass)
    msg = await ws.receive_json()
    assert msg["id"] == sub_id
    assert msg["event"]["item"]["user_name"] == "Alice Liddell"


async def test_subscribe_ends_for_demoted_admin(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    ws: MockHAClientWebSocket,
    hass_admin_user: MockUser,
) -> None:
    """A user who is no longer an active admin gets no more pushes."""
    worker = setup_entry.runtime_data.worker
    before = len(worker._listeners)
    sub_id = await subscribe(ws)
    assert len(worker._listeners) == before + 1

    hass_admin_user.groups = []
    worker.submit(attempt())
    await flush_worker(hass)

    assert len(worker._listeners) == before
    assert not hass.data[fence_ws.DATA_SUBSCRIPTIONS][setup_entry.entry_id]
    # The next message is the answer to this ping, not a pushed record.
    await ws.send_json_auto_id({"type": "ping"})
    assert (await ws.receive_json())["type"] == "pong"
    msg = await call(ws, "unsubscribe_events", subscription=sub_id)
    assert msg["error"]["code"] == "not_found"


async def test_subscribe_ends_for_deactivated_admin(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    ws: MockHAClientWebSocket,
    hass_admin_user: MockUser,
) -> None:
    """A deactivated admin gets no more pushes either."""
    worker = setup_entry.runtime_data.worker
    before = len(worker._listeners)
    await subscribe(ws)
    hass_admin_user.is_active = False
    worker.submit(attempt())
    await flush_worker(hass)
    assert len(worker._listeners) == before


BAN_KEYS = {
    "ip",
    "source",
    "banned_at",
    "expires_at",
    "failures",
    "ban_number",
    "country_code",
    "hostname",
    "ended_at",
    "end_reason",
    "ended_by_name",
}
ATTACK_KEYS = {
    "username",
    "started_at",
    "ended_at",
    "failures",
    "ip_count",
    "countries",
    "end_reason",
    "active",
}


async def test_bans_lists_both_sources(
    hass: HomeAssistant, setup_entry: MockConfigEntry, ws: MockHAClientWebSocket
) -> None:
    """Active bans hold Electric Fence's and others; history pages the store."""
    runtime = setup_entry.runtime_data
    bans = runtime.bans
    assert bans is not None
    assert await bans.async_ban("203.0.113.9", 5, "RU", "bad.example")
    await bans.backend.async_add(ip_address("203.0.113.50"))
    assert await bans.async_ban("203.0.113.10", 5, None, None)
    await bans.async_unban("203.0.113.10", "u1", "Alice")
    await flush_worker(hass)

    res = await result(ws, "electric_fence/bans", state="active")
    assert res["available"] is True
    assert res["reason"] is None
    assert res["next_before_id"] is None
    by_ip = {i["ip"]: i for i in res["items"]}
    assert set(by_ip) == {"203.0.113.9", "203.0.113.50"}
    ours, other = by_ip["203.0.113.9"], by_ip["203.0.113.50"]
    assert set(ours) == BAN_KEYS
    assert ours["source"] == "electric_fence"
    assert ours["country_code"] == "RU"
    assert ours["failures"] == 5
    assert ours["ended_at"] is None
    assert other["source"] == "other"
    assert other["failures"] is None
    assert other["expires_at"] is None

    res = await result(ws, "electric_fence/bans", state="history", limit=1)
    assert [i["ip"] for i in res["items"]] == ["203.0.113.10"]
    item = res["items"][0]
    assert set(item) == BAN_KEYS
    assert item["end_reason"] == "unbanned"
    assert item["ended_by_name"] == "Alice"
    assert item["ended_at"] is not None
    assert res["next_before_id"] is not None
    res = await result(
        ws,
        "electric_fence/bans",
        state="history",
        limit=1,
        before_id=res["next_before_id"],
    )
    assert res["items"] == []


async def test_bans_unavailable(
    setup_entry: MockConfigEntry, ws: MockHAClientWebSocket
) -> None:
    """Without a ban manager the list is empty with a reason."""
    setup_entry.runtime_data.bans = None
    res = await result(ws, "electric_fence/bans", state="active")
    assert res == {
        "available": False,
        "reason": "not_loaded",
        "items": [],
        "next_before_id": None,
    }
    res = await result(ws, "electric_fence/bans", state="history")
    assert res["available"] is False
    assert res["items"] == []


async def test_bans_backend_unavailable_reason(
    hass: HomeAssistant, setup_entry: MockConfigEntry, ws: MockHAClientWebSocket
) -> None:
    """A manager that can't ban says why, and still lists what it has."""
    bans = setup_entry.runtime_data.bans
    assert bans is not None
    bans.backend.health = lambda: "ip_ban_disabled"  # type: ignore[method-assign]
    res = await result(ws, "electric_fence/bans", state="active")
    assert res["available"] is False
    assert res["reason"] == "ip_ban_disabled"


async def test_unban_requires_admin(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
) -> None:
    """A user who is not an admin cannot unban."""
    hass_admin_user.groups = []
    client = await hass_ws_client(hass)
    msg = await call(client, "electric_fence/unban", ip="203.0.113.9")
    assert not msg["success"]
    assert msg["error"]["code"] == "unauthorized"


async def test_unban_audited_with_actor(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    ws: MockHAClientWebSocket,
    hass_admin_user: MockUser,
) -> None:
    """An unban lifts the ban and records the admin as the actor."""
    bans = setup_entry.runtime_data.bans
    assert bans is not None
    assert await bans.async_ban("203.0.113.9", 5, None, None)

    res = await result(ws, "electric_fence/unban", ip="203.0.113.9")
    assert res == {"unbanned": True}
    assert await result(ws, "electric_fence/unban", ip="203.0.113.9") == {
        "unbanned": False
    }
    await flush_worker(hass)

    rows = await hass.async_add_executor_job(
        lambda: setup_entry.runtime_data.store.query_changes(
            ChangeFilter(kind=ChangeKind.IP_BAN)
        )
    )
    unbanned = [r for r in rows if r.action == ChangeAction.UNBANNED]
    assert len(unbanned) == 1
    assert unbanned[0].actor_name == hass_admin_user.name
    assert unbanned[0].actor_user_id == hass_admin_user.id


async def test_unban_not_loaded_managers(
    setup_entry: MockConfigEntry, ws: MockHAClientWebSocket
) -> None:
    """Mutations answer not_loaded when the managers are missing."""
    runtime = setup_entry.runtime_data
    runtime.bans = None
    runtime.username_guard = None
    for cmd in ("unban", "end_slowdown", "attacks"):
        args = {"ip": "203.0.113.9"} if cmd == "unban" else {"username": "x"}
        if cmd == "attacks":
            msg = await call(ws, f"electric_fence/{cmd}", state="active")
            assert msg["success"]
            assert msg["result"] == {"items": []}
            continue
        msg = await call(ws, f"electric_fence/{cmd}", **args)
        assert not msg["success"]
        assert msg["error"]["code"] == "not_loaded"


async def test_attacks_active_and_history(
    hass: HomeAssistant, setup_entry: MockConfigEntry, ws: MockHAClientWebSocket
) -> None:
    """Active attacks come from memory; history from the store."""
    runtime = setup_entry.runtime_data
    guard = runtime.username_guard
    assert guard is not None
    started = dt_util.utcnow()
    guard.active_attacks = lambda: [  # type: ignore[method-assign]
        AttackView("admin", started, 12, 4, ["RU", "CN"], 3)
    ]
    res = await result(ws, "electric_fence/attacks", state="active")
    assert len(res["items"]) == 1
    item = res["items"][0]
    assert set(item) == ATTACK_KEYS
    assert item["active"] is True
    assert item["ended_at"] is None
    assert item["end_reason"] is None
    assert item["countries"] == ["RU", "CN"]
    assert item["ip_count"] == 4

    store_ = runtime.store

    def seed() -> None:
        attack_id = store_.add_attack("old", started - timedelta(hours=2))
        store_.end_attack(attack_id, started - timedelta(hours=1), "quiet", None, None)

    await hass.async_add_executor_job(seed)
    res = await result(ws, "electric_fence/attacks", state="history")
    assert [i["username"] for i in res["items"]] == ["old"]
    assert res["items"][0]["active"] is False
    assert res["items"][0]["end_reason"] == "quiet"
    assert set(res["items"][0]) == ATTACK_KEYS


async def test_end_slowdown(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    ws: MockHAClientWebSocket,
    hass_admin_user: MockUser,
) -> None:
    """The username is mapped to its key and the admin is the actor."""
    guard = setup_entry.runtime_data.username_guard
    assert guard is not None
    end = AsyncMock(return_value=True)
    guard.async_end = end  # type: ignore[method-assign]

    res = await result(ws, "electric_fence/end_slowdown", username="  Admin ")
    assert res == {"ended": True}
    end.assert_awaited_once_with("admin", hass_admin_user.id, hass_admin_user.name)

    end.return_value = False
    res = await result(ws, "electric_fence/end_slowdown", username="ghost")
    assert res == {"ended": False}


async def test_clear_countries(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    ws: MockHAClientWebSocket,
    hass_admin_user: MockUser,
) -> None:
    """Known countries are forgotten and the admin's action is audited."""
    store_ = setup_entry.runtime_data.store
    await hass.async_add_executor_job(store_.note_country, "u1", "DE")
    await hass.async_add_executor_job(store_.note_country, "u2", "FR")

    res = await result(ws, "electric_fence/clear_countries")
    assert res == {"cleared": 2}
    assert await hass.async_add_executor_job(store_.known_countries, "u1") == []
    await flush_worker(hass)

    rows = await hass.async_add_executor_job(
        lambda: store_.query_changes(ChangeFilter(kind=ChangeKind.LOGIN_PROTECTION))
    )
    assert [r.action for r in rows] == [ChangeAction.COUNTRIES_CLEARED]
    assert rows[0].actor_name == hass_admin_user.name
    assert rows[0].details == {"cleared": 2}


async def test_overview_protection(
    hass: HomeAssistant, setup_entry: MockConfigEntry, ws: MockHAClientWebSocket
) -> None:
    """The overview carries protection counts and the active attacks."""
    runtime = setup_entry.runtime_data
    assert runtime.bans is not None
    assert await runtime.bans.async_ban("203.0.113.9", 5, None, None)
    assert runtime.username_guard is not None
    started = dt_util.utcnow()
    runtime.username_guard.active_attacks = lambda: [  # type: ignore[method-assign]
        AttackView("admin", started, 12, 4, ["RU"], None)
    ]

    res = await result(ws, "electric_fence/overview")
    prot = res["protection"]
    assert prot["available"] is True
    assert prot["active_bans"] == 1
    assert prot["bans_24h"] == 1
    assert [a["username"] for a in prot["attacks"]] == ["admin"]
    assert set(prot["attacks"][0]) == ATTACK_KEYS
    assert {"failed_24h", "changes_7d", "recent_success", "posture"} <= set(res)

    runtime.bans = None
    runtime.username_guard = None
    res = await result(ws, "electric_fence/overview")
    assert res["protection"] == {
        "available": False,
        "active_bans": 0,
        "bans_24h": 0,
        "attacks": [],
    }


async def test_status_notify_targets_is_list(
    setup_entry: MockConfigEntry, ws: MockHAClientWebSocket
) -> None:
    """Status settings include the protection fields; targets are a list."""
    runtime = setup_entry.runtime_data
    runtime.settings = replace(runtime.settings, notify_targets=("notify.phone",))
    res = await result(ws, "electric_fence/status")
    assert res["settings"]["notify_targets"] == ["notify.phone"]
    assert res["settings"]["auto_ban"] is True
    assert res["settings"]["ban_threshold"] == 5
