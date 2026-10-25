"""Tests for the protection binary sensors."""

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.electric_fence.models import (
    ChangeAction,
    ChangeKind,
    LoginReason,
    LoginResult,
    SettingsChange,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from tests.helpers import flush_worker

BINARY = "binary_sensor.electric_fence_"


async def test_login_attack_on_off(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """login_attack follows the username guard."""
    guard = setup_entry.runtime_data.username_guard
    assert guard is not None
    assert hass.states.get(f"{BINARY}login_attack").state == "off"
    for i in range(10):
        guard.on_attempt(
            "admin",
            f"198.51.100.{i % 3 + 1}",
            LoginResult.FAILED,
            LoginReason.WRONG_PASSWORD,
        )
    await hass.async_block_till_done()
    assert hass.states.get(f"{BINARY}login_attack").state == "on"

    assert await guard.async_end("admin", None, None)
    await hass.async_block_till_done()
    assert hass.states.get(f"{BINARY}login_attack").state == "off"


async def test_security_warnings_problem(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    freezer: FrozenDateTimeFactory,
) -> None:
    """security_warnings is on while any posture item warns or errors."""
    items: list[dict[str, object]] = [{"id": "x", "level": "ok", "params": {}}]

    async def check(*_args: object) -> list[dict[str, object]]:
        return items

    monkeypatch.setattr(
        "custom_components.electric_fence.binary_sensor.async_check", check
    )
    freezer.tick(timedelta(minutes=16))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(f"{BINARY}security_warnings").state == "off"

    items.append({"id": "y", "level": "warning", "params": {}})
    setup_entry.runtime_data.worker.submit(
        SettingsChange(
            ts=dt_util.utcnow(),
            kind=ChangeKind.USER,
            action=ChangeAction.ADDED,
            target_id=None,
            target_name=None,
            details={},
            actor_user_id=None,
            actor_name=None,
            actor_ip=None,
        )
    )
    await flush_worker(hass)
    await hass.async_block_till_done()
    assert hass.states.get(f"{BINARY}security_warnings").state == "on"
    assert hass.states.get(f"{BINARY}security_warnings").attributes.get("users") is None


async def test_auto_ban_available(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """auto_ban_available mirrors the ban manager."""
    bans = setup_entry.runtime_data.bans
    assert bans is not None
    expected = "on" if bans.available else "off"
    assert hass.states.get(f"{BINARY}auto_ban_available").state == expected


async def test_security_warnings_ignores_failed_check(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An error item (a failed check) does not turn the sensor on."""

    async def check(*_args: object) -> list[dict[str, object]]:
        return [{"id": "x", "level": "error", "params": {}}]

    monkeypatch.setattr(
        "custom_components.electric_fence.binary_sensor.async_check", check
    )
    freezer.tick(timedelta(minutes=16))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(f"{BINARY}security_warnings").state == "off"


async def test_options_change_refreshes_without_time(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Turning auto-ban off live changes both sensors at once."""
    hass.config_entries.async_update_entry(
        setup_entry, options={**setup_entry.options, "geoip_provider": "none"}
    )
    await hass.async_block_till_done()
    assert hass.states.get(f"{BINARY}security_warnings").state == "off"
    assert hass.states.get(f"{BINARY}auto_ban_available").state == "on"

    hass.config_entries.async_update_entry(
        setup_entry, options={**setup_entry.options, "auto_ban": False}
    )
    await hass.async_block_till_done()
    assert hass.states.get(f"{BINARY}security_warnings").state == "on"
