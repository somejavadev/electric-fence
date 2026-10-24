"""Tests for the login collector (H1 login flow, H8 refresh-token confirmation)."""

# ruff: noqa: SLF001

from ipaddress import ip_address
import logging
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    ensure_auth_manager_loaded,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.electric_fence import COLLECTOR_TYPES
from custom_components.electric_fence.collectors.login import LoginCollector
from custom_components.electric_fence.const import DOMAIN, MIN_HA_VERSION
from custom_components.electric_fence.models import (
    LoginReason,
    LoginResult,
    Source,
    UserExists,
)
from custom_components.electric_fence.settings import Settings
from homeassistant.auth import auth_manager_from_config
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.http import current_request
from homeassistant.setup import async_setup_component
from tests.helpers import (
    CLIENT_ID,
    REDIRECT_URI,
    create_hass_user,
    dump_db,
    flush_worker,
    login_flow,
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
    assert row.client_id == CLIENT_ID
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


async def test_flow_lookup_failure_falls_back_to_request(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """Without the flow, handler comes from the result and IP from the request."""
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
        raise RuntimeError

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
    assert _collector(hass).guard.errors == 1


async def test_login_response_identical_with_hook_raising(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Logins succeed and fail exactly as without Electric Fence."""
    await create_hass_user(hass, "alice", "right")
    client = await hass_client_no_auth()
    for name in ("_observe", "_flow_info", "_confirm_token"):
        monkeypatch.setattr(LoginCollector, name, Mock(side_effect=RuntimeError))
    good = await login_flow(client, "alice", "right")
    assert good["type"] == "create_entry"
    bad = await login_flow(client, "alice", "wrong")
    assert bad["errors"] == {"base": "invalid_auth"}
    monkeypatch.undo()
    tokens = await _exchange_code(client, good["result"])
    assert tokens["token_type"] == "Bearer"


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
    with pytest.raises(Exception) as err:
        await hass.auth.login_flow.async_configure("no-such-flow", {"username": "a"})
    assert type(err.value).__name__ == "UnknownFlow"
    with pytest.raises(Exception) as err:
        await hass.auth.login_flow.async_init(("nope", None), context={})
    assert type(err.value).__name__ == "UnknownHandler"
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
            collector._observe("f", None, {"username": "a"}, result, None)
        assert submitted == []

        # No username, non-dict input, unknown provider.
        invalid = {
            "type": FlowResultType.FORM,
            "flow_id": "f",
            "handler": ("trusted_networks", None),
            "errors": {"base": "invalid_auth"},
        }
        collector._observe("f", None, ["not", "a", "dict"], invalid, None)
        collector._observe("f", None, {"username": 5}, invalid, None)
        collector._observe("f", None, {"username": "bob"}, invalid, None)
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
            None,
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
        collector._observe("f", info, {"username": "a"}, result, None)
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
        collector._observe(f"f{i}", None, {"username": "a"}, mfa_form, None)
    assert list(collector._flows) == ["f2", "f3", "f4"]
    # Seen again: moved to the newest position.
    collector._observe("f2", None, None, mfa_form, None)
    assert list(collector._flows) == ["f3", "f4", "f2"]
    assert collector._flows["f2"].username == "a"
    collector._observe("f2", None, {"username": "b"}, mfa_form, None)
    assert collector._flows["f2"].username == "b"
    # No flow ID: nothing remembered.
    collector._observe(None, None, {"username": "c"}, mfa_form, None)
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
        collector._observe("f", None, None, "not a result", None)
    assert submitted == []
    assert collector._flow_info(None) is None
    assert await collector._read_client_id("not a result") is None

    created = {"type": FlowResultType.CREATE_ENTRY, "flow_id": "f"}
    request = Mock(can_read_body=False, content_type="application/json")
    request.json = AsyncMock(side_effect=ValueError("hunter2"))
    token = current_request.set(request)
    try:
        assert await collector._read_client_id(created) is None
        assert collector.guard.errors == 1
        request.json = AsyncMock(return_value=["not", "a", "dict"])
        assert await collector._read_client_id(created) is None
        # A body still to be read is never awaited.
        request.can_read_body = True
        request.json = AsyncMock(return_value={"client_id": "x"})
        assert await collector._read_client_id(created) is None
        request.json.assert_not_awaited()
    finally:
        current_request.reset(token)


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
                None,
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
