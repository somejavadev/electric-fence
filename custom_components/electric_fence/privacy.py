"""Privacy and sanitising helpers."""

import re

from .models import UserExists

MAX_TEXT = 256
REDACTED = "**REDACTED**"

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
# Line breaks that Markdown renderers and logs may honour, beyond C0 controls.
_LINE_BREAKS = str.maketrans({"\u2028": " ", "\u2029": " ", "\u0085": " "})


def sanitise_text(value: str | None, limit: int = MAX_TEXT) -> str | None:
    """Remove C0 control characters and DEL, then truncate to limit."""
    if value is None:
        return None
    return _CONTROL_RE.sub("", value)[:limit]


def username_key(username: str | None) -> str | None:
    """Return the key a typed username is counted under, None for none.

    Matches Core's own normalising (strip, casefold) after sanitising.
    """
    if username is None:
        return None
    return (sanitise_text(username) or "").strip().casefold() or None


def inline_text(value: str, limit: int = MAX_TEXT) -> str:
    """Return `value` as a single line, safe to mask or put in `md_code`.

    Strips C0 controls and DEL, truncates, and turns U+2028, U+2029 and U+0085
    into spaces.
    """
    return (sanitise_text(value, limit) or "").translate(_LINE_BREAKS)


def mask_username(value: str | None, exists: UserExists) -> tuple[str | None, bool]:
    """Return (display, masked) for a typed username."""
    if value is None:
        return None, False
    if exists is UserExists.YES:
        return value, False
    if len(value) < 3:
        return "•••", True
    return value[:2] + "•••", True


def md_code(value: str) -> str:
    """Return Markdown inline code that cannot be broken out of."""
    flat = value.replace("`", "ˋ").replace("\r", " ").replace("\n", " ")
    return f"`{flat}`"
