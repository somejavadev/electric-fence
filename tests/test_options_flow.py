"""Tests for the Electric Fence options flow and live settings."""

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

import probatio
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.electric_fence.geo.maxmind import DOWNLOAD_URL
from custom_components.electric_fence.settings import Settings
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from tests.test_worker import failed, logins

GENERAL = {
    "notify_failed_login": True,
    "ha_login_notification": "replace",
    "retention_days": 90,
    "geoip_provider": "dbip_lite",
    "reverse_dns": True,
}


async def _init(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    return result


async def test_options_defaults_shown(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """The init form is prefilled with the current (default) settings."""
    result = await _init(hass, setup_entry)
    assert result["step_id"] == "init"
    defaults = {str(k): k.default() for k in result["data_schema"].schema if k.default}
    assert defaults == GENERAL


async def test_options_update_applies_without_reload(
    hass: HomeAssistant, setup_entry: MockConfigEntry, geo_start: AsyncMock
) -> None:
    """Changing options updates the runtime and leaves the hooks installed."""
    runtime = setup_entry.runtime_data
    login_flow = hass.auth.login_flow
    init_hook = login_flow.async_init
    configure_hook = login_flow.async_configure
    result = await _init(hass, setup_entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {**GENERAL, "notify_failed_login": False, "geoip_provider": "none"},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert setup_entry.runtime_data is runtime
    assert runtime.settings.notify_failed_login is False
    assert runtime.settings.geoip_provider == "none"
    assert login_flow.async_init is init_hook
    assert login_flow.async_configure is configure_hook
    assert runtime.geo_status is not None
    assert runtime.geo_status.provider == "none"


async def test_maxmind_step_validates_credentials(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Rejected credentials keep the form open with an error."""
    aioclient_mock.head(DOWNLOAD_URL, status=401)
    result = await _init(hass, setup_entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**GENERAL, "geoip_provider": "maxmind"}
    )
    assert result["step_id"] == "maxmind"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"maxmind_account_id": "123", "maxmind_license_key": "secret-key"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_maxmind_credentials"}
    assert "secret-key" not in str(result["errors"])


async def test_maxmind_network_error_cannot_connect(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A server error maps to cannot_connect."""
    aioclient_mock.head(DOWNLOAD_URL, status=500)
    result = await _init(hass, setup_entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**GENERAL, "geoip_provider": "maxmind"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"maxmind_account_id": "123", "maxmind_license_key": "k"},
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_maxmind_valid_credentials_saved(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Accepted credentials are stored in the entry options."""
    aioclient_mock.head(DOWNLOAD_URL, status=302)
    result = await _init(hass, setup_entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**GENERAL, "geoip_provider": "maxmind"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"maxmind_account_id": "123", "maxmind_license_key": "secret-key"},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert setup_entry.options["maxmind_account_id"] == "123"
    assert setup_entry.options["maxmind_license_key"] == "secret-key"
    assert setup_entry.options["geoip_provider"] == "maxmind"
    assert setup_entry.runtime_data.settings.geoip_provider == "maxmind"


async def test_switch_away_from_maxmind_clears_credentials(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Choosing another provider drops the stored credentials."""
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", "2026.11.0")
    entry = MockConfigEntry(
        domain="electric_fence",
        options=Settings(
            geoip_provider="maxmind",
            maxmind_account_id="123",
            maxmind_license_key="secret-key",
        ).to_options(),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    result = await _init(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**GENERAL, "geoip_provider": "dbip_lite"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["maxmind_account_id"] is None
    assert entry.options["maxmind_license_key"] is None
    assert entry.runtime_data.settings.maxmind_license_key is None


@pytest.mark.parametrize("days", [0, 3651])
async def test_retention_out_of_range_rejected(
    hass: HomeAssistant, setup_entry: MockConfigEntry, days: int
) -> None:
    """The form refuses retention outside 1..3650."""
    result = await _init(hass, setup_entry)
    with pytest.raises(probatio.Invalid):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {**GENERAL, "retention_days": days}
        )


async def test_shorter_retention_purges_now(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Shortening retention deletes older rows immediately."""
    store = setup_entry.runtime_data.store
    old = failed(user="old")
    old.ts = dt_util.utcnow() - timedelta(days=40)
    new = failed(user="new")
    await hass.async_add_executor_job(store.insert, [old, new])
    result = await _init(hass, setup_entry)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {**GENERAL, "retention_days": 30}
    )
    await hass.async_block_till_done()
    rows = await hass.async_add_executor_job(logins, store)
    assert [r.username_typed for r in rows] == ["new"]


async def test_longer_retention_does_not_purge(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Raising retention keeps everything."""
    store = setup_entry.runtime_data.store
    old = failed(user="old")
    old.ts = dt_util.utcnow() - timedelta(days=40)
    await hass.async_add_executor_job(store.insert, [old])
    result = await _init(hass, setup_entry)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {**GENERAL, "retention_days": 365}
    )
    await hass.async_block_till_done()
    assert len(await hass.async_add_executor_job(logins, store)) == 1


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({"retention_days": 0}, 1),
        ({"retention_days": 99999}, 3650),
        ({"retention_days": "x"}, 90),
        ({"retention_days": True}, 90),
        ({"retention_days": 30.0}, 30),
    ],
)
def test_from_options_retention_tolerant(
    options: dict[str, Any], expected: int
) -> None:
    """Retention is clamped, and wrong types fall back to the default."""
    assert Settings.from_options(options).retention_days == expected


def test_from_options_wrong_types_fall_back() -> None:
    """Bad values for other fields fall back to the defaults."""
    s = Settings.from_options(
        {
            "notify_failed_login": "yes",
            "ha_login_notification": "other",
            "geoip_provider": 5,
            "maxmind_account_id": 7,
            "reverse_dns": None,
            "unknown": 1,
        }
    )
    assert s == Settings()
