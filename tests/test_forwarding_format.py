"""Tests for the forwarded event format."""

from datetime import UTC, datetime
import json
import re
from typing import Any

import pytest

from custom_components.electric_fence.forwarding.format import event_dict, syslog_line
from custom_components.electric_fence.models import (
    OUTSIDE_ACTOR,
    ChangeAction,
    ChangeKind,
    LoginAttempt,
    LoginReason,
    LoginResult,
    SettingsChange,
    Source,
    UserExists,
)

NOW = datetime(2026, 10, 5, 7, 8, 9, tzinfo=UTC)


def login(**over: Any) -> LoginAttempt:
    """Login."""
    fields: dict[str, Any] = {
        "ts": NOW,
        "result": LoginResult.FAILED,
        "reason": LoginReason.WRONG_PASSWORD,
        "username_typed": "=cmd",
        "user_exists": UserExists.NO,
        "user_id": None,
        "provider": None,
        "ip": "203.0.113.9",
        "source": Source.REMOTE,
        "url": None,
        "user_agent": "ua",
        "client_id": None,
    }
    fields.update(over)
    return LoginAttempt(**fields)


def change(**over: Any) -> SettingsChange:
    """Change."""
    fields: dict[str, Any] = {
        "ts": NOW,
        "kind": ChangeKind.CONFIG_ENTRY,
        "action": ChangeAction.OPTIONS_CHANGED,
        "target_id": "t",
        "target_name": "Target",
        "details": {"changed": ["a"], "values": {"a": 1}},
        "actor_user_id": None,
        "actor_name": None,
        "actor_ip": None,
    }
    fields.update(over)
    return SettingsChange(**fields)


def test_login_event_dict() -> None:
    """Login event dict."""
    event = event_dict(login(), values=False)
    assert event["event_type"] == "login_failed"
    assert event["username"] == "=cmd"
    assert event["ip"] == "203.0.113.9"
    assert event["ts"] == "2026-10-05T07:08:09Z"
    assert event["user_exists"] is False
    assert event["count"] == 1


def test_login_event_types() -> None:
    """Login event types."""
    ok = login(result=LoginResult.SUCCESS, reason=LoginReason.OK)
    assert event_dict(ok, values=False)["event_type"] == "login_succeeded"
    new = login(result=LoginResult.SUCCESS, reason=LoginReason.OK, new_country=True)
    assert event_dict(new, values=False)["event_type"] == "new_country_login"
    blocked = login(result=LoginResult.BLOCKED)
    assert event_dict(blocked, values=False)["event_type"] == "login_failed"


def test_change_event_values_follow_setting() -> None:
    """Change event values follow setting."""
    assert "values" not in event_dict(change(), values=False)
    event = event_dict(change(), values=True)
    assert event["values"] == {"a": 1}
    assert event["changed"] == ["a"]
    assert event["event_type"] == "settings_changed"
    assert "values" not in event_dict(change(details={"changed": ["a"]}), values=True)


def test_change_event_details_without_values() -> None:
    """Details go out without values, redacted, and with lists capped at 50."""
    files = [f"lib/file_{n}.py" for n in range(120)]
    record = change(
        kind=ChangeKind.FILES,
        action=ChangeAction.FILES_CHANGED,
        details={
            "changed": files,
            "missing": ["a.py"],
            "repository": "owner/repo",
            "values": {"a": 1},
            "nested": {"api_token": "abc", "apps": list(range(60))},
        },
    )
    for values in (False, True):
        event = event_dict(record, values=values)
        details = event["details"]
        assert "values" not in details
        assert details["changed"] == files[:50]
        assert details["changed_truncated"] is True
        assert details["missing"] == ["a.py"]
        assert "missing_truncated" not in details
        assert details["repository"] == "owner/repo"
        assert details["nested"]["api_token"] == {"hidden": True}
        assert details["nested"]["apps"] == list(range(50))
        assert details["nested"]["apps_truncated"] is True
        assert event["changed"] == files[:50]
        assert event["changed_truncated"] is True
    assert event_dict(record, values=True)["values"] == {"a": 1}
    assert record.details["changed"] == files
    small = event_dict(change(), values=False)
    assert small["details"] == {"changed": ["a"]}
    assert "changed_truncated" not in small
    line = syslog_line(event_dict(record, values=True), hostname="ha", now=NOW)
    assert len(line) < 8192


def test_actor_kind() -> None:
    """Actor kind."""
    assert (
        event_dict(change(actor_name=OUTSIDE_ACTOR), values=False)["actor_kind"]
        == "outside"
    )
    assert event_dict(change(), values=False)["actor_kind"] == "system"
    user = change(actor_user_id="u1", actor_name="Ann")
    assert event_dict(user, values=False)["actor_kind"] == "user"


def test_syslog_line_shape() -> None:
    """Syslog line shape."""
    line = syslog_line(event_dict(login(), values=False), hostname="ha", now=NOW)
    text = line.decode()
    assert re.match(r"^<132>Oct  5 07:08:09 ha electric_fence: \{.*\}$", text), text
    assert "\n" not in text
    assert json.loads(text.split(": ", 1)[1])["username"] == "=cmd"


@pytest.mark.parametrize(
    ("event", "pri"),
    [
        ({"event_type": "login_succeeded"}, 134),
        ({"event_type": "login_failed"}, 132),
        ({"event_type": "new_country_login"}, 132),
        ({"event_type": "settings_changed", "action": "banned"}, 132),
        ({"event_type": "settings_changed", "action": "files_changed"}, 132),
        ({"event_type": "settings_changed", "action": "forwarding_failed"}, 132),
        ({"event_type": "settings_changed", "action": "updated"}, 133),
    ],
)
def test_syslog_severity(event: dict[str, Any], pri: int) -> None:
    """Syslog severity."""
    line = syslog_line(event, hostname="ha", now=NOW).decode()
    assert line.startswith(f"<{pri}>")


def test_syslog_line_escapes_newlines_and_keeps_utf8() -> None:
    """Syslog line escapes newlines and keeps utf8."""
    event = {"event_type": "login_failed", "username": "a\nbé"}
    line = syslog_line(event, hostname="my host", now=NOW)
    assert b"\n" not in line
    assert b" my-host " in line
    assert "é".encode() in line
    multiline_host = syslog_line(event, hostname="ha\nbox\r", now=NOW)
    assert b"\n" not in multiline_host
    assert b" ha-box " in multiline_host
