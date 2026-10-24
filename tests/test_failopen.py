"""Tests for the fail-open guard."""

from freezegun.api import FrozenDateTimeFactory
import pytest

from custom_components.electric_fence.failopen import FailOpen


def test_failopen_swallows_and_rate_limits(
    caplog: pytest.LogCaptureFixture, freezer: FrozenDateTimeFactory
) -> None:
    """Errors are swallowed, counted, and logged at most once a minute."""
    g = FailOpen("x")
    for _ in range(3):
        assert g.call(lambda: 1 / 0) is None
    assert g.errors == 3
    assert caplog.text.count("Electric Fence hook x failed") == 1
    freezer.tick(61)
    g.call(lambda: 1 / 0)
    assert caplog.text.count("Electric Fence hook x failed") == 2


def test_failopen_returns_result_and_never_logs_arguments(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Results pass through and arguments stay out of the log."""
    g = FailOpen("x")
    assert g.call(lambda a, b=0: a + b, 1, b=2) == 3
    assert g.errors == 0

    def boom(name: str) -> None:
        raise ValueError("bad")

    g.call(boom, "secret-user")
    assert "secret-user" not in caplog.text
