"""Background worker: enrich, merge, hold and store records off the login path."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import sqlite3
from typing import Protocol

from homeassistant.auth.models import Credentials
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .attribution import hass_user_id
from .failopen import FailOpen, reraise
from .models import (
    LoginAttempt,
    LoginReason,
    LoginResult,
    Record,
    SettingsChange,
    Source,
    UserExists,
)
from .storage import FenceStore

_LOGGER = logging.getLogger(__name__)

# Pause after an unexpected pipeline error, so a repeating error can't spin.
_ERROR_BACKOFF = 0.1

# Storage counts as healthy again this long after its last error.
STORAGE_HEALTHY_AFTER = timedelta(minutes=5)

# The provider whose usernames map to users (`hass_user_id`).
_HASS_PROVIDER = "homeassistant"

type RecordListener = Callable[[Record], None]


class Enricher(Protocol):
    """Adds source, country and hostname to a login attempt."""

    async def async_enrich(self, record: LoginAttempt) -> None:
        """Mutate only `source`, `country_code` and `hostname`."""


class NullEnricher:
    """Enricher that does nothing."""

    async def async_enrich(self, record: LoginAttempt) -> None:
        """Leave the record unchanged."""


@dataclass(slots=True)
class _Confirm:
    """A success confirmation, queued so it is processed after its record."""

    user_id: str
    client_id: str | None
    client_name: str | None


@dataclass(slots=True)
class _Held:
    """A success waiting for confirmation."""

    deadline: float
    record: LoginAttempt


@dataclass(slots=True)
class _Merge:
    """The pending row that identical failures are merged into."""

    row: LoginAttempt
    last_ts: datetime


type _MergeKey = tuple[str | None, str | None, LoginReason]
# None in the queue wakes the worker to check for stop.
type _Item = Record | _Confirm | None


class FenceWorker:
    """Single background task that stores records in batches.

    `submit` and `confirm_success` are safe to call from any hook: they only
    queue, never raise, never await.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        store: FenceStore,
        enricher: Enricher,
        *,
        max_queue: int = 10_000,
        flush_interval: float = 1.0,
        flush_size: int = 200,
        merge_window: float = 1.0,
        confirm_timeout: float = 60.0,
        enrich_timeout: float = 5.0,
    ) -> None:
        """Initialise the worker (not started)."""
        self._hass = hass
        self._store = store
        self._enricher = enricher
        self._max_queue = max_queue
        self._flush_interval = flush_interval
        self._flush_size = flush_size
        self._merge_window = timedelta(seconds=merge_window)
        self._confirm_timeout = confirm_timeout
        self._enrich_timeout = enrich_timeout
        self._queue: asyncio.Queue[_Item] = asyncio.Queue(maxsize=max_queue)
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._batch: list[Record] = []
        self._flush_at: float | None = None
        self._merges: dict[_MergeKey, _Merge] = {}
        self._held: list[_Held] = []
        self._flush_waiters: list[asyncio.Future[None]] = []
        self._listeners: list[tuple[RecordListener, FailOpen]] = []
        self._enrich_guard = FailOpen("enricher")
        self._resolve_guard = FailOpen("user resolution")
        self._country_guard = FailOpen("new-country detection")
        self._loop_guard = FailOpen("worker")
        self._storage_ok = True
        # Records never stored: queue full, or lost in a failed write (whose
        # listeners still ran).
        self.dropped = 0
        self.storage_errors = 0
        self.last_storage_error: str | None = None
        self.last_storage_error_at: datetime | None = None

    @property
    def queued(self) -> int:
        """Return the number of items waiting in the queue."""
        return self._queue.qsize()

    @property
    def storage_healthy(self) -> bool:
        """Return False within STORAGE_HEALTHY_AFTER of a storage error."""
        at = self.last_storage_error_at
        return at is None or dt_util.utcnow() - at > STORAGE_HEALTHY_AFTER

    @callback
    def submit(self, record: Record) -> bool:
        """Queue a record; True if queued. O(1), never raises, never awaits."""
        try:
            if self._stopping:
                self.dropped += 1
                return False
            self._queue.put_nowait(record)
            self._wake.set()
        except asyncio.QueueFull:
            self.dropped += 1
            return False
        return True

    @callback
    def confirm_success(
        self, user_id: str, client_id: str | None, client_name: str | None
    ) -> None:
        """Confirm the oldest held success of `user_id`. Never raises."""
        try:
            if self._stopping:
                return
            self._queue.put_nowait(_Confirm(user_id, client_id, client_name))
            self._wake.set()
        except asyncio.QueueFull:
            # The success is stored unconfirmed after the timeout.
            return

    @callback
    def add_listener(self, cb: RecordListener) -> CALLBACK_TYPE:
        """Call `cb` for each record after its write, even a failed one."""
        entry = (cb, FailOpen(f"listener {getattr(cb, '__name__', 'callback')}"))
        self._listeners.append(entry)

        @callback
        def _remove() -> None:
            if entry in self._listeners:
                self._listeners.remove(entry)

        return _remove

    async def async_start(self) -> None:
        """Start the background task."""
        if self._task is not None:
            return
        self._stopping = False
        task = self._hass.async_create_background_task(
            self._run(), "electric_fence worker"
        )
        # Never leave a flush waiting on a task that has ended.
        task.add_done_callback(lambda _: self._resolve_waiters())
        self._task = task

    async def async_stop(self) -> None:
        """Store everything queued and held, then stop. Idempotent."""
        task = self._task
        if task is None:
            return
        self._stopping = True
        self._wake.set()
        try:
            # Does not raise if the task was cancelled or failed.
            await asyncio.wait({task})
        finally:
            self._task = None
        if not task.cancelled() and (err := task.exception()) is not None:
            _LOGGER.error("Electric Fence worker ended: %s", type(err).__name__)

    async def async_flush(self) -> None:
        """Wait until the queue is processed and the pending batch is written."""
        task = self._task
        if task is None or task.done():
            return
        fut: asyncio.Future[None] = self._hass.loop.create_future()
        self._flush_waiters.append(fut)
        self._wake.set()
        await fut

    async def _run(self) -> None:
        """Worker loop."""
        loop = self._hass.loop
        while True:
            self._wake.clear()
            current: _Item = None
            try:
                while not self._queue.empty():
                    current = self._queue.get_nowait()
                    if current is not None:
                        await self._process(current)
                    current = None
                    if len(self._batch) >= self._flush_size:
                        await self._flush()
                now = loop.time()
                self._release_expired(now)
                if self._stopping:
                    self._release_all()
                if self._batch and (
                    self._stopping
                    or self._flush_waiters
                    or (self._flush_at is not None and now >= self._flush_at)
                ):
                    await self._flush()
                if self._queue.empty():
                    self._resolve_waiters()
                    if self._stopping:
                        return
            except Exception as err:  # noqa: BLE001
                # Never let a bug end the worker. Rate-limited, message-free log.
                self._loop_guard.call(reraise, err)
                if isinstance(current, (LoginAttempt, SettingsChange)):
                    self.dropped += 1
                if self._stopping:
                    # Don't retry forever on stop: count what is left and end.
                    self.dropped += len(self._batch) + len(self._held)
                    self._batch, self._held = [], []
                    while not self._queue.empty():
                        if isinstance(
                            self._queue.get_nowait(), (LoginAttempt, SettingsChange)
                        ):
                            self.dropped += 1
                    self._resolve_waiters()
                    return
                await asyncio.sleep(_ERROR_BACKOFF)
                continue
            # A submit during processing has set the event: no wait then.
            try:
                async with asyncio.timeout(self._next_timeout(loop.time())):
                    await self._wake.wait()
            except TimeoutError:
                pass

    def _next_timeout(self, now: float) -> float | None:
        deadlines: list[float] = []
        if self._batch and self._flush_at is not None:
            deadlines.append(self._flush_at)
        if self._held:
            deadlines.append(self._held[0].deadline)
        if not deadlines:
            return None
        return max(0.0, min(deadlines) - now)

    async def _process(self, item: Record | _Confirm) -> None:
        if isinstance(item, _Confirm):
            await self._confirm(item)
            return
        if isinstance(item, LoginAttempt):
            before = (item.source, item.country_code, item.hostname)
            try:
                async with asyncio.timeout(self._enrich_timeout):
                    await self._enricher.async_enrich(item)
            except Exception as err:  # noqa: BLE001
                # Includes the timeout: store the record un-enriched.
                item.source, item.country_code, item.hostname = before
                self._enrich_guard.call(reraise, err)
            if (credentials := item.credentials) is not None:
                # Never kept past this point, whatever the result.
                item.credentials = None
                await self._resolve_success(item, credentials)
            if item.result is LoginResult.FAILED:
                self._resolve_guard.call(self._resolve_failure, item)
                self._merge_or_add(item)
                return
            if (
                item.result is LoginResult.SUCCESS
                and not item.confirmed
                and item.user_id is not None
            ):
                self._hold(item)
                return
            if item.confirmed:
                await self._detect_new_country(item)
        self._add(item)

    async def _resolve_success(
        self, rec: LoginAttempt, credentials: Credentials
    ) -> None:
        """Set the user of a success and apply the login view's block rules.

        On failure, the record keeps `user_id=None` and is stored unconfirmed.
        """
        if rec.result is not LoginResult.SUCCESS:
            return
        try:
            user = await self._hass.auth.async_get_user_by_credentials(credentials)
        except Exception as err:  # noqa: BLE001
            self._resolve_guard.call(reraise, err)
            return
        if user is None:
            # Credentials not linked to a user yet: nothing to confirm against.
            return
        rec.user_id = user.id
        # Same rules as Core's async_user_not_allowed_do_auth.
        if not user.is_active:
            rec.result, rec.reason = LoginResult.BLOCKED, LoginReason.BLOCKED_INACTIVE
        elif user.local_only and rec.source is not Source.LOCAL:
            rec.result, rec.reason = LoginResult.BLOCKED, LoginReason.BLOCKED_LOCAL_ONLY

    async def _detect_new_country(self, rec: LoginAttempt) -> None:
        """Flag a success from a country the user never logged in from."""
        if (
            rec.result is not LoginResult.SUCCESS
            or rec.user_id is None
            or rec.country_code is None
            or rec.source is Source.LOCAL
        ):
            return
        try:
            is_new, known = await self._hass.async_add_executor_job(
                self._note_country, rec.user_id, rec.country_code
            )
        except Exception as err:  # noqa: BLE001
            self._country_guard.call(reraise, err)
            return
        rec.new_country = is_new
        rec.known_countries_before = known - 1

    def _note_country(self, user_id: str, country_code: str) -> tuple[bool, int]:
        """Record the country; return (is new, countries known afterwards)."""
        is_new = self._store.note_country(user_id, country_code)
        return is_new, len(self._store.known_countries(user_id))

    def _resolve_failure(self, rec: LoginAttempt) -> None:
        """Set the user of a failure for an existing `homeassistant` username.

        Memory only. Covers wrong passwords and wrong two-factor codes.
        """
        if (
            rec.user_id is None
            and rec.user_exists is UserExists.YES
            and rec.provider == _HASS_PROVIDER
        ):
            rec.user_id = hass_user_id(self._hass, rec.username_typed)

    def _merge_or_add(self, rec: LoginAttempt) -> None:
        """Merge into a pending identical failure within the window, or add."""
        key: _MergeKey = (rec.ip, rec.username_typed, rec.reason)
        state = self._merges.get(key)
        window = self._merge_window
        if state is not None and (
            abs(rec.ts - state.row.ts) <= window
            or abs(rec.ts - state.last_ts) <= window
        ):
            state.row.count += rec.count
            state.last_ts = max(state.last_ts, rec.ts)
            return
        self._merges[key] = _Merge(row=rec, last_ts=rec.ts)
        self._add(rec)

    def _hold(self, rec: LoginAttempt) -> None:
        if len(self._held) >= self._max_queue:
            self._add(self._held.pop(0).record)
        self._held.append(
            _Held(deadline=self._hass.loop.time() + self._confirm_timeout, record=rec)
        )

    async def _confirm(self, conf: _Confirm) -> None:
        for i, held in enumerate(self._held):
            rec = held.record
            if rec.user_id == conf.user_id:
                del self._held[i]
                rec.confirmed = True
                if conf.client_id is not None:
                    rec.client_id = conf.client_id
                if conf.client_name is not None:
                    rec.client_name = conf.client_name
                await self._detect_new_country(rec)
                self._add(rec)
                return

    def _release_expired(self, now: float) -> None:
        while self._held and self._held[0].deadline <= now:
            self._add(self._held.pop(0).record)

    def _release_all(self) -> None:
        held, self._held = self._held, []
        for h in held:
            self._add(h.record)

    def _add(self, rec: Record) -> None:
        if not self._batch:
            self._flush_at = self._hass.loop.time() + self._flush_interval
        self._batch.append(rec)

    def _resolve_waiters(self) -> None:
        waiters, self._flush_waiters = self._flush_waiters, []
        for fut in waiters:
            if not fut.done():
                fut.set_result(None)

    async def _flush(self) -> None:
        batch, self._batch = self._batch, []
        self._flush_at = None
        # Stored rows can't be merged into any more.
        self._merges.clear()
        try:
            await self._hass.async_add_executor_job(self._store.insert, batch)
        except Exception as err:  # noqa: BLE001
            self.storage_errors += 1
            self.dropped += len(batch)
            self.last_storage_error_at = dt_util.utcnow()
            # sqlite messages carry no row data; other messages might.
            self.last_storage_error = (
                f"{type(err).__name__}: {err}"
                if isinstance(err, sqlite3.Error)
                else type(err).__name__
            )
            if self._storage_ok:
                self._storage_ok = False
                _LOGGER.warning(
                    "Electric Fence could not store %d records: %s",
                    len(batch),
                    self.last_storage_error,
                )
        else:
            if not self._storage_ok:
                self._storage_ok = True
                _LOGGER.info("Electric Fence storage recovered")
        # Even when the write failed: events, the notification, the sensor and
        # the live feed must keep working during a storage outage.
        listeners = list(self._listeners)
        for rec in batch:
            for cb, guard in listeners:
                guard.call(cb, rec)
