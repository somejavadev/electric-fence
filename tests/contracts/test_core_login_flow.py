"""Contracts: Core's login views use the shared `hass.auth.login_flow` (spike 10.2).

These tests characterise Home Assistant Core, not Electric Fence.
"""

import json
from pathlib import Path
import re
from typing import Any
from unittest.mock import patch

from aiohttp import web
import hass_frontend
import pytest
from pytest_homeassistant_custom_component.common import ensure_auth_manager_loaded
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from homeassistant.auth import auth_manager_from_config
from homeassistant.components.http.ban import KEY_FAILED_LOGIN_ATTEMPTS
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from tests.helpers import CLIENT_ID, create_hass_user, login_flow

BUNDLE = Path(hass_frontend.__file__).parent
# `r=await e.json();if(403===e.status)return this._state="error",
# void(this._errorMessage=r.message)`, with any minified names.
FRONTEND_403 = re.compile(
    r"(\w+)=await (\w+)\.json\(\);if\(403===\2\.status\)return "
    r'this\._state="error",void\(this\._errorMessage=\1\.message\)'
)

# The test HTTP client needs real loopback sockets.
pytestmark = pytest.mark.usefixtures("socket_enabled")


async def test_login_views_use_hass_auth_login_flow_instance(
    hass: HomeAssistant, aiohttp_client: ClientSessionGenerator
) -> None:
    """A wrapper on the instance's `async_configure` sees view traffic."""
    await create_hass_user(hass, "alice", "correct-password")
    assert await async_setup_component(hass, "auth", {})
    client = await aiohttp_client(hass.http.app)

    flow_mgr = hass.auth.login_flow
    original = flow_mgr.async_configure
    recorded_user_input: dict[str, Any] = {}

    async def recording_configure(
        flow_id: str, user_input: dict[str, Any] | None = None
    ) -> Any:
        recorded_user_input.update(user_input or {})
        return await original(flow_id, user_input)

    # Installed after the views were registered, as Electric Fence will be.
    flow_mgr.async_configure = recording_configure  # type: ignore[method-assign]

    last_result = await login_flow(client, "alice", "wrong-password")

    assert recorded_user_input["username"] == "alice"
    assert last_result["errors"] == {"base": "invalid_auth"}


async def test_mfa_invalid_code_reported_in_errors(
    hass: HomeAssistant, aiohttp_client: ClientSessionGenerator
) -> None:
    """A wrong TOTP code returns the `mfa` step with `invalid_code`."""
    # Same approach as Core's tests/auth/mfa_modules/test_totp.py.
    hass.auth = await auth_manager_from_config(
        hass, [{"type": "homeassistant"}], [{"type": "totp"}]
    )
    ensure_auth_manager_loaded(hass.auth)
    user = await create_hass_user(hass, "alice", "correct-password")
    await hass.auth.async_enable_user_mfa(user, "totp", {})
    assert await async_setup_component(hass, "auth", {})
    client = await aiohttp_client(hass.http.app)

    password_result = await login_flow(client, "alice", "correct-password")
    assert password_result["step_id"] == "mfa"

    with patch("pyotp.TOTP.verify", return_value=False):
        resp = await client.post(
            f"/auth/login_flow/{password_result['flow_id']}",
            json={"client_id": CLIENT_ID, "code": "000000"},
        )
        last_result = await resp.json()

    assert last_result["step_id"] == "mfa"
    assert last_result["errors"] == {"base": "invalid_code"}


async def test_forbidden_from_configure_passes_through(
    hass: HomeAssistant, aiohttp_client: ClientSessionGenerator
) -> None:
    """`HTTPForbidden` from `async_configure` reaches the client as is.

    Electric Fence refuses a throttled step this way. Core's view neither
    catches it nor counts it as a failed login, and it clears the flow's
    in-progress mark, so the flow can go on later.
    """
    await create_hass_user(hass, "alice", "correct-password")
    assert await async_setup_component(hass, "auth", {})
    client = await aiohttp_client(hass.http.app)
    flow_mgr = hass.auth.login_flow
    original = flow_mgr.async_configure
    body = {"message": "Too many login attempts. Try again later."}

    async def refusing_configure(
        flow_id: str, user_input: dict[str, Any] | None = None
    ) -> Any:
        raise web.HTTPForbidden(text=json.dumps(body), content_type="application/json")

    resp = await client.post(
        "/auth/login_flow",
        json={
            "client_id": CLIENT_ID,
            "handler": ["homeassistant", None],
            "redirect_uri": CLIENT_ID,
        },
    )
    flow_id = (await resp.json())["flow_id"]
    flow_mgr.async_configure = refusing_configure  # type: ignore[method-assign]
    step = {"client_id": CLIENT_ID, "username": "alice", "password": "wrong"}
    resp = await client.post(f"/auth/login_flow/{flow_id}", json=step)
    assert resp.status == 403
    assert resp.content_type == "application/json"
    assert await resp.json() == body
    assert not hass.http.app[KEY_FAILED_LOGIN_ATTEMPTS]

    flow_mgr.async_configure = original  # type: ignore[method-assign]
    resp = await client.post(f"/auth/login_flow/{flow_id}", json=step)
    assert resp.status == 200
    assert (await resp.json())["errors"] == {"base": "invalid_auth"}


def test_frontend_shows_message_of_403() -> None:
    """The login page shows a 403 step response's `message` as its error.

    Source: `src/auth/ha-auth-flow.ts`, `_handleSubmit`; matched in the
    minified bundle Core serves.
    """
    bundles = sorted((BUNDLE / "frontend_latest").glob("authorize.*.js"))
    assert bundles
    text = "".join(path.read_text(encoding="utf-8") for path in bundles)
    assert FRONTEND_403.search(text)
