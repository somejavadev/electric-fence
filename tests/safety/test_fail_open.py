"""Fail-open suite (spec §4.7, §11 criterion 9).

Every collector's observe or listener entry point, the worker's queueing
calls and the record listeners are forced to raise, one case at a time and
all at once. Each test first runs its scenario on plain Core, then sets up
Electric Fence on the same instance with the failure in place, runs the
scenario again, and compares the responses (volatile fields stripped).
"""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

from aiohttp import WSMsgType
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    ensure_auth_manager_loaded,
)
from pytest_homeassistant_custom_component.typing import (
    ClientSessionGenerator,
    WebSocketGenerator,
)

from custom_components.electric_fence.attribution import ActionScope
from custom_components.electric_fence.collectors.auth_failures import (
    AuthFailureCollector,
)
from custom_components.electric_fence.collectors.config_entries import (
    ConfigEntryCollector,
)
from custom_components.electric_fence.collectors.core_config import CoreConfigCollector
from custom_components.electric_fence.collectors.login import LoginCollector
from custom_components.electric_fence.collectors.users import UserCollector
from custom_components.electric_fence.const import DOMAIN, MIN_HA_VERSION
from custom_components.electric_fence.notifier import Notifier
from custom_components.electric_fence.sensor import FailedLoginsSensor
from custom_components.electric_fence.settings import Settings
from custom_components.electric_fence.worker import FenceWorker
from homeassistant.auth import auth_manager_from_config
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from tests.helpers import CLIENT_ID, DUMMY, create_hass_user, flush_worker, login_flow

# (class, attribute) pairs patched to raise.
type Patches = tuple[tuple[type, str], ...]

CASES: dict[str, Patches] = {
    "login_observe": ((LoginCollector, "_observe"),),
    "login_flow_info": ((LoginCollector, "_flow_info"),),
    "login_confirm_token": ((LoginCollector, "_confirm_token"),),
    "auth_failures": ((AuthFailureCollector, "_handle"),),
    "config_entries_h4": ((ConfigEntryCollector, "_handle"),),
    "users_h5": (
        (UserCollector, "_handle_added"),
        (UserCollector, "_handle_updated"),
        (UserCollector, "_handle_removed"),
    ),
    "core_config_h6": ((CoreConfigCollector, "_handle"),),
    "users_credentials": ((UserCollector, "_record_credential_change"),),
    "attribution_enter": ((ActionScope, "__init__"),),
    "attribution_exit": ((ActionScope, "close"),),
    "worker_submit": ((FenceWorker, "submit"),),
    "worker_confirm": ((FenceWorker, "confirm_success"),),
    "notifier_on_record": ((Notifier, "on_record"),),
    "sensor_listener": ((FailedLoginsSensor, "_on_record"),),
}
CASES["everything"] = tuple(p for patches in CASES.values() for p in patches)

case_param = pytest.mark.parametrize("case", list(CASES))

# Replaced by a placeholder: they differ between any two runs.
VOLATILE = {
    "flow_id",
    "entry_id",
    "subentry_id",
    "id",
    "created_at",
    "modified_at",
    "access_token",
    "refresh_token",
}
TOKEN_URL = "/auth/token"


@dataclass
class Env:
    """A Home Assistant with users, MFA, `config` and an admin token."""

    hass: HomeAssistant
    client_no_auth: ClientSessionGenerator
    client: ClientSessionGenerator
    ws_client: WebSocketGenerator
    token: str
    bob_id: str


@pytest.fixture
async def env(
    hass: HomeAssistant,
    dummy: None,
    hass_client_no_auth: ClientSessionGenerator,
    hass_client: ClientSessionGenerator,
    hass_ws_client: WebSocketGenerator,
) -> Env:
    """Set up plain Core: no Electric Fence yet."""
    hass.auth = await auth_manager_from_config(
        hass, [{"type": "homeassistant"}], [{"type": "totp"}]
    )
    ensure_auth_manager_loaded(hass.auth)
    admin = await create_hass_user(hass, "admin", "admin-pw")
    await create_hass_user(hass, "alice", "right")
    bob = await create_hass_user(hass, "bob", "right")
    mia = await create_hass_user(hass, "mia", "right")
    await hass.auth.async_enable_user_mfa(mia, "totp", {})
    for domain in ("http", "api", "auth", "websocket_api", "config"):
        assert await async_setup_component(hass, domain, {})
    refresh = await hass.auth.async_create_refresh_token(admin, CLIENT_ID)
    return Env(
        hass=hass,
        client_no_auth=hass_client_no_auth,
        client=hass_client,
        ws_client=hass_ws_client,
        token=hass.auth.async_create_access_token(refresh),
        bob_id=bob.id,
    )


