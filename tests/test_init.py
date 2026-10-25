"""Tests for Electric Fence setup and unload."""

import asyncio
from ipaddress import ip_address
from pathlib import Path
import sqlite3
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.electric_fence.collectors.base import BaseCollector
from custom_components.electric_fence.const import DOMAIN, MIN_HA_VERSION
from custom_components.electric_fence.geo.enrich import GeoEnricher
from custom_components.electric_fence.protection.network import normalise_ip
from custom_components.electric_fence.runtime import Runtime
from custom_components.electric_fence.settings import Settings
from custom_components.electric_fence.storage import SCHEMA_VERSION
from homeassistant import config_entries
from homeassistant.components.http.ban import KEY_BAN_MANAGER
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import yaml as yaml_util


async def test_setup_and_unload(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """The entry loads and unloads cleanly."""
    assert setup_entry.state is ConfigEntryState.LOADED
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    assert setup_entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_and_unload_return_none(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setup and unload return None, as HA 2026.11 expects (no bool)."""
    report_usage = Mock()
    monkeypatch.setattr(config_entries, "report_usage", report_usage)
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", MIN_HA_VERSION)
    entry = MockConfigEntry(domain=DOMAIN, options=Settings().to_options())
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    assert await hass.config_entries.async_unload(entry.entry_id)

    assert entry.state is ConfigEntryState.NOT_LOADED
    report_usage.assert_not_called()


async def test_refuses_old_home_assistant(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Setup is refused with a log error on an older Home Assistant."""
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", "2026.10.3")
    entry = MockConfigEntry(domain=DOMAIN, options=Settings().to_options())
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert "requires Home Assistant 2026.11.0" in caplog.text
    # Raised as ConfigEntryError, not returned as False.
    assert "returns a boolean" not in caplog.text
    assert entry.reason is not None
    assert "requires Home Assistant 2026.11.0" in entry.reason


async def test_failed_setup_rolls_back(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure partway through setup rolls back what was started.

    The worker stops, the store closes, and installed collectors are removed.
    """
    events: list[str] = []
    tasks: list[asyncio.Task[Any]] = []
    original = hass.async_create_background_task

    def spy(target: Any, name: str, eager_start: bool = True) -> asyncio.Task[Any]:
        task = original(target, name, eager_start)
        tasks.append(task)
        return task

    monkeypatch.setattr(hass, "async_create_background_task", spy)

    class Good(BaseCollector):
        name = "good"

        def _install(self) -> None:
            events.append("install good")

        def _uninstall(self) -> None:
            events.append("uninstall good")

    class Broken(BaseCollector):
        name = "broken"

        def __init__(self, hass: HomeAssistant, runtime: Runtime) -> None:
            raise RuntimeError("constructor bug")

        def _install(self) -> None:
            pass

        def _uninstall(self) -> None:
            pass

    monkeypatch.setattr(
        "custom_components.electric_fence.COLLECTOR_TYPES", [Good, Broken]
    )
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", MIN_HA_VERSION)
    entry = MockConfigEntry(domain=DOMAIN, options=Settings().to_options())
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert events == ["install good", "uninstall good"]
    assert [t.get_name() for t in tasks] == ["electric_fence worker"]
    assert all(t.done() for t in tasks)
    with pytest.raises(RuntimeError, match="not open"):
        entry.runtime_data.store.stats()


async def test_geo_wired_and_started(
    hass: HomeAssistant, setup_entry: MockConfigEntry, geo_start: AsyncMock
) -> None:
    """Setup uses the real enricher and starts the GeoIP updater."""
    runtime = setup_entry.runtime_data
    assert isinstance(runtime.enricher, GeoEnricher)
    assert runtime.enricher.runtime is runtime
    assert runtime.geo_updater is not None
    assert runtime.geo_status is not None
    assert runtime.geo_status.provider == "dbip_lite"
    geo_start.assert_awaited_once()
    assert await hass.config_entries.async_unload(setup_entry.entry_id)


async def test_geo_start_waits_for_started(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, geo_start: AsyncMock
) -> None:
    """While Home Assistant is still starting, the updater starts on STARTED."""
    hass.set_state(CoreState.starting)
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", MIN_HA_VERSION)
    entry = MockConfigEntry(domain=DOMAIN, options=Settings().to_options())
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    geo_start.assert_not_awaited()
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    geo_start.assert_awaited_once()


async def test_newer_database_refused_with_repair_issue(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database from a newer version stops setup and raises a repair issue."""
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", MIN_HA_VERSION)
    path = Path(hass.config.path(".storage", "electric_fence.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    con.close()
    entry = MockConfigEntry(domain=DOMAIN, options=Settings().to_options())
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.reason is not None
    assert "newer version of Electric Fence" in entry.reason
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "schema_too_new")
    assert issue is not None
    assert issue.translation_key == "schema_too_new"
    assert issue.translation_placeholders == {
        "version": str(SCHEMA_VERSION + 1),
        "supported": str(SCHEMA_VERSION),
    }
    con = sqlite3.connect(path)
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1
    finally:
        con.close()

    # Once the database is usable again, setup works and clears the issue.
    path.unlink()
    assert await hass.config_entries.async_reload(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED
    assert ir.async_get(hass).async_get_issue(DOMAIN, "schema_too_new") is None


async def test_remove_lifts_own_bans_and_clears_issues(
    hass: HomeAssistant, setup_entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removal lifts Electric Fence's active bans only, then deletes the data."""
    bans = setup_entry.runtime_data.bans
    assert bans is not None
    assert await bans.async_ban("203.0.113.9", 5, None, None)
    assert await bans.async_ban("203.0.113.10", 5, None, None)
    assert await bans.async_unban("203.0.113.10", None, None)
    other = ip_address("198.51.100.1")
    await bans.backend.async_add(other)
    ir.async_create_issue(
        hass,
        DOMAIN,
        "collector_disabled_protection",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="collector_disabled",
    )
    ir.async_create_issue(
        hass,
        DOMAIN,
        "ip_ban_disabled",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="ip_ban_disabled",
    )
    db = Path(hass.config.path(".storage", "electric_fence.db"))

    await hass.config_entries.async_remove(setup_entry.entry_id)
    await hass.async_block_till_done()

    lookup = hass.http.app[KEY_BAN_MANAGER].ip_bans_lookup
    # The other ban stays, with its mapped form.
    assert {normalise_ip(k) for k in lookup} == {other}
    bans_file = yaml_util.load_yaml_dict(hass.config.path("ip_bans.yaml"))
    assert {normalise_ip(k) for k in bans_file} == {other}
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, "collector_disabled_protection") is None
    assert registry.async_get_issue(DOMAIN, "ip_ban_disabled") is None
    assert not db.exists()


async def test_remove_fails_open_on_ban_errors(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A database or ban that can't be handled never stops the removal."""
    bans = setup_entry.runtime_data.bans
    assert bans is not None
    assert await bans.async_ban("203.0.113.9", 5, None, None)
    assert await bans.async_ban("203.0.113.10", 5, None, None)
    db = Path(hass.config.path(".storage", "electric_fence.db"))
    monkeypatch.setattr(
        "custom_components.electric_fence.read_active_ban_ips",
        lambda path: ["bad", "203.0.113.9", "203.0.113.10"],
    )
    calls: list[object] = []

    async def remove(self: object, ip: object) -> bool:
        calls.append(ip)
        if len(calls) == 1:
            raise RuntimeError("203.0.113.9")
        return True

    monkeypatch.setattr(
        "custom_components.electric_fence.CoreBanBackend.async_remove", remove
    )
    await hass.config_entries.async_remove(setup_entry.entry_id)
    await hass.async_block_till_done()
    assert calls == [ip_address("203.0.113.9"), ip_address("203.0.113.10")]
    assert "could not lift a ban on removal: RuntimeError" in caplog.text
    assert "203.0.113" not in caplog.text
    assert not db.exists()


async def test_remove_fails_open_on_unreadable_database(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unreadable database is logged by type and the files still go."""

    def boom(path: Path) -> list[str]:
        raise sqlite3.DatabaseError(str(path))

    monkeypatch.setattr("custom_components.electric_fence.read_active_ban_ips", boom)
    db = Path(hass.config.path(".storage", "electric_fence.db"))
    await hass.config_entries.async_remove(setup_entry.entry_id)
    await hass.async_block_till_done()
    assert "could not read its bans on removal: DatabaseError" in caplog.text
    assert not db.exists()
