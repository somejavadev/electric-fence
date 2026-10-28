"""Tests for the exempt-address rules."""

from ipaddress import IPv4Address, ip_network
from types import SimpleNamespace

import pytest

from custom_components.electric_fence.protection import network
from custom_components.electric_fence.protection.network import (
    address_key,
    http_config_value,
    is_exempt,
    normalise_ip,
)
from homeassistant.core import HomeAssistant

LOCAL = [
    "10.1.2.3",
    "192.168.1.5",
    "172.17.0.1",
    "100.64.0.1",
    "fd00::1",
    "fe80::1",
    "::ffff:192.168.1.5",
]


@pytest.mark.parametrize("ip", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
@pytest.mark.parametrize("exempt_local", [True, False])
async def test_loopback_always_exempt(
    hass: HomeAssistant, ip: str, exempt_local: bool
) -> None:
    """Loopback is this machine: never counted, whatever the setting."""
    assert is_exempt(hass, ip, exempt_local=exempt_local) is True


@pytest.mark.parametrize("ip", LOCAL)
async def test_local_counted_by_default(hass: HomeAssistant, ip: str) -> None:
    """Local addresses (LAN, Docker, CGNAT/Tailscale, ULA) are not exempt."""
    assert is_exempt(hass, ip, exempt_local=False) is False


@pytest.mark.parametrize("ip", LOCAL)
async def test_local_exempt_when_set(hass: HomeAssistant, ip: str) -> None:
    """The setting brings back the old exemption, IPv4-mapped included."""
    assert is_exempt(hass, ip, exempt_local=True) is True


@pytest.mark.parametrize("ip", ["203.0.113.9", "::ffff:203.0.113.9"])
@pytest.mark.parametrize("exempt_local", [True, False])
async def test_not_exempt_public(
    hass: HomeAssistant, ip: str, exempt_local: bool
) -> None:
    """Public addresses are not exempt, mapped or not."""
    assert is_exempt(hass, ip, exempt_local=exempt_local) is False


@pytest.mark.parametrize("ip", [None, "nope", ""])
async def test_invalid_is_exempt(hass: HomeAssistant, ip: str | None) -> None:
    """An address that cannot be parsed is never banned."""
    assert is_exempt(hass, ip, exempt_local=False) is True


@pytest.mark.parametrize(
    ("ip", "prefix", "key"),
    [
        ("203.0.113.9", 64, "203.0.113.9"),
        ("::ffff:203.0.113.9", 64, "203.0.113.9"),
        ("2001:db8:1:2:aaaa::1", 64, "2001:db8:1:2::/64"),
        ("2001:db8:1:2:bbbb::9", 64, "2001:db8:1:2::/64"),
        ("2001:db8:1:2:aaaa::1", 48, "2001:db8:1::/48"),
        ("2001:db8:1:2:aaaa::1", 128, "2001:db8:1:2:aaaa::1"),
    ],
)
def test_address_key(ip: str, prefix: int, key: str) -> None:
    """IPv4 is counted per address; IPv6 per prefix (128 means per address)."""
    addr = normalise_ip(ip)
    assert addr is not None
    assert address_key(addr, prefix) == key


def test_same_block() -> None:
    """Two addresses share a block when their keys are equal."""
    a, b, c = (normalise_ip(x) for x in ("2001:db8::1", "2001:db8::2", "2001:db9::1"))
    assert a is not None
    assert b is not None
    assert c is not None
    assert address_key(a, 64) == address_key(b, 64) != address_key(c, 64)


@pytest.mark.parametrize(
    "proxies", [["198.51.100.0/24"], [ip_network("198.51.100.0/24")]]
)
async def test_exempt_trusted_proxy(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, proxies: list[object]
) -> None:
    """Addresses in http.trusted_proxies are exempt."""
    monkeypatch.setitem(
        hass.data,
        "http",
        SimpleNamespace(
            active_config_type="stable", stable={"trusted_proxies": proxies}
        ),
    )
    assert is_exempt(hass, "198.51.100.20", exempt_local=False) is True
    assert is_exempt(hass, "::ffff:198.51.100.20", exempt_local=False) is True
    assert is_exempt(hass, "198.51.101.20", exempt_local=False) is False


async def test_bad_trusted_proxies_ignored(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unparsable trusted_proxies entries are skipped."""
    monkeypatch.setitem(
        hass.data,
        "http",
        SimpleNamespace(
            active_config_type="stable",
            stable={"trusted_proxies": ["junk", 5, "198.51.100.0/24"]},
        ),
    )
    assert is_exempt(hass, "198.51.100.20", exempt_local=False) is True
    assert is_exempt(hass, "203.0.113.9", exempt_local=False) is False


async def test_exempt_supervisor(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Supervisor address is exempt."""
    monkeypatch.setattr(network, "is_hassio", lambda _hass: True)
    monkeypatch.setattr(network, "get_supervisor_ip", lambda: "172.30.32.2")
    assert is_exempt(hass, "172.30.32.2", exempt_local=False) is True
    assert is_exempt(hass, "203.0.113.9", exempt_local=False) is False


async def test_supervisor_ip_public_value_exempt(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Supervisor address is exempt even if not local."""
    monkeypatch.setattr(network, "is_hassio", lambda _hass: True)
    monkeypatch.setattr(network, "get_supervisor_ip", lambda: "203.0.113.50")
    assert is_exempt(hass, "203.0.113.50", exempt_local=False) is True


async def test_supervisor_lookup_error_not_exempt(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing Supervisor lookup only skips that check."""

    def boom(_hass: HomeAssistant) -> bool:
        raise RuntimeError

    monkeypatch.setattr(network, "is_hassio", boom)
    assert is_exempt(hass, "203.0.113.9", exempt_local=False) is False


def test_normalise_ip() -> None:
    """Zones are dropped and mapped addresses unmapped."""
    assert normalise_ip("::ffff:203.0.113.9") == IPv4Address("203.0.113.9")
    assert normalise_ip(IPv4Address("1.2.3.4")) == IPv4Address("1.2.3.4")
    assert normalise_ip("fe80::1%eth0") == normalise_ip("fe80::1")
    assert normalise_ip("nope") is None
    assert normalise_ip(None) is None


async def test_http_config_value(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Values come from the active HTTP config."""
    monkeypatch.setitem(
        hass.data,
        "http",
        SimpleNamespace(active_config_type="pending", pending={"a": 1}),
    )
    assert http_config_value(hass, "a") == 1
    assert http_config_value(hass, "b") is None
    monkeypatch.setitem(
        hass.data, "http", SimpleNamespace(active_config_type="default")
    )
    assert http_config_value(hass, "a") is None
