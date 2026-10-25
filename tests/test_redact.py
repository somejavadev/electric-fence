"""Tests for settings change redaction and diffing."""

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
