"""Tests for the login collector (H1 login flow, H8 refresh-token confirmation)."""

# ruff: noqa: SLF001

import asyncio
from collections.abc import Iterator
from ipaddress import ip_address
import json
import logging
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import ClientResponse, web
from aiohttp.test_utils import TestClient
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    ensure_auth_manager_loaded,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.electric_fence import COLLECTOR_TYPES
from custom_components.electric_fence.collectors.login import LoginCollector, _FlowMemo
from custom_components.electric_fence.const import DOMAIN, MIN_HA_VERSION
from custom_components.electric_fence.models import (
    LoginReason,
    LoginResult,
    Source,
    UserExists,
)
from custom_components.electric_fence.notifier import EVENT_LOGIN_FAILED
from custom_components.electric_fence.protection import throttle as throttle_module
from custom_components.electric_fence.protection.policy import IpPolicy
from custom_components.electric_fence.protection.throttle import (
    BASE_DELAY,
    MAX_WAITING,
    THROTTLE_MESSAGE,
    Refused,
    UsernameGuard,
)
from custom_components.electric_fence.settings import Settings
from homeassistant.auth import auth_manager_from_config
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
from homeassistant.auth.providers.homeassistant import HassAuthProvider
from homeassistant.components.http.ban import KEY_BAN_MANAGER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, UnknownFlow, UnknownHandler
from homeassistant.helpers.http import current_request
from homeassistant.setup import async_setup_component
from tests.helpers import (
    CLIENT_ID,
    REDIRECT_URI,
    create_hass_user,
    dump_db,
    flush_worker,
    login_flow,
    setup_forwarded_http,
    store_rows,
)

type Client = TestClient[web.Request, web.Application]


def _collector(hass: HomeAssistant) -> LoginCollector:
    entry = hass.config_entries.async_loaded_entries(DOMAIN)[0]
    return next(
        c for c in entry.runtime_data.collectors if isinstance(c, LoginCollector)
    )


