"""Tests for the config entry diagnostics."""

from datetime import date
import json

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.electric_fence.const import DOMAIN, MIN_HA_VERSION
from custom_components.electric_fence.contracts import CONTRACTS, Contract
from custom_components.electric_fence.settings import Settings
from homeassistant.components.diagnostics import REDACTED
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from tests.helpers import CLIENT_ID, REDIRECT_URI, flush_worker, store_rows

USERNAME = "hunter2-secret"
REMOTE_IP = "203.0.113.7"
ACCOUNT_ID = "424242"
LICENSE_KEY = "license-key-do-not-share"


async def test_diagnostics_redacts_and_has_no_personal_data(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Diagnostics carry status only: no rows, usernames, IPs or credentials."""
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", MIN_HA_VERSION)
    # The test client connects from 127.0.0.1, a trusted proxy here.
    assert await async_setup_component(
        hass,
        "http",
        {"http": {"use_x_forwarded_for": True, "trusted_proxies": ["127.0.0.1"]}},
    )
    settings = Settings(
        geoip_provider="maxmind",
        maxmind_account_id=ACCOUNT_ID,
        maxmind_license_key=LICENSE_KEY,
    )
    entry = MockConfigEntry(domain=DOMAIN, options=settings.to_options())
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    client = await hass_client_no_auth()
    headers = {"X-Forwarded-For": REMOTE_IP}
    for _ in range(2):
        resp = await client.post(
            "/auth/login_flow",
            json={
                "client_id": CLIENT_ID,
                "handler": ["homeassistant", None],
                "redirect_uri": REDIRECT_URI,
            },
            headers=headers,
        )
        step = await resp.json()
        resp = await client.post(
            f"/auth/login_flow/{step['flow_id']}",
            json={"client_id": CLIENT_ID, "username": USERNAME, "password": "x"},
            headers=headers,
        )
        assert (await resp.json())["errors"] == {"base": "invalid_auth"}
    await flush_worker(hass)
    (row,) = store_rows(hass)
    assert (row.username_typed, row.ip) == (USERNAME, REMOTE_IP)

    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    text = json.dumps(diag)
    for secret in (USERNAME, "hunter2", REMOTE_IP, ACCOUNT_ID, LICENSE_KEY):
        assert secret not in text
    assert diag["settings"]["maxmind_license_key"] == REDACTED
    assert diag["settings"]["maxmind_account_id"] == REDACTED
    assert diag["settings"]["geoip_provider"] == "maxmind"
    assert diag["settings"]["retention_days"] == 90
    assert set(diag) == {
        "settings",
        "collectors",
        "contracts",
        "worker",
        "store",
        "geo",
        "ha_version",
    }
    names = [c["name"] for c in diag["collectors"]]
    assert names == [
        "login",
        "auth_failures",
        "attribution",
        "config_entries",
        "users",
        "core_config",
    ]
    login = diag["collectors"][0]
    assert login == {"name": "login", "enabled": True, "reason": None, "errors": 0}
    assert diag["contracts"]
    assert diag["contracts"]["login_flow_manager"] == "ok"
    assert diag["worker"] == {
        "queued": 0,
        "dropped": 0,
        "storage_errors": 0,
        "last_storage_error": None,
    }
    assert diag["store"]["logins"] == 1
    assert diag["store"]["changes"] == 0
    assert diag["store"]["size_bytes"] > 0
    assert diag["geo"] == {"provider": "maxmind", "db_date": None, "last_error": None}
    assert diag["ha_version"] == MIN_HA_VERSION


async def test_diagnostics_reports_disabled_collector_and_failures(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
) -> None:
    """A disabled collector, a failing contract and a broken store are shown."""
    runtime = setup_entry.runtime_data
    login = runtime.collectors[0]
    login.uninstall()
    login.disabled_reason = ["login_flow_manager: async_init is missing"]
    login.guard.errors = 3

    def _raise(_hass: HomeAssistant) -> str | None:
        raise RuntimeError("boom")

    def _stats() -> None:
        raise OSError("disk gone")

    contract = CONTRACTS["login_flow_manager"]
    broken = Contract(contract.name, contract.collector, _raise)
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(CONTRACTS, contract.name, broken)
        mp.setattr(runtime.store, "stats", _stats)
        mp.setattr(runtime, "geo_status", None)
        diag = await get_diagnostics_for_config_entry(hass, hass_client, setup_entry)

    assert diag["collectors"][0] == {
        "name": "login",
        "enabled": False,
        "reason": "login_flow_manager: async_init is missing",
        "errors": 3,
    }
    assert diag["contracts"]["login_flow_manager"] == (
        "login_flow_manager check raised RuntimeError"
    )
    assert diag["store"] == {"error": "OSError"}
    assert diag["geo"] == {"provider": "dbip_lite", "db_date": None, "last_error": None}


async def test_diagnostics_geo_status(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
) -> None:
    """The GeoIP database date and last error are shown."""
    status = setup_entry.runtime_data.geo_status
    assert status is not None
    status.db_date = date(2026, 10, 1)
    status.last_error = "download failed: HTTP 503"
    diag = await get_diagnostics_for_config_entry(hass, hass_client, setup_entry)
    assert diag["geo"] == {
        "provider": "dbip_lite",
        "db_date": "2026-10-01",
        "last_error": "download failed: HTTP 503",
    }


async def test_diagnostics_for_entry_not_loaded(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entry that failed to set up gets its state and reason, not an error."""
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", "2026.10.3")
    entry = MockConfigEntry(domain=DOMAIN, options=Settings().to_options())
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR

    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert diag == {
        "state": "setup_error",
        "ha_version": HA_VERSION,
        "reason": entry.reason,
    }
    assert "requires Home Assistant 2026.11.0" in diag["reason"]
