"""Tests for the auth failure and notification collector (H2+H3)."""

# ruff: noqa: SLF001

from collections.abc import Iterator
from contextlib import contextmanager
import sqlite3
from typing import Any
from unittest.mock import Mock, patch

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.electric_fence import COLLECTOR_TYPES
from custom_components.electric_fence.collectors.auth_failures import (
    AuthFailureCollector,
)
from custom_components.electric_fence.const import DOMAIN, MIN_HA_VERSION
from custom_components.electric_fence.contracts import (
    CONTRACTS,
    Contract,
    check_contracts,
)
from custom_components.electric_fence.notifier import (
    EVENT_LOGIN_FAILED,
    NOTIFICATION_ID,
)
from custom_components.electric_fence.settings import Settings
from homeassistant.components import persistent_notification
from homeassistant.components.http import ban
from homeassistant.components.persistent_notification import UpdateType
from homeassistant.core import HomeAssistant
from homeassistant.helpers.http import current_request
from homeassistant.setup import async_setup_component
from tests.helpers import (
    CLIENT_ID,
    REDIRECT_URI,
    create_hass_user,
    flush_worker,
    login_flow,
    store_rows,
)

type Client = TestClient[web.Request, web.Application]

UA = "fence-test-agent/1.0"


async def _setup(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    http_config: dict[str, Any] | None = None,
    **settings: Any,
) -> MockConfigEntry:
    """Set up `http` (optionally configured), `api` and an entry."""
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", MIN_HA_VERSION)
    assert await async_setup_component(hass, "http", {"http": http_config or {}})
    assert await async_setup_component(hass, "api", {})
    entry = MockConfigEntry(domain=DOMAIN, options=Settings(**settings).to_options())
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _collector(hass: HomeAssistant) -> AuthFailureCollector:
    entry = hass.config_entries.async_loaded_entries(DOMAIN)[0]
    return next(
        c for c in entry.runtime_data.collectors if isinstance(c, AuthFailureCollector)
    )


def _notifications(hass: HomeAssistant) -> dict[str, Any]:
    return persistent_notification._async_get_or_create_notifications(hass)


async def _bad_bearer(client: Client) -> None:
    resp = await client.get(
        "/api/",
        headers={"Authorization": "Bearer not-a-valid-token", "User-Agent": UA},
    )
    assert resp.status == 401


async def _bad_websocket_token(client: Client) -> None:
    async with client.ws_connect(
        "/api/websocket", headers={"User-Agent": UA}
    ) as websocket:
        assert (await websocket.receive_json())["type"] == "auth_required"
        await websocket.send_json({"type": "auth", "access_token": "not-valid"})
        assert (await websocket.receive_json())["type"] == "auth_invalid"
        assert (await websocket.receive()).type is WSMsgType.CLOSE


@contextmanager
def _request(path: str = "/api/", remote: str | None = "203.0.113.7") -> Iterator[Any]:
    """Set `current_request` to a fake request."""
    request = Mock(path=path, remote=remote, headers={"User-Agent": UA})
    token = current_request.set(request)
    try:
        yield request
    finally:
        current_request.reset(token)


def _login_update(update_type: UpdateType = UpdateType.ADDED) -> dict[str, Any]:
    return {"update_type": update_type, "notifications": {"http-login": {}}}


def test_collector_registered() -> None:
    """The collector is set up with the integration."""
    assert AuthFailureCollector in COLLECTOR_TYPES
    assert AuthFailureCollector.name == "auth_failures"


