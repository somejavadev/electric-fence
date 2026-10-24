"""User settings for Electric Fence."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass
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

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> Settings:
        """Build settings from stored options, ignoring unknown keys."""
        known = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in options.items() if k in known})

    def to_options(self) -> dict[str, Any]:
        """Return the settings as a plain dict for storing in options."""
        return asdict(self)