def _norm(value: Any, ids: frozenset[str] = frozenset()) -> Any:
    """Return `value` with the volatile fields and `ids` replaced by a placeholder."""
    if isinstance(value, dict):
        return {
            k: "*"
            if k in VOLATILE or (k == "result" and isinstance(v, str))
            else _norm(v, ids)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_norm(v, ids) for v in value]
    if isinstance(value, str) and value in ids:
        return "*"
    return value


@contextmanager
def _failing(case: str) -> Iterator[list[int]]:
    """Make every entry point of `case` raise; yield the call count."""
    calls = [0]

    def boom(*args: Any, **kwargs: Any) -> Any:
        calls[0] += 1
        raise RuntimeError("Electric Fence fail-open test")

    with ExitStack() as stack:
        for target, attr in CASES[case]:
            stack.enter_context(patch.object(target, attr, boom))
        yield calls


async def _setup_fence(hass: HomeAssistant) -> None:
    with patch("custom_components.electric_fence.HA_VERSION", MIN_HA_VERSION):
        entry = MockConfigEntry(domain=DOMAIN, options=Settings().to_options())
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert hass.config_entries.async_loaded_entries(DOMAIN)


type Scenario = Callable[[Env], Awaitable[list[Any]]]


async def _compare(env: Env, case: str, scenario: Scenario) -> list[Any]:
    """Run `scenario` on plain Core, then with the failing fence; compare."""
    plain = await scenario(env)
    with _failing(case):
        await _setup_fence(env.hass)
        hooked = await scenario(env)
        # Record listeners (notifier, sensor) run after the write.
        await flush_worker(env.hass)
        await env.hass.async_block_till_done()
    assert hooked == plain
    return plain


# Scenarios. Each returns the normalised responses.


async def _successful_login(env: Env) -> list[Any]:
    client = await env.client_no_auth()
    step = await login_flow(client, "alice", "right")
    resp = await client.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": step.get("result"),
            "client_id": CLIENT_ID,
        },
    )
    token = await resp.json()
    api = await client.get(
        "/api/", headers={"Authorization": f"Bearer {token.get('access_token')}"}
    )
    return [_norm(step), resp.status, _norm(token), api.status, await api.json()]


async def _wrong_password(env: Env) -> list[Any]:
    client = await env.client_no_auth()
    return [
        _norm(await login_flow(client, "alice", "wrong")),
        _norm(await login_flow(client, "nobody", "wrong")),
    ]


async def _wrong_2fa(env: Env) -> list[Any]:
    client = await env.client_no_auth()
    step = await login_flow(client, "mia", "right")
    with patch("pyotp.TOTP.verify", return_value=False):
        resp = await client.post(
            f"/auth/login_flow/{step['flow_id']}",
            json={"client_id": CLIENT_ID, "code": "000000"},
        )
        result = await resp.json()
    return [_norm(step), resp.status, _norm(result)]


async def _invalid_token(env: Env) -> list[Any]:
    client = await env.client_no_auth()
    resp = await client.get("/api/", headers={"Authorization": "Bearer not-valid"})
    out: list[Any] = [resp.status, await resp.text()]
    async with client.ws_connect("/api/websocket") as websocket:
        out.append(await websocket.receive_json())
        await websocket.send_json({"type": "auth", "access_token": "not-valid"})
        out.append(await websocket.receive_json())
        out.append((await websocket.receive()).type)
    return out


async def _admin_ws(env: Env) -> list[Any]:
    ws = await env.ws_client(env.hass, access_token=env.token)
    out: list[Any] = []

    async def send(msg: dict[str, Any]) -> dict[str, Any]:
        await ws.send_json_auto_id(msg)
        reply: dict[str, Any] = await ws.receive_json()
        out.append(_norm(reply))
        return reply

    created = await send({"type": "config/auth/create", "name": "Fay"})
    user_id = created["result"]["user"]["id"]
    await send({"type": "config/auth/update", "user_id": user_id, "name": "Fay B"})
    await send({"type": "config/auth/delete", "user_id": user_id})
    await send({"type": "config/core/update", "location_name": "Hut"})
    prefix = "config/auth_provider/homeassistant"
    await send(
        {
            "type": f"{prefix}/admin_change_password",
            "user_id": env.bob_id,
            "password": "right",
        }
    )
    for username in ("bobby", "bob"):
        await send(
            {
                "type": f"{prefix}/admin_change_username",
                "user_id": env.bob_id,
                "username": username,
            }
        )
    await env.hass.async_block_till_done()
    await ws.close()
    return out


