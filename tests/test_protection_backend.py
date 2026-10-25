"""Tests for the blocking backends."""

from datetime import UTC, datetime
from ipaddress import ip_address
from pathlib import Path
from typing import Any

import pytest

from custom_components.electric_fence.protection.backend import (
    BlockingBackend,
    CoreBanBackend,
)
from homeassistant.components.http.ban import (
    KEY_BAN_MANAGER,
    KEY_FAILED_LOGIN_ATTEMPTS,
    IpBan,
)
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import yaml as yaml_util

IP = ip_address("203.0.113.9")
FOREIGN = ip_address("198.51.100.1")
FOREIGN_AT = "2026-01-02T03:04:05+00:00"


async def _setup(hass: HomeAssistant, enabled: bool = True) -> CoreBanBackend:
    config: dict[str, Any] = {"ip_ban_enabled": enabled}
    assert await async_setup_component(hass, "http", {"http": config})
    await hass.async_block_till_done()
    return CoreBanBackend(hass)


def _bans_path(hass: HomeAssistant) -> Path:
    return Path(hass.config.path("ip_bans.yaml"))


def _file_ips(hass: HomeAssistant) -> dict[str, Any]:
    return yaml_util.load_yaml_dict(str(_bans_path(hass)))


async def test_add_and_remove(hass: HomeAssistant) -> None:
    """A ban reaches memory and the file; removal clears both."""
    backend = await _setup(hass)
    assert isinstance(backend, BlockingBackend)
    assert backend.supports("ip")
    assert backend.health() is None

    await backend.async_add(IP)
    assert IP in backend.list_ips()
    assert str(IP) in _file_ips(hass)

    assert await backend.async_remove(IP) is True
    assert IP not in backend.list_ips()
    assert str(IP) not in _file_ips(hass)


async def test_add_normalises_mapped_address(hass: HomeAssistant) -> None:
    """An IPv4-mapped IPv6 address bans the IPv4 address."""
    backend = await _setup(hass)
    await backend.async_add(ip_address("::ffff:203.0.113.9"))
    assert list(backend.list_ips()) == [IP]


async def test_remove_keeps_foreign_entries(hass: HomeAssistant) -> None:
    """Entries Electric Fence did not write survive, unchanged."""
    backend = await _setup(hass)
    _bans_path(hass).write_text(
        f'"{FOREIGN}":\n  banned_at: "{FOREIGN_AT}"\n', encoding="utf8"
    )
    await backend.async_add(IP)
    assert await backend.async_remove(IP)
    entries = _file_ips(hass)
    assert list(entries) == [str(FOREIGN)]
    assert entries[str(FOREIGN)]["banned_at"] == FOREIGN_AT


async def test_remove_restores_memory_entries_missing_from_file(
    hass: HomeAssistant,
) -> None:
    """A ban only held in memory is written back by any removal."""
    backend = await _setup(hass)
    manager = hass.http.app[KEY_BAN_MANAGER]
    only_memory = ip_address("192.0.2.77")
    when = datetime(2026, 5, 6, 7, 8, 9, tzinfo=UTC)
    manager.ip_bans_lookup[only_memory] = IpBan(only_memory, when)
    manager.ip_bans_lookup[IP] = IpBan(IP)

    assert await backend.async_remove(IP)
    entries = _file_ips(hass)
    assert list(entries) == [str(only_memory)]
    assert entries[str(only_memory)]["banned_at"] == when.isoformat()


@pytest.mark.parametrize("content", ["[unclosed", "- a\n- b\n"])
async def test_remove_malformed_file_untouched(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, content: str
) -> None:
    """A malformed or non-mapping file is not rewritten; memory still changes."""
    backend = await _setup(hass)
    manager = hass.http.app[KEY_BAN_MANAGER]
    manager.ip_bans_lookup[IP] = IpBan(IP)
    path = _bans_path(hass)
    path.write_text(content, encoding="utf8")
    before = path.read_bytes()

    assert await backend.async_remove(IP) is True
    assert path.read_bytes() == before
    assert IP not in manager.ip_bans_lookup
    assert "malformed" in caplog.text
    assert content not in caplog.text
    assert not path.with_name("ip_bans.yaml.tmp").exists()


async def test_remove_missing_file(hass: HomeAssistant) -> None:
    """A missing file is treated as empty."""
    backend = await _setup(hass)
    hass.http.app[KEY_BAN_MANAGER].ip_bans_lookup[IP] = IpBan(IP)
    assert not _bans_path(hass).exists()
    assert await backend.async_remove(IP)
    assert _file_ips(hass) == {}


async def test_remove_resets_core_counter(hass: HomeAssistant) -> None:
    """Unbanning clears Core's failed-login counter for the address."""
    backend = await _setup(hass)
    hass.http.app[KEY_FAILED_LOGIN_ATTEMPTS][IP] = 9
    await backend.async_remove(IP)
    assert IP not in hass.http.app[KEY_FAILED_LOGIN_ATTEMPTS]


async def test_health_ip_ban_disabled(hass: HomeAssistant) -> None:
    """Without Core's ban manager the backend reports why."""
    backend = await _setup(hass, enabled=False)
    assert backend.health() == "ip_ban_disabled"
    assert backend.list_ips() == {}
    await backend.async_add(IP)
    assert await backend.async_remove(IP) is False


async def test_reads_replaced_lookup(hass: HomeAssistant) -> None:
    """Core replacing `ip_bans_lookup` is seen on the next call."""
    backend = await _setup(hass)
    manager = hass.http.app[KEY_BAN_MANAGER]
    manager.ip_bans_lookup = {IP: IpBan(IP)}
    assert IP in backend.list_ips()
