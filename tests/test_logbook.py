"""Tests for the logbook descriptions."""

from collections.abc import Callable

from custom_components.electric_fence.logbook import async_describe_events
from homeassistant.components.logbook import (
    LOGBOOK_ENTRY_ICON,
    LOGBOOK_ENTRY_MESSAGE,
    LOGBOOK_ENTRY_NAME,
)
from homeassistant.core import Event, HomeAssistant


def describers(hass: HomeAssistant) -> dict[str, Callable[[Event], dict[str, str]]]:
    """Return the registered describers by event name."""
    found: dict[str, Callable[[Event], dict[str, str]]] = {}

    def register(
        domain: str, event_type: str, describe: Callable[[Event], dict[str, str]]
    ) -> None:
        assert domain == "electric_fence"
        found[event_type] = describe

    async_describe_events(hass, register)
    return found


async def test_logbook_describes_events(hass: HomeAssistant) -> None:
    """All three events are described in plain text."""
    found = describers(hass)
    assert set(found) == {
        "electric_fence_login_failed",
        "electric_fence_login_succeeded",
        "electric_fence_settings_changed",
    }
    failed = found["electric_fence_login_failed"](
        Event(
            "electric_fence_login_failed",
            {
                "username": "ad•••",
                "reason": "no_such_user",
                "ip": "203.0.113.7",
                "country": "RU",
            },
        )
    )
    assert failed[LOGBOOK_ENTRY_NAME] == "Electric Fence"
    assert failed[LOGBOOK_ENTRY_MESSAGE] == (
        "failed login for ad••• (no such user) from 203.0.113.7, RU"
    )
    assert failed[LOGBOOK_ENTRY_ICON]

    succeeded = found["electric_fence_login_succeeded"](
        Event(
            "electric_fence_login_succeeded",
            {"username": "alice", "ip": "192.168.1.5", "mfa_used": True},
        )
    )
    assert succeeded[LOGBOOK_ENTRY_MESSAGE] == (
        "alice logged in from 192.168.1.5 with two-factor authentication"
    )
    wrong_code = found["electric_fence_login_failed"](
        Event(
            "electric_fence_login_failed",
            {"username": "alice", "reason": "wrong_2fa_code", "ip": "203.0.113.7"},
        )
    )
    assert wrong_code[LOGBOOK_ENTRY_MESSAGE] == (
        "failed login for alice (wrong two-factor code) from 203.0.113.7"
    )

    changed = found["electric_fence_settings_changed"](
        Event(
            "electric_fence_settings_changed",
            {
                "kind": "user",
                "action": "password_changed",
                "target": "u2",
                "target_name": "Bob",
                "changed_by_name": None,
            },
        )
    )
    assert changed[LOGBOOK_ENTRY_MESSAGE] == "user Bob password_changed by System"


async def test_logbook_handles_sparse_and_odd_data(hass: HomeAssistant) -> None:
    """Missing fields and unknown reasons still give plain one-line text."""
    found = describers(hass)
    failed = found["electric_fence_login_failed"](
        Event("electric_fence_login_failed", {"reason": "invalid_token"})
    )
    assert failed[LOGBOOK_ENTRY_MESSAGE] == (
        "failed login (invalid token) from unknown"
    )
    odd = found["electric_fence_login_failed"](
        Event(
            "electric_fence_login_failed",
            {"username": "a\nb", "reason": "new\nreason", "ip": "1.2.3.4"},
        )
    )
    assert (
        odd[LOGBOOK_ENTRY_MESSAGE] == "failed login for a b (new reason) from 1.2.3.4"
    )
    succeeded = found["electric_fence_login_succeeded"](
        Event("electric_fence_login_succeeded", {})
    )
    assert succeeded[LOGBOOK_ENTRY_MESSAGE] == "a user logged in"