async def _config_flows(env: Env) -> list[Any]:
    client = await env.client(access_token=env.token)
    out: list[Any] = []
    base = "/api/config/config_entries"

    async def post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        resp = await client.post(url, json=body)
        reply: dict[str, Any] = await resp.json()
        out.append([resp.status, reply])
        return reply

    created = await post(f"{base}/flow", {"handler": DUMMY})
    entry_id = created["result"]["entry_id"]
    await env.hass.async_block_till_done()
    step = await post(f"{base}/flow", {"handler": DUMMY, "entry_id": entry_id})
    await post(f"{base}/flow/{step['flow_id']}", {})
    await env.hass.async_block_till_done()
    step = await post(f"{base}/options/flow", {"handler": entry_id})
    await post(f"{base}/options/flow/{step['flow_id']}", {})
    await post(f"{base}/subentries/flow", {"handler": [entry_id, "thing"]})
    await env.hass.async_block_till_done()

    ws = await env.ws_client(env.hass, access_token=env.token)
    await ws.send_json_auto_id(
        {"type": "config_entries/update", "entry_id": entry_id, "title": "Renamed"}
    )
    out.append(await ws.receive_json())
    await ws.close()

    resp = await client.delete(f"{base}/entry/{entry_id}")
    out.append([resp.status, await resp.json()])
    await env.hass.async_block_till_done()
    # The options and subentry flows name the entry as their handler.
    return list(_norm(out, frozenset({entry_id})))


SCENARIOS: tuple[Scenario, ...] = (
    _successful_login,
    _wrong_password,
    _wrong_2fa,
    _invalid_token,
    _admin_ws,
    _config_flows,
)


@case_param
async def test_successful_login_unchanged(env: Env, case: str) -> None:
    """A good password still logs in, and its code still gives a working token."""
    step, status, token, api_status, _ = await _compare(env, case, _successful_login)
    assert step["type"] == "create_entry"
    assert status == 200
    assert token["token_type"] == "Bearer"
    assert api_status == 200


@case_param
async def test_wrong_password_unchanged(env: Env, case: str) -> None:
    """A wrong password or unknown user is still refused with `invalid_auth`."""
    for result in await _compare(env, case, _wrong_password):
        assert result["errors"] == {"base": "invalid_auth"}


@case_param
async def test_wrong_2fa_unchanged(env: Env, case: str) -> None:
    """A wrong 2FA code is still refused with `invalid_code`."""
    step, _, result = await _compare(env, case, _wrong_2fa)
    assert step["step_id"] == "mfa"
    assert result["errors"] == {"base": "invalid_code"}


@pytest.mark.parametrize("expected_lingering_timers", [True])
@case_param
async def test_invalid_token_unchanged(env: Env, case: str) -> None:
    """A bad bearer token still gets 401, and a bad WebSocket token `auth_invalid`."""
    # aiohttp re-arms the server heartbeat after close; see the contract test.
    status, _, required, invalid, closed = await _compare(env, case, _invalid_token)
    assert status == 401
    assert required["type"] == "auth_required"
    assert invalid["type"] == "auth_invalid"
    assert closed is WSMsgType.CLOSE


@case_param
async def test_admin_ws_commands_still_work(env: Env, case: str) -> None:
    """`config/auth/create`, `config/core/update` and the other commands succeed."""
    replies = await _compare(env, case, _admin_ws)
    assert all(reply["success"] for reply in replies), replies
    assert env.hass.config.location_name == "Hut"


@case_param
async def test_config_flow_still_works(env: Env, case: str) -> None:
    """Config, reconfigure, options and subentry flows, update and delete work."""
    out = await _compare(env, case, _config_flows)
    *flows, update, delete = out
    assert [(status, reply["type"]) for status, reply in flows] == [
        (200, "create_entry"),
        (200, "form"),
        (200, "abort"),
        (200, "form"),
        (200, "create_entry"),
        (200, "create_entry"),
    ]
    assert update["success"]
    assert delete == [200, {"require_restart": False}]


@pytest.mark.parametrize("expected_lingering_timers", [True])
@case_param
async def test_failing_entry_point_is_reached(env: Env, case: str) -> None:
    """The scenarios really run every patched entry point (not a vacuous pass)."""
    with _failing(case) as calls:
        # Inside: the record listeners are bound at setup.
        await _setup_fence(env.hass)
        for scenario in SCENARIOS:
            await scenario(env)
        await flush_worker(env.hass)
        await env.hass.async_block_till_done()
    assert calls[0] > 0