async def test_invalid_bearer_token_recorded(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad REST bearer token is recorded as an invalid token."""
    await _setup(hass, monkeypatch)
    await _bad_bearer(await hass_client_no_auth())
    await flush_worker(hass)
    rows = store_rows(hass)
    assert len(rows) == 1
    row = rows[0]
    assert (row.result, row.reason, row.username_typed, row.user_exists) == (
        "failed",
        "invalid_token",
        None,
        "unknown",
    )
    assert row.url == "/api/"
    assert row.ip == "127.0.0.1"
    assert row.source == "local"
    assert row.user_agent == UA


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_invalid_websocket_token_recorded(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A WebSocket `auth_invalid` is recorded as an invalid token."""
    # aiohttp re-arms the server heartbeat after close; see the contract test.
    await _setup(hass, monkeypatch)
    await _bad_websocket_token(await hass_client_no_auth())
    await flush_worker(hass)
    rows = store_rows(hass)
    assert len(rows) == 1
    assert (rows[0].reason, rows[0].url, rows[0].ip, rows[0].user_agent) == (
        "invalid_token",
        "/api/websocket",
        "127.0.0.1",
        UA,
    )


async def test_login_flow_failure_not_double_counted(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wrong password is recorded once, by the login collector."""
    await _setup(hass, monkeypatch)
    await create_hass_user(hass, "alice", "right")
    result = await login_flow(await hass_client_no_auth(), "alice", "wrong")
    assert result["errors"] == {"base": "invalid_auth"}
    await flush_worker(hass)
    rows = store_rows(hass)
    assert len(rows) == 1
    assert rows[0].reason == "wrong_password"


async def test_replace_mode_dismisses_http_login(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In replace mode, Core's `http-login` is dismissed after it's created."""
    await _setup(hass, monkeypatch, ha_login_notification="replace")
    await _bad_bearer(await hass_client_no_auth())
    await hass.async_block_till_done()
    assert "http-login" not in _notifications(hass)


async def test_replace_mode_dismisses_login_flow_http_login(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A login-flow failure isn't recorded here, but `http-login` is dismissed."""
    await _setup(hass, monkeypatch)
    await create_hass_user(hass, "alice", "right")
    await login_flow(await hass_client_no_auth(), "alice", "wrong")
    await hass.async_block_till_done()
    assert "http-login" not in _notifications(hass)


async def test_keep_mode_leaves_http_login(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In keep mode, `http-login` stays."""
    await _setup(hass, monkeypatch, ha_login_notification="keep")
    await _bad_bearer(await hass_client_no_auth())
    await hass.async_block_till_done()
    assert "http-login" in _notifications(hass)


async def test_replace_mode_inactive_when_notify_off(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without Electric Fence's own notification, `http-login` stays."""
    await _setup(
        hass, monkeypatch, ha_login_notification="replace", notify_failed_login=False
    )
    await _bad_bearer(await hass_client_no_auth())
    await hass.async_block_till_done()
    assert "http-login" in _notifications(hass)


async def test_settings_read_at_event_time(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A settings change applies to the next failure without a reload."""
    entry = await _setup(hass, monkeypatch, ha_login_notification="keep")
    client = await hass_client_no_auth()
    await _bad_bearer(client)
    await hass.async_block_till_done()
    assert "http-login" in _notifications(hass)
    entry.runtime_data.settings = Settings(ha_login_notification="replace")
    await _bad_bearer(client)
    await hass.async_block_till_done()
    assert "http-login" not in _notifications(hass)


async def test_ip_ban_notification_never_dismissed(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ip-ban` stays, even in replace mode."""
    await _setup(hass, monkeypatch, http_config={"login_attempts_threshold": 1})
    await _bad_bearer(await hass_client_no_auth())
    await hass.async_block_till_done()
    notifications = _notifications(hass)
    assert "ip-ban" in notifications
    assert "http-login" not in notifications


async def test_ignores_other_updates(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Other notifications, removals and calls without a request do nothing."""
    await _setup(hass, monkeypatch)
    collector = _collector(hass)
    with (
        patch.object(collector.runtime.worker, "submit") as submit,
        patch.object(persistent_notification, "async_dismiss") as dismiss,
    ):
        with _request():
            collector._on_update(UpdateType.ADDED, {"ip-ban": Mock()})
            collector._on_update(UpdateType.REMOVED, {"http-login": Mock()})
            collector._on_update(UpdateType.CURRENT, {"http-login": Mock()})
        collector._on_update(**_login_update())
        await hass.async_block_till_done()
    submit.assert_not_called()
    dismiss.assert_not_called()
    assert collector.guard.errors == 0


@pytest.mark.parametrize(
    ("remote", "cloud", "ip", "source"),
    [
        ("203.0.113.7", False, "203.0.113.7", "remote"),
        ("192.168.1.30", True, "192.168.1.30", "cloud"),
        ("not-an-ip", False, "not-an-ip", None),
        (None, False, None, None),
    ],
)
async def test_source_from_request(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    remote: str | None,
    cloud: bool,
    ip: str | None,
    source: str | None,
) -> None:
    """The source is cloud, local or remote; an unusable IP has none."""
    await _setup(hass, monkeypatch)
    collector = _collector(hass)
    with (
        patch(
            "custom_components.electric_fence.collectors.auth_failures."
            "is_cloud_connection",
            return_value=cloud,
        ),
        patch.object(collector.runtime.worker, "submit") as submit,
        _request(remote=remote),
    ):
        collector._on_update(**_login_update(UpdateType.UPDATED))
    (attempt,), _ = submit.call_args
    assert (attempt.ip, attempt.source) == (ip, source)
    assert collector.guard.errors == 0


async def test_listener_fails_open(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing submit or dismiss is counted, never raised."""
    await _setup(hass, monkeypatch)
    collector = _collector(hass)
    with (
        patch.object(collector.runtime.worker, "submit", side_effect=RuntimeError),
        patch.object(persistent_notification, "async_dismiss") as dismiss,
        _request(),
    ):
        collector._on_update(**_login_update())
        await hass.async_block_till_done()
    # Nothing recorded, so nothing of ours replaces `http-login`.
    dismiss.assert_not_called()
    assert collector.guard.errors == 1
    with (
        patch.object(
            persistent_notification, "async_dismiss", side_effect=RuntimeError
        ) as dismiss,
        _request(),
    ):
        collector._on_update(**_login_update())
        await hass.async_block_till_done()
    dismiss.assert_called_once_with(hass, "http-login")
    assert collector.guard.errors == 2


def _break_login_collector(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the login collector's contract fail, so it is not installed."""
    monkeypatch.setitem(
        CONTRACTS,
        "login_flow_manager",
        Contract("login_flow_manager", "login", lambda hass: "broken"),
    )


async def test_replace_mode_keeps_http_login_without_login_collector(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the login collector off, a login-flow `http-login` stays."""
    _break_login_collector(monkeypatch)
    await _setup(hass, monkeypatch)
    await create_hass_user(hass, "alice", "right")
    await login_flow(await hass_client_no_auth(), "alice", "wrong")
    await hass.async_block_till_done()
    assert "http-login" in _notifications(hass)


async def test_replace_mode_keeps_http_login_while_storage_fails(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While storage fails, `http-login` stays; our event and notification fire."""
    entry = await _setup(hass, monkeypatch)
    runtime = entry.runtime_data
    monkeypatch.setattr(
        runtime.store, "insert", Mock(side_effect=sqlite3.OperationalError("full"))
    )
    events = async_capture_events(hass, EVENT_LOGIN_FAILED)
    client = await hass_client_no_auth()
    await _bad_bearer(client)
    await flush_worker(hass)
    assert runtime.worker.storage_errors == 1

    await create_hass_user(hass, "alice", "right")
    await login_flow(client, "alice", "wrong")
    await _bad_bearer(client)
    await flush_worker(hass)
    await hass.async_block_till_done()

    notifications = _notifications(hass)
    assert "http-login" in notifications
    assert NOTIFICATION_ID in notifications
    assert [e.data["reason"] for e in events] == [
        "invalid_token",
        "wrong_password",
        "invalid_token",
    ]
    assert store_rows(hass) == []


async def test_malformed_login_flow_post_recorded_and_dismissed(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A login-flow 4xx Core counts without a form is recorded here."""
    await _setup(hass, monkeypatch)
    client = await hass_client_no_auth()
    resp = await client.post(
        "/auth/login_flow",
        json={
            "client_id": "not a client id",
            "handler": ["homeassistant", None],
            "redirect_uri": REDIRECT_URI,
        },
        headers={"User-Agent": UA},
    )
    assert resp.status == 400
    await flush_worker(hass)
    await hass.async_block_till_done()
    rows = store_rows(hass)
    assert len(rows) == 1
    row = rows[0]
    assert (row.result, row.reason, row.username_typed, row.url) == (
        "failed",
        "invalid_credentials",
        None,
        "/auth/login_flow",
    )
    assert "http-login" not in _notifications(hass)


async def test_replace_mode_dismisses_blocked_login_http_login(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked login is ours to notify: one row, and `http-login` goes."""
    await _setup(hass, monkeypatch)
    await create_hass_user(hass, "owner", "pw")  # the owner can't be deactivated
    alice = await create_hass_user(hass, "alice", "right")
    await hass.auth.async_update_user(alice, is_active=False)
    client = await hass_client_no_auth()
    resp = await client.post(
        "/auth/login_flow",
        json={
            "client_id": CLIENT_ID,
            "handler": ["homeassistant", None],
            "redirect_uri": REDIRECT_URI,
        },
    )
    step = await resp.json()
    resp = await client.post(
        f"/auth/login_flow/{step['flow_id']}",
        json={"client_id": CLIENT_ID, "username": "alice", "password": "right"},
    )
    assert resp.status == 403
    await flush_worker(hass)
    await hass.async_block_till_done()
    assert [(r.result, r.reason) for r in store_rows(hass)] == [
        ("blocked", "blocked_inactive_user")
    ]
    assert "http-login" not in _notifications(hass)


async def test_login_flow_redirect_failure_keeps_http_login(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Right password, bad redirect URI: Core counts it, we notify nothing."""
    await _setup(hass, monkeypatch)
    await create_hass_user(hass, "alice", "right")
    client = await hass_client_no_auth()
    resp = await client.post(
        "/auth/login_flow",
        json={
            "client_id": CLIENT_ID,
            "handler": ["homeassistant", None],
            "redirect_uri": "http://evil.example/",
        },
    )
    step = await resp.json()
    resp = await client.post(
        f"/auth/login_flow/{step['flow_id']}",
        json={"client_id": CLIENT_ID, "username": "alice", "password": "right"},
    )
    assert resp.status == 403
    await flush_worker(hass)
    await hass.async_block_till_done()
    assert "http-login" in _notifications(hass)
    # H1 holds its success for a token; nothing failed is recorded here.
    assert [r for r in store_rows(hass) if r.result != "success"] == []


async def test_unload_unsubscribes(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After unload, failures are no longer seen and `http-login` stays."""
    entry = await _setup(hass, monkeypatch)
    collector = _collector(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    with patch.object(collector, "_on_update") as on_update:
        await _bad_bearer(await hass_client_no_auth())
        await hass.async_block_till_done()
    on_update.assert_not_called()
    assert "http-login" in _notifications(hass)


async def test_wrong_login_notification_contract_passes(hass: HomeAssistant) -> None:
    """Core has the notification IDs and helpers the collector relies on."""
    assert CONTRACTS["wrong_login_notification"].collector == "auth_failures"
    assert check_contracts(hass, "auth_failures") == []


@pytest.mark.parametrize(
    ("module", "attr", "value", "expected"),
    [
        (ban, "NOTIFICATION_ID_LOGIN", "login", "NOTIFICATION_ID_LOGIN"),
        (ban, "NOTIFICATION_ID_BAN", "ban", "NOTIFICATION_ID_BAN"),
        (persistent_notification, "async_register_callback", None, "register"),
        (persistent_notification, "async_dismiss", "x", "async_dismiss"),
    ],
)
async def test_wrong_login_notification_contract_fails(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    attr: str,
    value: Any,
    expected: str,
) -> None:
    """A changed ID or a missing helper fails the contract."""
    monkeypatch.setattr(module, attr, value)
    reason = CONTRACTS["wrong_login_notification"].check(hass)
    assert reason is not None
    assert expected in reason
