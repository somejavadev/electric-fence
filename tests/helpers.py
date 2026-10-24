"""Test helpers shared by Electric Fence tests."""

from pathlib import Path
from typing import Any, cast

from aiohttp import web
from aiohttp.test_utils import TestClient
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    mock_integration,
    register_auth_provider,
)

from custom_components.electric_fence.attribution import (
    SCOPE,
    current_actor,
    direct_request_actor,
)
from custom_components.electric_fence.const import DOMAIN
from custom_components.electric_fence.runtime import Runtime
from custom_components.electric_fence.settings import Settings
from custom_components.electric_fence.storage import (
    ChangeFilter,
    ChangeRow,
    FenceStore,
    LoginFilter,
    LoginRow,
)
from custom_components.electric_fence.worker import FenceWorker, NullEnricher
from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_USER
from homeassistant.auth.models import User
from homeassistant.auth.providers.homeassistant import HassAuthProvider
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.http import current_request

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


def store_rows(hass: HomeAssistant) -> list[LoginRow]:
    """Return all stored login rows of the loaded entry, newest first."""
    rows: list[LoginRow] = []
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        rows.extend(entry.runtime_data.store.query_logins(LoginFilter(), limit=500))
    return rows


def change_rows(hass: HomeAssistant) -> list[ChangeRow]:
    """Return all stored settings changes of the loaded entry, oldest first."""
    rows: list[ChangeRow] = []
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        rows.extend(
            entry.runtime_data.store.query_changes(
                ChangeFilter(include_system=True), limit=500
            )
        )
    return rows[::-1]


def dump_db(hass: HomeAssistant) -> str:
    """Return the text of the database file and its WAL, for leak checks."""
    base = Path(hass.config.path(".storage", "electric_fence.db"))
    paths = (base, base.with_name(base.name + "-wal"))
    return "\n".join(
        p.read_bytes().decode(errors="replace") for p in paths if p.exists()
    )


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


# The `ef_dummy` test integration: a config flow with reconfigure, options
# and subentry flows, whose setup updates its own entry.

DUMMY = "ef_dummy"


class _Probe:
    """What the dummy integration saw (reset per test)."""

    spawned: dict[str, Any]

    def __init__(self) -> None:
        self.spawned = {}


PROBE = _Probe()


async def _probe_spawned_task() -> None:
    """Record what a task spawned inside the flow's request sees."""
    PROBE.spawned["request"] = current_request.get()
    PROBE.spawned["direct_actor"] = direct_request_actor()
    PROBE.spawned["actor"] = current_actor()
    PROBE.spawned["scope"] = SCOPE.get()


class _DummyOptionsFlow(OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="init")
        return self.async_create_entry(data={"opt": True})


class _DummySubentryFlow(ConfigSubentryFlow):
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return self.async_create_entry(title="Thing", data={})


class DummyFlow(ConfigFlow):
    """Config flow of `ef_dummy`."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create an entry, probing a task spawned in the request."""
        await self.hass.async_create_task(_probe_spawned_task())
        return self.async_create_entry(title="Dummy", data={})

    async def async_step_integration_discovery(
        self, discovery_info: dict[str, Any]
    ) -> ConfigFlowResult:
        """Create an entry for a discovery."""
        return self.async_create_entry(title="Discovered", data={})

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Merge the input into the entry's data, and reload."""
        if user_input is None:
            return self.async_show_form(step_id="reconfigure")
        return self.async_update_reload_and_abort(
            self._get_reconfigure_entry(),
            data_updates={"reconfigured": True, **user_input},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return _DummyOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return the subentry flows."""
        return {"thing": _DummySubentryFlow}


async def _dummy_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update the own entry during every setup, as integrations do.

    An entry with `quiet` in its data leaves itself alone.
    """
    if entry.data.get("quiet"):
        return
    setups = entry.data.get("setups", 0) + 1
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "touched": True, "setups": setups}
    )


async def _dummy_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Unload (HA 2026.11 rejects a boolean result)."""


async def _reloading_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Set up like `ef_dummy`, and reload when the options change."""
    options = dict(entry.options)

    async def _on_update(hass: HomeAssistant, entry: ConfigEntry) -> None:
        if entry.options != options:
            await hass.config_entries.async_reload(entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(_on_update))
    await _dummy_setup_entry(hass, entry)


def mock_dummy(hass: HomeAssistant, *, reloading: bool = False) -> None:
    """Register the `ef_dummy` integration (reloading on options change)."""
    mock_integration(
        hass,
        MockModule(
            DUMMY,
            async_setup_entry=_reloading_setup_entry
            if reloading
            else _dummy_setup_entry,
            async_unload_entry=_dummy_unload_entry,
        ),
    )


async def dummy_entry(
    hass: HomeAssistant, data: dict[str, Any] | None = None
) -> MockConfigEntry:
    """Add and set up an `ef_dummy` entry."""
    entry = MockConfigEntry(domain=DUMMY, data=data or {})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
