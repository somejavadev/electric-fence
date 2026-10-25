"""Tests for the Electric Fence options flow and live settings."""

from datetime import timedelta
import json
from pathlib import Path
import threading
from typing import Any
from unittest.mock import AsyncMock, patch

import probatio
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.electric_fence.geo.maxmind import DOWNLOAD_URL
from custom_components.electric_fence.models import (
    ChangeAction,
    ChangeKind,
    SettingsChange,
)
from custom_components.electric_fence.settings import Settings
from custom_components.electric_fence.storage import ChangeFilter
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, section
from homeassistant.util import dt as dt_util
from tests.test_collector_auth_failures import _bad_bearer, _notifications, _setup
from tests.test_geo import _attempt
from tests.test_worker import failed, logins

SECTIONS = {
    "bans": (
        "auto_ban",
        "ban_threshold",
        "ban_window_minutes",
        "ban_hours",
        "ban_repeat_offenders",
    ),
    "username_protection": (
        "username_protection",
        "attack_threshold",
        "attack_min_ips",
        "attack_window_minutes",
    ),
    "notifications": (
        "notify_failed_login",
        "ha_login_notification",
        "notify_ip_banned",
        "notify_username_attack",
        "notify_new_country",
        "notify_ban_ended",
        "notify_targets",
    ),
}
TOP_LEVEL = (
    "retention_days",
    "geoip_provider",
    "reverse_dns",
    "record_values",
)


def _nest(flat: dict[str, Any]) -> dict[str, Any]:
    """Group flat settings the way the options form submits them."""
    out = {k: flat[k] for k in TOP_LEVEL if k in flat}
    for name, keys in SECTIONS.items():
        out[name] = {k: flat[k] for k in keys if k in flat}
    return out


_DEFAULTS = Settings().to_options()
GENERAL = _nest({k: v for k, v in _DEFAULTS.items() if k != "notify_targets"})
GENERAL["notifications"]["notify_targets"] = []


