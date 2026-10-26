"""Queue every stored record and send it to the SIEM, off the login path."""

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import socket
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from ..const import SIGNAL_FORWARDING_UPDATED
from ..models import ChangeAction, ChangeKind, Record, SettingsChange
from ..runtime import Runtime
from .format import event_dict, syslog_line
from .transport import SyslogTransport

_LOGGER = logging.getLogger(__name__)

QUEUE_MAX = 1000
BACKOFF_START = 1.0
BACKOFF_MAX = 300.0
DRAIN_TIMEOUT = 5.0
FAILING_AFTER = timedelta(minutes=5)
_DEFAULT_HOSTNAME = "homeassistant"


@dataclass(frozen=True, slots=True)
class ForwardStats:
    """A snapshot of the forwarder."""

    connected: bool
    queued: int
    dropped: int
    sent: int
    last_error: str | None
    failing_since: datetime | None


def _hostname() -> str:
    return socket.gethostname() or _DEFAULT_HOSTNAME


class Forwarder:
    """Sends events in order over one transport; reconnects with backoff.

    Never raises into Home Assistant: `on_record` only enqueues, and the
    background task catches `Exception` around every network call.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        runtime: Runtime,
        transport_factory: Callable[[], SyslogTransport],
    ) -> None:
        """Set up the forwarder; nothing runs until `start`."""
        self._hass = hass
        self._runtime = runtime
        self._transport = transport_factory()
        self._queue: deque[dict[str, Any]] = deque(maxlen=QUEUE_MAX)
        self._wake = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._unlisten: CALLBACK_TYPE | None = None
        self._stopping = False
        self._dropped = 0
        self._sent = 0
        self._last_error: str | None = None
        self._failing_since: datetime | None = None
        self._failure_reported = False
        self._announced: tuple[bool, bool] | None = None

    def start(self) -> None:
        """Listen for records and connect in the background; returns at once."""
        if self._task is not None:
            return
        self._unlisten = self._runtime.worker.add_listener(self.on_record)
        self._task = self._hass.async_create_background_task(
            self._run(), "electric_fence forwarder"
        )

    def detach(self) -> None:
        """Stop listening for new records."""
        if self._unlisten is not None:
            self._unlisten()
            self._unlisten = None

    async def stop(self) -> None:
        """Send what is queued for up to DRAIN_TIMEOUT, then close. Idempotent."""
        self.detach()
        task, self._task = self._task, None
        self._stopping = True
        self._wake.set()
        self._stop_event.set()
        if task is not None:
            done, _ = await asyncio.wait({task}, timeout=DRAIN_TIMEOUT)
            if not done:
                task.cancel()
                await asyncio.wait({task})
        await self._transport.close()

    def take_queue(self) -> list[dict[str, Any]]:
        """Remove and return the events not sent yet (after `stop`)."""
        events = list(self._queue)
        self._queue.clear()
        return events

    def adopt(self, events: list[dict[str, Any]]) -> None:
        """Put older events in front of the queue, keeping the newest ones."""
        room = QUEUE_MAX - len(self._queue)
        keep = events[len(events) - room :] if room < len(events) else events
        self._dropped += len(events) - len(keep)
        self._queue.extendleft(reversed(keep))
        self._wake.set()

    def on_record(self, record: Record) -> None:
        """Queue the event of a stored record (a worker listener)."""
        event = event_dict(record, values=self._runtime.settings.record_values)
        if len(self._queue) == QUEUE_MAX:
            self._dropped += 1
        self._queue.append(event)
        self._wake.set()

    def stats(self) -> ForwardStats:
        """Return the current counts and link state."""
        return ForwardStats(
            connected=self._transport.connected,
            queued=len(self._queue),
            dropped=self._dropped,
            sent=self._sent,
            last_error=self._last_error,
            failing_since=self._failing_since,
        )

    def _announce(self) -> None:
        state = (self._transport.connected, self._failing_since is not None)
        if state != self._announced:
            self._announced = state
            async_dispatcher_send(self._hass, SIGNAL_FORWARDING_UPDATED)

    def _submit_health(self, action: ChangeAction, details: dict[str, Any]) -> None:
        self._runtime.worker.submit(
            SettingsChange(
                ts=dt_util.utcnow(),
                kind=ChangeKind.FORWARDING,
                action=action,
                target_id=None,
                target_name=None,
                details=details,
                actor_user_id=None,
                actor_name=None,
                actor_ip=None,
            )
        )

    def _failed(self, err: Exception) -> None:
        now = dt_util.utcnow()
        name = type(err).__name__
        self._last_error = name
        if self._failing_since is None:
            self._failing_since = now
        if not self._failure_reported and now - self._failing_since >= FAILING_AFTER:
            self._failure_reported = True
            self._submit_health(ChangeAction.FORWARDING_FAILED, {"error": name})
        self._announce()

    def _succeeded(self) -> None:
        self._failing_since = None
        if self._failure_reported:
            self._failure_reported = False
            self._submit_health(ChangeAction.FORWARDING_RECOVERED, {})
        self._announce()

    async def _sleep(self, seconds: float) -> None:
        try:
            async with asyncio.timeout(seconds):
                await self._stop_event.wait()
        except TimeoutError:
            pass

    async def _run(self) -> None:
        try:
            await self._loop()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Electric Fence forwarder ended: %s", type(err).__name__)

    async def _loop(self) -> None:
        try:
            hostname = await self._hass.async_add_executor_job(_hostname)
        except Exception:  # noqa: BLE001
            hostname = _DEFAULT_HOSTNAME
        backoff = BACKOFF_START
        transport = self._transport
        while True:
            if self._stopping and not self._queue:
                return
            if not transport.connected:
                try:
                    await transport.connect()
                except Exception as err:  # noqa: BLE001
                    self._failed(err)
                    if self._stopping:
                        return
                    await self._sleep(backoff)
                    backoff = min(backoff * 2, BACKOFF_MAX)
                    continue
                backoff = BACKOFF_START
                if not self._queue:
                    self._succeeded()
            if not self._queue:
                self._wake.clear()
                await self._wake.wait()
                continue
            event = self._queue[0]
            try:
                line = syslog_line(event, hostname=hostname, now=dt_util.utcnow())
                await transport.send(line)
            except Exception as err:  # noqa: BLE001
                await transport.close()
                self._failed(err)
                if self._stopping:
                    return
                await self._sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
                continue
            if self._queue and self._queue[0] is event:
                self._queue.popleft()
            else:
                # Overflow pushed it out while sending, and it was counted.
                self._dropped -= 1
            self._sent += 1
            self._succeeded()
