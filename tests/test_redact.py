"""Tests for settings change redaction and diffing."""

import enum
from typing import Any

import pytest

from custom_components.electric_fence.redact import (
    HIDDEN,
    cap_value,
    diff_details,
    is_sensitive_name,
    redact_value,
)


@pytest.mark.parametrize(
    "name",
    [
        "password",
        "user_password",
        "userPassword",
        "passwd",
        "passphrase",
        "client_secret",
        "access_token",
        "authToken",
        "apikey",
        "api_key",
        "encryption_key",
        "key",
        "auth",
        "pin",
        "pwd",
        "psk",
        "credentials",
        "cookie",
        "latitude",
        "longitude",
        "elevation",
        "radius",
        "lat",
        "lon",
        "lng",
        "pass",
        "Authorization",
        "proxy_authorization",
        "passcode",
        "alarmPasscode",
        "pincode",
        "code",
        "alarm_code",
        "otp",
        "otp_value",
        "PINCode",
        "PSKValue",
        "country_code",
    ],
)
def test_sensitive_names(name: str) -> None:
    """Sensitive words match as substrings or whole tokens."""
    assert is_sensitive_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "translation",
        "latest",
        "plate",
        "author",
        "monkey",
        "keyboard_layout",
        "location_name",
        "time_zone",
        "unit_system",
        "passenger",
        "longtext",
        "scan_interval",
        "name",
        "radiusless",
        "barcode",
        "codec",
        "encoding",
        "hotplug",
        "HTTPSPort",
    ],
)
def test_harmless_names(name: str) -> None:
    """Short words never match inside longer words."""
    assert not is_sensitive_name(name)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(
            "https://u:p@host.example:8123/api?token=abc&x=1#frag",
            "https://•••@host.example:8123/api?token=•••&x=•••#•••",
            id="url-parts",
        ),
        pytest.param(
            "https://hooks.example/api/webhook/9f8e7d6c5b4a39281706f5e4d3c2b1a0",
            "https://hooks.example/api/webhook/•••",
            id="url-webhook-path",
        ),
        pytest.param(
            "http://192.168.1.10:8123/",
            "http://192.168.1.10:8123/",
            id="url-plain",
        ),
        pytest.param("sk_live_4eC39HqLyjWDarjtT1zdp7dc", HIDDEN, id="opaque-key"),
        pytest.param("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc123", HIDDEN, id="jwt"),
        pytest.param("-----BEGIN PRIVATE KEY-----\nMIIB...", HIDDEN, id="pem"),
        pytest.param(
            "light.living_room_ceiling_main",
            "light.living_room_ceiling_main",
            id="entity-id-no-digit",
        ),
        pytest.param("sensor.temp_2", "sensor.temp_2", id="entity-id-short"),
        pytest.param("Home test", "Home test", id="whitespace"),
        pytest.param(
            "abcdefghijklmnopqrstuvwxyz",
            "abcdefghijklmnopqrstuvwxyz",
            id="letters-only",
        ),
        pytest.param(
            ["a", "sk_live_4eC39HqLyjWDarjtT1zdp7dc"], ["a", HIDDEN], id="list"
        ),
        pytest.param(
            {"a": "sk_live_4eC39HqLyjWDarjtT1zdp7dc"}, {"a": HIDDEN}, id="dict"
        ),
        pytest.param(5, 5, id="non-string"),
        pytest.param(
            "https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/sendMessage",
            "https://api.telegram.org/•••/sendMessage",
            id="url-colon-token-segment",
        ),
        pytest.param(
            " https://u:p@host/x", "https://•••@host/x", id="url-leading-space"
        ),
        pytest.param(
            "https://host/x;sess=abc123def456ghi789jkl",
            "https://host/x;sess=•••",
            id="url-path-param",
        ),
        pytest.param("http://[bad/x", HIDDEN, id="url-unparseable"),
        pytest.param("Bearer eyJhbGciOiJIUzI1NiJ9.abc.def", HIDDEN, id="bearer"),
        pytest.param("Basic dXNlcjpwYXNzd29yZA==", HIDDEN, id="basic"),
        pytest.param("token abc", HIDDEN, id="token-scheme-lower"),
        pytest.param("DIGEST username=x", HIDDEN, id="digest-upper"),
        pytest.param(" Bearer x ", HIDDEN, id="bearer-spaces"),
        pytest.param("Bearer", "Bearer", id="scheme-word-alone"),
        pytest.param("Basic setup", HIDDEN, id="scheme-then-word"),
        pytest.param("user:pass@host:1883", "•••@host:1883", id="creds-no-scheme"),
        pytest.param(
            "mqtt:s3cr3t:x@broker.local", "•••@broker.local", id="creds-colon-pass"
        ),
        pytest.param("me@example.com", "me@example.com", id="email"),
        pytest.param("a: b@c", "a: b@c", id="colon-space-not-creds"),
        pytest.param(b"secret-bytes", HIDDEN, id="bytes"),
        pytest.param(bytearray(b"secret"), HIDDEN, id="bytearray"),
    ],
)
def test_redact_value_shapes(value: Any, expected: Any) -> None:
    """Redaction by value shape."""
    assert redact_value(value) == expected


def test_diff_names_only_when_values_off() -> None:
    """Without include_values only changed names are returned."""
    assert diff_details(
        {"location_name": "Home", "latitude": 1.0},
        {"location_name": "Home test", "latitude": 2.0},
        include_values=False,
    ) == {"changed": ["latitude", "location_name"]}


