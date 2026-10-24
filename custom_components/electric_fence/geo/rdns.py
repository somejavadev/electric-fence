"""Reverse DNS with a timeout and an LRU cache."""

import asyncio
from collections import OrderedDict
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

    async def async_lookup(self, ip: str) -> str | None:
        """Return the hostname of `ip`, or None (also on timeout)."""
        now = time.monotonic()
        if (hit := self._cache.get(ip)) is not None and hit[0] > now:
            self._cache.move_to_end(ip)
            return hit[1]
        try:
            async with asyncio.timeout(self._timeout):
                name = await self._hass.async_add_executor_job(_lookup, ip)
        except TimeoutError:
            # Not cached: the next attempt may succeed.
            return None
        self._cache[ip] = (time.monotonic() + self._ttl, name)
        self._cache.move_to_end(ip)
        while len(self._cache) > self._maxsize:
            self._cache.popitem(last=False)
        return name
