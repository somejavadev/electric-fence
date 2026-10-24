"""Reverse DNS with a timeout and an LRU cache."""

import asyncio
from collections import OrderedDict
from functools import partial
import socket
import time

from homeassistant.core import HomeAssistant

_MAX_HOSTNAME = 253


def _sanitise(name: str) -> str | None:
    """Return a printable-ASCII hostname, or None. PTR records are untrusted."""
    if not name or len(name) > _MAX_HOSTNAME or not name.isascii():
        return None
    if not name.isprintable() or " " in name:
        return None
    return name


def _lookup(ip: str) -> str | None:
    try:
        return _sanitise(socket.gethostbyaddr(ip)[0])
    except OSError, UnicodeError:
        return None


class ReverseDns:
    """Look up hostnames off the event loop. Failures and misses are cached."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        ttl: float = 86400,
        timeout: float = 2.0,
        maxsize: int = 4096,
    ) -> None:
        """Initialise an empty cache."""
        self._hass = hass
        self._ttl = ttl
        self._timeout = timeout
        self._maxsize = maxsize
        self._cache: OrderedDict[str, tuple[float, str | None]] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[str | None]] = {}

    async def async_lookup(self, ip: str) -> str | None:
        """Return the hostname of `ip`, or None (also on timeout).

        Concurrent lookups of one IP share a single resolution.
        """
        if (hit := self._cache.get(ip)) is not None and hit[0] > time.monotonic():
            self._cache.move_to_end(ip)
            return hit[1]
        task = self._inflight.get(ip)
        if task is None or task.done():
            task = self._hass.async_create_background_task(
                self._resolve(ip), "electric_fence rdns"
            )
            self._inflight[ip] = task
            # Not in _resolve: an eagerly started task can finish before the
            # line above runs.
            task.add_done_callback(partial(self._forget, ip))
        # A cancelled waiter must not cancel the lookup the others wait for.
        return await asyncio.shield(task)

    def _forget(self, ip: str, task: asyncio.Task[str | None]) -> None:
        if self._inflight.get(ip) is task:
            del self._inflight[ip]

    async def _resolve(self, ip: str) -> str | None:
        name: str | None = None
        try:
            async with asyncio.timeout(self._timeout):
                name = await self._hass.async_add_executor_job(_lookup, ip)
        except Exception:  # noqa: BLE001
            # Includes the timeout: cached as a miss, so a slow resolver is
            # not asked again for every attempt from the same address.
            name = None
        self._cache[ip] = (time.monotonic() + self._ttl, name)
        self._cache.move_to_end(ip)
        while len(self._cache) > self._maxsize:
            self._cache.popitem(last=False)
        return name
