"""Tests for privacy helpers."""

import pytest

from custom_components.electric_fence.models import UserExists
from custom_components.electric_fence.privacy import (
    inline_text,
    mask_username,
    md_code,
    sanitise_text,
)


@pytest.mark.parametrize(
    ("value", "exists", "expected"),
    [
        ("alice", UserExists.YES, ("alice", False)),
        ("admin", UserExists.NO, ("ad•••", True)),
        ("hunter2-secret", UserExists.UNKNOWN, ("hu•••", True)),
        ("ab", UserExists.NO, ("•••", True)),
        (None, UserExists.NO, (None, False)),
    ],
)
def test_mask_username(value, exists, expected):
    """Check mask username."""
    assert mask_username(value, exists) == expected


def test_typed_username_sanitised_and_truncated():
    """Check typed username sanitised and truncated."""
    raw = "a\x00b\u202e" + "x" * 10_000
    out = sanitise_text(raw)
    assert "\x00" not in out
    assert len(out) == 256
    assert out.startswith("ab⟨U+202E⟩x")


def test_sanitise_text_none_and_controls():
    """Check sanitise text none and controls."""
    assert sanitise_text(None) is None
    assert sanitise_text("a\tb\nc\x7fd\x1f") == "abcd"


def test_sanitise_text_strips_c1_controls() -> None:
    """C1 controls (e.g. CSI 0x9B) never reach logs, the SIEM or a terminal."""
    assert sanitise_text("a\x80b\x9b31mc\x9fd") == "ab31mcd"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("\u202enimda", "⟨U+202E⟩nimda"),
        ("admin\u200b", "admin⟨U+200B⟩"),
        ("a\u200e\u200fb", "a⟨U+200E⟩⟨U+200F⟩b"),
        ("\u2066x\u2069", "⟨U+2066⟩x⟨U+2069⟩"),
        ("\ufeffadmin", "⟨U+FEFF⟩admin"),
        ("a\u2060b", "a⟨U+2060⟩b"),
        ("Ann-Marie Øster", "Ann-Marie Øster"),
    ],
)
def test_sanitise_text_shows_hidden_characters(value: str, expected: str) -> None:
    """Invisible and direction characters are shown, not dropped: they're evidence."""
    assert sanitise_text(value) == expected


def test_sanitise_text_is_idempotent() -> None:
    """Sanitising twice changes nothing, so usernames keep one key."""
    once = sanitise_text("\u202ead\x9bmin\u200b")
    assert sanitise_text(once) == once


def test_md_code_cannot_break_out():
    """Check md code cannot break out."""
    assert md_code("`](javascript:x)\n**b**") == "`ˋ](javascript:x) **b**`"


def test_inline_text_single_line() -> None:
    """Controls are stripped and Unicode line breaks become spaces."""
    assert inline_text("a\x00b\nc\rd\x7fe") == "abcde"
    # NEL is a C1 control, so it is removed like the others.
    assert inline_text("a b c\u0085d") == "a b cd"
    assert inline_text("x" * 300) == "x" * 256
    assert inline_text("x" * 10, limit=3) == "xxx"
    assert inline_text("") == ""
