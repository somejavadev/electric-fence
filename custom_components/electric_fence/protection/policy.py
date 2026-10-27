"""Automatic IP bans: count failed logins per address in memory.

IPv4 is counted per address and IPv6 per prefix (`ipv6_prefix`), since one
IPv6 host holds a whole /64. A confirmed success trusts its address only for
the username that logged in: failures for any other username still ban.
"""

from collections import OrderedDict, deque
from collections.abc import Iterable
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from ..failopen import FailOpen
from ..models import COUNTED_FAILURES, LoginAttempt, LoginReason, LoginResult, Record
from ..privacy import username_key
from .network import IpAddress, address_key, is_exempt, normalise_ip

if TYPE_CHECKING:
    from ..runtime import Runtime
    from .bans import BanManager

_LOGGER = logging.getLogger(__name__)

MAX_TRACKED = 10_000
# Usernames trusted per address: a household shares one public address.
MAX_TRUSTED_USERS = 32

type _Failure = tuple[datetime, str | None]


class IpPolicy:
    """Ask the ban manager to ban an address that fails too often."""

    def __init__(self, hass: HomeAssistant, runtime: Runtime) -> None:
        """Initialise the policy."""
        self._hass = hass
        self._runtime = runtime
        # address key -> (time, username key) of each counted failure
        self._failures: OrderedDict[str, deque[_Failure]] = OrderedDict()
        # address key -> username key -> latest confirmed success
        self._trusted: OrderedDict[str, dict[str, datetime]] = OrderedDict()
        self._guard = FailOpen("ip policy")
        self._record_guard = FailOpen("ip policy record")

    def seed_trusted(
        self, successes: Iterable[tuple[str, str | None, datetime]]
    ) -> None:
        """Trust (address, username) pairs that logged in before this run."""
        for ip, username, when in sorted(successes, key=lambda item: item[2]):
            if (addr := normalise_ip(ip)) is not None:
                self._trust(addr, username, when)

    @callback
    def on_attempt(
        self,
        ip: str | None,
        result: LoginResult,
        reason: LoginReason,
        *,
        username: str | None = None,
    ) -> None:
        """Count a login attempt; memory only, never raises."""
        self._guard.call(self._on_attempt, ip, result, reason, username)

    @callback
    def on_record(self, record: Record) -> None:
        """Trust the address of a confirmed success (worker listener)."""
        self._record_guard.call(self._on_record, record)

    def _key(self, addr: IpAddress) -> str:
        return address_key(addr, self._runtime.settings.ipv6_prefix)

    def _exempt(self, addr: IpAddress) -> bool:
        return is_exempt(
            self._hass, addr, exempt_local=self._runtime.settings.exempt_local
        )

    def _on_record(self, record: Record) -> None:
        # The same rule as the startup seed: a success counts once its token
        # was issued, so an unconfirmed (or blocked) one never trusts.
        if (
            not isinstance(record, LoginAttempt)
            or record.result is not LoginResult.SUCCESS
            or not record.confirmed
            or (addr := normalise_ip(record.ip)) is None
        ):
            return
        self._trust(addr, record.username_typed, record.ts)

    def _trust(self, addr: IpAddress, username: str | None, when: datetime) -> None:
        if (user := username_key(username)) is None or self._exempt(addr):
            return
        key = self._key(addr)
        users = self._trusted.setdefault(key, {})
        users[user] = max(when, users.get(user, when))
        while len(users) > MAX_TRUSTED_USERS:
            del users[min(users, key=users.__getitem__)]
        self._trusted.move_to_end(key)
        while len(self._trusted) > MAX_TRACKED:
            self._trusted.popitem(last=False)

    def _trusted_users(self, key: str, now: datetime) -> set[str]:
        window = self._runtime.settings.trusted_window
        users = self._trusted.get(key, {})
        return {user for user, when in users.items() if now - when < window}

    async def _async_ban(self, bans: BanManager, ip: str, count: int) -> None:
        try:
            await bans.async_ban(ip, count, None, None)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Electric Fence ban request failed: %s", type(err).__name__)

    def _on_attempt(
        self,
        ip: str | None,
        result: LoginResult,
        reason: LoginReason,
        username: str | None,
    ) -> None:
        addr = normalise_ip(ip)
        if addr is None:
            return
        key = self._key(addr)
        user = username_key(username)
        now = dt_util.utcnow()
        if result is LoginResult.SUCCESS:
            # Only this username's failures: a guest login in between must
            # not reset the count of guesses at another account.
            if (attempts := self._failures.get(key)) is not None:
                kept = deque(f for f in attempts if user is None or f[1] != user)
                if kept:
                    self._failures[key] = kept
                else:
                    del self._failures[key]
            return
        if reason not in COUNTED_FAILURES or self._exempt(addr):
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
        attempts.append((now, user))
        while attempts and now - attempts[0][0] > window:
            attempts.popleft()
        if len(attempts) < settings.ban_threshold:
            return

        count = len(attempts)
        users = {u for _, u in attempts}
        attempts.clear()
        bans = self._runtime.bans
        if not settings.auto_ban or bans is None or not bans.available:
            return
        # Trusted only when every failure was for a username that logged in
        # from here recently: the owner mistyping, not someone guessing.
        trusted = self._trusted_users(key, now)
        if trusted and None not in users and users <= trusted:
            return
        if bans.is_blocked(str(addr)):
            return  # its prefix is banned already
        self._hass.async_create_task(
            self._async_ban(bans, str(addr), count),
            f"electric_fence ban {addr}",
            eager_start=True,
        )
