"""App changes made outside Home Assistant (CLI, Supervisor, host).

Supervisor installs only (`is_hassio`). The watcher keeps a snapshot of
every app and the store's repositories, and compares a fresh one on every
poll: on each Supervisor event about apps or the store (debounced), and
every 15 minutes. Changes Core made are recorded by the app collector,
which tells the watcher (`note_core_change`), so they aren't recorded twice.

Attribution, never guessed: an update of an app with auto-update on is
System; everything else is Outside. Supervisor's watchdog restarts can't
be told apart: its `app_restart_after_problem` job is internal, so Core
never sees it, and the start and restart jobs it runs carry nothing else.
They are recorded Outside.

Options are kept as a hash only, never their values. Repository URLs are
kept and recorded masked (`redact_value`).
"""

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from functools import partial
import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import CALLBACK_TYPE, HassJob, HomeAssistant, callback
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.util import dt as dt_util

from ..models import OUTSIDE_ACTOR, ChangeAction, ChangeKind, SettingsChange
from ..redact import redact_value
from ..storage import AppSnapshot
from .apps import READ_TIMEOUT

if TYPE_CHECKING:
    from ..runtime import Runtime

_LOGGER = logging.getLogger(__name__)

# `hassio.const.EVENT_SUPERVISOR_EVENT`, the dispatcher signal of every
# Supervisor event; imported lazily with the rest of `hassio`.
SIGNAL_SUPERVISOR_EVENT: Final = "supervisor_event"
POLL_INTERVAL: Final = timedelta(minutes=15)
EVENT_DEBOUNCE: Final = 5.0
BURST_WINDOW: Final = 60.0
SUPPRESS_WINDOW: Final = timedelta(seconds=120)
# A Core background install or update is noted when Supervisor accepts it;
# it suppresses until a poll sees it done, for at most this long.
PENDING_WINDOW: Final = timedelta(hours=1)
_PENDING_ACTIONS: Final = frozenset({ChangeAction.INSTALLED, ChangeAction.UPDATED})
WITH_UPDATE: Final = "with_update"

# Supervisor events (`event`) about apps or the store. Core gets `addon`
# for an app's state change (`app` with Supervisor's WebSocket v2).
POLL_EVENTS: Final = frozenset({"addon", "app", "store_reloaded"})
EVENT_JOB: Final = "job"
# Jobs (`data.name`) that change apps or repositories, polled once done:
# `app_*` (`addon_*` before Supervisor renamed them) and `store_manager_*`.
POLL_JOB_PREFIXES: Final = ("app_", "addon_", "store_manager_")

SETTINGS_FIELDS: Final = (
    "boot",
    "watchdog",
    "auto_update",
    "ingress_panel",
    "protected",
    "network",
)
_STARTED: Final = "started"
_STOPPED: Final = "stopped"
_STATE_ACTIONS: Final = {_STARTED: ChangeAction.STARTED, _STOPPED: ChangeAction.STOPPED}
_BURST_ACTIONS: Final = frozenset({ChangeAction.STARTED, ChangeAction.STOPPED})


@dataclass(frozen=True)
class AppDiff:
    """One difference between two snapshots.

    `slug` is the repository's (masked) URL for a repository change.
    """

    slug: str | None
    name: str | None
    action: ChangeAction
    details: dict[str, Any]


