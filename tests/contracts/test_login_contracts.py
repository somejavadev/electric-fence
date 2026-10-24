"""Contracts the login collector relies on (H1, H8)."""

from typing import Any

import pytest

from custom_components.electric_fence.contracts import CONTRACTS, check_contracts
from homeassistant.core import HomeAssistant


def _check(hass: HomeAssistant, name: str) -> str | None:
    return CONTRACTS[name].check(hass)


async def test_login_flow_manager_contract_passes(hass: HomeAssistant) -> None:
    """Core's login flow manager has the methods the collector wraps."""
    assert CONTRACTS["login_flow_manager"].collector == "login"
    assert _check(hass, "login_flow_manager") is None


async def test_refresh_token_create_contract_passes(hass: HomeAssistant) -> None:
    """Core's refresh token creation has the expected signature."""
    assert CONTRACTS["refresh_token_create"].collector == "login"
    assert _check(hass, "refresh_token_create") is None
    assert check_contracts(hass, "login") == []


async def _init_without_context(handler: Any, data: Any = None) -> None:
    """Async init without the keyword-only `context`."""


async def _configure_renamed(flow: str, user_input: Any = None) -> None:
    """Async configure with a renamed first parameter."""


def _configure_sync(flow_id: str, user_input: Any = None) -> None:
    """Sync configure."""


async def _get_async(flow_id: str) -> None:
    """Async-only lookup with the right parameter."""


def _get_no_args() -> None:
    """Lookup without a flow ID."""


@pytest.mark.parametrize(
    ("attr", "replacement", "expected"),
    [
        ("async_init", _init_without_context, "async_init"),
        ("async_configure", _configure_renamed, "async_configure"),
        ("async_configure", _configure_sync, "not a coroutine function"),
        ("async_get", _get_no_args, "async_get"),
        ("async_get", None, "async_get"),
    ],
)
async def test_contract_fails_when_signature_changes(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    attr: str,
    replacement: Any,
    expected: str,
) -> None:
    """A changed login flow method makes the contract fail with a reason."""
    monkeypatch.setattr(hass.auth.login_flow, attr, replacement)
    reason = _check(hass, "login_flow_manager")
    assert reason is not None
    assert expected in reason


async def test_contract_accepts_sync_or_async_get(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`async_get` only has to take a flow ID."""
    monkeypatch.setattr(hass.auth.login_flow, "async_get", _get_async)
    assert _check(hass, "login_flow_manager") is None


async def test_contract_fails_without_login_flow(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing login flow manager is reported."""
    monkeypatch.setattr(hass.auth, "login_flow", None)
    reason = _check(hass, "login_flow_manager")
    assert reason is not None
    assert "login_flow" in reason


async def _token_wrong_first(owner: Any, client_id: Any, client_name: Any) -> None:
    """First parameter not called `user`."""


async def _token_no_client_name(user: Any, client_id: Any = None) -> None:
    """Missing `client_name`."""


def _token_sync(user: Any, client_id: Any = None, client_name: Any = None) -> None:
    """Not a coroutine function."""


async def _token_no_params() -> None:
    """No parameters at all."""


@pytest.mark.parametrize(
    "replacement",
    [_token_wrong_first, _token_no_client_name, _token_sync, _token_no_params, None],
)
async def test_refresh_token_contract_fails_when_signature_changes(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, replacement: Any
) -> None:
    """A changed refresh token method makes the contract fail with a reason."""
    monkeypatch.setattr(hass.auth, "async_create_refresh_token", replacement)
    reason = _check(hass, "refresh_token_create")
    assert reason is not None
    assert "async_create_refresh_token" in reason
