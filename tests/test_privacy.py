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
    assert out.startswith("ab\u202e")


def test_sanitise_text_none_and_controls():
    """Check sanitise text none and controls."""
    assert sanitise_text(None) is None
    assert sanitise_text("a\tb\nc\x7fd\x1f") == "abcd"


def test_md_code_cannot_break_out():
    """Check md code cannot break out."""
    assert md_code("`](javascript:x)\n**b**") == "`ˋ](javascript:x) **b**`"


def test_inline_text_single_line() -> None:
    """Controls are stripped and Unicode line breaks become spaces."""
    assert inline_text("a\x00b\nc\rd\x7fe") == "abcde"
    assert inline_text("a b c\u0085d") == "a b c d"
    assert inline_text("x" * 300) == "x" * 256
    assert inline_text("x" * 10, limit=3) == "xxx"
    assert inline_text("") == ""