def test_diff_nothing_changed() -> None:
    """Identical inputs give an empty change list."""
    assert diff_details({"a": 1}, {"a": 1}, include_values=True) == {"changed": []}


def test_diff_values_redacted_when_on() -> None:
    """Sensitive names are hidden and others carried."""
    d = diff_details(
        {"location_name": "Home", "latitude": 1.0, "same": 1},
        {"location_name": "Home test", "latitude": 2.0, "same": 1},
        include_values=True,
    )
    assert d == {
        "changed": ["latitude", "location_name"],
        "values": {
            "latitude": HIDDEN,
            "location_name": {"before": "Home", "after": "Home test"},
        },
    }


def test_diff_nested_sensitive_part_hidden() -> None:
    """A sensitive key inside a nested value hides only that part."""
    d = diff_details(
        {"create_backup": {"password": "a", "name": "x"}},
        {"create_backup": {"password": "b", "name": "y"}},
        include_values=True,
    )
    assert d["values"]["create_backup"] == {
        "before": {"password": HIDDEN, "name": "x"},
        "after": {"password": HIDDEN, "name": "y"},
    }


def test_diff_missing_key_is_none() -> None:
    """A key absent on one side reads as None."""
    assert diff_details({}, {"a": 1}, include_values=True)["values"] == {
        "a": {"before": None, "after": 1}
    }


def test_cap_value_long() -> None:
    """Long values are truncated with an ellipsis."""
    out = cap_value("x" * 5000)
    assert len(out) <= 1025 and out.endswith("…")


def test_cap_value_short_and_unserialisable() -> None:
    """Short values pass through; odd types become text."""
    assert cap_value({"a": [1, "b"]}) == {"a": [1, "b"]}
    assert cap_value({1, 2}) in ("{1, 2}", "{2, 1}")


def test_diff_enum_key_does_not_raise() -> None:
    """Non-JSON dict keys are converted to text."""

    class Mode(enum.Enum):
        A = 1

    d = diff_details({"x": {Mode.A: 1}}, {"x": {Mode.A: 2}}, include_values=True)
    assert d["changed"] == ["x"]
    assert isinstance(d["values"]["x"]["after"], dict)


def test_diff_scrape_headers_hidden() -> None:
    """Scrape keeps request headers in its options (Core shape)."""
    before = {
        "resource": "https://example.com",
        "method": "GET",
        "auth": {},
        "additional": {
            "headers": {"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.abc.def"},
            "verify_ssl": True,
            "timeout": 10,
            "encoding": "UTF-8",
        },
    }
    after = {
        **before,
        "additional": {
            **before["additional"],
            "headers": {
                "Authorization": "Basic dXNlcjpwYXNzd29yZA==",
                "X-Api": "Token abc",
                "Accept": "text/html",
            },
        },
    }

    details = diff_details(before, after, include_values=True)

    assert details["changed"] == ["additional"]
    value = details["values"]["additional"]
    assert value["before"]["headers"] == {"Authorization": HIDDEN}
    assert value["after"]["headers"] == {
        "Authorization": HIDDEN,
        "X-Api": HIDDEN,
        "Accept": "text/html",
    }
    assert "dXNlcjpwYXNzd29yZA" not in repr(details)
    assert "eyJhbGci" not in repr(details)


def test_diff_flat_scrape_headers_hidden() -> None:
    """Headers as a top-level option are hidden too."""
    details = diff_details(
        {"headers": {"Authorization": "Bearer abc"}},
        {"headers": {"Authorization": "Bearer xyz"}},
        include_values=True,
    )

    # Only the hidden token differs, so the whole change is hidden.
    assert details["values"]["headers"] == HIDDEN


def test_diff_alarm_code_hidden() -> None:
    """Satel Integra and SimpliSafe keep the alarm code under `code` (CONF_CODE)."""
    details = diff_details({"code": "1234"}, {"code": "5678"}, include_values=True)

    assert details == {"changed": ["code"], "values": {"code": HIDDEN}}


def test_diff_backup_password_only_change_hidden() -> None:
    """A create_backup change of only its password is hidden as a whole."""
    base = {
        "name": "MyName",
        "agent_ids": ["hassio.local"],
        "include_database": True,
        "password": "old-pass",
    }
    details = diff_details(
        {"create_backup": base},
        {"create_backup": {**base, "password": "new-pass"}},
        include_values=True,
    )

    assert details == {
        "changed": ["create_backup"],
        "values": {"create_backup": HIDDEN},
    }


def test_diff_scrape_token_only_change_hidden() -> None:
    """A Scrape headers change of only the Authorization token is hidden."""
    details = diff_details(
        {"headers": {"Authorization": "Bearer abc", "Accept": "text/html"}},
        {"headers": {"Authorization": "Bearer xyz", "Accept": "text/html"}},
        include_values=True,
    )

    assert details["values"] == {"headers": HIDDEN}


def test_diff_visible_change_alongside_hidden_kept() -> None:
    """A change with a visible difference keeps its values."""
    details = diff_details(
        {"create_backup": {"name": "N", "password": "a", "include_database": True}},
        {"create_backup": {"name": "N", "password": "b", "include_database": False}},
        include_values=True,
    )

    assert details["values"]["create_backup"] == {
        "before": {"name": "N", "password": HIDDEN, "include_database": True},
        "after": {"name": "N", "password": HIDDEN, "include_database": False},
    }
