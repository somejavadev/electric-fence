"""Tests for settings."""

from datetime import timedelta

from custom_components.electric_fence.settings import Settings


def test_settings_defaults() -> None:
    """The protection defaults match the spec."""
    s = Settings()
    assert s.auto_ban is True
    assert s.ban_threshold == 5
    assert s.ban_window_minutes == 15
    assert s.ban_hours == 24
    assert s.ban_repeat_offenders is False
    assert s.username_protection is True
    assert s.attack_threshold == 10
    assert s.attack_min_ips == 3
    assert s.attack_window_minutes == 15
    assert s.notify_ip_banned is True
    assert s.notify_username_attack is True
    assert s.notify_new_country is True
    assert s.notify_ban_ended is False
    assert s.notify_targets == ()


def test_settings_clamps() -> None:
    """Out-of-range and malformed values are clamped or defaulted."""
    s = Settings.from_options(
        {
            "ban_threshold": 0,
            "ban_hours": 99999,
            "attack_min_ips": "x",
            "notify_targets": ["notify.phone", 3, "light.x"],
        }
    )
    assert s.ban_threshold == 1
    assert s.ban_hours == 8760
    assert s.attack_min_ips == 3
    assert s.notify_targets == ("notify.phone",)


def test_settings_round_trip() -> None:
    """Stored options (lists after JSON) parse back to the same settings."""
    s = Settings(notify_targets=("notify.a", "notify.b"), ban_hours=0)
    options = s.to_options()
    options["notify_targets"] = list(options["notify_targets"])
    assert Settings.from_options(options) == s


def test_settings_bad_notify_targets_type() -> None:
    """A non-list notify_targets falls back to none."""
    assert Settings.from_options({"notify_targets": "notify.x"}).notify_targets == ()


def test_trusted_window() -> None:
    """Permanent bans trust a success for 24 hours."""
    assert Settings(ban_hours=0).trusted_window == timedelta(hours=24)
    assert Settings(ban_hours=2).trusted_window == timedelta(hours=2)
