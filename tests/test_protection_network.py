"""Tests for the exempt-address rules."""

from ipaddress import IPv4Address, ip_network
from types import SimpleNamespace

import pytest

from custom_components.electric_fence.protection import network
from custom_components.electric_fence.protection.network import (
    http_config_value,
    is_exempt,
    normalise_ip,
)
from homeassistant.core import HomeAssistant


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.1.2.3",
        "192.168.1.5",
        "100.64.0.1",
        "fd00::1",
        "fe80::1",
        "::1",
        "::ffff:192.168.1.5",
    ],
)
async def test_exempt_local(hass: HomeAssistant, ip: str) -> None:
    """Local addresses, including IPv4-mapped ones, are exempt."""
    assert is_exempt(hass, ip) is True


@pytest.mark.parametrize("ip", ["203.0.113.9", "::ffff:203.0.113.9"])
async def test_not_exempt_public(hass: HomeAssistant, ip: str) -> None:
    """Public addresses are not exempt, mapped or not."""
    assert is_exempt(hass, ip) is False


@pytest.mark.parametrize("ip", [None, "nope", ""])
async def test_invalid_is_exempt(hass: HomeAssistant, ip: str | None) -> None:
    """An address that cannot be parsed is never banned."""
    assert is_exempt(hass, ip) is True


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
    assert is_exempt(hass, "198.51.100.20") is True
    assert is_exempt(hass, "::ffff:198.51.100.20") is True
    assert is_exempt(hass, "198.51.101.20") is False


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
    assert is_exempt(hass, "198.51.100.20") is True
    assert is_exempt(hass, "203.0.113.9") is False


async def test_exempt_supervisor(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Supervisor address is exempt."""
    monkeypatch.setattr(network, "is_hassio", lambda _hass: True)
    monkeypatch.setattr(network, "get_supervisor_ip", lambda: "172.30.32.2")
    assert is_exempt(hass, "172.30.32.2") is True
    assert is_exempt(hass, "203.0.113.9") is False


async def test_supervisor_ip_public_value_exempt(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Supervisor address is exempt even if not local."""
    monkeypatch.setattr(network, "is_hassio", lambda _hass: True)
    monkeypatch.setattr(network, "get_supervisor_ip", lambda: "203.0.113.50")
    assert is_exempt(hass, "203.0.113.50") is True


async def test_supervisor_lookup_error_not_exempt(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing Supervisor lookup only skips that check."""

    def boom(_hass: HomeAssistant) -> bool:
        raise RuntimeError

    monkeypatch.setattr(network, "is_hassio", boom)
    assert is_exempt(hass, "203.0.113.9") is False


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
