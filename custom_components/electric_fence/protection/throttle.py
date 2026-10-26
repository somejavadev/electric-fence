"""Username attacks: detect them and slow down logins for the username."""

import asyncio
from collections import OrderedDict, deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
import logging
from typing import TYPE_CHECKING, Any, Literal, Protocol

from homeassistant.core import CALLBACK_TYPE, HassJob, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from ..failopen import FailOpen
from ..models import (
    COUNTED_FAILURES,
    ChangeAction,
    ChangeKind,
    LoginAttempt,
    LoginReason,
    LoginResult,
    Record,
    SettingsChange,
    UserExists,
)
from ..notifier import EVENT_USERNAME_ATTACK, display_name, exists_of
from ..privacy import sanitise_text
from .network import is_exempt, normalise_ip

if TYPE_CHECKING:
    from ..runtime import Runtime

_LOGGER = logging.getLogger(__name__)

BASE_DELAY = 2.0
MAX_DELAY = 30.0
MAX_WAITING = 3
THROTTLE_MESSAGE = "Too many login attempts. Try again later."
# Usernames are attacker-controlled, so tracking is bounded.
MAX_TRACKED = 10_000
# The largest allowed attack threshold; older entries never matter.
_MAX_ENTRIES = 1000
# Beyond this the delay is capped anyway; keeps 2**n small.
_MAX_EXPONENT = 16
UPDATE_INTERVAL = timedelta(seconds=10)
# The longest a healthy queue takes to reach an attempt, plus a margin. A
# holder that never releases (Core stalled, a slot leaked) must not lock the
# username out, so a waiter gives up after this and goes on to Core.
ACQUIRE_TIMEOUT = (MAX_WAITING + 1) * MAX_DELAY + 30.0
EVENT_MAX_IPS = 20

# Patched in tests.
_sleep = asyncio.sleep

type EndReason = Literal["quiet", "ended_by_user"]


class Refused(Exception):
    """Too many attempts already wait for this username."""


class AttackAlerts(Protocol):
    """The alert call the guard makes (implemented by Task 9's Alerter)."""

    def username_attack(self, view: AttackView) -> None:
        """Alert that a username is under attack."""


@dataclass(frozen=True, slots=True)
class AttackView:
    """An active username attack."""

    username: str
    started_at: datetime
    failures: int
    ip_count: int
    countries: list[str]
    attack_id: int | None
    # Whether the username belongs to an existing user: only then is it shown
    # in full outside the panel.
    user_exists: bool = False

    def display(self) -> tuple[str, bool]:
        """Return (name, masked) as shown outside the panel."""
        name, masked = display_name(self.username, exists_of(self.user_exists))
        return name or "", masked


