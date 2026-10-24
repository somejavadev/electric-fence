"""Contracts: Core's login views use the shared `hass.auth.login_flow` (spike 10.2).

These tests characterise Home Assistant Core, not Electric Fence.
"""

from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import ensure_auth_manager_loaded
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from homeassistant.auth import auth_manager_from_config
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from tests.helpers import CLIENT_ID, create_hass_user, login_flow

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

    assert last_result["step_id"] == "mfa" and last_result["errors"] == {
        "base": "invalid_code"
    }
