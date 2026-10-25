"""Electric Fence's IP bans: banning, expiry, unban and reconcile."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any, Literal, Protocol

from homeassistant.core import CALLBACK_TYPE, HassJob, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from ..contracts import check_contracts
from ..models import (
    ChangeAction,
    ChangeKind,
    LoginAttempt,
    LoginReason,
    LoginResult,
    SettingsChange,
    UserExists,
)
from ..notifier import EVENT_IP_BANNED
from ..repairs import async_report_disabled_collector
from ..storage import BanRow, FenceStore
from .backend import BlockingBackend
from .network import IpAddress, is_exempt, normalise_ip

if TYPE_CHECKING:
    from ..runtime import Runtime

_LOGGER = logging.getLogger(__name__)

ENRICH_TIMEOUT = 5.0
REPEAT_WINDOW = timedelta(days=30)
RECENT_WINDOW = timedelta(hours=24)
PERMANENT_FROM = 4
# A due ban whose removal failed is retried after this, not in a tight loop.
RETRY_DELAY = timedelta(minutes=1)
UNAVAILABLE_CONTRACTS = "contracts"
_PAGE = 500

type EndReason = Literal["expired", "unbanned", "removed"]


class BanAlerts(Protocol):
    """The alert calls the ban manager makes (implemented by Task 9's Alerter)."""

    def ip_banned(self, view: BanView) -> None:
        """Alert that an address was banned."""

    def ban_ended(self, view: BanView, how: Literal["expired", "unbanned"]) -> None:
        """Alert that a ban ended."""


@dataclass(frozen=True, slots=True)
class BanView:
    """An active ban, Electric Fence's own or one it found in Core."""

    ip: IpAddress
    source: Literal["electric_fence", "other"]
    banned_at: datetime
    expires_at: datetime | None
    failures: int | None
    ban_number: int | None
    country_code: str | None
    hostname: str | None
    ban_id: int | None


def _view(row: BanRow) -> BanView:
    addr = normalise_ip(row.ip)
    assert addr is not None
    return BanView(
        ip=addr,
        source="electric_fence",
        banned_at=row.banned_at,
        expires_at=row.expires_at,
        failures=row.failures,
        ban_number=row.ban_number,
        country_code=row.country_code,
        hostname=row.hostname,
        ban_id=row.id,
    )


def _other_view(addr: IpAddress, banned_at: datetime) -> BanView:
    return BanView(
        ip=addr,
        source="other",
        banned_at=banned_at,
        expires_at=None,
        failures=None,
        ban_number=None,
        country_code=None,
        hostname=None,
        ban_id=None,
    )


def ban_length(ban_hours: int, repeat: bool, ban_number: int) -> timedelta | None:
    """Return how long a ban lasts; None means permanent."""
    if ban_hours == 0:
        return None
    if not repeat:
        return timedelta(hours=ban_hours)
    if ban_number >= PERMANENT_FROM:
        return None
    return timedelta(hours=ban_hours * 2 ** (ban_number - 1))


@dataclass(slots=True)
class _Loaded:
    active: list[BanRow]
    recent: list[datetime]
    last_ban_at: datetime | None


def _load(store: FenceStore, since: datetime) -> _Loaded:
    """Read the active bans and the ban times since `since`."""
    active = store.active_bans()
    times = [row.banned_at for row in active]
    before_id: int | None = None
    while True:
        page = store.query_bans(active=False, limit=_PAGE, before_id=before_id)
        times.extend(row.banned_at for row in page)
        # Newest first by id, which follows ban time.
        if len(page) < _PAGE or page[-1].banned_at < since:
            break
        before_id = page[-1].id
    return _Loaded(
        active=active,
        recent=sorted(t for t in times if t >= since),
        last_ban_at=max(times, default=None),
    )


class BanManager:
    """Owns Electric Fence's bans. Every public method fails open."""

    def __init__(
        self, hass: HomeAssistant, runtime: Runtime, backend: BlockingBackend
    ) -> None:
        """Initialise; nothing is read until `async_start`."""
        self._hass = hass
        self._runtime = runtime
        self.backend = backend
        self.last_ban_at: datetime | None = None
        self._rows: dict[IpAddress, BanRow] = {}
        self._recent: list[datetime] = []
        self._contract_reasons: list[str] = []
        self._lock = asyncio.Lock()
        self._listeners: list[Callable[[], None]] = []
        self._unsub_expiry: CALLBACK_TYPE | None = None
        self._unsub_started: CALLBACK_TYPE | None = None
        self._stopped = False
        self._expiry_job = HassJob(
            self._async_expire_due, "electric_fence ban expiry", cancel_on_shutdown=True
        )

    @property
    def unavailable_reason(self) -> str | None:
        """Return None when bans work, otherwise why they don't."""
        if self._contract_reasons:
            return UNAVAILABLE_CONTRACTS
        try:
            return self.backend.health()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Ban backend health failed: %s", type(err).__name__)
            return "backend_error"

    @property
    def available(self) -> bool:
        """Return whether bans can be applied."""
        return self.unavailable_reason is None

    async def async_start(self) -> None:
        """Check contracts, load the active bans, reconcile once started."""
        self._contract_reasons = check_contracts(self._hass, "protection")
        if self._contract_reasons:
            async_report_disabled_collector(
                self._hass, "protection", self._contract_reasons
            )
        try:
            loaded = await self._hass.async_add_executor_job(
                _load, self._runtime.store, dt_util.utcnow() - RECENT_WINDOW
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not load the bans: %s", type(err).__name__)
        else:
            for row in loaded.active:
                if (addr := normalise_ip(row.ip)) is not None:
                    self._rows[addr] = row
            self._recent = loaded.recent
            self.last_ban_at = loaded.last_ban_at
        # Core replaces its lookup when the HTTP server starts, so the lookup
        # is only meaningful once Home Assistant has started.
        self._unsub_started = async_at_started(self._hass, self._async_reconcile)

    @callback
    def async_stop(self) -> None:
        """Cancel the timers; a change still in flight does not re-arm them."""
        self._stopped = True
        if self._unsub_started is not None:
            self._unsub_started()
            self._unsub_started = None
        if self._unsub_expiry is not None:
            self._unsub_expiry()
            self._unsub_expiry = None

    def add_listener(self, cb: Callable[[], None]) -> CALLBACK_TYPE:
        """Call `cb` after any change; returns the remover."""
        self._listeners.append(cb)

        @callback
        def remove() -> None:
            if cb in self._listeners:
                self._listeners.remove(cb)

        return remove

    def active(self) -> list[BanView]:
        """Return the active bans, Electric Fence's first, without I/O."""
        views = [_view(row) for row in self._rows.values()]
        views.extend(
            _other_view(addr, banned_at)
            for addr, banned_at in self._backend_ips().items()
            if addr not in self._rows
        )
        return views

    def bans_24h(self) -> int:
        """Return the number of Electric Fence bans in the last 24 hours."""
        since = dt_util.utcnow() - RECENT_WINDOW
        self._recent = [t for t in self._recent if t >= since]
        return len(self._recent)

    async def async_ban(
        self,
        ip: str,
        failures: int,
        country_code: str | None,
        hostname: str | None,
    ) -> bool:
        """Ban an address; False when unavailable, exempt or already banned."""
        try:
            return await self._async_ban(ip, failures, country_code, hostname)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Ban failed: %s", type(err).__name__)
            return False

    async def _async_ban(
        self,
        ip: str,
        failures: int,
        country_code: str | None,
        hostname: str | None,
    ) -> bool:
        if not self.available:
            return False
        addr = normalise_ip(ip)
        if addr is None or is_exempt(self._hass, addr):
            return False
        async with self._lock:
            if addr in self._rows or addr in self._backend_ips():
                return False
            try:
                await self.backend.async_add(addr)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Could not ban an address: %s", type(err).__name__)
                return False
            now = dt_util.utcnow()
            settings = self._runtime.settings
            ban_number = await self._async_ban_number(addr, now)
            length = ban_length(
                settings.ban_hours, settings.ban_repeat_offenders, ban_number
            )
            expires_at = now + length if length is not None else None
            if country_code is None or hostname is None:
                found_country, found_host = await self._async_enrich(addr)
                country_code = country_code or found_country
                hostname = hostname or found_host
            row = await self._async_store_ban(
                addr, now, expires_at, failures, ban_number, country_code, hostname
            )
            self._recent.append(now)
            self.last_ban_at = now
            view = (
                _view(row)
                if row is not None
                else _other_view(addr, self._backend_ips().get(addr, now))
            )
            self._submit(
                ChangeAction.BANNED,
                addr,
                {
                    "failures": failures,
                    "expires_at": expires_at.isoformat() if expires_at else None,
                    "ban_number": ban_number,
                },
            )
            self._hass.bus.async_fire(
                EVENT_IP_BANNED,
                {
                    "ip": str(addr),
                    "country": country_code,
                    "hostname": hostname,
                    "failures": failures,
                    "expires_at": expires_at.isoformat() if expires_at else None,
                    "ban_number": ban_number,
                },
            )
            self._alert(lambda alerts: alerts.ip_banned(view))
            self._changed()
        return True

    async def async_unban(
        self, ip: str, by_user_id: str | None, by_name: str | None
    ) -> bool:
        """Lift a ban, Electric Fence's or another; False when not banned."""
        try:
            return await self._async_unban(ip, by_user_id, by_name)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Unban failed: %s", type(err).__name__)
            return False

    async def _async_unban(
        self, ip: str, by_user_id: str | None, by_name: str | None
    ) -> bool:
        if not self.available or (addr := normalise_ip(ip)) is None:
            return False
        async with self._lock:
            row = self._rows.get(addr)
            if row is not None:
                view = _view(row)
            elif (banned_at := self._backend_ips().get(addr)) is not None:
                view = _other_view(addr, banned_at)
            else:
                return False
            if not await self._async_backend_remove(addr):
                return False
            if row is not None:
                await self._async_end_row(row, "unbanned", by_user_id, by_name)
            self._submit(
                ChangeAction.UNBANNED,
                addr,
                {"source": view.source},
                actor_user_id=by_user_id,
                actor_name=by_name,
            )
            self._alert(lambda alerts: alerts.ban_ended(view, "unbanned"))
            self._changed()
        return True

    async def _async_reconcile(self, _hass: HomeAssistant) -> None:
        """End bans that expired while off or were removed from the file."""
        self._unsub_started = None
        async with self._lock:
            if self.available:
                now = dt_util.utcnow()
                present = self._backend_ips()
                for row in list(self._rows.values()):
                    if row.expires_at is not None and row.expires_at <= now:
                        await self._async_expire(row)
                    elif normalise_ip(row.ip) not in present:
                        await self._async_end_row(row, "removed", None, None)
            self._changed()

    async def _async_expire_due(self, _now: datetime) -> None:
        self._unsub_expiry = None
        async with self._lock:
            now = dt_util.utcnow()
            for row in list(self._rows.values()):
                if row.expires_at is not None and row.expires_at <= now:
                    await self._async_expire(row)
            self._changed()

    async def _async_expire(self, row: BanRow) -> None:
        addr = normalise_ip(row.ip)
        assert addr is not None
        if not await self._async_backend_remove(addr):
            return
        view = _view(row)
        await self._async_end_row(row, "expired", None, None)
        self._submit(ChangeAction.EXPIRED, addr, {"ban_number": row.ban_number})
        self._alert(lambda alerts: alerts.ban_ended(view, "expired"))

    def _schedule(self) -> None:
        """Arm one timer for the earliest expiry."""
        if self._unsub_expiry is not None:
            self._unsub_expiry()
            self._unsub_expiry = None
        if self._stopped:
            return
        expiries = [r.expires_at for r in self._rows.values() if r.expires_at]
        if not expiries:
            return
        when = min(expiries)
        now = dt_util.utcnow()
        if when <= now:
            when = now + RETRY_DELAY
        self._unsub_expiry = async_track_point_in_utc_time(
            self._hass, self._expiry_job, when
        )

    def _changed(self) -> None:
        self._schedule()
        for cb in list(self._listeners):
            try:
                cb()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Ban listener failed: %s", type(err).__name__)

    def _backend_ips(self) -> dict[IpAddress, datetime]:
        try:
            return self.backend.list_ips()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not list the bans: %s", type(err).__name__)
            return {}

    async def _async_backend_remove(self, addr: IpAddress) -> bool:
        try:
            return await self.backend.async_remove(addr)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not lift a ban: %s", type(err).__name__)
            return False

    async def _async_ban_number(self, addr: IpAddress, now: datetime) -> int:
        try:
            count = await self._hass.async_add_executor_job(
                self._runtime.store.count_ip_bans_since, str(addr), now - REPEAT_WINDOW
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not count earlier bans: %s", type(err).__name__)
            return 1
        return int(count) + 1

    async def _async_enrich(self, addr: IpAddress) -> tuple[str | None, str | None]:
        probe = LoginAttempt(
            ts=dt_util.utcnow(),
            result=LoginResult.FAILED,
            reason=LoginReason.INVALID_CREDENTIALS,
            username_typed=None,
            user_exists=UserExists.UNKNOWN,
            user_id=None,
            provider=None,
            ip=str(addr),
            source=None,
            url=None,
            user_agent=None,
            client_id=None,
        )
        try:
            async with asyncio.timeout(ENRICH_TIMEOUT):
                await self._runtime.enricher.async_enrich(probe)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Ban enrichment failed: %s", type(err).__name__)
            return None, None
        return probe.country_code, probe.hostname

    async def _async_store_ban(
        self,
        addr: IpAddress,
        now: datetime,
        expires_at: datetime | None,
        failures: int,
        ban_number: int,
        country_code: str | None,
        hostname: str | None,
    ) -> BanRow | None:
        try:
            ban_id = await self._hass.async_add_executor_job(
                self._runtime.store.add_ban,
                str(addr),
                now,
                expires_at,
                failures,
                ban_number,
                country_code,
                hostname,
            )
        except Exception as err:  # noqa: BLE001
            # The address stays banned; it shows as another ban, with no expiry.
            _LOGGER.warning("Could not store a ban: %s", type(err).__name__)
            return None
        row = BanRow(
            id=ban_id,
            ip=str(addr),
            banned_at=now,
            expires_at=expires_at,
            failures=failures,
            ban_number=ban_number,
            country_code=country_code,
            hostname=hostname,
            ended_at=None,
            end_reason=None,
            ended_by_user_id=None,
            ended_by_name=None,
        )
        self._rows[addr] = row
        return row

    async def _async_end_row(
        self,
        row: BanRow,
        reason: EndReason,
        by_user_id: str | None,
        by_name: str | None,
    ) -> None:
        if (addr := normalise_ip(row.ip)) is not None:
            self._rows.pop(addr, None)
        try:
            await self._hass.async_add_executor_job(
                self._runtime.store.end_ban,
                row.id,
                dt_util.utcnow(),
                reason,
                by_user_id,
                by_name,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not store a ban's end: %s", type(err).__name__)

    def _submit(
        self,
        action: ChangeAction,
        addr: IpAddress,
        details: dict[str, Any],
        *,
        actor_user_id: str | None = None,
        actor_name: str | None = None,
    ) -> None:
        self._runtime.worker.submit(
            SettingsChange(
                ts=dt_util.utcnow(),
                kind=ChangeKind.IP_BAN,
                action=action,
                target_id=str(addr),
                target_name=None,
                details=details,
                actor_user_id=actor_user_id,
                actor_name=actor_name,
                actor_ip=None,
            )
        )

    def _alert(self, call: Callable[[BanAlerts], None]) -> None:
        if (alerts := self._runtime.alerts) is None:
            return
        try:
            call(alerts)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Ban alert failed: %s", type(err).__name__)