def _plain(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def options_hash(options: Any) -> str:
    """Return the sha256 of the options' canonical JSON, as hex."""
    text = json.dumps(options, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def snapshot_of(info: Any) -> AppSnapshot:
    """Return the snapshot of an app from Supervisor's `addon_info`."""
    slug = str(info.slug)
    version = getattr(info, "version", None)
    state = _plain(getattr(info, "state", None))
    return AppSnapshot(
        slug=slug,
        name=str(getattr(info, "name", None) or slug),
        version=None if version is None else str(version),
        state=None if state is None else str(state),
        options_hash=options_hash(getattr(info, "options", None)),
        settings={key: _plain(getattr(info, key, None)) for key in SETTINGS_FIELDS},
    )


def _app_diffs(old: AppSnapshot, new: AppSnapshot) -> Iterable[AppDiff]:
    name = new.name or old.name
    version_changed = old.version != new.version
    if version_changed:
        yield AppDiff(
            new.slug,
            name,
            ChangeAction.UPDATED,
            {"version_before": old.version, "version_after": new.version},
        )
    if new.state != old.state and new.state in _STATE_ACTIONS:
        yield AppDiff(new.slug, name, _STATE_ACTIONS[new.state], {})
    if old.options_hash != new.options_hash:
        details: dict[str, Any] = {"changed": ["options"], "outside": True}
        if version_changed:
            # May be only the new version's option defaults; see `_poll_once`.
            details[WITH_UPDATE] = True
        yield AppDiff(new.slug, name, ChangeAction.OPTIONS_CHANGED, details)
    changed = sorted(
        key
        for key in old.settings.keys() | new.settings.keys()
        if old.settings.get(key) != new.settings.get(key)
    )
    if changed:
        yield AppDiff(
            new.slug,
            name,
            ChangeAction.SETTINGS_CHANGED,
            {"changed": changed, "outside": True},
        )


def diff_snapshots(
    before: dict[str, AppSnapshot], after: dict[str, AppSnapshot]
) -> list[AppDiff]:
    """Return what changed between two app snapshots, by slug. Pure."""
    diffs: list[AppDiff] = []
    for slug in sorted(before.keys() | after.keys()):
        old, new = before.get(slug), after.get(slug)
        if old is None and new is not None:
            diffs.append(
                AppDiff(
                    slug,
                    new.name,
                    ChangeAction.INSTALLED,
                    {"version_before": None, "version_after": new.version},
                )
            )
        elif new is None and old is not None:
            diffs.append(AppDiff(slug, old.name, ChangeAction.UNINSTALLED, {}))
        elif old is not None and new is not None:
            diffs.extend(_app_diffs(old, new))
    return diffs


def diff_repositories(before: set[str], after: set[str]) -> list[AppDiff]:
    """Return the repositories added and removed. Pure."""
    return [
        AppDiff(url, url, action, {"repository": url})
        for action, urls in (
            (ChangeAction.REPOSITORY_ADDED, after - before),
            (ChangeAction.REPOSITORY_REMOVED, before - after),
        )
        for url in sorted(urls)
    ]


def _masked_url(url: Any) -> str | None:
    shown = redact_value(str(url))
    return shown if isinstance(shown, str) else None


def is_poll_event(event: Any) -> bool:
    """Return True for a Supervisor event about apps or the store."""
    if not isinstance(event, dict):
        return False
    kind = event.get("event")
    if kind in POLL_EVENTS:
        return True
    if kind != EVENT_JOB:
        return False
    data = event.get("data")
    if not isinstance(data, dict) or data.get("done") is not True:
        return False
    name = data.get("name")
    return isinstance(name, str) and name.startswith(POLL_JOB_PREFIXES)


@dataclass(frozen=True)
class _Seen:
    diff: AppDiff
    at: datetime


class OutsideAppWatcher:
    """Record app changes Supervisor reports that Core didn't make."""

    def __init__(self, hass: HomeAssistant, runtime: Runtime) -> None:
        """Initialise the watcher; `start` takes the first snapshot."""
        self.hass = hass
        self.runtime = runtime
        # None until a snapshot is known: the first one records nothing.
        self._apps: dict[str, AppSnapshot] | None = None
        self._repos: set[str] | None = None
        self._core: dict[tuple[str, ChangeAction], datetime] = {}
        self._pending: dict[tuple[str, ChangeAction], datetime] = {}
        self._bursts: dict[ChangeAction, list[_Seen]] = {}
        self._burst_timers: dict[ChangeAction, CALLBACK_TYPE] = {}
        self._lock = asyncio.Lock()
        self._again = False
        self._stopped = False
        self._unsubs: list[CALLBACK_TYPE] = []
        self._debouncer: Debouncer[Any] | None = None
        self._start_task: asyncio.Task[None] | None = None

    @callback
    def start_in_background(self) -> None:
        """Run `start` in a task, so setup never waits for Supervisor."""
        self._start_task = self.hass.async_create_task(
            self.start(), "electric_fence app snapshot", eager_start=False
        )

    async def start(self) -> None:
        """Load the stored snapshot, listen, and take a snapshot. Never raises."""
        if self._stopped:
            return
        try:
            apps, repos = await self.hass.async_add_executor_job(self._load)
        except Exception as err:  # noqa: BLE001
            # Taken as a first snapshot: nothing is recorded rather than guessed.
            _LOGGER.debug("Electric Fence could not load app snapshot: %r", err)
        else:
            # Supervisor always lists its built-in repositories, so both
            # empty means no snapshot was ever stored.
            if apps or repos:
                self._apps, self._repos = apps, repos
        try:
            self._listen()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Electric Fence app watcher start failed: %r", err)
            return
        await self.poll()

    def _load(self) -> tuple[dict[str, AppSnapshot], set[str]]:
        store = self.runtime.store
        return store.app_snapshot(), store.app_repositories()

    def _listen(self) -> None:
        if self._stopped:
            return
        self._debouncer = Debouncer(
            self.hass,
            _LOGGER,
            cooldown=EVENT_DEBOUNCE,
            immediate=False,
            function=self.poll,
        )
        self._unsubs.append(
            async_dispatcher_connect(
                self.hass, SIGNAL_SUPERVISOR_EVENT, self._on_supervisor_event
            )
        )
        self._unsubs.append(
            async_track_time_interval(
                self.hass,
                self._on_interval,
                POLL_INTERVAL,
                name="electric_fence app poll",
                cancel_on_shutdown=True,
            )
        )

    async def stop(self) -> None:
        """Stop polling and record any pending start or stop burst now."""
        self._stopped = True
        # Not cancelled: a poll it runs is waited for below instead.
        self._start_task = None
        while self._unsubs:
            self._unsubs.pop()()
        if self._debouncer is not None:
            self._debouncer.async_shutdown()
            self._debouncer = None
        # A poll in flight records its diffs (or, read after this, drops its
        # snapshot unsaved) before the bursts are flushed.
        locked = False
        try:
            async with asyncio.timeout(READ_TIMEOUT):
                await self._lock.acquire()
            locked = True
        except TimeoutError:
            _LOGGER.debug("Electric Fence app poll still running at stop")
        try:
            for action in list(self._bursts):
                self._flush_burst(action)
        finally:
            if locked:
                self._lock.release()

    @callback
    def note_core_change(
        self,
        slug: str | None,
        action: ChangeAction,
        at: datetime,
        *,
        pending: bool = False,
    ) -> None:
        """Note a change Core recorded, to suppress its outside record.

        `pending` is a background install or update Supervisor accepted but
        may not have done yet: it suppresses the first matching diff, for up
        to an hour.
        """
        if slug is None:
            return
        key = (slug, action)
        self._core[key] = max(at, self._core.get(key, at))
        if pending and action in _PENDING_ACTIONS:
            self._pending[key] = at
        self._prune(dt_util.utcnow())

    def _prune(self, now: datetime) -> None:
        core_cutoff, pending_cutoff = now - SUPPRESS_WINDOW, now - PENDING_WINDOW
        self._core = {k: v for k, v in self._core.items() if v >= core_cutoff}
        self._pending = {k: v for k, v in self._pending.items() if v >= pending_cutoff}

    @callback
    def _on_supervisor_event(self, event: Any) -> None:
        try:
            if self._debouncer is not None and is_poll_event(event):
                self._debouncer.async_schedule_call()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Electric Fence app watcher event failed: %r", err)

    @callback
    def _on_interval(self, _now: datetime) -> None:
        self.hass.async_create_task(self.poll(), "electric_fence app poll")

    async def poll(self) -> None:
        """Compare a fresh snapshot with the last one. Never raises.

        A poll asked for while one runs runs once more after it.
        """
        if self._stopped:
            return
        if self._lock.locked():
            self._again = True
            return
        async with self._lock:
            while True:
                self._again = False
                try:
                    await self._poll_once()
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Electric Fence app poll failed: %r", err)
                if not self._again or self._stopped:
                    return

    async def _read(self) -> tuple[dict[str, AppSnapshot], set[str]]:
        from homeassistant.components import hassio  # noqa: PLC0415

        client = hassio.get_supervisor_client(self.hass)
        async with asyncio.timeout(READ_TIMEOUT):
            listed = await client.addons.list()
            infos = await asyncio.gather(
                *(client.addons.addon_info(app.slug) for app in listed),
                return_exceptions=True,
            )
            repositories = await client.store.repositories_list()
        for info in infos:
            if isinstance(info, BaseException):
                raise info
        apps = {snap.slug: snap for snap in map(snapshot_of, infos)}
        repos = {url for r in repositories if (url := _masked_url(r.url))}
        return apps, repos

    async def _poll_once(self) -> None:
        apps, repos = await self._read()
        if self._stopped:
            # Saving it would lose its diffs: the next start finds them.
            return
        now = dt_util.utcnow()
        diffs: list[AppDiff] = []
        if self._apps is not None and self._repos is not None:
            diffs = diff_snapshots(self._apps, apps) + diff_repositories(
                self._repos, repos
            )
        try:
            await self.hass.async_add_executor_job(self._save, apps, repos, now)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Electric Fence could not store app snapshot: %r", err)
        before, self._apps, self._repos = self._apps or {}, apps, repos
        self._prune(now)
        core_updated: set[str | None] = set()
        for diff in diffs:
            if diff.details.get(WITH_UPDATE) and diff.slug in core_updated:
                # Core's update filled in the new version's option defaults.
                continue
            if self._suppressed(diff, now):
                if diff.action == ChangeAction.UPDATED:
                    core_updated.add(diff.slug)
                continue
            self._handle(diff, before, apps, now)

    def _save(
        self, apps: dict[str, AppSnapshot], repos: set[str], at: datetime
    ) -> None:
        self.runtime.store.save_app_snapshot(apps, at)
        self.runtime.store.save_app_repositories(repos)

    def _suppressed(self, diff: AppDiff, now: datetime) -> bool:
        """Return True for a diff Core recorded; consumes a pending note."""
        if diff.slug is None:
            return False
        if self._pending.pop((diff.slug, diff.action), None) is not None:
            return True
        covering = [diff.action]
        if diff.action in _BURST_ACTIONS:
            covering.append(ChangeAction.RESTARTED)
        return any(
            (at := self._core.get((diff.slug, action))) is not None
            and now - at <= SUPPRESS_WINDOW
            for action in covering
        )

    def _handle(
        self,
        diff: AppDiff,
        before: dict[str, AppSnapshot],
        after: dict[str, AppSnapshot],
        now: datetime,
    ) -> None:
        if diff.action in _BURST_ACTIONS and not self._stopped:
            self._add_to_burst(_Seen(diff, now))
            return
        system = diff.action == ChangeAction.UPDATED and all(
            (snap := snaps.get(diff.slug or "")) is not None
            and snap.settings.get("auto_update") is True
            for snaps in (before, after)
        )
        self._submit(diff.action, diff.slug, diff.name, diff.details, now, system)

    def _add_to_burst(self, seen: _Seen) -> None:
        action = seen.diff.action
        self._bursts.setdefault(action, []).append(seen)
        if action not in self._burst_timers:
            self._burst_timers[action] = async_call_later(
                self.hass,
                BURST_WINDOW,
                HassJob(
                    partial(self._on_burst_timer, action),
                    "electric_fence app burst",
                    cancel_on_shutdown=True,
                ),
            )

    @callback
    def _on_burst_timer(self, action: ChangeAction, _now: datetime) -> None:
        self._burst_timers.pop(action, None)
        self._flush_burst(action)

    def _flush_burst(self, action: ChangeAction) -> None:
        if (cancel := self._burst_timers.pop(action, None)) is not None:
            cancel()
        seen = self._bursts.pop(action, [])
        apps = {s.diff.slug: s.diff.name for s in seen}
        if len(apps) < 2:
            for s in seen:
                d = s.diff
                self._submit(d.action, d.slug, d.name, d.details, s.at, False)
            return
        self._submit(
            action,
            None,
            f"{len(apps)} apps",
            {"apps": [{"slug": slug, "name": name} for slug, name in apps.items()]},
            seen[0].at,
            False,
        )

    def _submit(
        self,
        action: ChangeAction,
        target_id: str | None,
        target_name: str | None,
        details: dict[str, Any],
        at: datetime,
        system: bool,
    ) -> None:
        try:
            self.runtime.worker.submit(
                SettingsChange(
                    ts=at,
                    kind=ChangeKind.APP,
                    action=action,
                    target_id=target_id,
                    target_name=target_name,
                    details=details,
                    actor_user_id=None,
                    actor_name=None if system else OUTSIDE_ACTOR,
                    actor_ip=None,
                )
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Electric Fence could not record an app change: %r", err)
