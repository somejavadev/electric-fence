"""Record types for Electric Fence."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.auth.models import Credentials


class LoginResult(StrEnum):
    """Outcome of a login attempt."""

    SUCCESS = "success"
    FAILED = "failed"
    BLOCKED = "blocked"


class LoginReason(StrEnum):
    """Reason for a login outcome."""

    OK = "ok"
    NO_SUCH_USER = "no_such_user"
    WRONG_PASSWORD = "wrong_password"
    INVALID_CREDENTIALS = "invalid_credentials"
    WRONG_2FA_CODE = "wrong_2fa_code"
    INVALID_TOKEN = "invalid_token"
    BLOCKED_INACTIVE = "blocked_inactive_user"
    BLOCKED_LOCAL_ONLY = "blocked_local_only"


class UserExists(StrEnum):
    """Whether the typed username matches an existing user."""

    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class Source(StrEnum):
    """Where a request came from."""

    LOCAL = "local"
    REMOTE = "remote"
    CLOUD = "cloud"


class ChangeKind(StrEnum):
    """What kind of object a settings change touched."""

    CONFIG_ENTRY = "config_entry"
    USER = "user"
    CORE_CONFIG = "core_config"


class ChangeAction(StrEnum):
    """What was done in a settings change."""

    ADDED = "added"
    REMOVED = "removed"
    UPDATED = "updated"
    RECONFIGURED = "reconfigured"
    OPTIONS_CHANGED = "options_changed"
    ENABLED = "enabled"
    DISABLED = "disabled"
    PASSWORD_CHANGED = "password_changed"


@dataclass(slots=True)
class LoginAttempt:
    """A login attempt."""

    ts: datetime
    result: LoginResult
    reason: LoginReason
    username_typed: str | None
    user_exists: UserExists
    user_id: str | None
    provider: str | None
    ip: str | None
    source: Source | None
    url: str | None
    user_agent: str | None
    client_id: str | None
    client_name: str | None = None
    mfa_module: str | None = None
    country_code: str | None = None
    hostname: str | None = None
    confirmed: bool = True
    count: int = 1
    flow_id: str | None = None
    # In memory only, never stored.
    credentials: Credentials | None = field(default=None, repr=False)


@dataclass(slots=True)
class SettingsChange:
    """A settings change."""

    ts: datetime
    kind: ChangeKind
    action: ChangeAction
    target_id: str | None
    target_name: str | None
    details: dict[str, Any]
    actor_user_id: str | None
    actor_name: str | None
    actor_ip: str | None

    @property
    def is_system(self) -> bool:
        """Return True when no user caused the change."""
        return self.actor_user_id is None


type Record = LoginAttempt | SettingsChange
