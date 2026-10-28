"""Privacy and sanitising helpers."""

import re

from .models import UserExists

MAX_TEXT = 256
REDACTED = "**REDACTED**"

# C0 controls, DEL and C1 controls (e.g. 0x9B, a terminal escape).
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# Invisible and direction characters: `\u202enimda` reads as "admin" and
# `admin\u200b` looks like "admin". Shown as their code point, not dropped,
# because which character an attacker used is evidence.
_HIDDEN_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")
# Line breaks that Markdown renderers and logs may honour, beyond controls.
_LINE_BREAKS = str.maketrans({"\u2028": " ", "\u2029": " "})


def sanitise_text(value: str | None, limit: int = MAX_TEXT) -> str | None:
    """Remove control characters, show hidden ones, then truncate to limit.

    Hidden characters become `⟨U+202E⟩`. The result sanitises to itself, so a
    username keeps the same key however often it is sanitised.
    """
    if value is None:
        return None
    text = _HIDDEN_RE.sub(_show_hidden, _CONTROL_RE.sub("", value))
    return text[:limit]


def _show_hidden(match: re.Match[str]) -> str:
    return f"⟨U+{ord(match[0]):04X}⟩"


def username_key(username: str | None) -> str | None:
    """Return the key a typed username is counted under, None for none.

    Matches Core's own normalising (strip, casefold) after sanitising.
    """
    if username is None:
        return None
    return (sanitise_text(username) or "").strip().casefold() or None


def inline_text(value: str, limit: int = MAX_TEXT) -> str:
    """Return `value` as a single line, safe to mask or put in `md_code`.

    Sanitises like `sanitise_text` and turns U+2028 and U+2029 into spaces.
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
