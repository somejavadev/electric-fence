"""Fail-open guard: Electric Fence code must never break Home Assistant."""

from collections.abc import Callable
import logging
import time
import traceback

_LOGGER = logging.getLogger(__name__)


class FailOpen:
    """Run callables, swallowing and rate-limit logging any exception."""

    def __init__(self, name: str, *, log_interval: float = 60.0) -> None:
        """Initialise the guard for the hook called `name`."""
        self.name = name
        self.errors = 0
        self._log_interval = log_interval
        self._last_logged: float | None = None

    def call[**P, R](
        self, fn: Callable[P, R], *args: P.args, **kwargs: P.kwargs
    ) -> R | None:
        """Call `fn`; return its result, or None if it raised."""
        try:
            return fn(*args, **kwargs)
        except Exception as err:  # noqa: BLE001
            self.errors += 1
            now = time.monotonic()
            if (
                self._last_logged is None
                or now - self._last_logged >= self._log_interval
            ):
                self._last_logged = now
                # Never include call arguments or the exception message: they
                # may contain usernames. Log the type and the frames only.
                _LOGGER.error(
                    "Electric Fence hook %s failed: %s\n%s",
                    self.name,
                    type(err).__name__,
                    "".join(traceback.format_tb(err.__traceback__)),
                )
            return None


def reraise(err: Exception) -> None:
    """Re-raise `err` inside a guard so it is counted and logged."""
    raise err
