"""JSON shapes of the `electric_fence/*` WebSocket API, and their serialisers.

This docstring is the contract the panel is written against. Timestamps are
ISO 8601 strings in UTC (`2026-10-24T09:15:02.123456+00:00`), dates are
`YYYY-MM-DD`, enums are their string values, and absent values are `null`.
All commands are admin only; a non-admin gets the error code
`unauthorized`, and with no loaded entry every command answers `not_loaded`.

LoginItem (a stored login attempt)::

    {id: int | null,             # null only in subscription events
     ts: str,
     result: "success" | "failed" | "blocked",
     reason: "ok" | "no_such_user" | "wrong_password" | "invalid_credentials"
             | "wrong_2fa_code" | "invalid_token" | "blocked_inactive_user"
             | "blocked_local_only",
     username_typed: str | null,  # RAW attacker-controlled text: render as text
     user_exists: "yes" | "no" | "unknown",
     user_id: str | null,
     user_name: str | null,       # the user's name from the auth store, looked
                                  # up by user_id when the item is built
     provider: str | null,
     ip: str | null, country_code: str | null, hostname: str | null,
     source: "local" | "remote" | "cloud" | null,
     client_id: str | null, client_name: str | null,
     user_agent: str | null, url: str | null, mfa_module: str | null,
     confirmed: bool,             # success confirmed by a token being issued
     count: int}                  # identical failures merged into this row

ChangeItem (a stored settings change; `actor_user_id` null means System)::

    {id: int | null, ts: str,
     kind: "config_entry" | "user" | "core_config" | "backup" | "http" | "network"
           | "analytics" | "logger" | "recorder" | <any text from a newer version>,
     action: "added" | "removed" | "updated" | "reconfigured"
             | "options_changed" | "enabled" | "disabled" | "password_changed"
             | "restore_requested" | "confirmed" | "purged" | "withdrawn"
             | <any text from a newer version>,
     target_id: str | null, target_name: str | null, details: object,
     actor_user_id: str | null, actor_name: str | null, actor_ip: str | null}

PostureItem::

    {id: str, level: "ok" | "warning" | "error", params: object}

    id                  params
    admins_without_mfa  {users: [{id, name}]}        (active, non-system admins)
    auto_ban_off        {threshold: int | null}
    proxy_suspected     {ip: str | null, users: int, use_x_forwarded_for: bool | null}
    geoip_missing       {provider: str, last_error: str | null}
    collector_disabled  {} when ok; one error item per disabled collector with
                        {collector: str, reasons: [str]}
    storage_error       {storage_errors: int, last_error: str | null,
                         last_error_at: str | null}

    A check that itself fails gives `level: "error"` with
    `params: {check_failed: "<exception type>"}`. The text lives in the panel.

UserItem::

    {id, name: str | null, username: str | null,  # homeassistant provider
     is_admin, is_owner, is_active, local_only, system_generated: bool,
     mfa_modules: [str],                           # enabled MFA module IDs
     last_login: LoginItem | null,                 # latest confirmed success
     sessions: [{id, client_name, client_id, token_type, created_at,
                 last_used_at, last_used_ip}]}     # newest first, no tokens

GeoStatus::

    {provider: "dbip_lite" | "maxmind" | "none", db_date: str | null,
     last_error: str | null, attribution: str | null}

Commands::

    electric_fence/overview  {}
      -> {failed_24h: {count: int, items: LoginItem[<=5]},  # failed + blocked
          changes_7d: {count: int, items: ChangeItem[<=5]}, # System excluded
          recent_success: {items: LoginItem[<=5]},
          posture: PostureItem[]}
    electric_fence/logins    {result? ("success" | "failed" | "blocked"
                                       | "failed_or_blocked"),
                              user_id?, username? (substring),
                              ip?, country?, since?, until?, before_id?,
                              limit? (1..200, default 50)}
      -> {items: LoginItem[], next_before_id: int | null}
    electric_fence/changes   {kind?, actor_user_id?, include_system? (false),
                              since?, until?, before_id?, limit? (1..200)}
      -> {items: ChangeItem[], next_before_id: int | null}
    electric_fence/users     {} -> {items: UserItem[]}
    electric_fence/status    {}
      -> {version: str | null,
          settings: {notify_failed_login, ha_login_notification,
                     retention_days, geoip_provider, maxmind_account_id,
                     reverse_dns, maxmind_license_key_set: bool},
          geo: GeoStatus | null,
          collectors: [{name: str, enabled: bool, reason: [str]}],
          worker: {queued: int, dropped: int, storage_errors: int}}
    electric_fence/subscribe {}  (subscription; result null, then events)
      event {type: "login", item: LoginItem}   # item.id is null
      event {type: "change", item: ChangeItem} # item.id is null
      event {type: "unloaded"}                 # entry unloaded: subscription
                                               # ended, subscribe again later
      A subscription ends without an event once its user is no longer an
      active admin.

Paging: pass the previous `next_before_id` as `before_id`; it is null when
the page was not full. `since`/`until` are ISO 8601; without an offset they
are read as UTC.
"""

from collections.abc import Mapping
from dataclasses import fields
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any

from .geo.base import GeoStatus
from .models import LoginAttempt, Record
from .storage import ChangeRow, LoginRow

_LOGIN_FIELDS = tuple(f.name for f in fields(LoginRow))
_CHANGE_FIELDS = tuple(f.name for f in fields(ChangeRow))


def iso(value: datetime | None) -> str | None:
    """Return an aware datetime as an ISO 8601 UTC string."""
    return None if value is None else value.astimezone(UTC).isoformat()


def _value(value: Any) -> Any:
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


def _item(obj: Any, names: tuple[str, ...]) -> dict[str, Any]:
    # A record from the worker has no `id` yet: it becomes null.
    return {name: _value(getattr(obj, name, None)) for name in names}


type UserNames = Mapping[str, str | None]


def login_item(row: LoginRow | LoginAttempt, names: UserNames) -> dict[str, Any]:
    """Return a LoginItem. Only stored fields: never in-memory credentials.

    `names` maps user IDs to names (from memory) for `user_name`.
    """
    item = _item(row, _LOGIN_FIELDS)
    user_id = row.user_id
    item["user_name"] = names.get(user_id) if user_id is not None else None
    return item


def change_item(row: Any) -> dict[str, Any]:
    """Return a ChangeItem from a ChangeRow or a SettingsChange."""
    item = _item(row, _CHANGE_FIELDS)
    item["details"] = dict(item["details"] or {})
    return item


def record_event(rec: Record, names: UserNames) -> dict[str, Any]:
    """Return the subscription event for a stored record."""
    if isinstance(rec, LoginAttempt):
        return {"type": "login", "item": login_item(rec, names)}
    return {"type": "change", "item": change_item(rec)}


def geo_status_item(status: GeoStatus | None) -> dict[str, Any] | None:
    """Return a GeoStatus as JSON."""
    if status is None:
        return None
    return {
        "provider": status.provider,
        "db_date": _value(status.db_date),
        "last_error": status.last_error,
        "attribution": status.attribution,
    }
