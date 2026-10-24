"""Privacy and sanitising helpers."""

import re

from .models import UserExists

MAX_TEXT = 256
REDACTED = "**REDACTED**"

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def sanitise_text(value: str | None, limit: int = MAX_TEXT) -> str | None:
    """Remove C0 control characters and DEL, then truncate to limit."""
    if value is None:
        return None
    return _CONTROL_RE.sub("", value)[:limit]


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
