"""Tests for record types."""

from custom_components.electric_fence.models import (
    ChangeAction,
    ChangeKind,
    SettingsChange,
)
from homeassistant.util.dt import utcnow


def _change(actor_user_id: str | None) -> SettingsChange:
    return SettingsChange(
        ts=utcnow(),
        kind=ChangeKind.USER,
        action=ChangeAction.ADDED,
        target_id="u",
        target_name="u",
        details={},
        actor_user_id=actor_user_id,
        actor_name=None,
        actor_ip=None,
    )


def test_settings_change_is_system():
    """Check settings change is system."""
    assert _change(None).is_system
    assert not _change("abc").is_system
