"""Tests for the logbook descriptions."""

from collections.abc import Callable

import pytest

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


@pytest.mark.parametrize(
    ("kind", "action", "target", "changed", "expected"),
    [
        (
            "backup",
            "updated",
            "Backup",
            ["schedule"],
            "changed backup settings (schedule)",
        ),
        ("backup", "removed", "abc", [], "deleted a backup"),
        ("backup", "restore_requested", "abc", [], "requested a backup restore"),
        (
            "http",
            "updated",
            "HTTP",
            ["login_attempts_threshold"],
            "changed HTTP settings (login_attempts_threshold)",
        ),
        ("http", "withdrawn", "HTTP", [], "withdrew pending HTTP settings"),
        ("http", "confirmed", "HTTP", [], "confirmed HTTP settings"),
        ("network", "updated", "Network", [], "changed network settings"),
        (
            "analytics",
            "updated",
            "Analytics",
            ["base"],
            "changed analytics settings (base)",
        ),
        (
            "logger",
            "updated",
            "Logger",
            ["homeassistant.components.http"],
            "changed log levels (homeassistant.components.http)",
        ),
        ("recorder", "disabled", "Recorder", [], "disabled the recorder"),
        ("recorder", "enabled", "Recorder", [], "enabled the recorder"),
        ("recorder", "purged", "Recorder", [], "purged the recorder"),
    ],
)
async def test_logbook_new_kinds(
    hass: HomeAssistant,
    kind: str,
    action: str,
    target: str,
    changed: list[str],
    expected: str,
) -> None:
    """New kinds read as short sentences, with names and no values."""
    describe = describers(hass)["electric_fence_settings_changed"]
    out = describe(
        Event(
            "electric_fence_settings_changed",
            {
                "kind": kind,
                "action": action,
                "target_name": target,
                "changed": changed,
                "changed_by_name": "Admin",
            },
        )
    )
    assert out[LOGBOOK_ENTRY_MESSAGE] == f"{expected} by Admin"


async def test_logbook_unknown_kind_and_odd_changed(hass: HomeAssistant) -> None:
    """Unknown kinds fall back to raw names; a bad `changed` is ignored."""
    describe = describers(hass)["electric_fence_settings_changed"]
    out = describe(
        Event(
            "electric_fence_settings_changed",
            {"kind": "future", "action": "zap", "target_name": "T", "changed": "x"},
        )
    )
    assert out[LOGBOOK_ENTRY_MESSAGE] == "future T zap by System"
    out = describe(
        Event(
            "electric_fence_settings_changed",
            {"kind": "http", "action": "updated", "changed": [None, "a\nb"]},
        )
    )
    assert out[LOGBOOK_ENTRY_MESSAGE] == "changed HTTP settings (a b) by System"
