"""Contracts: Core's ban manager has what the blocking backend relies on.

These tests characterise Home Assistant Core, not Electric Fence.
"""

import pytest

from custom_components.electric_fence.contracts import CONTRACTS, check_contracts
from homeassistant.components.http import ban
from homeassistant.core import HomeAssistant


async def test_protection_contracts_pass(hass: HomeAssistant) -> None:
    """Core matches every protection contract."""
    for name in ("ban_manager", "ban_middleware_lookup"):
        assert CONTRACTS[name].collector == "protection"
    assert check_contracts(hass, "protection") == []


async def test_ban_manager_contract_fails_when_key_missing(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A removed app key fails the contract."""
    monkeypatch.delattr(ban, "KEY_FAILED_LOGIN_ATTEMPTS")
    assert CONTRACTS["ban_manager"].check(hass) == (
        "http.ban.KEY_FAILED_LOGIN_ATTEMPTS is missing"
    )


async def test_ban_manager_contract_fails_without_path(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manager without `path` fails the contract."""

    class NoPath:
        def __init__(self, hass: HomeAssistant) -> None:
            self.ip_bans_lookup: dict = {}

        async def async_add_ban(self, remote_addr: object) -> None:
            """Add a ban."""

    monkeypatch.setattr(ban, "IpBanManager", NoPath)
    assert CONTRACTS["ban_manager"].check(hass) == "IpBanManager.path is missing"


async def test_ban_middleware_contract_fails_when_lookup_unused(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A middleware that no longer reads `ip_bans_lookup` fails the contract."""

    async def ban_middleware(request: object, handler: object) -> None:
        """Ban nothing."""

    monkeypatch.setattr(ban, "ban_middleware", ban_middleware)
    assert CONTRACTS["ban_middleware_lookup"].check(hass) == (
        "ban_middleware no longer reads ip_bans_lookup"
    )


@pytest.mark.parametrize("missing", ["ip_address", "banned_at"])
async def test_ban_manager_contract_fails_without_ip_ban_attribute(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """An `IpBan` without `ip_address` or `banned_at` fails the contract."""

    class Bare:
        def __init__(self, ip: object) -> None:
            if missing != "ip_address":
                self.ip_address = ip
            if missing != "banned_at":
                self.banned_at = None

    monkeypatch.setattr(ban, "IpBan", Bare)
    assert CONTRACTS["ban_manager"].check(hass) == f"IpBan.{missing} is missing"


async def test_ban_manager_contract_fails_when_add_ban_is_sync(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A synchronous `async_add_ban` fails the contract."""
    monkeypatch.setattr(ban.IpBanManager, "async_add_ban", lambda self, ip: None)
    assert CONTRACTS["ban_manager"].check(hass) == (
        "IpBanManager.async_add_ban is not a coroutine function"
    )


async def test_ban_manager_contract_fails_without_add_ban(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manager without `async_add_ban` fails the contract."""
    monkeypatch.delattr(ban.IpBanManager, "async_add_ban")
    assert CONTRACTS["ban_manager"].check(hass) == (
        "IpBanManager.async_add_ban is missing"
    )


async def test_ban_manager_contract_fails_without_ip_ban(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing `IpBan` class fails the contract."""
    monkeypatch.delattr(ban, "IpBan")
    assert CONTRACTS["ban_manager"].check(hass) == "IpBan is missing"


async def test_ban_manager_contract_fails_when_lookup_not_dict(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lookup that is no longer a dict fails the contract."""
    original = ban.IpBanManager.__init__

    def init(self: ban.IpBanManager, hass: HomeAssistant) -> None:
        original(self, hass)
        self.ip_bans_lookup = []  # type: ignore[assignment]

    monkeypatch.setattr(ban.IpBanManager, "__init__", init)
    assert CONTRACTS["ban_manager"].check(hass) == (
        "IpBanManager.ip_bans_lookup is not a dict"
    )


async def test_ban_middleware_contract_fails_when_missing(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A removed middleware fails the contract."""
    monkeypatch.delattr(ban, "ban_middleware")
    assert CONTRACTS["ban_middleware_lookup"].check(hass) == (
        "http.ban.ban_middleware is missing"
    )


async def test_ban_middleware_contract_fails_without_source(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A middleware whose source can't be read fails the contract."""
    monkeypatch.setattr(
        "custom_components.electric_fence.contracts.inspect.getsource",
        lambda fn: (_ for _ in ()).throw(OSError()),
    )
    assert CONTRACTS["ban_middleware_lookup"].check(hass) == (
        "the ban_middleware source is unavailable"
    )
