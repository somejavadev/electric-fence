"""User and session snapshots for the Users tab (H7).

Read on demand when the panel asks; nothing is stored. Token values
(`token`, `jwt_key`) and credential data other than the username never
leave this module. Shape: `UserItem` in `shapes.py`.
"""

from datetime import datetime
from typing import Any

from homeassistant.auth.models import RefreshToken, User
from homeassistant.core import HomeAssistant

from .shapes import iso, login_item
from .storage import FenceStore

_HASS_PROVIDER = "homeassistant"


def username(user: User) -> str | None:
    """Return the user's `homeassistant` provider username, from credentials."""
    for cred in user.credentials:
        if cred.auth_provider_type == _HASS_PROVIDER:
            if isinstance(value := cred.data.get("username"), str):
                return value
    return None


def _session(token: RefreshToken) -> dict[str, Any]:
    # Explicit allow-list: never `token`, `jwt_key` or the credential.
    return {
        "id": token.id,
        "client_name": token.client_name,
        "client_id": token.client_id,
        "token_type": token.token_type,
        "created_at": iso(token.created_at),
        "last_used_at": iso(token.last_used_at),
        "last_used_ip": token.last_used_ip,
    }


def _newest_first(token: RefreshToken) -> datetime:
    return token.created_at


async def snapshot_users(
    hass: HomeAssistant, store: FenceStore
) -> list[dict[str, Any]]:
    """Return a UserItem for every user, sorted by name."""
    users = await hass.auth.async_get_users()
    last = await hass.async_add_executor_job(store.last_success_per_user)
    items: list[dict[str, Any]] = []
    for user in sorted(users, key=lambda u: ((u.name or "").casefold(), u.id)):
        mfa = await hass.auth.async_get_enabled_mfa(user)
        row = last.get(user.id)
        tokens = sorted(user.refresh_tokens.values(), key=_newest_first, reverse=True)
        items.append(
            {
                "id": user.id,
                "name": user.name,
                "username": username(user),
                "is_admin": user.is_admin,
                "is_owner": user.is_owner,
                "is_active": user.is_active,
                "local_only": user.local_only,
                "system_generated": user.system_generated,
                "mfa_modules": list(mfa),
                "last_login": login_item(row) if row is not None else None,
                "sessions": [_session(t) for t in tokens],
            }
        )
    return items