@dataclass(slots=True, eq=False)
class _Attack:
    key: str
    username: str
    started_at: datetime
    failures: int
    ips: set[str]
    countries: set[str] = field(default_factory=set)
    user_exists: bool = False
    # Counted failures since the start; the delay doubles with each.
    since_start: int = 0
    attack_id: int | None = None
    id_task: asyncio.Task[int | None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    waiting: int = 0
    # Resolved when the attack ends, so waiting attempts go on to Core in
    # the next loop iteration.
    wakers: set[asyncio.Future[None]] = field(default_factory=set)
    unsub_quiet: CALLBACK_TYPE | None = None
    unsub_update: CALLBACK_TYPE | None = None
    last_update: datetime | None = None

    def view(self) -> AttackView:
        return AttackView(
            username=self.username,
            started_at=self.started_at,
            failures=self.failures,
            ip_count=len(self.ips),
            countries=sorted(self.countries),
            attack_id=self.attack_id,
            user_exists=self.user_exists,
        )

    def wake(self) -> None:
        for waker in self.wakers:
            if not waker.done():
                waker.set_result(None)
        self.wakers.clear()

    def cancel_timers(self) -> None:
        if self.unsub_quiet is not None:
            self.unsub_quiet()
            self.unsub_quiet = None
        if self.unsub_update is not None:
            self.unsub_update()
            self.unsub_update = None


class _Slot:
    """Held while an attempt runs; release after Core's step."""

    __slots__ = ("_lock",)

    def __init__(self, lock: asyncio.Lock) -> None:
        self._lock: asyncio.Lock | None = lock

    def release(self) -> None:
        """Let the next attempt run; safe to call twice."""
        lock, self._lock = self._lock, None
        if lock is not None:
            lock.release()


class UsernameGuard:
    """Detect attacks on one username and slow down its logins."""

    def __init__(self, hass: HomeAssistant, runtime: Runtime) -> None:
        """Initialise the guard."""
        self._hass = hass
        self._runtime = runtime
        self._failures: OrderedDict[str, deque[tuple[datetime, str]]] = OrderedDict()
        self._attacks: dict[str, _Attack] = {}
        self._listeners: list[Callable[[], None]] = []
        self._stopped = False
        self._attempt_guard = FailOpen("username guard")
        self._record_guard = FailOpen("username guard record")
        self._timer_guard = FailOpen("username guard timer")

    @staticmethod
    def key(username: str | None) -> str | None:
        """Return the key a username is counted under, None for none."""
        if username is None:
            return None
        return (sanitise_text(username) or "").strip().casefold() or None

    @callback
    def on_attempt(
        self,
        username: str | None,
        ip: str | None,
        result: LoginResult,
        reason: LoginReason,
        exists: UserExists = UserExists.UNKNOWN,
    ) -> None:
        """Count a login attempt; memory only, never raises."""
        self._attempt_guard.call(self._on_attempt, username, ip, result, reason, exists)

    @callback
    def on_record(self, record: Record) -> None:
        """Take countries from an enriched record (worker listener)."""
        self._record_guard.call(self._on_record, record)

    async def async_enter(self, username: str | None, ip: str | None) -> _Slot | None:
        """Wait for this attempt's turn; None means go straight to Core."""
        try:
            return await self._async_enter(username, ip)
        except Refused:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Login slow-down failed: %s", type(err).__name__)
            return None

    def active_attacks(self) -> list[AttackView]:
        """Return the active attacks, oldest first."""
        attacks = sorted(self._attacks.values(), key=lambda a: a.started_at)
        return [a.view() for a in attacks]

    async def async_end(
        self, username_key: str, by_user_id: str | None, by_name: str | None
    ) -> bool:
        """End an attack's slow-down; False when it isn't under attack."""
        try:
            key = self.key(username_key)
            attack = self._attacks.get(key) if key is not None else None
            if attack is None:
                return False
            return self._end(attack, "ended_by_user", by_user_id, by_name)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Ending a slow-down failed: %s", type(err).__name__)
            return False

    async def async_start(self) -> None:
        """End attacks an earlier run left open; they can't be resumed."""
        try:
            await self._hass.async_add_executor_job(
                self._runtime.store.end_open_attacks, dt_util.utcnow()
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not close old attacks: %s", type(err).__name__)

    @callback
    def async_stop(self) -> None:
        """End the attacks in memory and let every waiting attempt go on."""
        self._stopped = True
        attacks = list(self._attacks.values())
        self._attacks.clear()
        self._failures.clear()
        for attack in attacks:
            attack.cancel_timers()
            attack.wake()

    def add_listener(self, cb: Callable[[], None]) -> CALLBACK_TYPE:
        """Call `cb` after an attack starts, changes or ends; returns the remover."""
        self._listeners.append(cb)

        @callback
        def remove() -> None:
            if cb in self._listeners:
                self._listeners.remove(cb)

        return remove

    def _on_attempt(
        self,
        username: str | None,
        ip: str | None,
        result: LoginResult,
        reason: LoginReason,
        exists: UserExists,
    ) -> None:
        if self._stopped or result is LoginResult.SUCCESS:
            return
        if reason not in COUNTED_FAILURES or (key := self.key(username)) is None:
            return
        addr = normalise_ip(ip)
        if addr is None or is_exempt(self._hass, addr):
            return
        assert username is not None
        ip_key = str(addr)
        if (attack := self._attacks.get(key)) is not None:
            attack.user_exists |= exists is UserExists.YES
            attack.failures += 1
            attack.since_start += 1
            if len(attack.ips) < MAX_TRACKED:
                attack.ips.add(ip_key)
            self._arm_quiet(attack)
            return

        now = dt_util.utcnow()
        settings = self._runtime.settings
        entries = self._failures.get(key)
        if entries is None:
            if len(self._attacks) >= MAX_TRACKED:
                return
            entries = self._failures[key] = deque(maxlen=_MAX_ENTRIES)
            while len(self._failures) + len(self._attacks) > MAX_TRACKED:
                self._failures.popitem(last=False)
        else:
            self._failures.move_to_end(key)
        entries.append((now, ip_key))
        window = timedelta(minutes=settings.attack_window_minutes)
        while entries and now - entries[0][0] > window:
            entries.popleft()
        if not settings.username_protection or len(entries) < settings.attack_threshold:
            return
        ips = {entry_ip for _, entry_ip in entries}
        if len(ips) < settings.attack_min_ips:
            return
        del self._failures[key]
        name = sanitise_text(username) or key
        self._start(
            _Attack(
                key, name, now, len(entries), ips, user_exists=exists is UserExists.YES
            )
        )

    def _start(self, attack: _Attack) -> None:
        self._attacks[attack.key] = attack
        attack.id_task = self._hass.async_create_task(
            self._async_add(attack), "electric_fence attack store", eager_start=True
        )
        self._submit(
            ChangeAction.ATTACK_STARTED,
            attack,
            {"failures": attack.failures, "ip_count": len(attack.ips)},
        )
        self._fire(attack, "started")
        view = attack.view()
        if (alerts := self._runtime.alerts) is not None:
            try:
                alerts.username_attack(view)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Attack alert failed: %s", type(err).__name__)
        self._arm_quiet(attack)
        self._changed()

    def _on_record(self, record: Record) -> None:
        if not isinstance(record, LoginAttempt):
            return
        if (
            record.result is LoginResult.SUCCESS
            or record.reason not in COUNTED_FAILURES
        ):
            return
        key = self.key(record.username_typed)
        if key is None or (attack := self._attacks.get(key)) is None:
            return
        attack.user_exists |= record.user_exists is UserExists.YES
        addr = normalise_ip(record.ip)
        if addr is not None and not is_exempt(self._hass, addr):
            if len(attack.ips) < MAX_TRACKED:
                attack.ips.add(str(addr))
        if record.country_code:
            attack.countries.add(record.country_code)
        self._schedule_update(attack)
        self._changed()

    def _schedule_update(self, attack: _Attack) -> None:
        if attack.unsub_update is not None:
            return
        now = dt_util.utcnow()
        if attack.last_update is None or now - attack.last_update >= UPDATE_INTERVAL:
            self._write_update(attack)
            return
        attack.unsub_update = async_call_later(
            self._hass,
            attack.last_update + UPDATE_INTERVAL - now,
            HassJob(
                partial(self._on_update_due, attack),
                "electric_fence attack update",
                cancel_on_shutdown=True,
            ),
        )

    @callback
    def _on_update_due(self, attack: _Attack, _now: datetime) -> None:
        attack.unsub_update = None
        if self._attacks.get(attack.key) is attack:
            self._timer_guard.call(self._write_update, attack)

    def _write_update(self, attack: _Attack) -> None:
        attack.last_update = dt_util.utcnow()
        self._spawn(self._async_update(attack), "electric_fence attack update")

    def _arm_quiet(self, attack: _Attack) -> None:
        if attack.unsub_quiet is not None:
            attack.unsub_quiet()
        attack.unsub_quiet = async_call_later(
            self._hass,
            timedelta(minutes=self._runtime.settings.attack_window_minutes),
            HassJob(
                partial(self._on_quiet, attack),
                "electric_fence attack quiet",
                cancel_on_shutdown=True,
            ),
        )

    @callback
    def _on_quiet(self, attack: _Attack, _now: datetime) -> None:
        attack.unsub_quiet = None
        self._timer_guard.call(self._end, attack, "quiet", None, None)

    def _end(
        self,
        attack: _Attack,
        reason: EndReason,
        by_user_id: str | None,
        by_name: str | None,
    ) -> bool:
        # The quiet timer and a manual end must not both end it.
        if self._attacks.get(attack.key) is not attack:
            return False
        del self._attacks[attack.key]
        attack.cancel_timers()
        attack.wake()
        self._spawn(
            self._async_store_end(
                attack, reason, dt_util.utcnow(), by_user_id, by_name
            ),
            "electric_fence attack end",
        )
        details: dict[str, Any] = {
            "failures": attack.failures,
            "ip_count": len(attack.ips),
            "countries": sorted(attack.countries),
            "reason": reason,
        }
        if reason == "quiet":
            self._submit(ChangeAction.ATTACK_ENDED, attack, details)
        else:
            self._submit(
                ChangeAction.SLOWDOWN_ENDED,
                attack,
                details,
                actor_user_id=by_user_id,
                actor_name=by_name,
            )
        self._fire(attack, "ended")
        self._changed()
        return True

    async def _async_enter(self, username: str | None, ip: str | None) -> _Slot | None:
        if self._stopped or (key := self.key(username)) is None:
            return None
        attack = self._attacks.get(key)
        if attack is None or not self._runtime.settings.username_protection:
            return None
        if is_exempt(self._hass, ip):
            return None
        if attack.waiting >= MAX_WAITING:
            raise Refused
        attack.waiting += 1
        try:
            acquired = await self._async_acquire(attack)
        finally:
            attack.waiting -= 1
        if not acquired:
            return None
        slot = _Slot(attack.lock)
        try:
            if self._over(attack):
                slot.release()
                return None
            exponent = min(attack.since_start, _MAX_EXPONENT)
            delay = min(MAX_DELAY, BASE_DELAY * 2**exponent)
            await self._async_until_over(attack, _sleep(delay))
            if self._over(attack):
                slot.release()
                return None
        except BaseException:
            slot.release()
            raise
        return slot

    def _over(self, attack: _Attack) -> bool:
        return self._stopped or self._attacks.get(attack.key) is not attack

    async def _async_acquire(self, attack: _Attack) -> bool:
        """Acquire the attack's lock; False when the attack ends first."""
        # Free and no one else waiting (a woken waiter still counts until it
        # resumes): acquire() returns at once, so it needs no racing task.
        if not attack.lock.locked() and attack.waiting == 1:
            return await attack.lock.acquire()
        task = asyncio.ensure_future(attack.lock.acquire())
        try:
            async with asyncio.timeout(ACQUIRE_TIMEOUT):
                await self._async_until_over(attack, task)
        except TimeoutError:
            pass  # the work was cancelled; the result below says False
        except BaseException:
            if task.done() and not task.cancelled() and task.exception() is None:
                attack.lock.release()
            raise
        return task.done() and not task.cancelled()

    async def _async_until_over(
        self,
        attack: _Attack,
        work: Coroutine[Any, Any, Any] | asyncio.Future[Any],
    ) -> None:
        """Await `work`, cancelling it if the attack ends."""
        task = asyncio.ensure_future(work)
        loop = asyncio.get_running_loop()
        waker = loop.create_future()

        def done(_task: asyncio.Future[Any] | None = None) -> None:
            if not waker.done():
                waker.set_result(None)

        task.add_done_callback(done)
        attack.wakers.add(waker)
        try:
            await waker
        finally:
            attack.wakers.discard(waker)
            task.remove_done_callback(done)
            if not task.done():
                task.cancel()
        if task.done() and not task.cancelled():
            task.result()

    def _spawn(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        self._hass.async_create_task(coro, name, eager_start=True)

    async def _async_add(self, attack: _Attack) -> int | None:
        try:
            attack_id: int = await self._hass.async_add_executor_job(
                self._runtime.store.add_attack, attack.username, attack.started_at
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not store an attack: %s", type(err).__name__)
            return None
        attack.attack_id = attack_id
        return attack_id

    async def _async_attack_id(self, attack: _Attack) -> int | None:
        assert attack.id_task is not None
        return await attack.id_task

    async def _async_update(self, attack: _Attack) -> None:
        if (attack_id := await self._async_attack_id(attack)) is None:
            return
        try:
            await self._hass.async_add_executor_job(
                self._runtime.store.update_attack,
                attack_id,
                attack.failures,
                len(attack.ips),
                sorted(attack.countries),
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not update an attack: %s", type(err).__name__)

    async def _async_store_end(
        self,
        attack: _Attack,
        reason: EndReason,
        ended_at: datetime,
        by_user_id: str | None,
        by_name: str | None,
    ) -> None:
        if (attack_id := await self._async_attack_id(attack)) is None:
            return
        await self._async_update(attack)
        try:
            await self._hass.async_add_executor_job(
                self._runtime.store.end_attack,
                attack_id,
                ended_at,
                reason,
                by_user_id,
                by_name,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not store an attack's end: %s", type(err).__name__)

    def _submit(
        self,
        action: ChangeAction,
        attack: _Attack,
        details: dict[str, Any],
        *,
        actor_user_id: str | None = None,
        actor_name: str | None = None,
    ) -> None:
        # The stored name is the full one (for the panel); the flag tells
        # every other sink whether to mask it.
        details = {**details, "user_exists": attack.user_exists}
        self._runtime.worker.submit(
            SettingsChange(
                ts=dt_util.utcnow(),
                kind=ChangeKind.LOGIN_PROTECTION,
                action=action,
                target_id=None,
                target_name=attack.username,
                details=details,
                actor_user_id=actor_user_id,
                actor_name=actor_name,
                actor_ip=None,
            )
        )

    def _fire(self, attack: _Attack, state: Literal["started", "ended"]) -> None:
        username, masked = attack.view().display()
        self._hass.bus.async_fire(
            EVENT_USERNAME_ATTACK,
            {
                "username": username,
                "username_masked": masked,
                "ips": sorted(attack.ips)[:EVENT_MAX_IPS],
                "ip_count": len(attack.ips),
                "countries": sorted(attack.countries),
                "started_at": attack.started_at.isoformat(),
                "state": state,
            },
        )

    def _changed(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Attack listener failed: %s", type(err).__name__)
