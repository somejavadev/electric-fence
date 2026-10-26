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


# Sentence for a (kind, action) pair; `{names}` is " (a, b)" or "", and
# `{target}` the target's name (masked by the notifier for unknown usernames).
_CHANGE_TEXT: dict[tuple[str, str], str] = {
    ("backup", "updated"): "changed backup settings{names}",
    ("backup", "removed"): "deleted a backup",
    ("backup", "restore_requested"): "requested a backup restore",
    ("http", "updated"): "changed HTTP settings{names}",
    ("http", "withdrawn"): "withdrew pending HTTP settings",
    ("http", "confirmed"): "confirmed HTTP settings",
    ("network", "updated"): "changed network settings{names}",
    ("analytics", "updated"): "changed analytics settings{names}",
    ("logger", "updated"): "changed log levels{names}",
    ("recorder", "disabled"): "disabled the recorder",
    ("recorder", "enabled"): "enabled the recorder",
    ("recorder", "purged"): "purged the recorder",
    ("ip_ban", "banned"): "banned {target}",
    ("ip_ban", "unbanned"): "unbanned {target}",
    ("ip_ban", "expired"): "ban of {target} expired",
    ("ip_ban", "removed"): "ban of {target} was removed from ip_bans.yaml",
    ("login_protection", "attack_started"): "username {target} came under attack",
    ("login_protection", "attack_ended"): "attack on {target} ended",
    ("login_protection", "slowdown_ended"): "ended the slow-down for {target}",
    ("login_protection", "countries_cleared"): "cleared known login countries",
}
# Electric Fence does these itself: no "by System" without a person.
_SELF_KINDS = frozenset({"ip_ban", "login_protection"})


def _names(value: object) -> str:
    """Return the changed setting names as " (a, b)": names only, no values."""
    if not isinstance(value, list):
        return ""
    names = [_text(n) for n in value if n not in (None, "")]
    return f" ({', '.join(names)})" if names else ""


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
        kind = str(data.get("kind"))
        template = _CHANGE_TEXT.get((kind, str(data.get("action"))))
        if template is not None:
            # Format arguments are never re-parsed, so a hostile name with
            # braces stays text.
            message = template.format(names=_names(data.get("changed")), target=target)
            if kind not in _SELF_KINDS or data.get("changed_by_name"):
                message += f" by {by}"
        else:
            message = (
                f"{_text(data.get('kind'))} {target} "
                f"{_text(data.get('action'))} by {by}"
            )
        return {
            LOGBOOK_ENTRY_NAME: NAME,
            LOGBOOK_ENTRY_MESSAGE: message,
            LOGBOOK_ENTRY_ICON: "mdi:cog-transfer",
        }

    async_describe_event(DOMAIN, EVENT_LOGIN_FAILED, describe_failed)
    async_describe_event(DOMAIN, EVENT_LOGIN_SUCCEEDED, describe_succeeded)
    async_describe_event(DOMAIN, EVENT_SETTINGS_CHANGED, describe_changed)