async def _setup(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """Set up an entry without the fixture (for a custom auth manager)."""
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", MIN_HA_VERSION)
    entry = MockConfigEntry(domain=DOMAIN, options=Settings().to_options())
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _exchange_code(client: Client, code: str) -> dict[str, Any]:
    resp = await client.post(
        "/auth/token",
        data={"grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID},
    )
    assert resp.status == 200
    return dict(await resp.json())


async def _mfa_hass(hass: HomeAssistant) -> None:
    """Replace the auth manager with one that has TOTP."""
    hass.auth = await auth_manager_from_config(
        hass, [{"type": "homeassistant"}], [{"type": "totp"}]
    )
    ensure_auth_manager_loaded(hass.auth)


def test_collector_registered() -> None:
    """The login collector is set up with the integration."""
    assert LoginCollector in COLLECTOR_TYPES


async def test_failed_login_unknown_user_recorded(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """A wrong username is recorded as no such user, with request details."""
    await create_hass_user(hass, "alice", "right")
    client = await hass_client_no_auth()
    result = await login_flow(client, "admin", "x")
    assert result["errors"] == {"base": "invalid_auth"}
    await flush_worker(hass)
    row = store_rows(hass)[0]
    assert (row.result, row.reason, row.username_typed, row.user_exists) == (
        "failed",
        "no_such_user",
        "admin",
        "no",
    )
    assert row.ip == "127.0.0.1"
    assert row.source == "local"
    assert row.provider == "homeassistant"
    assert row.client_id is None  # failures have no token to take it from
    assert row.url is not None
    assert row.url.startswith("/auth/login_flow/")
    assert row.user_agent
    assert row.user_id is None


async def test_failed_login_wrong_password_existing_user(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """A wrong password for a real user is recorded as such."""
    await create_hass_user(hass, "alice", "right")
    client = await hass_client_no_auth()
    await login_flow(client, " Alice ", "wrong")
    await flush_worker(hass)
    row = store_rows(hass)[0]
    assert row.reason == "wrong_password"
    assert row.user_exists == "yes"
    # Stored as typed (sanitised), not normalised.
    assert row.username_typed == " Alice "


async def test_failed_login_existing_user_has_user_id(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """A wrong password for a real user carries its user ID, row and event."""
    alice = await create_hass_user(hass, "alice", "right")
    events = async_capture_events(hass, EVENT_LOGIN_FAILED)
    await login_flow(await hass_client_no_auth(), " Alice ", "wrong")
    await login_flow(await hass_client_no_auth(), "nobody", "wrong")
    await flush_worker(hass)
    rows = {r.username_typed: r for r in store_rows(hass)}
    assert rows[" Alice "].user_id == alice.id
    assert rows["nobody"].user_id is None
    assert {e.data["username"]: e.data["user_id"] for e in events} == {
        " Alice ": alice.id,
        "no•••": None,
    }


async def test_wrong_2fa_code_recorded(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wrong TOTP code is recorded with the module and the earlier username."""
    await _mfa_hass(hass)
    user = await create_hass_user(hass, "alice", "right")
    await hass.auth.async_enable_user_mfa(user, "totp", {})
    await _setup(hass, monkeypatch)
    client = await hass_client_no_auth()
    events = async_capture_events(hass, EVENT_LOGIN_FAILED)

    step = await login_flow(client, "alice", "right")
    assert step["step_id"] == "mfa"
    with patch("pyotp.TOTP.verify", return_value=False):
        resp = await client.post(
            f"/auth/login_flow/{step['flow_id']}",
            json={"client_id": CLIENT_ID, "code": "000000"},
        )
        assert (await resp.json())["errors"] == {"base": "invalid_code"}
    await flush_worker(hass)
    rows = store_rows(hass)
    assert len(rows) == 1
    row = rows[0]
    assert row.reason == "wrong_2fa_code"
    assert row.mfa_module == "totp"
    assert row.user_exists == "yes"
    assert row.username_typed == "alice"
    assert row.user_id == user.id
    assert [e.data["user_id"] for e in events] == [user.id]


async def test_successful_login_with_mfa_records_module(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A login that passed MFA is recorded with its module, once."""
    await _mfa_hass(hass)
    user = await create_hass_user(hass, "alice", "right")
    await hass.auth.async_enable_user_mfa(user, "totp", {})
    await _setup(hass, monkeypatch)
    client = await hass_client_no_auth()

    step = await login_flow(client, "alice", "right")
    with patch("pyotp.TOTP.verify", return_value=True):
        resp = await client.post(
            f"/auth/login_flow/{step['flow_id']}",
            json={"client_id": CLIENT_ID, "code": "123456"},
        )
        result = await resp.json()
    assert result["type"] == "create_entry"
    await _exchange_code(client, result["result"])
    await flush_worker(hass)
    rows = store_rows(hass)
    assert len(rows) == 1
    row = rows[0]
    assert (row.result, row.mfa_module, row.username_typed) == (
        "success",
        "totp",
        "alice",
    )
    assert row.confirmed
    assert _collector(hass)._flows == {}


async def test_successful_login_confirmed_by_token(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """A success is stored once its refresh token is created."""
    alice = await create_hass_user(hass, "alice", "right")
    client = await hass_client_no_auth()
    result = await login_flow(client, "alice", "right")
    assert result["type"] == "create_entry"
    await flush_worker(hass)
    # Held for confirmation: nothing stored yet.
    assert store_rows(hass) == []

    await _exchange_code(client, result["result"])
    await flush_worker(hass)
    row = store_rows(hass)[0]
    assert row.result == "success"
    assert row.reason == "ok"
    assert row.confirmed
    assert row.user_id == alice.id
    assert row.user_exists == "yes"
    assert row.username_typed == "alice"
    assert row.client_id == CLIENT_ID
    assert row.mfa_module is None


async def test_inactive_user_recorded_as_blocked(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """A correct password for an inactive user is recorded as blocked."""
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
    row = store_rows(hass)[0]
    assert (row.result, row.reason) == ("blocked", "blocked_inactive_user")
    assert row.user_id == alice.id


async def test_password_never_stored_or_logged(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The password never reaches the logs or the database."""
    caplog.set_level(logging.DEBUG)
    await create_hass_user(hass, "admin", "Sup3r-Secret!")
    client = await hass_client_no_auth()
    await login_flow(client, "admin", "Sup3r-Secret!")
    await login_flow(client, "nobody", "Sup3r-Secret!")
    await flush_worker(hass)
    assert len(store_rows(hass)) == 1  # the failure; the success is held
    await hass.config_entries.async_unload(
        hass.config_entries.async_entries(DOMAIN)[0].entry_id
    )
    assert "Sup3r-Secret!" not in caplog.text
    assert "Sup3r-Secret!" not in dump_db(hass)
    assert "nobody" in dump_db(hass)


async def test_raw_unknown_username_never_logged(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A typed username is never logged, even at debug level or on errors."""
    caplog.set_level(logging.DEBUG)
    await create_hass_user(hass, "alice", "right")
    client = await hass_client_no_auth()
    await login_flow(client, "hunter2-password", "x")
    await flush_worker(hass)
    assert store_rows(hass)[0].username_typed == "hunter2-password"

    # Also when Electric Fence's own code fails while handling it.
    collector = _collector(hass)
    with patch.object(
        collector.runtime.worker, "submit", side_effect=KeyError("hunter2-password")
    ):
        await login_flow(client, "hunter2-password", "x")
    assert collector.guard.errors == 1
    assert "hunter2-password" not in caplog.text


async def test_username_sanitised(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """Typed usernames are stored without control characters and truncated."""
    await create_hass_user(hass, "alice", "right")
    client = await hass_client_no_auth()
    await login_flow(client, "ad\x00min" + "x" * 1000, "x")
    await flush_worker(hass)
    row = store_rows(hass)[0]
    assert row.username_typed is not None
    assert row.username_typed.startswith("admin")
    assert len(row.username_typed) == 256


async def test_configure_for_unseen_flow_recorded(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flow started before the hooks were installed is still recorded."""
    await create_hass_user(hass, "alice", "right")
    assert await async_setup_component(hass, "auth", {})
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

    await _setup(hass, monkeypatch)
    resp = await client.post(
        f"/auth/login_flow/{step['flow_id']}",
        json={"client_id": CLIENT_ID, "username": "admin", "password": "x"},
    )
    assert (await resp.json())["errors"] == {"base": "invalid_auth"}
    await flush_worker(hass)
    row = store_rows(hass)[0]
    assert row.username_typed == "admin"
    assert row.reason == "no_such_user"
    assert row.ip == "127.0.0.1"


@pytest.mark.parametrize(("error", "errors"), [(RuntimeError, 1), (IndexError, 0)])
async def test_flow_lookup_failure_falls_back_to_request(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    error: type[Exception],
    errors: int,
) -> None:
    """Without the flow, handler comes from the result and IP from the request.

    IndexError (a flow without a step yet) is expected, like UnknownFlow, and
    is not counted as an error.
    """
    await create_hass_user(hass, "alice", "right")
    client = await hass_client_no_auth()
    real_get = hass.auth.login_flow.async_get
    calls = 0

    def flaky_get(flow_id: str) -> Any:
        # The view looks the flow up first; only our lookup fails.
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_get(flow_id)
        raise error

    with patch.object(hass.auth.login_flow, "async_get", flaky_get):
        await login_flow(client, "admin", "x")
    await flush_worker(hass)
    row = store_rows(hass)[0]
    assert (row.username_typed, row.provider, row.ip, row.source) == (
        "admin",
        "homeassistant",
        "127.0.0.1",
        "local",
    )
    assert _collector(hass).guard.errors == errors


async def test_hook_never_reads_request_body(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """Only Core reads the request body: once per request, never the hook."""
    await create_hass_user(hass, "alice", "right")
    client = await hass_client_no_auth()
    reads: list[str] = []
    real_read = web.BaseRequest.read

    async def counting_read(self: web.BaseRequest) -> bytes:
        reads.append(self.path)
        return await real_read(self)

    with patch.object(web.BaseRequest, "read", counting_read):
        await login_flow(client, "alice", "wrong")
        good = await login_flow(client, "alice", "right")
    # One read per POST (two POSTs per login flow), all by Core's views.
    assert len(reads) == 4
    await _exchange_code(client, good["result"])
    await flush_worker(hass)
    rows = store_rows(hass)
    assert [(r.result, r.client_id) for r in rows] == [
        ("success", CLIENT_ID),
        ("failed", None),
    ]


async def test_too_many_wrong_codes_last_attempt_recorded(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wrong code that makes Core abort the flow is recorded too."""
    await _mfa_hass(hass)
    user = await create_hass_user(hass, "alice", "right")
    await hass.auth.async_enable_user_mfa(user, "totp", {})
    await _setup(hass, monkeypatch)
    client = await hass_client_no_auth()
    collector = _collector(hass)
    worker = collector.runtime.worker

    step = await login_flow(client, "alice", "right")
    results = []
    with (
        patch("pyotp.TOTP.verify", return_value=False),
        patch.object(worker, "submit", wraps=worker.submit) as submit,
    ):
        for _ in range(5):
            resp = await client.post(
                f"/auth/login_flow/{step['flow_id']}",
                json={"client_id": CLIENT_ID, "code": "000000"},
            )
            results.append(await resp.json())
    assert [r["type"] for r in results] == ["form"] * 4 + ["abort"]
    assert results[-1]["reason"] == "too_many_retry"
    records = [c.args[0] for c in submit.call_args_list]
    assert [
        (r.result, r.reason, r.username_typed, r.mfa_module, r.user_exists)
        for r in records
    ] == [
        (
            LoginResult.FAILED,
            LoginReason.WRONG_2FA_CODE,
            "alice",
            "totp",
            UserExists.YES,
        )
    ] * 5
    await flush_worker(hass)
    rows = store_rows(hass)
    # Identical failures within a second are merged into one row with a count.
    assert sum(r.count for r in rows) == 5
    assert {r.reason for r in rows} == {"wrong_2fa_code"}
    assert {r.user_id for r in rows} == {user.id}
    assert collector._flows == {}


async def test_other_aborts_not_recorded(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Aborts other than too many wrong codes are not attempts."""
    collector = _collector(hass)
    submitted: list[Any] = []
    with patch.object(collector.runtime.worker, "submit", submitted.append):
        for reason in ("login_expired", "unknown_error", None):
            collector._observe(
                "f",
                None,
                None,
                {"type": FlowResultType.ABORT, "flow_id": "f", "reason": reason},
            )
    assert submitted == []


async def test_2fa_code_never_stored_or_logged(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A submitted 2FA code never reaches the logs or the database."""
    caplog.set_level(logging.DEBUG)
    await _mfa_hass(hass)
    user = await create_hass_user(hass, "alice", "right")
    await hass.auth.async_enable_user_mfa(user, "totp", {})
    await _setup(hass, monkeypatch)
    client = await hass_client_no_auth()
    step = await login_flow(client, "alice", "right")
    with patch("pyotp.TOTP.verify", return_value=False):
        resp = await client.post(
            f"/auth/login_flow/{step['flow_id']}",
            json={"client_id": CLIENT_ID, "code": "731946"},
        )
        assert (await resp.json())["errors"] == {"base": "invalid_code"}
    await flush_worker(hass)
    assert store_rows(hass)
    assert "731946" not in caplog.text
    assert "731946" not in dump_db(hass)


async def _login_sequence(client: Client) -> list[dict[str, Any]]:
    """Run password, MFA, token and failure steps; return every response."""
    out: list[dict[str, Any]] = []
    # bob: password only, then the token exchange.
    good = await login_flow(client, "bob", "right")
    out.extend([good, await _exchange_code(client, good["result"])])
    # alice: password, MFA code, then the token exchange.
    step = await login_flow(client, "alice", "right")
    with patch("pyotp.TOTP.verify", return_value=True):
        resp = await client.post(
            f"/auth/login_flow/{step['flow_id']}",
            json={"client_id": CLIENT_ID, "code": "123456"},
        )
        done = await resp.json()
    out.extend([step, done, await _exchange_code(client, done["result"])])
    # Failures: a wrong password and a wrong code.
    out.append(await login_flow(client, "bob", "wrong"))
    step = await login_flow(client, "alice", "right")
    with patch("pyotp.TOTP.verify", return_value=False):
        resp = await client.post(
            f"/auth/login_flow/{step['flow_id']}",
            json={"client_id": CLIENT_ID, "code": "000000"},
        )
        out.append(await resp.json())
    return out


def _strip(response: dict[str, Any]) -> dict[str, Any]:
    """Drop the fields that differ between any two runs."""
    volatile = {"flow_id", "result", "access_token", "refresh_token"}
    return {k: v for k, v in response.items() if k not in volatile}


async def test_login_response_identical_with_hook_raising(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With every hook body raising, logins behave exactly as on plain Core."""
    await _mfa_hass(hass)
    await create_hass_user(hass, "bob", "right")
    alice = await create_hass_user(hass, "alice", "right")
    await hass.auth.async_enable_user_mfa(alice, "totp", {})
    assert await async_setup_component(hass, "auth", {})
    client = await hass_client_no_auth()

    plain = [_strip(r) for r in await _login_sequence(client)]

    await _setup(hass, monkeypatch)
    collector = _collector(hass)
    with (
        patch.object(LoginCollector, "_observe", side_effect=RuntimeError),
        patch.object(LoginCollector, "_flow_info", side_effect=RuntimeError),
        patch.object(LoginCollector, "_confirm_token", side_effect=RuntimeError),
    ):
        hooked = [_strip(r) for r in await _login_sequence(client)]
    assert hooked == plain
    assert [r.get("type", r.get("token_type")) for r in plain] == [
        "create_entry",
        "Bearer",
        "form",
        "create_entry",
        "Bearer",
        "form",
        "form",
    ]
    assert collector.guard.errors > 0


async def test_argument_extraction_is_guarded(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """Even reading the call's arguments can't break a login."""
    await create_hass_user(hass, "alice", "right")
    client = await hass_client_no_auth()
    with patch(
        "custom_components.electric_fence.collectors.login._arg",
        side_effect=RuntimeError,
    ):
        assert (await login_flow(client, "alice", "wrong"))["errors"] == {
            "base": "invalid_auth"
        }
        good = await login_flow(client, "alice", "right")
        assert good["type"] == "create_entry"
    assert _collector(hass).guard.errors > 0


async def test_login_response_identical_to_core(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hooked flow returns the same responses as Core alone."""
    assert await async_setup_component(hass, "auth", {})
    await create_hass_user(hass, "alice", "right")
    client = await hass_client_no_auth()

    def strip(result: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in result.items() if k not in ("flow_id", "result")}

    before = [strip(await login_flow(client, "alice", pw)) for pw in ("right", "wrong")]
    await _setup(hass, monkeypatch)
    after = [strip(await login_flow(client, "alice", pw)) for pw in ("right", "wrong")]
    assert after == before


async def test_original_exception_reraised_and_not_observed(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Exceptions from Core pass through unchanged, and nothing is recorded."""
    collector = _collector(hass)
    with pytest.raises(UnknownFlow):
        await hass.auth.login_flow.async_configure("no-such-flow", {"username": "a"})
    with pytest.raises(UnknownHandler):
        await hass.auth.login_flow.async_init(("nope", None), context={})
    await flush_worker(hass)
    assert store_rows(hass) == []
    assert collector.guard.errors == 0


async def test_async_init_create_entry_recorded(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """A flow that finishes in async_init is recorded too."""
    alice = await create_hass_user(hass, "alice", "right")
    credentials = alice.credentials[0]
    result: dict[str, Any] = {
        "type": FlowResultType.CREATE_ENTRY,
        "flow_id": "f1",
        "handler": ("homeassistant", None),
        "result": credentials,
    }
    collector = _collector(hass)
    original = AsyncMock(return_value=result)
    wrapper = collector._wrap_init(original)
    context = {"ip_address": ip_address("203.0.113.9")}
    assert await wrapper(("homeassistant", None), context=context) is result
    original.assert_awaited_once_with(("homeassistant", None), context=context)
    await flush_worker(hass)
    await hass.auth.async_create_refresh_token(alice, CLIENT_ID)
    await flush_worker(hass)
    row = store_rows(hass)[0]
    assert (row.result, row.ip, row.source, row.user_id, row.url) == (
        "success",
        "203.0.113.9",
        "remote",
        alice.id,
        None,
    )


async def test_observe_edge_results(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Results that are not attempts are ignored; odd inputs don't fail."""
    collector = _collector(hass)
    submitted: list[Any] = []
    with patch.object(collector.runtime.worker, "submit", submitted.append):
        handler = ("homeassistant", None)
        for result in (
            {"type": FlowResultType.FORM, "flow_id": "f", "handler": handler},
            {"type": FlowResultType.ABORT, "flow_id": "f", "handler": handler},
            {
                "type": FlowResultType.FORM,
                "flow_id": "f",
                "handler": handler,
                "errors": {"base": "other"},
            },
        ):
            collector._observe("f", None, {"username": "a"}, result)
        assert submitted == []

        # No username, non-dict input, unknown provider.
        invalid = {
            "type": FlowResultType.FORM,
            "flow_id": "f",
            "handler": ("trusted_networks", None),
            "errors": {"base": "invalid_auth"},
        }
        collector._observe("f", None, ["not", "a", "dict"], invalid)
        collector._observe("f", None, {"username": 5}, invalid)
        collector._observe("f", None, {"username": "bob"}, invalid)
        assert [
            (r.reason, r.user_exists, r.username_typed, r.provider) for r in submitted
        ] == [
            (
                LoginReason.INVALID_CREDENTIALS,
                UserExists.UNKNOWN,
                None,
                "trusted_networks",
            )
        ] * 2 + [
            (
                LoginReason.INVALID_CREDENTIALS,
                UserExists.UNKNOWN,
                "bob",
                "trusted_networks",
            )
        ]
        submitted.clear()

        # No handler, no request: still a record, without IP or source.
        collector._observe(
            "f",
            None,
            {"username": "bob"},
            {"type": FlowResultType.FORM, "errors": {"base": "invalid_auth"}},
        )
        rec = submitted[0]
        assert (rec.provider, rec.ip, rec.source, rec.user_exists) == (
            None,
            None,
            None,
            UserExists.UNKNOWN,
        )


async def test_source_cloud(hass: HomeAssistant, setup_entry: MockConfigEntry) -> None:
    """A login over Home Assistant Cloud is marked as cloud."""
    collector = _collector(hass)
    submitted: list[Any] = []
    result = {
        "type": FlowResultType.FORM,
        "flow_id": "f",
        "handler": ("homeassistant", None),
        "errors": {"base": "invalid_auth"},
    }
    info = collector._flow_info_from(
        {"handler": ("homeassistant", None), "context": {"ip_address": "x"}}
    )
    with (
        patch.object(collector.runtime.worker, "submit", submitted.append),
        patch(
            "custom_components.electric_fence.collectors.login.is_cloud_connection",
            return_value=True,
        ),
    ):
        collector._observe("f", info, {"username": "a"}, result)
    assert submitted[0].source is Source.CLOUD
    assert submitted[0].ip is None  # not an IP address object


async def test_flow_memory_bounded(
    hass: HomeAssistant, setup_entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-flow memory never grows past its limit."""
    monkeypatch.setattr(
        "custom_components.electric_fence.collectors.login.MAX_FLOWS", 3
    )
    collector = _collector(hass)
    mfa_form = {
        "type": FlowResultType.FORM,
        "handler": ("homeassistant", None),
        "step_id": "mfa",
        "description_placeholders": {"mfa_module_id": "totp"},
    }
    for i in range(5):
        collector._observe(f"f{i}", None, {"username": "a"}, mfa_form)
    assert list(collector._flows) == ["f2", "f3", "f4"]
    # Seen again: moved to the newest position.
    collector._observe("f2", None, None, mfa_form)
    assert list(collector._flows) == ["f3", "f4", "f2"]
    assert collector._flows["f2"].username == "a"
    collector._observe("f2", None, {"username": "b"}, mfa_form)
    assert collector._flows["f2"].username == "b"
    # No flow ID: nothing remembered.
    collector._observe(None, None, {"username": "c"}, mfa_form)
    assert len(collector._flows) == 3
    collector.uninstall()
    assert collector._flows == {}


async def test_defensive_paths_never_raise(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Unexpected values are ignored, and failures are contained."""
    collector = _collector(hass)
    submitted: list[Any] = []
    with patch.object(collector.runtime.worker, "submit", submitted.append):
        collector._observe("f", None, None, "not a result")
    assert submitted == []
    assert collector._flow_info((None,), {}) is None
    assert collector._flow_info((), {}) is None


@pytest.mark.parametrize(
    ("remote", "ip", "source"),
    [
        (None, None, None),
        ("not-an-ip", None, None),
        ("203.0.113.5", "203.0.113.5", Source.REMOTE),
        ("fe80::1", "fe80::1", Source.LOCAL),
    ],
)
async def test_ip_from_request_when_flow_unknown(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    remote: str | None,
    ip: str | None,
    source: Source | None,
) -> None:
    """Without flow details, the IP comes from the request's remote address."""
    collector = _collector(hass)
    submitted: list[Any] = []
    request = Mock(remote=remote, path="/auth/login_flow/f", headers={})
    token = current_request.set(request)
    try:
        with patch.object(collector.runtime.worker, "submit", submitted.append):
            collector._observe(
                "f",
                None,
                {"username": "a"},
                {"type": FlowResultType.FORM, "errors": {"base": "invalid_auth"}},
            )
    finally:
        current_request.reset(token)
    assert (submitted[0].ip, submitted[0].source) == (ip, source)
    assert submitted[0].user_agent is None


async def test_refresh_token_other_types_not_confirmed(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Only normal refresh tokens confirm a login."""
    alice = await create_hass_user(hass, "alice", "right")
    collector = _collector(hass)
    with patch.object(collector.runtime.worker, "confirm_success") as confirm:
        await hass.auth.async_create_refresh_token(
            alice,
            client_name="script",
            token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
        )
        confirm.assert_not_called()
        await hass.auth.async_create_refresh_token(alice, CLIENT_ID, "Phone")
        confirm.assert_called_once_with(alice.id, CLIENT_ID, "Phone")
    # Core's exceptions pass through unchanged.
    with pytest.raises(ValueError, match="Client is required"):
        await hass.auth.async_create_refresh_token(alice)


async def test_unload_restores_login_flow_methods(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Unloading puts Core's methods back."""
    flow_cls = type(hass.auth.login_flow)
    assert "async_configure" in vars(hass.auth.login_flow)
    await hass.config_entries.async_unload(setup_entry.entry_id)
    login = hass.auth.login_flow
    assert login.async_configure == flow_cls.async_configure.__get__(login)
    assert login.async_init == flow_cls.async_init.__get__(login)
    assert hass.auth.async_create_refresh_token == type(
        hass.auth
    ).async_create_refresh_token.__get__(hass.auth)
    assert "async_configure" not in vars(login)
    assert "async_create_refresh_token" not in vars(hass.auth)


async def test_records_carry_enum_values(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """Records carry enum values, not plain strings."""
    collector = _collector(hass)
    submitted: list[Any] = []
    await create_hass_user(hass, "alice", "right")
    client = await hass_client_no_auth()
    with patch.object(collector.runtime.worker, "submit", submitted.append):
        await login_flow(client, "admin", "x")
    assert submitted[0].result is LoginResult.FAILED
    assert submitted[0].source is Source.LOCAL
    assert submitted[0].flow_id is not None


async def test_will_block_mirrors_worker_rules(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """The request mark predicts the worker's block rules, from memory."""
    collector = _collector(hass)
    alice = await create_hass_user(hass, "alice", "right")
    credentials = alice.credentials[0]
    assert not collector._will_block(None, Source.REMOTE)
    assert not collector._will_block(Mock(id="unlinked"), Source.REMOTE)
    assert not collector._will_block(credentials, Source.REMOTE)
    await hass.auth.async_update_user(alice, local_only=True)
    assert not collector._will_block(credentials, Source.LOCAL)
    assert collector._will_block(credentials, Source.REMOTE)
    with patch(
        "custom_components.electric_fence.collectors.login.auth_users",
        return_value=None,
    ):
        assert not collector._will_block(credentials, Source.REMOTE)


FORWARDED_IP = "203.0.113.9"
XFF = {"X-Forwarded-For": FORWARDED_IP}
ATTACK_IPS = ("198.51.100.1", "198.51.100.2", "198.51.100.3")


def _guard(hass: HomeAssistant) -> UsernameGuard:
    guard = _collector(hass).runtime.username_guard
    assert guard is not None
    return guard


def _policy(hass: HomeAssistant) -> IpPolicy:
    policy = _collector(hass).runtime.ip_policy
    assert policy is not None
    return policy


def _attack(hass: HomeAssistant, username: str = "alice") -> UsernameGuard:
    """Start an attack on `username` from three outside addresses."""
    guard = _guard(hass)
    for i in range(10):
        guard.on_attempt(
            username,
            ATTACK_IPS[i % len(ATTACK_IPS)],
            LoginResult.FAILED,
            LoginReason.WRONG_PASSWORD,
        )
    assert [a.username for a in guard.active_attacks()] == [username]
    return guard


@pytest.fixture
def sleeps() -> Iterator[list[float]]:
    """Record the slow-down delays instead of sleeping."""
    durations: list[float] = []

    async def fake(delay: float) -> None:
        durations.append(delay)

    with patch.object(throttle_module, "_sleep", fake):
        yield durations


async def _start_flow(client: Client) -> str:
    resp = await client.post(
        "/auth/login_flow",
        json={
            "client_id": CLIENT_ID,
            "handler": ["homeassistant", None],
            "redirect_uri": REDIRECT_URI,
        },
        headers=XFF,
    )
    assert resp.status == 200
    return str((await resp.json())["flow_id"])


async def _step(
    client: Client, flow_id: str, username: str, password: str
) -> ClientResponse:
    return await client.post(
        f"/auth/login_flow/{flow_id}",
        json={"client_id": CLIENT_ID, "username": username, "password": password},
        headers=XFF,
    )


async def test_refused_returns_403_message(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
    sleeps: list[float],
) -> None:
    """With the queue full, the step is refused with a 403 the frontend shows."""
    await setup_forwarded_http(hass)
    await create_hass_user(hass, "alice", "right")
    await _setup(hass, monkeypatch)
    guard = _attack(hass)
    attack = guard._attacks["alice"]
    attack.waiting = MAX_WAITING
    client = await hass_client_no_auth()
    flow_id = await _start_flow(client)

    with patch.object(
        HassAuthProvider, "async_validate_login", autospec=True
    ) as validate:
        resp = await _step(client, flow_id, " Alice ", "right")
    assert resp.status == 403
    assert await resp.json() == {"message": THROTTLE_MESSAGE}
    validate.assert_not_called()
    assert sleeps == []

    # Counted as a failure by both policies.
    assert attack.failures == 11
    assert len(_policy(hass)._failures[FORWARDED_IP]) == 1
    await flush_worker(hass)
    (row,) = store_rows(hass)
    assert (row.result, row.reason, row.username_typed, row.ip, row.source) == (
        "failed",
        "throttled",
        " Alice ",
        FORWARDED_IP,
        "remote",
    )
    assert row.user_exists == "yes"
    assert row.url == f"/auth/login_flow/{flow_id}"

    # Core's in-progress mark is cleared: the same flow goes on once there's room.
    attack.waiting = 0
    resp = await _step(client, flow_id, "alice", "right")
    assert resp.status == 200
    assert (await resp.json())["type"] == "create_entry"
    assert sleeps == [BASE_DELAY * 2]


async def test_slowdown_before_core(
    hass: HomeAssistant, setup_entry: MockConfigEntry, sleeps: list[float]
) -> None:
    """During an attack the delay runs before Core's step, then the login goes on."""
    await create_hass_user(hass, "alice", "right")
    _attack(hass)
    order: list[str] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        order.append("sleep")

    original = HassAuthProvider.async_validate_login

    async def validate(self: HassAuthProvider, username: str, password: str) -> None:
        order.append("core")
        await original(self, username, password)

    flow = await hass.auth.login_flow.async_init(
        ("homeassistant", None), context={"ip_address": ip_address(FORWARDED_IP)}
    )
    request = Mock(remote=FORWARDED_IP, path="/auth/login_flow/f", headers={})
    token = current_request.set(request)
    try:
        with (
            patch.object(throttle_module, "_sleep", fake_sleep),
            patch.object(HassAuthProvider, "async_validate_login", validate),
        ):
            result = await hass.auth.login_flow.async_configure(
                flow["flow_id"], {"username": "alice", "password": "right"}
            )
    finally:
        current_request.reset(token)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert sleeps == [BASE_DELAY]
    assert order == ["sleep", "core"]
    # The slot was released: the next attempt doesn't wait for a lock.
    assert not _guard(hass)._attacks["alice"].lock.locked()


async def test_no_slowdown_outside_requests(
    hass: HomeAssistant, setup_entry: MockConfigEntry, sleeps: list[float]
) -> None:
    """Calls outside an HTTP request (other integrations, tests) never wait."""
    await create_hass_user(hass, "alice", "right")
    _attack(hass)
    flow = await hass.auth.login_flow.async_init(
        ("homeassistant", None), context={"ip_address": ip_address(FORWARDED_IP)}
    )
    with patch.object(UsernameGuard, "async_enter") as enter:
        result = await hass.auth.login_flow.async_configure(
            flow["flow_id"], {"username": "alice", "password": "right"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    enter.assert_not_called()
    assert sleeps == []


async def test_policy_fed(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Five wrong passwords from one outside address ban it through Core."""
    await setup_forwarded_http(hass)
    await create_hass_user(hass, "alice", "right")
    await _setup(hass, monkeypatch)
    client = await hass_client_no_auth()
    for _ in range(5):
        result = await login_flow(client, "alice", "wrong", headers=XFF)
        assert result["errors"] == {"base": "invalid_auth"}
    await hass.async_block_till_done()
    assert ip_address(FORWARDED_IP) in hass.http.app[KEY_BAN_MANAGER].ip_bans_lookup
    resp = await client.post(
        "/auth/login_flow",
        json={
            "client_id": CLIENT_ID,
            "handler": ["homeassistant", None],
            "redirect_uri": REDIRECT_URI,
        },
        headers=XFF,
    )
    assert resp.status == 403


async def test_observe_feeds_policies(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Every queued attempt reaches both policies; a blocked success is BLOCKED."""
    # The first user is the owner, who can't be deactivated.
    await create_hass_user(hass, "owner", "right")
    alice = await create_hass_user(hass, "alice", "right")
    collector = _collector(hass)
    runtime = collector.runtime
    info = collector._flow_info_from(
        {"handler": ("homeassistant", None), "context": {"ip_address": None}}
    )
    request = Mock(remote=FORWARDED_IP, path="/auth/login_flow/f", headers={})
    token = current_request.set(request)
    try:
        with (
            patch.object(runtime.ip_policy, "on_attempt") as ip_attempt,
            patch.object(runtime.username_guard, "on_attempt") as name_attempt,
        ):
            collector._observe(
                "f",
                info,
                {"username": " Alice "},
                {"type": FlowResultType.FORM, "errors": {"base": "invalid_auth"}},
            )
            await hass.auth.async_update_user(alice, local_only=True)
            collector._observe(
                "g",
                info,
                {"username": "alice"},
                {
                    "type": FlowResultType.CREATE_ENTRY,
                    "result": alice.credentials[0],
                },
            )
            await hass.auth.async_update_user(alice, local_only=False, is_active=False)
            collector._observe(
                "h",
                info,
                {"username": "alice"},
                {
                    "type": FlowResultType.CREATE_ENTRY,
                    "result": alice.credentials[0],
                },
            )
            await hass.auth.async_update_user(alice, is_active=True)
            collector._observe(
                "i",
                info,
                {"username": "alice"},
                {
                    "type": FlowResultType.CREATE_ENTRY,
                    "result": alice.credentials[0],
                },
            )
    finally:
        current_request.reset(token)
    assert [c.args for c in ip_attempt.call_args_list] == [
        (FORWARDED_IP, LoginResult.FAILED, LoginReason.WRONG_PASSWORD),
        (FORWARDED_IP, LoginResult.BLOCKED, LoginReason.BLOCKED_LOCAL_ONLY),
        (FORWARDED_IP, LoginResult.BLOCKED, LoginReason.BLOCKED_INACTIVE),
        (FORWARDED_IP, LoginResult.SUCCESS, LoginReason.OK),
    ]
    assert [c.args for c in name_attempt.call_args_list] == [
        (" Alice ", FORWARDED_IP, LoginResult.FAILED, LoginReason.WRONG_PASSWORD),
        ("alice", FORWARDED_IP, LoginResult.BLOCKED, LoginReason.BLOCKED_LOCAL_ONLY),
        ("alice", FORWARDED_IP, LoginResult.BLOCKED, LoginReason.BLOCKED_INACTIVE),
        ("alice", FORWARDED_IP, LoginResult.SUCCESS, LoginReason.OK),
    ]


async def test_observe_without_policies(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Attempts are still queued when the policies are missing."""
    collector = _collector(hass)
    runtime = collector.runtime
    monkeypatch_values = (runtime.ip_policy, runtime.username_guard)
    runtime.ip_policy = runtime.username_guard = None
    submitted: list[Any] = []
    try:
        with patch.object(runtime.worker, "submit", submitted.append):
            collector._observe(
                "f",
                None,
                {"username": "a"},
                {"type": FlowResultType.FORM, "errors": {"base": "invalid_auth"}},
            )
    finally:
        runtime.ip_policy, runtime.username_guard = monkeypatch_values
    assert len(submitted) == 1


async def test_gate_uses_flow_memo_username(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """A step without a username (MFA code) is gated by the flow's username."""
    collector = _collector(hass)
    collector._flows["f"] = _FlowMemo(username="alice")
    info = collector._flow_info_from(
        {"handler": None, "context": {"ip_address": ip_address(FORWARDED_IP)}}
    )
    original = AsyncMock(return_value={"type": FlowResultType.FORM})
    wrapper = collector._wrap_configure(original)
    request = Mock(remote="127.0.0.1", path="/auth/login_flow/f", headers={})
    token = current_request.set(request)
    try:
        with (
            patch.object(collector, "_flow_info", return_value=info),
            patch.object(
                UsernameGuard, "async_enter", AsyncMock(return_value=None)
            ) as enter,
        ):
            await wrapper("f", {"code": "123456"})
            await wrapper("g", None)
            await wrapper(5)
    finally:
        current_request.reset(token)
    assert [c.args for c in enter.call_args_list] == [
        ("alice", FORWARDED_IP),
        (None, FORWARDED_IP),
        (None, FORWARDED_IP),
    ]


async def test_gate_ip_from_request(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Without flow details, the gate takes the IP from the request."""
    collector = _collector(hass)
    original = AsyncMock(return_value={"type": FlowResultType.FORM})
    wrapper = collector._wrap_configure(original)
    request = Mock(remote=FORWARDED_IP, path="/auth/login_flow/f", headers={})
    token = current_request.set(request)
    try:
        with patch.object(
            UsernameGuard, "async_enter", AsyncMock(return_value=None)
        ) as enter:
            await wrapper("unknown", {"username": "bob"})
    finally:
        current_request.reset(token)
    enter.assert_awaited_once_with("bob", FORWARDED_IP)


async def test_gate_without_guard(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """With no username guard the step goes straight to Core."""
    collector = _collector(hass)
    runtime = collector.runtime
    guard, runtime.username_guard = runtime.username_guard, None
    original = AsyncMock(return_value={"type": FlowResultType.FORM})
    wrapper = collector._wrap_configure(original)
    token = current_request.set(Mock(remote=FORWARDED_IP, path="/", headers={}))
    try:
        assert await wrapper("f", {"username": "bob"}) == {"type": FlowResultType.FORM}
    finally:
        current_request.reset(token)
        runtime.username_guard = guard


@pytest.mark.parametrize("error", [RuntimeError("boom"), ValueError()])
async def test_gate_error_goes_to_core(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    error: Exception,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An error in the gate is logged without details; Core's step still runs."""
    collector = _collector(hass)
    original = AsyncMock(return_value={"type": FlowResultType.FORM})
    wrapper = collector._wrap_configure(original)
    token = current_request.set(Mock(remote=FORWARDED_IP, path="/", headers={}))
    try:
        with patch.object(UsernameGuard, "async_enter", side_effect=error):
            assert await wrapper("f", {"username": "secret-name"}) == {
                "type": FlowResultType.FORM
            }
    finally:
        current_request.reset(token)
    original.assert_awaited_once()
    assert "secret-name" not in caplog.text
    assert type(error).__name__ in caplog.text


async def test_slot_released_when_core_raises(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Core's exception passes through unchanged and the slot is released."""
    collector = _collector(hass)
    slot = Mock()
    original = AsyncMock(side_effect=UnknownFlow)
    wrapper = collector._wrap_configure(original)
    token = current_request.set(Mock(remote=FORWARDED_IP, path="/", headers={}))
    try:
        with (
            patch.object(UsernameGuard, "async_enter", AsyncMock(return_value=slot)),
            pytest.raises(UnknownFlow),
        ):
            await wrapper("f", {"username": "bob"})
    finally:
        current_request.reset(token)
    slot.release.assert_called_once_with()


async def test_slot_release_error_keeps_result(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """A failing release never replaces Core's result."""
    collector = _collector(hass)
    slot = Mock()
    slot.release.side_effect = RuntimeError
    result = {"type": FlowResultType.FORM}
    wrapper = collector._wrap_configure(AsyncMock(return_value=result))
    token = current_request.set(Mock(remote=FORWARDED_IP, path="/", headers={}))
    try:
        with patch.object(UsernameGuard, "async_enter", AsyncMock(return_value=slot)):
            assert await wrapper("f", {"username": "bob"}) is result
    finally:
        current_request.reset(token)


async def test_gate_cancellation_propagates(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """A cancelled wait is not swallowed, and Core's step doesn't run."""
    collector = _collector(hass)
    original = AsyncMock()
    wrapper = collector._wrap_configure(original)
    token = current_request.set(Mock(remote=FORWARDED_IP, path="/", headers={}))
    try:
        with (
            patch.object(
                UsernameGuard,
                "async_enter",
                AsyncMock(side_effect=asyncio.CancelledError),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await wrapper("f", {"username": "bob"})
    finally:
        current_request.reset(token)
    original.assert_not_awaited()


async def test_refused_record_error_still_403(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """If recording the refusal fails, the 403 is still returned."""
    collector = _collector(hass)
    original = AsyncMock()
    wrapper = collector._wrap_configure(original)
    token = current_request.set(Mock(remote=FORWARDED_IP, path="/", headers={}))
    try:
        with (
            patch.object(UsernameGuard, "async_enter", side_effect=Refused),
            patch.object(collector, "_observe_refused", side_effect=RuntimeError),
            pytest.raises(web.HTTPForbidden) as err,
        ):
            await wrapper("f", {"username": "bob"})
    finally:
        current_request.reset(token)
    assert json.loads(err.value.text or "") == {"message": THROTTLE_MESSAGE}
    assert err.value.content_type == "application/json"
    original.assert_not_awaited()
