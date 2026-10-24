"""Describe Electric Fence events in the logbook."""

from collections.abc import Callable

from homeassistant.components.logbook import (
    LOGBOOK_ENTRY_ICON,
    LOGBOOK_ENTRY_MESSAGE,
    LOGBOOK_ENTRY_NAME,
)
from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN
from .models import LoginReason
from .notifier import (
    EVENT_LOGIN_FAILED,
    EVENT_LOGIN_SUCCEEDED,
    EVENT_SETTINGS_CHANGED,
    REASON_TEXT,
)

NAME = "Electric Fence"


def _text(value: object, default: str = "unknown") -> str:
    """Return event data as plain one-line text."""
    return " ".join(str(value).split()) if value not in (None, "") else default


def _reason(value: object) -> str:
    try:
        return REASON_TEXT.get(LoginReason(str(value)), _text(value))
    except ValueError:
        return _text(value)


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, str]]], None],
) -> None:
    """Describe logbook events."""

    @callback
    def describe_failed(event: Event) -> dict[str, str]:
        data = event.data
        # The event carries the masked username only.
        who = f"for {_text(data.get('username'))} " if data.get("username") else ""
        message = (
            f"failed login {who}({_reason(data.get('reason'))}) "
            f"from {_text(data.get('ip'))}"
        )
        if data.get("country"):
            message += f", {_text(data.get('country'))}"
        return {
            LOGBOOK_ENTRY_NAME: NAME,
            LOGBOOK_ENTRY_MESSAGE: message,
            LOGBOOK_ENTRY_ICON: "mdi:account-lock",
        }

    @callback
    def describe_succeeded(event: Event) -> dict[str, str]:
        data = event.data
        message = f"{_text(data.get('username'), 'a user')} logged in"
        if data.get("ip"):
            message += f" from {_text(data.get('ip'))}"
        if data.get("mfa_used"):
            message += " with two-factor authentication"
        return {
            LOGBOOK_ENTRY_NAME: NAME,
            LOGBOOK_ENTRY_MESSAGE: message,
            LOGBOOK_ENTRY_ICON: "mdi:login",
        }

    @callback
    def describe_changed(event: Event) -> dict[str, str]:
        data = event.data
        target = _text(data.get("target_name") or data.get("target"))
        by = _text(data.get("changed_by_name"), "System")
        message = (
            f"{_text(data.get('kind'))} {target} {_text(data.get('action'))} by {by}"
        )
        return {
            LOGBOOK_ENTRY_NAME: NAME,
            LOGBOOK_ENTRY_MESSAGE: message,
            LOGBOOK_ENTRY_ICON: "mdi:cog-transfer",
        }

    async_describe_event(DOMAIN, EVENT_LOGIN_FAILED, describe_failed)
    async_describe_event(DOMAIN, EVENT_LOGIN_SUCCEEDED, describe_succeeded)
    async_describe_event(DOMAIN, EVENT_SETTINGS_CHANGED, describe_changed)
