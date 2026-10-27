"""Reverse DNS with a timeout and an LRU cache."""

import asyncio
from collections import OrderedDict
from functools import partial
import ipaddress
import socket
import time

from homeassistant.core import HomeAssistant

_MAX_HOSTNAME = 253
# A miss for one IPv6 address answers for its /64: one host holds all of it,
# so a hostile reverse zone costs one lookup, not one per address.
_IPV6_MISS_PREFIX = 64


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
        max_running: int = 4,
    ) -> None:
        """Initialise an empty cache."""
        self._hass = hass
        self._ttl = ttl
        self._timeout = timeout
        self._maxsize = maxsize
        self._max_running = max_running
        self._cache: OrderedDict[str, tuple[float, str | None]] = OrderedDict()
        self._missed: OrderedDict[str, float] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[str | None]] = {}
        # Resolver calls still in a thread, including ones past their timeout:
        # a timeout can't stop `gethostbyaddr`.
        self.running = 0

    async def async_lookup(self, ip: str) -> str | None:
        """Return the hostname of `ip`, or None (also on timeout).

        Concurrent lookups of one IP share a single resolution.
        """
        now = time.monotonic()
        if (hit := self._cache.get(ip)) is not None and hit[0] > now:
            self._cache.move_to_end(ip)
            return hit[1]
        if (prefix := _miss_prefix(ip)) is not None and self._missed.get(
            prefix, 0
        ) > now:
            return None
        task = self._inflight.get(ip)
        if task is None or task.done():
            if self.running >= self._max_running:
                return None  # not cached: tried again when the resolver frees up
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

    def _done_running(self, _future: asyncio.Future[str | None]) -> None:
        self.running -= 1

    async def _resolve(self, ip: str) -> str | None:
        name: str | None = None
        job = self._hass.async_add_executor_job(_lookup, ip)
        self.running += 1
        job.add_done_callback(self._done_running)
        try:
            async with asyncio.timeout(self._timeout):
                name = await asyncio.shield(job)
        except Exception:  # noqa: BLE001
            # Includes the timeout: cached as a miss, so a slow resolver is
            # not asked again for every attempt from the same address.
            name = None
        expires = time.monotonic() + self._ttl
        self._cache[ip] = (expires, name)
        self._cache.move_to_end(ip)
        while len(self._cache) > self._maxsize:
            self._cache.popitem(last=False)
        if name is None and (prefix := _miss_prefix(ip)) is not None:
            self._missed[prefix] = expires
            self._missed.move_to_end(prefix)
            while len(self._missed) > self._maxsize:
                self._missed.popitem(last=False)
        return name


def _miss_prefix(ip: str) -> str | None:
    """Return the /64 of an IPv6 address, None for IPv4 or junk."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if not isinstance(addr, ipaddress.IPv6Address):
        return None
    return str(ipaddress.ip_network(f"{addr}/{_IPV6_MISS_PREFIX}", strict=False))
