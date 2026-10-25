"""Automatic IP bans: count failed logins per address in memory."""

from collections import OrderedDict, deque
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from ..failopen import FailOpen
from ..models import COUNTED_FAILURES, LoginReason, LoginResult
from .network import is_exempt, normalise_ip

if TYPE_CHECKING:
    from ..runtime import Runtime
    from .bans import BanManager

_LOGGER = logging.getLogger(__name__)

MAX_TRACKED = 10_000


class IpPolicy:
    """Ask the ban manager to ban an address that fails too often."""

    def __init__(self, hass: HomeAssistant, runtime: Runtime) -> None:
        """Initialise the policy."""
        self._hass = hass
        self._runtime = runtime
        self._failures: OrderedDict[str, deque[datetime]] = OrderedDict()
        self._trusted: OrderedDict[str, datetime] = OrderedDict()
        self._guard = FailOpen("ip policy")

    def seed_trusted(self, ips: dict[str, datetime]) -> None:
        """Trust addresses that logged in successfully before this run."""
        for ip, when in sorted(ips.items(), key=lambda item: item[1]):
            if (addr := normalise_ip(ip)) is not None:
                self._trust(str(addr), when)

    @callback
    def on_attempt(
        self, ip: str | None, result: LoginResult, reason: LoginReason
    ) -> None:
        """Count a login attempt; memory only, never raises."""
        self._guard.call(self._on_attempt, ip, result, reason)

    def _trust(self, key: str, when: datetime) -> None:
        self._trusted[key] = when
        self._trusted.move_to_end(key)
        while len(self._trusted) > MAX_TRACKED:
            self._trusted.popitem(last=False)

    async def _async_ban(self, bans: BanManager, ip: str, count: int) -> None:
        try:
            await bans.async_ban(ip, count, None, None)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Electric Fence ban request failed: %s", type(err).__name__)

    def _on_attempt(
        self, ip: str | None, result: LoginResult, reason: LoginReason
    ) -> None:
        addr = normalise_ip(ip)
        if addr is None:
            return
        key = str(addr)
        now = dt_util.utcnow()
        if result is LoginResult.SUCCESS:
            self._trust(key, now)
            self._failures.pop(key, None)
            return
        if reason not in COUNTED_FAILURES or is_exempt(self._hass, addr):
            return

        settings = self._runtime.settings
        window = timedelta(minutes=settings.ban_window_minutes)
        attempts = self._failures.get(key)
        if attempts is None:
            attempts = self._failures[key] = deque()
            while len(self._failures) > MAX_TRACKED:
                self._failures.popitem(last=False)
        else:
            self._failures.move_to_end(key)
        attempts.append(now)
        while attempts and now - attempts[0] > window:
            attempts.popleft()
        if len(attempts) < settings.ban_threshold:
            return

        count = len(attempts)
        attempts.clear()
        bans = self._runtime.bans
        if not settings.auto_ban or bans is None or not bans.available:
            return
        trusted_at = self._trusted.get(key)
        if trusted_at is not None and now - trusted_at < settings.trusted_window:
            return
        self._hass.async_create_task(
            self._async_ban(bans, key, count),
            f"electric_fence ban {key}",
            eager_start=True,
        )
