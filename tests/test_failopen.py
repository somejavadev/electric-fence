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


def test_failopen_does_not_leak_exception_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exception messages and args (which may hold usernames) are not logged."""
    g = FailOpen("x")
    secret = "secret-" + "user"
    assert g.call(lambda u: {}[u], secret) is None

    def boom() -> None:
        raise KeyError(secret)

    g.call(boom)
    FailOpen("y").call(boom)
    assert "secret-user" not in caplog.text
    assert "Electric Fence hook x failed: KeyError" in caplog.text
    assert "boom" in caplog.text


def test_failopen_rate_limit_is_per_instance(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two guards each log their first error."""
    FailOpen("a").call(lambda: 1 / 0)
    FailOpen("b").call(lambda: 1 / 0)
    assert caplog.text.count("Electric Fence hook a failed") == 1
    assert caplog.text.count("Electric Fence hook b failed") == 1
