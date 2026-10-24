"""Test helpers shared by Electric Fence tests."""

from pathlib import Path
from typing import Any, cast

from aiohttp import web
from aiohttp.test_utils import TestClient
from pytest_homeassistant_custom_component.common import register_auth_provider

from custom_components.electric_fence.const import DOMAIN
from custom_components.electric_fence.runtime import Runtime
from custom_components.electric_fence.settings import Settings
from custom_components.electric_fence.storage import FenceStore
from custom_components.electric_fence.worker import FenceWorker, NullEnricher
from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_USER
from homeassistant.auth.models import User
from homeassistant.auth.providers.homeassistant import HassAuthProvider
from homeassistant.const import __version__ as HA_VERSION
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


async def flush_worker(hass: HomeAssistant) -> None:
    """Wait until the worker's queue is empty and the pending batch is written."""
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        await entry.runtime_data.worker.async_flush()


def make_runtime(hass: HomeAssistant, store: FenceStore | None = None) -> Runtime:
    """Return a Runtime with a worker that is not started.

    Without `store`, the store is never opened (for tests that don't store).
    """
    if store is None:
        store = FenceStore(Path(hass.config.path(".storage", "unused.db")))
    enricher = NullEnricher()
    return Runtime(
        settings=Settings(),
        store=store,
        worker=FenceWorker(hass, store, enricher),
        enricher=enricher,
        collectors=[],
        ha_version=HA_VERSION,
    )
