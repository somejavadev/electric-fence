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


def test_settings_address_defaults() -> None:
    """Local addresses are counted, and IPv6 is counted per /64, by default."""
    s = Settings()
    assert s.exempt_local is False
    assert s.ipv6_prefix == 64


def test_settings_address_clamps() -> None:
    """The IPv6 prefix stays between 48 and 128; the flag must be a bool."""
    assert Settings.from_options({"ipv6_prefix": 8}).ipv6_prefix == 48
    assert Settings.from_options({"ipv6_prefix": 200}).ipv6_prefix == 128
    assert Settings.from_options({"ipv6_prefix": "x"}).ipv6_prefix == 64
    assert Settings.from_options({"exempt_local": "yes"}).exempt_local is False
    assert Settings.from_options({"exempt_local": True}).exempt_local is True


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


def test_settings_v03_defaults() -> None:
    """The v0.3 defaults match the spec."""
    s = Settings()
    assert s.verify_files is True
    assert s.ignored_files == ()
    assert s.unmaintained_months == 4
    assert s.integration_repairs is True
    assert s.scan_delay_minutes == 30
    assert s.forward_enabled is False
    assert s.forward_host == ""
    assert s.forward_port == 6514
    assert s.forward_protocol == "tls"
    assert s.forward_ca_pem == ""


def test_settings_v03_clamps() -> None:
    """Out-of-range v0.3 values are clamped, bad entries dropped."""
    s = Settings.from_options(
        {
            "unmaintained_months": 0,
            "scan_delay_minutes": 99999,
            "forward_port": 70000,
            "forward_protocol": "ftp",
            "ignored_files": "hacs_x/data.json\n\n../etc\nbad\n/abs/x\nother/sub/f.txt",
        }
    )
    assert s.unmaintained_months == 1
    assert s.scan_delay_minutes == 1440
    assert s.forward_port == 65535
    assert s.forward_protocol == "tls"
    assert s.ignored_files == ("hacs_x/data.json", "other/sub/f.txt")


def test_settings_ignored_files_list() -> None:
    """A list of entries is accepted as well as a newline string."""
    s = Settings.from_options({"ignored_files": ["a/x", 3, "a/../y"]})
    assert s.ignored_files == ("a/x",)


def test_ignored_for() -> None:
    """Ignored paths are grouped by integration."""
    s = Settings(ignored_files=("a/x.json", "a/sub/y", "b/z"))
    assert s.ignored_for("a") == frozenset({"x.json", "sub/y"})
    assert s.ignored_for("c") == frozenset()


def test_settings_db_size_limit() -> None:
    """The database is capped at 200 MB by default, between 20 and 10000."""
    assert Settings().max_db_mb == 200
    assert Settings.from_options({"max_db_mb": 1}).max_db_mb == 20
    assert Settings.from_options({"max_db_mb": 99999}).max_db_mb == 10000
