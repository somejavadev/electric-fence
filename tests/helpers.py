"""Test helpers shared by Electric Fence tests."""

from typing import Any, cast

from aiohttp import web
from aiohttp.test_utils import TestClient
from pytest_homeassistant_custom_component.common import register_auth_provider

from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_USER
from homeassistant.auth.models import User
from homeassistant.auth.providers.homeassistant import HassAuthProvider
from homeassistant.core import HomeAssistant

CLIENT_ID = "http://127.0.0.1:8123/"
REDIRECT_URI = "http://127.0.0.1:8123/"


async def create_hass_user(
    hass: HomeAssistant, username: str, password: str, *, admin: bool = True
) -> User:
    """Create a user with a `homeassistant` provider credential.

    The `homeassistant` auth provider is registered first if it is missing.
    """
    provider = hass.auth.get_auth_provider("homeassistant", None)
    if provider is None:
        provider = await register_auth_provider(hass, {"type": "homeassistant"})
    assert isinstance(provider, HassAuthProvider)
    await provider.async_add_auth(username, password)
    credentials = await provider.async_get_or_create_credentials({"username": username})
    user = await hass.auth.async_create_user(
        username, group_ids=[GROUP_ID_ADMIN if admin else GROUP_ID_USER]
    )
    await hass.auth.async_link_user(user, credentials)
    return user


async def login_flow(
    client: TestClient[web.Request, web.Application], username: str, password: str
) -> dict[str, Any]:
    """Run a `homeassistant` provider login flow over HTTP.

    Returns the JSON of the last response (the username and password step).
    """
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
        json={"client_id": CLIENT_ID, "username": username, "password": password},
    )
    return cast(dict[str, Any], await resp.json())
