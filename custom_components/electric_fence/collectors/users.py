"""User collector (H5): who added, changed or removed a user or a password.

The auth manager fires `user_added`, `user_updated` and `user_removed`
inside the call that made the change. A cached snapshot of each user (in
memory) names the fields an update changed, and keeps the name of a removed
user. Password and username changes fire no event, so the `homeassistant`
provider's `async_change_password` and `async_change_username` are wrapped:
the original runs first, its exception passes through unchanged, and a
change is recorded only after it returned.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.auth import EVENT_USER_ADDED, EVENT_USER_REMOVED, EVENT_USER_UPDATED
from homeassistant.auth.models import User
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .. import attribution
from ..hooks import wrap_async_method
from ..models import ChangeAction, ChangeKind, SettingsChange
from ..privacy import sanitise_text
from .base import BaseCollector

if TYPE_CHECKING:
    from ..runtime import Runtime

type _AsyncFn = Callable[..., Awaitable[Any]]

_HASS_PROVIDER = "homeassistant"


@dataclass(slots=True, frozen=True)
class UserSnapshot:
    """The fields of a user an update can change."""

    name: str | None
    is_active: bool
    group_ids: frozenset[str]
    local_only: bool

    @classmethod
    def of(cls, user: User) -> UserSnapshot:
        """Take a snapshot of a user."""
        return cls(
            name=user.name,
            is_active=user.is_active,
            group_ids=frozenset(group.id for group in user.groups),
            local_only=user.local_only,
        )


def _changed_fields(old: UserSnapshot, new: UserSnapshot) -> list[str]:
    return [
        name
        for name in ("name", "is_active", "group_ids", "local_only")
        if getattr(old, name) != getattr(new, name)
    ]


def _arg(args: tuple[Any, ...], kwargs: dict[str, Any], i: int, name: str) -> Any:
    """Return a call argument by position or keyword, else None."""
    return args[i] if len(args) > i else kwargs.get(name)


class UserCollector(BaseCollector):
    """Record user, password and username changes with strict attribution."""

    name = "users"

    def __init__(self, hass: HomeAssistant, runtime: Runtime) -> None:
        """Initialise the collector."""
        super().__init__(hass, runtime)
        self._users: dict[str, UserSnapshot] = {}

    def _install(self) -> None:
        self._users = {
            user_id: UserSnapshot.of(user)
            for user_id, user in (attribution.auth_users(self.hass) or {}).items()
        }
        bus = self.hass.bus
        for event_type, listener in (
            (EVENT_USER_ADDED, self._on_user_added),
            (EVENT_USER_UPDATED, self._on_user_updated),
            (EVENT_USER_REMOVED, self._on_user_removed),
        ):
            self._unsubs.append(bus.async_listen(event_type, listener))
        provider = self.hass.auth.get_auth_provider(_HASS_PROVIDER, None)
        if provider is not None:
            self._handles.append(
                wrap_async_method(
                    provider, "async_change_password", self._wrap_password
                )
            )
            self._handles.append(
                wrap_async_method(
                    provider, "async_change_username", self._wrap_username
                )
            )

    def _uninstall(self) -> None:
        self._users.clear()

    # Event listeners (inside the auth manager's call).

    @callback
    def _on_user_added(self, event: Event[dict[str, Any]]) -> None:
        self.guard.call(self._handle_added, event.data.get("user_id"))

    @callback
    def _on_user_updated(self, event: Event[dict[str, Any]]) -> None:
        self.guard.call(self._handle_updated, event.data.get("user_id"))

    @callback
    def _on_user_removed(self, event: Event[dict[str, Any]]) -> None:
        self.guard.call(self._handle_removed, event.data.get("user_id"))

    def _user(self, user_id: Any) -> User | None:
        users = attribution.auth_users(self.hass)
        return users.get(user_id) if users is not None else None

    def _handle_added(self, user_id: Any) -> None:
        user = self._user(user_id)
        if user is None:
            return
        snapshot = UserSnapshot.of(user)
        self._users[user.id] = snapshot
        self._record(ChangeAction.ADDED, user.id, snapshot.name, {})

    def _handle_updated(self, user_id: Any) -> None:
        user = self._user(user_id)
        if user is None:
            return
        new = UserSnapshot.of(user)
        old = self._users.get(user.id)
        self._users[user.id] = new
        if old is None:
            # Not seen before: nothing to compare with.
            return
        changed = _changed_fields(old, new)
        if changed:
            self._record(ChangeAction.UPDATED, user.id, new.name, {"changed": changed})

    def _handle_removed(self, user_id: Any) -> None:
        if not isinstance(user_id, str):
            return
        old = self._users.pop(user_id, None)
        self._record(ChangeAction.REMOVED, user_id, old.name if old else None, {})

    # Provider wrappers.

    def _wrap_password(self, original: _AsyncFn) -> _AsyncFn:
        async def async_change_password(*args: Any, **kwargs: Any) -> Any:
            result = await original(*args, **kwargs)
            self.guard.call(
                self._record_credential_change,
                ChangeAction.PASSWORD_CHANGED,
                _arg(args, kwargs, 0, "username"),
                {},
            )
            return result

        return async_change_password

    def _wrap_username(self, original: _AsyncFn) -> _AsyncFn:
        async def async_change_username(*args: Any, **kwargs: Any) -> Any:
            result = await original(*args, **kwargs)
            self.guard.call(
                self._record_credential_change,
                ChangeAction.UPDATED,
                _arg(args, kwargs, 1, "new_username"),
                {"field": "username"},
            )
            return result

        return async_change_username

    def _record_credential_change(
        self, action: ChangeAction, username: Any, details: dict[str, Any]
    ) -> None:
        """Record a change of the user with this (current) username."""
        user_id = attribution.hass_user_id(self.hass, username)
        snapshot = self._users.get(user_id) if user_id is not None else None
        self._record(action, user_id, snapshot.name if snapshot else None, details)

    def _record(
        self,
        action: ChangeAction,
        user_id: str | None,
        user_name: str | None,
        details: dict[str, Any],
    ) -> None:
        actor = attribution.credited_actor(ChangeKind.USER, action, user_id)
        self.runtime.worker.submit(
            SettingsChange(
                ts=dt_util.utcnow(),
                kind=ChangeKind.USER,
                action=action,
                target_id=user_id,
                target_name=sanitise_text(user_name),
                details=details,
                actor_user_id=actor.user_id if actor else None,
                actor_name=actor.user_name if actor else None,
                actor_ip=actor.ip if actor else None,
            )
        )
