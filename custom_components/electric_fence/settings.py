"""User settings for Electric Fence."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass
import math
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class Settings:
    """Options stored on the config entry."""

    notify_failed_login: bool = True
    ha_login_notification: Literal["replace", "keep"] = "replace"
    retention_days: int = 90
    geoip_provider: Literal["dbip_lite", "maxmind", "none"] = "dbip_lite"
    maxmind_account_id: str | None = None
    maxmind_license_key: str | None = None
    reverse_dns: bool = True
    record_values: bool = False

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> Settings:
        """Build settings from stored options.

        Unknown keys are ignored. A value of the wrong type or out of range
        falls back to its default; retention is clamped to 1..3650.
        """
        d = cls()
        retention = options.get("retention_days", d.retention_days)
        if (
            isinstance(retention, bool)
            or not isinstance(retention, int | float)
            or not math.isfinite(retention)
        ):
            retention = d.retention_days
        else:
            retention = min(max(int(retention), 1), 3650)

        def flag(key: str) -> bool:
            value = options.get(key)
            return value if isinstance(value, bool) else getattr(d, key)

        def choice(key: str, allowed: tuple[str, ...]) -> Any:
            value = options.get(key)
            return value if value in allowed else getattr(d, key)

        def text(key: str) -> str | None:
            value = options.get(key)
            return value if isinstance(value, str) else None

        return cls(
            notify_failed_login=flag("notify_failed_login"),
            ha_login_notification=choice("ha_login_notification", ("replace", "keep")),
            retention_days=retention,
            geoip_provider=choice("geoip_provider", ("dbip_lite", "maxmind", "none")),
            maxmind_account_id=text("maxmind_account_id"),
            maxmind_license_key=text("maxmind_license_key"),
            reverse_dns=flag("reverse_dns"),
            record_values=flag("record_values"),
        )

    def to_options(self) -> dict[str, Any]:
        """Return the settings as a plain dict for storing in options."""
        return asdict(self)
