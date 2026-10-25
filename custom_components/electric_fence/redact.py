"""Redaction and diffing of settings values before they are stored."""

from collections.abc import Mapping
import json
import re
from typing import Any, Final
from urllib.parse import urlsplit

HIDDEN: Final = {"hidden": True}

_MASK: Final = "•••"
_CAP: Final = 1024
_STRONG: Final = (
    "password",
    "passwd",
    "passphrase",
    "secret",
    "token",
    "apikey",
    "credential",
    "cookie",
    "latitude",
    "longitude",
    "elevation",
)
_SHORT: Final = frozenset(
    {"pass", "pwd", "pin", "key", "auth", "radius", "lat", "lon", "lng", "psk"}
)
_CAMEL: Final = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SPLIT: Final = re.compile(r"[_\-. ]+")
_OPAQUE: Final = re.compile(r"[A-Za-z0-9\-_+/=.~]{20,}")
_URL: Final = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")


def is_sensitive_name(name: str) -> bool:
    """Return whether a setting name suggests a secret or a location."""
    spaced = _CAMEL.sub("_", name).lower()
    lowered = name.lower()
    if any(word in lowered for word in _STRONG):
        return True
    return any(tok in _SHORT for tok in _SPLIT.split(spaced))


def _is_opaque(text: str) -> bool:
    """Return whether a string looks like a key, token or hash."""
    return (
        _OPAQUE.fullmatch(text) is not None
        and any(c.isalpha() for c in text)
        and any(c.isdigit() for c in text)
    )


def _redact_url(text: str) -> str:
    """Mask credentials, query values, fragment and opaque path segments."""
    parts = urlsplit(text)
    host = parts.netloc
    if "@" in host:
        host = f"{_MASK}@{host.rpartition('@')[2]}"
    path = "/".join(_MASK if _is_opaque(s) else s for s in parts.path.split("/"))
    out = f"{parts.scheme}://{host}{path}"
    if parts.query:
        pairs = (p.partition("=") for p in parts.query.split("&"))
        out += "?" + "&".join(f"{k}={_MASK}" if eq else k for k, eq, _ in pairs)
    if parts.fragment:
        out += f"#{_MASK}"
    return out


def _scrub(value: Any, *, by_name: bool) -> Any:
    """Redact by shape, and by key name too when by_name is set."""
    if isinstance(value, str):
        if _URL.match(value):
            return _redact_url(value)
        if "-----BEGIN" in value or _is_opaque(value):
            return dict(HIDDEN)
        return value
    if isinstance(value, Mapping):
        return {
            k: dict(HIDDEN)
            if by_name and is_sensitive_name(str(k))
            else _scrub(v, by_name=by_name)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_scrub(v, by_name=by_name) for v in value]
    return value


def redact_value(value: Any) -> Any:
    """Redact URLs, opaque strings and PEM blocks, recursing into containers."""
    return _scrub(value, by_name=False)


def cap_value(value: Any) -> Any:
    """Return a JSON-safe copy, truncated when its JSON text is too long."""
    text = json.dumps(value, default=str)
    if len(text) > _CAP:
        return text[:_CAP] + "…"
    return json.loads(text)


def diff_details(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    include_values: bool,
) -> dict[str, Any]:
    """Return changed setting names, plus redacted values when asked."""
    changed = sorted(
        k for k in before.keys() | after.keys() if before.get(k) != after.get(k)
    )
    details: dict[str, Any] = {"changed": changed}
    if include_values and changed:
        details["values"] = {
            name: dict(HIDDEN)
            if is_sensitive_name(name)
            else {
                "before": cap_value(_scrub(before.get(name), by_name=True)),
                "after": cap_value(_scrub(after.get(name), by_name=True)),
            }
            for name in changed
        }
    return details
