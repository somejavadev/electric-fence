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
