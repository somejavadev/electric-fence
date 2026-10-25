"""Tests for the blocking backends."""

import asyncio
from datetime import UTC, datetime
from ipaddress import ip_address
from pathlib import Path
import stat
import threading
import time
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.electric_fence.protection.backend import (
    BlockingBackend,
    CoreBanBackend,
)
from custom_components.electric_fence.protection.network import normalise_ip
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
    mapped = ip_address("::ffff:203.0.113.9")
    hass.http.app[KEY_FAILED_LOGIN_ATTEMPTS][IP] = 9
    hass.http.app[KEY_FAILED_LOGIN_ATTEMPTS][mapped] = 4
    assert await backend.async_remove(IP) is True
    assert IP not in hass.http.app[KEY_FAILED_LOGIN_ATTEMPTS]
    assert mapped not in hass.http.app[KEY_FAILED_LOGIN_ATTEMPTS]


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


async def test_remove_last_ban_leaves_appendable_file(hass: HomeAssistant) -> None:
    """Core appending after the last removal keeps the file loadable."""
    backend = await _setup(hass)
    await backend.async_add(IP)
    assert await backend.async_remove(IP)
    assert _bans_path(hass).read_text(encoding="utf8") == ""

    other = ip_address("192.0.2.5")
    await backend.async_add(other)
    assert str(other) in _file_ips(hass)


async def test_concurrent_removes(hass: HomeAssistant) -> None:
    """Two removals at once leave both addresses gone and the file valid."""
    backend = await _setup(hass)
    await backend.async_add(IP)
    await backend.async_add(FOREIGN)
    results = await asyncio.gather(
        backend.async_remove(IP), backend.async_remove(FOREIGN)
    )
    assert results == [True, True]
    assert backend.list_ips() == {}
    assert _file_ips(hass) == {}
    assert [p.name for p in Path(hass.config.path()).glob("ip_bans.yaml*")] == [
        "ip_bans.yaml"
    ]


async def test_add_bans_mapped_form_too(hass: HomeAssistant) -> None:
    """An IPv4 ban also covers the mapped form, listed once and removed together."""
    backend = await _setup(hass)
    mapped = ip_address("::ffff:203.0.113.9")
    lookup = hass.http.app[KEY_BAN_MANAGER].ip_bans_lookup
    await backend.async_add(IP)
    assert IP in lookup
    assert mapped in lookup
    assert list(backend.list_ips()) == [IP]

    assert await backend.async_remove(IP)
    assert IP not in lookup
    assert mapped not in lookup
    assert _file_ips(hass) == {}


async def test_rewrite_keeps_file_mode(hass: HomeAssistant) -> None:
    """A rewrite keeps the file's mode, not the temporary file's 0600."""
    backend = await _setup(hass)
    await backend.async_add(IP)
    path = _bans_path(hass)
    path.chmod(0o640)
    assert await backend.async_remove(IP)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


async def test_rewrite_new_file_is_readable(hass: HomeAssistant) -> None:
    """Without a file, the written one gets the usual 0644."""
    backend = await _setup(hass)
    lookup = hass.http.app[KEY_BAN_MANAGER].ip_bans_lookup
    lookup[FOREIGN] = IpBan(FOREIGN)
    lookup[IP] = IpBan(IP)
    path = _bans_path(hass)
    assert not path.exists()
    assert await backend.async_remove(IP)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert list(_file_ips(hass)) == [str(FOREIGN)]


async def test_concurrent_add_and_remove(hass: HomeAssistant) -> None:
    """A ban added during a rewrite is still in the file afterwards."""
    backend = await _setup(hass)
    kept = ip_address("192.0.2.5")
    await backend.async_add(kept)
    await backend.async_add(FOREIGN)
    original_dump = yaml_util.dump
    dumping = threading.Event()

    def slow_dump(data: Any) -> str:
        # Between reading the file and replacing it: Core's append would
        # land on the file that is about to be replaced.
        dumping.set()
        time.sleep(0.2)
        return original_dump(data)

    with patch.object(yaml_util, "dump", slow_dump):
        remove = asyncio.ensure_future(backend.async_remove(FOREIGN))
        assert await hass.async_add_executor_job(dumping.wait, 5)
        await backend.async_add(IP)
        assert await remove
    assert set(backend.list_ips()) == {IP, kept}
    assert {normalise_ip(k) for k in _file_ips(hass)} == {IP, kept}
