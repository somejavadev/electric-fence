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
             | "blocked_local_only" | "throttled",
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
     new_country: bool,           # first successful login from this country
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

BanItem (an IP ban, active or ended; `null` is expected for a ban Core holds
that Electric Fence did not make)::

    {ip: str, source: "electric_fence" | "other", banned_at: str,
     expires_at: str | null,       # null: permanent
     failures: int | null, ban_number: int | null,
     country_code: str | null, hostname: str | null,  # hostname: RAW text
     ended_at: str | null,
     end_reason: "expired" | "unbanned" | "removed" | null,
     ended_by_name: str | null}   # who unbanned it; null when not by a user

AttackItem (a username under attack)::

    {username: str,                # RAW attacker-controlled text
     started_at: str, ended_at: str | null,
     failures: int, ip_count: int, countries: [str],
     end_reason: "quiet" | "ended_by_user" | null,
     active: bool}

PostureItem::

    {id: str, level: "ok" | "notice" | "warning" | "critical",
     params: object}

    id                  params
    admins_without_mfa  {users: [{id, name}]}        (active, non-system admins)
    auto_ban_off        {threshold: int | null,  # Core's login threshold
                         electric_fence: bool}   # Electric Fence's auto-ban is
                                                 # on and can ban
    ip_ban_disabled     {}  (warning: Core's IP ban is off, nothing can be banned)
    proxy_suspected     {ip: str | null, users: int, use_x_forwarded_for: bool | null}
    geoip_missing       {provider: str, last_error: str | null}
    collector_disabled  {} when ok; one error item per disabled collector with
                        {collector: str, reasons: [str]}
    storage_error       {storage_errors: int, last_error: str | null,
                         last_error_at: str | null}
    integration_files_changed
                        {integrations: [domain]}  (warning while any differ)
    integration_health  {removed: [domain], archived: [domain],
                         unmaintained: [domain]}
                        (critical if any removed, else warning if any
                         archived or unmaintained)
    integration_checks_unchecked
                        {integrations: [domain]}  (info: couldn't be checked)

    The three integration items are left out while `integration_repairs` is
    off, and before the first integration scan.

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
          posture: PostureItem[],
          protection: {available: bool, active_bans: int, bans_24h: int,
                       attacks: AttackItem[]}}  # active attacks
    electric_fence/logins    {result? ("success" | "failed" | "blocked"
                                       | "failed_or_blocked"),
                              user_id?, username? (substring),
                              ip?, country?, since?, until?, before_id?,
                              limit? (1..200, default 50)}
      -> {items: LoginItem[], next_before_id: int | null}
    electric_fence/changes   {kind?, actor_user_id?, include_system? (false),
                              since?, until?, before_id?, limit? (1..200)}
      -> {items: ChangeItem[], next_before_id: int | null}
    electric_fence/bans      {state: "active" | "history", before_id?,
                              limit? (1..200, history only)}
      -> {available: bool,          # bans can be applied right now
          reason: str | null,       # why not: "not_loaded", "contracts",
                                    # "ip_ban_disabled", "backend_error"
          items: BanItem[],         # active: newest first, all of them
          next_before_id: int | null}  # history paging as below
    electric_fence/unban     {ip: str} -> {unbanned: bool}
    electric_fence/attacks   {state: "active" | "history"}
      -> {items: AttackItem[]}
    electric_fence/end_slowdown {username: str} -> {ended: bool}
    electric_fence/clear_countries {} -> {cleared: int}
      Forget every user's known login countries. Audited.
    electric_fence/users     {} -> {items: UserItem[]}
    electric_fence/status    {}
      -> {version: str | null,
          settings: {notify_failed_login, ha_login_notification,
                     retention_days, geoip_provider, maxmind_account_id,
                     reverse_dns, record_values, auto_ban, ban_threshold,
                     ban_window_minutes, ban_hours, ban_repeat_offenders,
                     username_protection, attack_threshold, attack_min_ips,
                     attack_window_minutes, notify_ip_banned,
                     notify_username_attack, notify_new_country,
                     notify_ban_ended, notify_targets: [str],
                     maxmind_license_key_set: bool, forward_ca_pem_set: bool,
                     forward_enabled: bool, verify_files: bool,
                     integration_repairs: bool, ...},
          geo: GeoStatus | null,
          collectors: [{name: str, enabled: bool, reason: [str]}],
          worker: {queued: int, dropped: int, storage_errors: int},
          forwarding: {state: "connected" | "failing", queued: int,
                       dropped: int, last_error: str | null} | null,
                       # null when forwarding is off
          apps: "available" | "disabled" | "not_available"}
                # disabled: a Supervisor install whose contracts turned the
                # app collector off (see Repairs)
    electric_fence/subscribe {}  (subscription; result null, then events)
      event {type: "login", item: LoginItem}   # item.id is null
      event {type: "change", item: ChangeItem} # item.id is null
      event {type: "unloaded"}                 # entry unloaded: subscription
                                               # ended, subscribe again later
      A subscription ends without an event once its user is no longer an
      active admin.

IntegrationItem (the last check of one custom integration)::

    {domain: str, name: str,    # name: manifest name, RAW author text
     source: "marketplace" | "manual" | ...,
     version: str | null, repository: str | null,
     files: null              # null when `verify_files` is off
          | {status: "match" | "changed" | "unchecked" | "baseline_recorded"
                     | "baseline_renewed",
             reason: str | null,        # why `unchecked`
             ref: str | null, version_before: str | null,
             report: null | {changed: [str], missing: [str], added: [str],
                             unreadable: [str], symlinks: [str]}},
                             # file names are RAW author text; each list holds
                             # at most 500 paths (see `truncated`)
     health: {status: "ok" | "archived" | "removed" | "unmaintained"
                      | "unknown",
              level: "ok" | "notice" | "warning" | "critical",
              last_updated: str | null, reason: str | null},
     truncated: bool}

    electric_fence/integrations {}
      -> {items: [IntegrationItem], scanned_at: str | null, running: bool,
          available: bool}   # available: false without a scheduler or a
                             # trusted Marketplace
    electric_fence/scan_integrations {}
      -> {started: bool}     # false when rate limited (5 minutes per entry)
                             # or unavailable; the scan runs in the background

Paging: pass the previous `next_before_id` as `before_id`; it is null when
the page was not full. `since`/`until` are ISO 8601; without an offset they
are read as UTC.
"""

from collections.abc import Mapping
from dataclasses import fields
from datetime import UTC, date, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from .geo.base import GeoStatus
from .models import LoginAttempt, Record
from .storage import AttackRow, BanRow, ChangeRow, LoginRow

if TYPE_CHECKING:
    from .integrity.scheduler import IntegrationStatus
    from .protection.bans import BanView
    from .protection.throttle import AttackView

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


def ban_view_item(view: BanView) -> dict[str, Any]:
    """Return a BanItem for an active ban."""
    return {
        "ip": str(view.ip),
        "source": view.source,
        "banned_at": iso(view.banned_at),
        "expires_at": iso(view.expires_at),
        "failures": view.failures,
        "ban_number": view.ban_number,
        "country_code": view.country_code,
        "hostname": view.hostname,
        "ended_at": None,
        "end_reason": None,
        "ended_by_name": None,
    }


def ban_row_item(row: BanRow) -> dict[str, Any]:
    """Return a BanItem for a stored ban."""
    return {
        "ip": row.ip,
        "source": "electric_fence",
        "banned_at": iso(row.banned_at),
        "expires_at": iso(row.expires_at),
        "failures": row.failures,
        "ban_number": row.ban_number,
        "country_code": row.country_code,
        "hostname": row.hostname,
        "ended_at": iso(row.ended_at),
        "end_reason": row.end_reason,
        "ended_by_name": row.ended_by_name,
    }


def attack_view_item(view: AttackView) -> dict[str, Any]:
    """Return an AttackItem for an active attack."""
    return {
        "username": view.username,
        "started_at": iso(view.started_at),
        "ended_at": None,
        "failures": view.failures,
        "ip_count": view.ip_count,
        "countries": list(view.countries),
        "end_reason": None,
        "active": True,
    }


def attack_row_item(row: AttackRow) -> dict[str, Any]:
    """Return an AttackItem for a stored attack."""
    return {
        "username": row.username,
        "started_at": iso(row.started_at),
        "ended_at": iso(row.ended_at),
        "failures": row.failures,
        "ip_count": row.ip_count,
        "countries": list(row.countries),
        "end_reason": row.end_reason,
        "active": row.ended_at is None,
    }


def integration_item(status: IntegrationStatus) -> dict[str, Any]:
    """Return an integration check result (see IntegrationItem above)."""
    files = status.files
    report = files.report if files is not None else None
    report_lists = (
        None
        if report is None
        else {
            name: list(getattr(report, name))
            for name in ("changed", "missing", "added", "unreadable", "symlinks")
        }
    )
    health = status.health
    return {
        "domain": status.domain,
        "name": status.name,
        "source": status.source,
        "version": status.version,
        "repository": status.repository,
        "files": None
        if files is None
        else {
            "status": files.status,
            "reason": files.reason,
            "ref": files.ref,
            "version_before": files.version_before,
            "report": report_lists,
        },
        "health": {
            "status": health.status,
            "level": health.level,
            "last_updated": iso(health.last_updated),
            "reason": health.reason,
        },
        "truncated": status.truncated,
    }