def _submit(base: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """Return `base` (a nested form) with flat `changes` applied."""
    out = {k: dict(v) if isinstance(v, dict) else v for k, v in base.items()}
    for key, value in changes.items():
        section = next((n for n, keys in SECTIONS.items() if key in keys), None)
        if section is None:
            out[key] = value
        else:
            out[section][key] = value
    return out


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
    schema = result["data_schema"].schema
    defaults: dict[str, Any] = {}
    for key, val in schema.items():
        if isinstance(val, section):
            defaults.update(
                {str(k): k.default() for k in val.schema.schema if k.default}
            )
        else:
            defaults[str(key)] = key.default()
    expected = Settings().to_options()
    for k in ("maxmind_account_id", "maxmind_license_key"):
        del expected[k]
    expected["notify_targets"] = []
    assert defaults == expected
    assert {
        str(k): v.options["collapsed"]
        for k, v in schema.items()
        if isinstance(v, section)
    } == {
        "bans": True,
        "username_protection": True,
        "notifications": False,
    }


async def test_sections_saved_flat(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Section values are stored as flat option keys."""
    result = await _init(hass, setup_entry)
    form = _submit(
        GENERAL,
        ban_threshold=7,
        ban_hours=0,
        auto_ban=False,
        attack_min_ips=4,
        notify_ban_ended=True,
        notify_targets=["notify.mobile_app_phone"],
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], form)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    options = setup_entry.options
    assert not any(isinstance(v, dict) for v in options.values())
    assert options["username_protection"] is True
    assert options["ban_threshold"] == 7
    assert options["ban_hours"] == 0
    assert options["auto_ban"] is False
    assert options["attack_min_ips"] == 4
    assert options["notify_targets"] == ["notify.mobile_app_phone"]
    settings = setup_entry.runtime_data.settings
    assert settings.notify_targets == ("notify.mobile_app_phone",)
    assert settings.notify_ban_ended is True
    assert settings.ban_threshold == 7


@pytest.mark.parametrize(
    ("key", "value"),
    [("ban_threshold", 0), ("ban_threshold", 101), ("attack_threshold", 1)],
)
async def test_section_number_out_of_range_rejected(
    hass: HomeAssistant, setup_entry: MockConfigEntry, key: str, value: int
) -> None:
    """Section numbers keep the Settings ranges."""
    result = await _init(hass, setup_entry)
    with pytest.raises(probatio.Invalid):
        await hass.config_entries.options.async_configure(
            result["flow_id"], _submit(GENERAL, **{key: value})
        )


async def test_non_notify_target_rejected(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """The picker only takes notify entities."""
    result = await _init(hass, setup_entry)
    with pytest.raises(probatio.Invalid):
        await hass.config_entries.options.async_configure(
            result["flow_id"], _submit(GENERAL, notify_targets=["light.kitchen"])
        )


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
        _submit(GENERAL, notify_failed_login=False, geoip_provider="none"),
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
        result["flow_id"], _submit(GENERAL, geoip_provider="maxmind")
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
        result["flow_id"], _submit(GENERAL, geoip_provider="maxmind")
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
        result["flow_id"], _submit(GENERAL, geoip_provider="maxmind")
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
        result["flow_id"], _submit(GENERAL, geoip_provider="dbip_lite")
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
            result["flow_id"], _submit(GENERAL, retention_days=days)
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
        result["flow_id"], _submit(GENERAL, retention_days=30)
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
        result["flow_id"], _submit(GENERAL, retention_days=365)
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


async def _change(hass: HomeAssistant, entry: MockConfigEntry, **changes: Any) -> None:
    """Submit the init step with `changes` over the current general settings."""
    current = Settings.from_options(entry.options).to_options()
    base = _nest({**current, "notify_targets": list(current["notify_targets"])})
    result = await _init(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _submit(base, **changes)
    )
    if result["type"] is FlowResultType.FORM:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"maxmind_account_id": "999", "maxmind_license_key": "other-key"},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


async def test_ha_login_notification_live(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep -> replace dismisses http-login on the next bad token, and back."""
    entry = await _setup(hass, monkeypatch, ha_login_notification="keep")
    client = await hass_client_no_auth()
    await _bad_bearer(client)
    await hass.async_block_till_done()
    assert "http-login" in _notifications(hass)

    await _change(hass, entry, ha_login_notification="replace")
    await _bad_bearer(client)
    await hass.async_block_till_done()
    assert "http-login" not in _notifications(hass)

    await _change(hass, entry, ha_login_notification="keep")
    await _bad_bearer(client)
    await hass.async_block_till_done()
    assert "http-login" in _notifications(hass)


async def test_reverse_dns_off_applies_live(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """With reverse DNS off, the next public-IP record gets no lookup."""
    enricher = setup_entry.runtime_data.enricher
    with patch("socket.gethostbyaddr", return_value=("h.example.net", [], [])) as rdns:
        rec = _attempt("8.8.8.7")
        await enricher.async_enrich(rec)
        assert rec.hostname == "h.example.net"
        rdns.reset_mock()
        await _change(hass, setup_entry, reverse_dns=False)
        rec = _attempt("8.8.8.8")
        await enricher.async_enrich(rec)
    rdns.assert_not_called()
    assert rec.hostname is None


async def test_provider_none_to_dbip_downloads(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Switching from none to DB-IP Lite starts a download, with no reload."""
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", "2026.11.0")
    aioclient_mock.get(
        "https://download.db-ip.com/free/dbip-country-lite-2026-10.mmdb.gz",
        status=404,
    )
    aioclient_mock.get(
        "https://download.db-ip.com/free/dbip-country-lite-2026-09.mmdb.gz",
        status=404,
    )
    entry = MockConfigEntry(
        domain="electric_fence", options=Settings(geoip_provider="none").to_options()
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    updater = entry.runtime_data.geo_updater
    assert updater is not None
    assert aioclient_mock.call_count == 0
    spy = AsyncMock(wraps=updater.async_set_provider)
    monkeypatch.setattr(updater, "async_set_provider", spy)

    await _change(hass, entry, geoip_provider="dbip_lite")
    await updater.async_wait()

    spy.assert_awaited_once()
    assert spy.await_args is not None
    assert spy.await_args.args[0].geoip_provider == "dbip_lite"
    assert aioclient_mock.call_count >= 1
    assert all("db-ip.com" in str(c[1]) for c in aioclient_mock.mock_calls)


async def test_maxmind_credential_change_sets_provider(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """New credentials while staying on MaxMind call async_set_provider."""
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", "2026.11.0")
    aioclient_mock.head(DOWNLOAD_URL, status=302)
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
    updater = entry.runtime_data.geo_updater
    assert updater is not None
    spy = AsyncMock()
    monkeypatch.setattr(updater, "async_set_provider", spy)

    await _change(hass, entry)

    spy.assert_awaited_once()
    assert entry.runtime_data.settings.maxmind_license_key == "other-key"


async def test_non_geo_change_does_not_reset_provider(
    hass: HomeAssistant, setup_entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Toggling an unrelated option leaves the GeoIP provider alone."""
    updater = setup_entry.runtime_data.geo_updater
    assert updater is not None
    spy = AsyncMock()
    monkeypatch.setattr(updater, "async_set_provider", spy)
    await _change(hass, setup_entry, notify_failed_login=False)
    spy.assert_not_awaited()
    assert setup_entry.runtime_data.settings.notify_failed_login is False


async def test_apply_settings_fails_open(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing provider switch is logged by type only; the rest still applies."""
    runtime = setup_entry.runtime_data
    assert runtime.geo_updater is not None
    monkeypatch.setattr(
        runtime.geo_updater,
        "async_set_provider",
        AsyncMock(side_effect=RuntimeError("secret-key")),
    )
    old = failed(user="old")
    old.ts = dt_util.utcnow() - timedelta(days=40)
    await hass.async_add_executor_job(runtime.store.insert, [old])

    await _change(
        hass, setup_entry, geoip_provider="none", retention_days=30, reverse_dns=False
    )

    assert runtime.settings.geoip_provider == "none"
    assert runtime.settings.reverse_dns is False
    assert await hass.async_add_executor_job(logins, runtime.store) == []
    assert "RuntimeError" in caplog.text
    assert "secret-key" not in caplog.text


async def test_retention_stored_as_int(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """The number selector's float is stored as an int."""
    await _change(hass, setup_entry, retention_days=45.0)
    assert setup_entry.options["retention_days"] == 45
    assert isinstance(setup_entry.options["retention_days"], int)


@pytest.mark.parametrize("value", [float("inf"), float("nan")])
def test_from_options_non_finite_retention(value: float) -> None:
    """Inf and NaN fall back to the default."""
    assert Settings.from_options({"retention_days": value}).retention_days == 90


def test_record_values_defaults_off() -> None:
    """Recording values is off unless turned on."""
    assert Settings.from_options({}).record_values is False
    assert Settings().to_options()["record_values"] is False
    assert Settings.from_options({"record_values": "yes"}).record_values is False


async def test_options_flow_shows_record_values(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """The form offers record_values (default off) and stores True."""
    result = await _init(hass, setup_entry)
    keys = {str(k): k for k in result["data_schema"].schema}
    assert keys["record_values"].default() is False
    await _change(hass, setup_entry, record_values=True)
    assert setup_entry.options["record_values"] is True
    assert setup_entry.runtime_data.settings.record_values is True


def _valued_change(name: str) -> SettingsChange:
    """Return a system change that carries recorded values."""
    return SettingsChange(
        ts=dt_util.utcnow(),
        kind=ChangeKind.CORE_CONFIG,
        action=ChangeAction.UPDATED,
        target_id=None,
        target_name=None,
        details={"changed": [name], "values": {name: {"before": 1, "after": 2}}},
        actor_user_id=None,
        actor_name=None,
        actor_ip=None,
    )


async def _stored(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    """Return the stored change details keyed by the first changed name."""
    store = entry.runtime_data.store
    rows = await hass.async_add_executor_job(
        lambda: store.query_changes(ChangeFilter(include_system=True))
    )
    return {r.details["changed"][0]: r.details for r in rows}


async def test_turning_off_strips_values(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Switching record_values off removes values that were stored."""
    runtime = setup_entry.runtime_data
    await _change(hass, setup_entry, record_values=True)
    runtime.worker.submit(_valued_change("a"))
    await runtime.worker.async_flush()
    assert "values" in (await _stored(hass, setup_entry))["a"]

    await _change(hass, setup_entry, record_values=False)
    assert (await _stored(hass, setup_entry))["a"] == {"changed": ["a"]}


async def test_turning_off_strips_rows_still_queued(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row whose write is still pending when the option goes off is stripped."""
    runtime = setup_entry.runtime_data
    await _change(hass, setup_entry, record_values=True)
    real_insert = runtime.store.insert
    release = threading.Event()

    def slow_insert(batch: Any) -> None:
        release.wait(5)
        real_insert(batch)

    monkeypatch.setattr(runtime.store, "insert", slow_insert)
    # The write only finishes after the options change has started, so the
    # strip must wait for it (flush first).
    hass.loop.call_later(0.2, release.set)
    runtime.worker.submit(_valued_change("late"))
    await _change(hass, setup_entry, record_values=False)
    release.set()
    await runtime.worker.async_flush()
    assert (await _stored(hass, setup_entry))["late"] == {"changed": ["late"]}


async def test_values_kept_when_not_turned_off(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Turning the option on, or saving with it still on or off, strips nothing."""
    runtime = setup_entry.runtime_data
    await _change(hass, setup_entry, record_values=True)
    runtime.worker.submit(_valued_change("a"))
    await runtime.worker.async_flush()
    await _change(hass, setup_entry, record_values=True, reverse_dns=False)
    assert "values" in (await _stored(hass, setup_entry))["a"]

    # Already off (nothing recorded under it): a save changes nothing stored.
    await _change(hass, setup_entry, record_values=False)
    runtime.worker.submit(_valued_change("b"))
    await runtime.worker.async_flush()
    await _change(hass, setup_entry, record_values=False, reverse_dns=True)
    assert "values" in (await _stored(hass, setup_entry))["b"]


async def test_setup_strips_values_left_by_a_failed_strip(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """With record_values off, setup strips values a failed strip left behind."""
    runtime = setup_entry.runtime_data
    await _change(hass, setup_entry, record_values=True)
    runtime.worker.submit(_valued_change("a"))
    await runtime.worker.async_flush()
    with patch.object(
        type(runtime.store), "strip_change_values", side_effect=OSError("disk")
    ):
        await _change(hass, setup_entry, record_values=False)
    assert "values" in (await _stored(hass, setup_entry))["a"]

    assert await hass.config_entries.async_reload(setup_entry.entry_id)
    await hass.async_block_till_done()

    assert (await _stored(hass, setup_entry))["a"] == {"changed": ["a"]}


async def test_setup_keeps_values_when_on(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """With record_values on, setup strips nothing."""
    runtime = setup_entry.runtime_data
    await _change(hass, setup_entry, record_values=True)
    runtime.worker.submit(_valued_change("a"))
    await runtime.worker.async_flush()

    assert await hass.config_entries.async_reload(setup_entry.entry_id)
    await hass.async_block_till_done()

    assert "values" in (await _stored(hass, setup_entry))["a"]


async def test_setup_strip_failure_does_not_block_setup(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A strip failing at setup is logged; setup still succeeds."""
    with patch(
        "custom_components.electric_fence.storage.FenceStore.strip_change_values",
        side_effect=OSError("disk"),
    ):
        assert await hass.config_entries.async_reload(setup_entry.entry_id)
        await hass.async_block_till_done()

    assert setup_entry.state.name == "LOADED"
    assert "could not remove stored values: OSError" in caplog.text


async def test_options_description_link_is_a_placeholder(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """The DB-IP link comes from a placeholder: hassfest bans URLs in strings."""
    result = await hass.config_entries.options.async_init(setup_entry.entry_id)
    assert result["description_placeholders"] == {"dbip_url": "https://db-ip.com"}
    strings = json.loads(
        (
            Path(__file__).parents[1]
            / "custom_components/electric_fence/translations/en.json"
        ).read_text()
    )
    description = strings["options"]["step"]["init"]["description"]
    assert "{dbip_url}" in description
    assert "Local network addresses are never banned" in description
    assert "https://" not in json.dumps(strings)
