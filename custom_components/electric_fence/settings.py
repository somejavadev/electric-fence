"""User settings for Electric Fence."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import timedelta
import math
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class Settings:
    """Options stored on the config entry."""

    notify_failed_login: bool = True
    ha_login_notification: Literal["replace", "keep"] = "replace"
    retention_days: int = 90
    # The database's size limit; the oldest failed logins go first.
    max_db_mb: int = 200
    geoip_provider: Literal["dbip_lite", "maxmind", "none"] = "dbip_lite"
    maxmind_account_id: str | None = None
    maxmind_license_key: str | None = None
    reverse_dns: bool = True
    record_values: bool = False
    mask_ips: bool = False
    auto_ban: bool = True
    ban_threshold: int = 5
    ban_window_minutes: int = 15
    ban_hours: int = 24
    ban_repeat_offenders: bool = False
    username_protection: bool = True
    attack_threshold: int = 10
    attack_min_ips: int = 3
    attack_window_minutes: int = 15
    # Local addresses (LAN, Docker, Tailscale, ...) are counted like any other
    # unless this is set. Loopback, the Supervisor and trusted proxies never are.
    exempt_local: bool = False
    # IPv6 failures and bans are counted per prefix of this length.
    ipv6_prefix: int = 64
    notify_ip_banned: bool = True
    notify_username_attack: bool = True
    notify_new_country: bool = True
    notify_ban_ended: bool = False
    notify_targets: tuple[str, ...] = ()
    verify_files: bool = True
    ignored_files: tuple[str, ...] = ()
    unmaintained_months: int = 4
    integration_repairs: bool = True
    scan_delay_minutes: int = 30
    forward_enabled: bool = False
    forward_host: str = ""
    forward_port: int = 514
    forward_protocol: Literal["tcp", "udp", "tls"] = "tcp"
    forward_ca_pem: str = ""

    @property
    def trusted_window(self) -> timedelta:
        """Return how long after a success an IP is never auto-banned."""
        return timedelta(hours=self.ban_hours or 24)

    def ignored_for(self, domain: str) -> frozenset[str]:
        """Return the relative paths ignored for one integration."""
        prefix = f"{domain}/"
        return frozenset(
            f.removeprefix(prefix) for f in self.ignored_files if f.startswith(prefix)
        )

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> Settings:
        """Build settings from stored options.

        Unknown keys are ignored. A value of the wrong type or out of range
        falls back to its default; numbers are clamped to their range.
        """
        d = cls()

        def number(key: str, low: int, high: int) -> int:
            value = options.get(key, getattr(d, key))
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
            ):
                value = getattr(d, key)
            return min(max(int(value), low), high)

        def targets() -> tuple[str, ...]:
            value = options.get("notify_targets")
            if not isinstance(value, list | tuple):
                return d.notify_targets
            return tuple(
                v for v in value if isinstance(v, str) and v.startswith("notify.")
            )

        def ignored() -> tuple[str, ...]:
            value = options.get("ignored_files")
            if isinstance(value, str):
                value = value.splitlines()
            if not isinstance(value, list | tuple):
                return d.ignored_files
            entries = (v.strip() for v in value if isinstance(v, str))
            return tuple(
                e
                for e in entries
                if "/" in e and ".." not in e and not e.startswith("/")
            )

        def flag(key: str) -> bool:
            value = options.get(key)
            return value if isinstance(value, bool) else getattr(d, key)

        def choice(key: str, allowed: tuple[str, ...]) -> Any:
            value = options.get(key)
            return value if value in allowed else getattr(d, key)

        def text(key: str) -> str | None:
            value = options.get(key)
            return value if isinstance(value, str) else None

        def string(key: str) -> str:
            value = options.get(key)
            return value if isinstance(value, str) else getattr(d, key)

        return cls(
            notify_failed_login=flag("notify_failed_login"),
            ha_login_notification=choice("ha_login_notification", ("replace", "keep")),
            retention_days=number("retention_days", 1, 3650),
            max_db_mb=number("max_db_mb", 20, 10000),
            geoip_provider=choice("geoip_provider", ("dbip_lite", "maxmind", "none")),
            maxmind_account_id=text("maxmind_account_id"),
            maxmind_license_key=text("maxmind_license_key"),
            reverse_dns=flag("reverse_dns"),
            record_values=flag("record_values"),
            mask_ips=flag("mask_ips"),
            auto_ban=flag("auto_ban"),
            ban_threshold=number("ban_threshold", 1, 100),
            ban_window_minutes=number("ban_window_minutes", 1, 1440),
            ban_hours=number("ban_hours", 0, 8760),
            ban_repeat_offenders=flag("ban_repeat_offenders"),
            username_protection=flag("username_protection"),
            attack_threshold=number("attack_threshold", 2, 1000),
            attack_min_ips=number("attack_min_ips", 1, 100),
            attack_window_minutes=number("attack_window_minutes", 1, 1440),
            exempt_local=flag("exempt_local"),
            ipv6_prefix=number("ipv6_prefix", 48, 128),
            notify_ip_banned=flag("notify_ip_banned"),
            notify_username_attack=flag("notify_username_attack"),
            notify_new_country=flag("notify_new_country"),
            notify_ban_ended=flag("notify_ban_ended"),
            notify_targets=targets(),
            verify_files=flag("verify_files"),
            ignored_files=ignored(),
            unmaintained_months=number("unmaintained_months", 1, 36),
            integration_repairs=flag("integration_repairs"),
            scan_delay_minutes=number("scan_delay_minutes", 1, 1440),
            forward_enabled=flag("forward_enabled"),
            forward_host=string("forward_host").strip(),
            forward_port=number("forward_port", 1, 65535),
            forward_protocol=choice("forward_protocol", ("tcp", "udp", "tls")),
            forward_ca_pem=string("forward_ca_pem"),
        )

    def to_options(self) -> dict[str, Any]:
        """Return the settings as a plain dict for storing in options."""
        return asdict(self)
