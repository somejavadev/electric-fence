"""Contracts the settings change collectors rely on (H4, H5, H6)."""

from enum import StrEnum
from typing import Any

import pytest

from custom_components.electric_fence.contracts import CONTRACTS, check_contracts
from homeassistant import auth, config_entries, const
from homeassistant.auth.providers.homeassistant import HassAuthProvider
from homeassistant.core import HomeAssistant


def _check(hass: HomeAssistant, name: str) -> str | None:
    return CONTRACTS[name].check(hass)


async def test_settings_contracts_pass(
    hass: HomeAssistant, local_auth: HassAuthProvider
) -> None:
    """Core matches every settings contract, with a `homeassistant` provider."""
    for name, collector in (
        ("config_entry_signal", "config_entries"),
        ("user_events", "users"),
        ("hass_provider_password", "users"),
        ("core_config_event", "core_config"),
    ):
        assert CONTRACTS[name].collector == collector
        assert _check(hass, name) is None, name
    for collector in ("config_entries", "users", "core_config"):
        assert check_contracts(hass, collector) == []


async def test_hass_provider_password_not_applicable_without_provider(
    hass: HomeAssistant,
) -> None:
    """Without a `homeassistant` provider the contract is skipped, not failed."""
    assert hass.auth.get_auth_provider("homeassistant", None) is None
    assert _check(hass, "hass_provider_password") is None


async def test_config_entry_signal_contract_fails_when_changed(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing signal or change member fails the contract."""
    with monkeypatch.context() as mp:
        mp.delattr(config_entries, "SIGNAL_CONFIG_ENTRY_CHANGED")
        assert _check(hass, "config_entry_signal") == (
            "config_entries.SIGNAL_CONFIG_ENTRY_CHANGED is missing"
        )

    class _Change(StrEnum):
        ADDED = "added"
        UPDATED = "updated"

    monkeypatch.setattr(config_entries, "ConfigEntryChange", _Change)
    assert _check(hass, "config_entry_signal") == (
        "config_entries.ConfigEntryChange.REMOVED is missing"
    )


async def test_user_events_contract_fails_when_changed(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A renamed user event fails the contract."""
    monkeypatch.setattr(auth, "EVENT_USER_UPDATED", "user_changed")
    assert (
        _check(hass, "user_events") == "auth.EVENT_USER_UPDATED is not 'user_updated'"
    )


async def test_core_config_event_contract_fails_when_changed(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A renamed core config event fails the contract."""
    monkeypatch.setattr(const, "EVENT_CORE_CONFIG_UPDATE", "core_config_changed")
    assert _check(hass, "core_config_event") == (
        "const.EVENT_CORE_CONFIG_UPDATE is not 'core_config_updated'"
    )


async def _change_password_renamed(user: str, new_password: str) -> None:
    """Change password with a renamed first parameter."""


def _change_username_sync(credential: Any, new_username: str) -> None:
    """Sync change username."""


_LABEL = "homeassistant auth provider"


@pytest.mark.parametrize(
    ("attr", "replacement", "reason"),
    [
        (
            "async_change_password",
            _change_password_renamed,
            f"{_LABEL}.async_change_password has an unexpected signature",
        ),
        (
            "async_change_password",
            None,
            f"{_LABEL}.async_change_password is missing",
        ),
        (
            "async_change_username",
            _change_username_sync,
            f"{_LABEL}.async_change_username is not a coroutine function",
        ),
    ],
)
async def test_hass_provider_password_contract_fails_when_changed(
    hass: HomeAssistant,
    local_auth: HassAuthProvider,
    monkeypatch: pytest.MonkeyPatch,
    attr: str,
    replacement: Any,
    reason: str,
) -> None:
    """Changed provider methods fail the contract."""
    monkeypatch.setattr(local_auth, attr, replacement)
    assert _check(hass, "hass_provider_password") == reason
