"""The forwarded event: one serializer and the syslog line around it."""

from datetime import UTC, datetime
import json
import re
from typing import Any

from ..models import (
    OUTSIDE_ACTOR,
    ChangeAction,
    LoginAttempt,
    LoginResult,
    Record,
    UserExists,
)
from ..notifier import _changed

FACILITY_LOCAL0 = 16
SEVERITY_WARNING = 4
SEVERITY_NOTICE = 5
SEVERITY_INFO = 6
APP_NAME = "electric_fence"

_WARNING_ACTIONS = frozenset(
    {
        ChangeAction.BANNED,
        ChangeAction.FILES_CHANGED,
        ChangeAction.REMOVED_FROM_STORE,
        ChangeAction.ARCHIVED,
        ChangeAction.FORWARDING_FAILED,
        ChangeAction.INSTALLED,
        ChangeAction.REPOSITORY_ADDED,
    }
)
_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
_SPACE = re.compile(r"\s+")


def _ts(value: datetime) -> str:
    """Return UTC ISO 8601 with a Z suffix (naive input is taken as UTC)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _exists(value: UserExists) -> bool | None:
    if value is UserExists.YES:
        return True
    if value is UserExists.NO:
        return False
    return None


def _login_event_type(record: LoginAttempt) -> str:
    if record.result is LoginResult.SUCCESS:
        return "new_country_login" if record.new_country else "login_succeeded"
    return "login_failed"


def event_dict(record: Record, *, values: bool) -> dict[str, Any]:
    """Return the forwarded event of a record.

    Usernames and IPs go out as recorded: the SIEM is the operator's own.
    """
    if isinstance(record, LoginAttempt):
        return {
            "event_type": _login_event_type(record),
            "ts": _ts(record.ts),
            "result": str(record.result),
            "reason": str(record.reason),
            "username": record.username_typed,
            "user_exists": _exists(record.user_exists),
            "user_id": record.user_id,
            "ip": record.ip,
            "country": record.country_code,
            "hostname": record.hostname,
            "source": str(record.source) if record.source is not None else None,
            "client_id": record.client_id,
            "client_name": record.client_name,
            "user_agent": record.user_agent,
            "mfa_module": record.mfa_module,
            "new_country": record.new_country,
            "count": record.count,
        }
    if record.actor_user_id is not None:
        actor_kind = "user"
    elif record.actor_name == OUTSIDE_ACTOR:
        actor_kind = "outside"
    else:
        actor_kind = "system"
    event: dict[str, Any] = {
        "event_type": "settings_changed",
        "ts": _ts(record.ts),
        "kind": str(record.kind),
        "action": str(record.action),
        "target": record.target_id,
        "target_name": record.target_name,
        "actor_user_id": record.actor_user_id,
        "actor_name": record.actor_name,
        "actor_ip": record.actor_ip,
        "actor_kind": actor_kind,
        "changed": _changed(record.details),
    }
    if record.action is ChangeAction.FORWARDING_FAILED and "error" in record.details:
        event["error"] = str(record.details["error"])
    if values and "values" in record.details:
        event["values"] = record.details["values"]
    return event


def _severity(event: dict[str, Any]) -> int:
    event_type = event.get("event_type")
    if event_type in ("login_failed", "new_country_login"):
        return SEVERITY_WARNING
    if event_type == "settings_changed":
        if event.get("action") in _WARNING_ACTIONS:
            return SEVERITY_WARNING
        return SEVERITY_NOTICE
    return SEVERITY_INFO


def _header(pri: int, hostname: str, now: datetime) -> str:
    """Return the syslog header up to the message.

    Wazuh's syslog pre-decoder reads the RFC 3164 form
    (`Mmm dd hh:mm:ss host program:`) and leaves the JSON for its JSON decoder.
    Its handling of an RFC 5424 header (`1 TS HOST APP - MSGID - `) is not
    something to rely on, so the 3164 form is used. Change only this function
    to switch.
    """
    now = now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)
    stamp = f"{_MONTHS[now.month - 1]} {now.day:2d} {now:%H:%M:%S}"
    return f"<{pri}>{stamp} {hostname} {APP_NAME}: "


def syslog_line(event: dict[str, Any], *, hostname: str, now: datetime) -> bytes:
    """Return the syslog line of an event, without framing."""
    pri = FACILITY_LOCAL0 * 8 + _severity(event)
    host = _SPACE.sub("-", hostname.strip()) or "-"
    body = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
    return (_header(pri, host, now) + body).encode("utf-8")
