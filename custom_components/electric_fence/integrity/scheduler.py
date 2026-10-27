"""Run the integration checks on a schedule and keep their findings."""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import importlib
import logging
import re
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HassJob, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.loader import async_get_custom_components
from homeassistant.util import dt as dt_util

from ..contracts import marketplace_contract_ok
from ..models import ChangeAction, ChangeKind, SettingsChange
from ..repairs import (
    INTEGRATION_ARCHIVED,
    INTEGRATION_FILES_CHANGED,
    INTEGRATION_FILES_CHANGED_MANUAL,
    INTEGRATION_UNMAINTAINED,
    async_report_integration_issue,
    async_sync_integration_issues,
)
from ..runtime import Runtime
from ..settings import Settings
from ..shapes import iso
from .files import FileReport
from .health import HealthResult, health_of
from .inventory import (
    InstalledIntegration,
    _marketplace_repositories as marketplace_repositories,
    async_inventory,
)
from .reference import FileResult, accept_baseline, verify

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(hours=24)
INSTALL_STALE_AFTER = timedelta(hours=1)
# Each file list kept and exposed holds at most this many paths.
FILE_LIST_CAP = 500
REPAIR_FILES_SHOWN = 10

FILES = "files"
HEALTH = "health"

_FILE_LISTS = ("changed", "missing", "added", "unreadable", "symlinks")
_RECORDED_LISTS = ("changed", "missing", "added", "unreadable")
# File names and manifest names come from the integration's author.
_MARKDOWN_SPECIAL = re.compile(r"[\\`*_{}\[\]()<>#+\-.!|~&]")
_HEALTH_ACTIONS = {
    "archived": ChangeAction.ARCHIVED,
    "removed": ChangeAction.REMOVED_FROM_STORE,
    "unmaintained": ChangeAction.UNMAINTAINED,
}


def _marketplace_attr(module: str, name: str) -> Any:
    try:
        found = importlib.import_module(
            f"homeassistant.components.marketplace.{module}"
        )
    except ImportError:
        return None
    return getattr(found, name, None)


# None when Core has no Marketplace (or renamed them): that trigger is then off.
INSTALLED_SIGNAL: str | None = _marketplace_attr("const", "SIGNAL_REPOSITORY_INSTALLED")
# Sent with `progress: False` when any install (first or update) ends.
INSTALL_PROGRESS_SIGNAL: str | None = getattr(
    _marketplace_attr("enums", "MarketplaceSignal"),
    "REPOSITORY_INSTALL_PROGRESS",
    None,
)


@dataclass(frozen=True)
class IntegrationStatus:
    """The last check of one installed custom integration."""

    domain: str
    name: str
    source: str
    version: str | None
    repository: str | None
    # None when file checks are off.
    files: FileResult | None
    health: HealthResult
    # True when a file list in `files.report` was cut to FILE_LIST_CAP paths.
    truncated: bool = False


@dataclass(frozen=True)
class IntegrityFindings:
    """Domains with an open finding, after the last scan."""

    files_changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    unmaintained: list[str] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)


def _capped(result: FileResult) -> tuple[FileResult, bool]:
    report = result.report
    if report is None:
        return result, False
    lists = {name: getattr(report, name) for name in _FILE_LISTS}
    if all(len(paths) <= FILE_LIST_CAP for paths in lists.values()):
        return result, False
    capped = FileReport(**{k: v[:FILE_LIST_CAP] for k, v in lists.items()})
    return replace(result, report=capped), True


def _markdown_text(text: str) -> str:
    """Escape text so a repair (rendered as Markdown) shows it as is."""
    return _MARKDOWN_SPECIAL.sub(r"\\\g<0>", " ".join(text.split()))


def _files_text(state: Mapping[str, Any]) -> str:
    paths = [path for name in _RECORDED_LISTS for path in state.get(name, [])]
    shown = paths[:REPAIR_FILES_SHOWN]
    text = ", ".join(_markdown_text(str(path)) for path in shown)
    more = int(state.get("count", len(paths))) - len(shown)
    return f"{text} and {more} more" if more > 0 else text


def _status_of(state: Mapping[str, Any] | None) -> str | None:
    return None if state is None else state.get("status")


async def _async_name(hass: HomeAssistant, domain: str) -> str:
    """Return the manifest name, or the domain when it can't be read."""
    try:
        integration = (await async_get_custom_components(hass)).get(domain)
    except Exception:  # noqa: BLE001
        return domain
    return (integration.name if integration is not None else None) or domain


class IntegrityScheduler:
    """Scans the custom integrations daily, and soon after a Marketplace install."""

    def __init__(self, hass: HomeAssistant, runtime: Runtime) -> None:
        """Initialise; nothing runs until `start`."""
        self.hass = hass
        self.runtime = runtime
        self._results: list[IntegrationStatus] = []
        self._files: dict[str, dict[str, Any]] = {}
        self._health: dict[str, str] = {}
        self._unchecked: list[str] = []
        self._scanned = False
        self._scanned_at: datetime | None = None
        self._task: asyncio.Task[list[IntegrationStatus]] | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._pending: CALLBACK_TYPE | None = None
        self._stopped = False
        # A rescan that came due while a scan ran; it runs right after.
        self._again = False
        # Marketplace installs running (repository -> when last heard of), and
        # a count of installs that ended, to spot one landing during a scan.
        self._installing: dict[str | None, datetime] = {}
        self._install_generation = 0
        # The last scan's Marketplace contract check: it walks the whole
        # catalogue, so the panel's frequent reads reuse it.
        self._contract_ok: bool | None = None

    def _delay(self) -> timedelta:
        return timedelta(minutes=self.runtime.settings.scan_delay_minutes)

    @callback
    def start(self) -> None:
        """Schedule the first scan a delay after startup, then daily."""
        self._unsubs.append(async_at_started(self.hass, self._on_started))
        if INSTALLED_SIGNAL is not None:
            self._unsubs.append(
                async_dispatcher_connect(
                    self.hass, INSTALLED_SIGNAL, self._on_installed
                )
            )
        if INSTALL_PROGRESS_SIGNAL is not None:
            self._unsubs.append(
                async_dispatcher_connect(
                    self.hass, INSTALL_PROGRESS_SIGNAL, self._on_install_progress
                )
            )

    @callback
    def _on_started(self, _hass: HomeAssistant) -> None:
        if self._stopped:
            return
        self._unsubs.append(
            async_call_later(
                self.hass,
                self._delay(),
                HassJob(
                    self._first_scan,
                    "electric_fence first integration scan",
                    cancel_on_shutdown=True,
                ),
            )
        )

    @callback
    def _first_scan(self, _now: datetime) -> None:
        self._start_scan()
        self._unsubs.append(
            async_track_time_interval(
                self.hass,
                self._daily_scan,
                SCAN_INTERVAL,
                name="electric_fence integration scan",
                cancel_on_shutdown=True,
            )
        )

    @callback
    def _daily_scan(self, _now: datetime) -> None:
        self._start_scan()

    @callback
    def _on_installed(self, *_args: Any) -> None:
        self._install_generation += 1
        self._schedule_rescan()

    @callback
    def _on_install_progress(self, data: Any = None, *_args: Any) -> None:
        if not isinstance(data, Mapping):
            return
        repository = data.get("repository")
        if data.get("progress") is False:
            self._installing.pop(repository, None)
            self._install_generation += 1
            self._schedule_rescan()
        else:
            self._installing[repository] = dt_util.utcnow()

    def _install_running(self) -> bool:
        # An install that never said it ended stops counting after a while.
        since = dt_util.utcnow() - INSTALL_STALE_AFTER
        return any(at > since for at in self._installing.values())

    @callback
    def _schedule_rescan(self) -> None:
        # One pending rescan at most: more signals in the delay join it.
        if self._stopped or self._pending is not None:
            return
        self._pending = async_call_later(
            self.hass,
            self._delay(),
            HassJob(
                self._rescan,
                "electric_fence integration rescan",
                cancel_on_shutdown=True,
            ),
        )

    @callback
    def _rescan(self, _now: datetime) -> None:
        self._pending = None
        if self._task is not None and not self._task.done():
            # The running scan may have listed the integrations before the
            # install, so one more follows it.
            self._again = True
            return
        self._start_scan()

    @callback
    def _start_scan(self) -> asyncio.Task[list[IntegrationStatus]]:
        if self._task is None or self._task.done():
            self._task = self.hass.async_create_background_task(
                self._async_run(), "electric_fence integration scan"
            )
            self._task.add_done_callback(self._scan_done)
        return self._task

    @callback
    def _scan_done(self, _task: asyncio.Task[list[IntegrationStatus]]) -> None:
        if self._again and not self._stopped:
            self._again = False
            self._start_scan()

    async def scan(self) -> list[IntegrationStatus]:
        """Scan now, or wait for the scan that is running and return its result."""
        return await asyncio.shield(self._start_scan())

    @property
    def scanned_at(self) -> datetime | None:
        """Return when the last scan began, None before the first."""
        return self._scanned_at

    @property
    def running(self) -> bool:
        """Return True while a scan runs."""
        return self._task is not None and not self._task.done()

    @property
    def marketplace_ok(self) -> bool:
        """Return the last scan's Marketplace contract result (checked once before)."""
        if self._contract_ok is None:
            self._contract_ok = marketplace_contract_ok(self.hass)
        return self._contract_ok

    def results(self) -> list[IntegrationStatus]:
        """Return the statuses of the last scan."""
        return list(self._results)

    def findings(self) -> IntegrityFindings | None:
        """Return the open findings, None before the first scan."""
        if not self._scanned:
            return None
        found = IntegrityFindings(unchecked=list(self._unchecked))
        verify_files = self.runtime.settings.verify_files
        for status in self._results:
            domain = status.domain
            if verify_files and _status_of(self._files.get(domain)) == "changed":
                found.files_changed.append(domain)
            health = self._health.get(domain)
            if health == "removed":
                found.removed.append(domain)
            elif health == "archived":
                found.archived.append(domain)
            elif health == "unmaintained":
                found.unmaintained.append(domain)
        return found

    async def stop(self) -> None:
        """Cancel the timers and a running scan. Idempotent."""
        self._stopped = True
        unsubs, self._unsubs = self._unsubs, []
        if self._pending is not None:
            unsubs.append(self._pending)
            self._pending = None
        for unsub in unsubs:
            unsub()
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task})

    @callback
    def apply_repairs(self) -> None:
        """Raise the repairs of the current findings; remove the others."""
        settings = self.runtime.settings
        wanted: dict[
            str, tuple[str, dict[str, str], dict[str, str | int | float | None] | None]
        ] = {}
        if settings.integration_repairs:
            months = str(settings.unmaintained_months)
            for status in self._results:
                domain, name = status.domain, status.name
                state = self._files.get(domain)
                if settings.verify_files and state and _status_of(state) == "changed":
                    # A manual install's baseline is Electric Fence's own: an
                    # admin can accept new files. A Marketplace one is the
                    # official copy, so there is nothing to accept.
                    manual = state.get("source") == "manual"
                    wanted[f"{INTEGRATION_FILES_CHANGED}_{domain}"] = (
                        INTEGRATION_FILES_CHANGED_MANUAL
                        if manual
                        else INTEGRATION_FILES_CHANGED,
                        {"name": _markdown_text(name), "files": _files_text(state)},
                        {"domain": domain} if manual else None,
                    )
                # Never one for `removed`: the Marketplace raises its own.
                health = self._health.get(domain)
                if health == "archived":
                    key = INTEGRATION_ARCHIVED
                elif health == "unmaintained":
                    key = INTEGRATION_UNMAINTAINED
                else:
                    continue
                wanted[f"{key}_{domain}"] = (
                    key,
                    {"name": _markdown_text(name), "months": months},
                    None,
                )
        async_sync_integration_issues(self.hass, set(wanted))
        for issue_id, (key, placeholders, data) in wanted.items():
            async_report_integration_issue(self.hass, issue_id, key, placeholders, data)

    async def async_accept_files(
        self, domain: str, user_id: str | None, user_name: str | None
    ) -> bool:
        """Accept the installed files of a manual install as its baseline.

        Records who did it and rechecks soon. False when the integration is
        gone, is a Marketplace install, or the files can't be read.
        """
        items = await async_inventory(self.hass)
        item = next((i for i in items if i.domain == domain), None)
        if item is None or item.source != "manual":
            return False
        try:
            count = await accept_baseline(self.hass, self.runtime, item)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Electric Fence could not accept the files of %s: %s",
                domain,
                type(err).__name__,
            )
            return False
        name = await _async_name(self.hass, domain)
        self._record(
            ChangeKind.FILES,
            ChangeAction.BASELINE_ACCEPTED,
            domain,
            name,
            {"files": count, "version": item.version},
            dt_util.utcnow(),
            actor_user_id=user_id,
            actor_name=user_name,
        )
        # Until the recheck, the old finding must not raise the repair again.
        self._files.pop(domain, None)
        self.apply_repairs()
        self._schedule_rescan()
        return True

    async def _async_run(self) -> list[IntegrationStatus]:
        try:
            return await self._async_scan()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Electric Fence integration scan failed: %s", type(err).__name__
            )
            return self.results()

    async def _async_scan(self) -> list[IntegrationStatus]:
        hass = self.hass
        settings = self.runtime.settings
        contract_ok = self._contract_ok = marketplace_contract_ok(hass)
        generation = self._install_generation
        items = await async_inventory(hass)
        now = dt_util.utcnow()
        results: list[IntegrationStatus] = []
        files_state: dict[str, dict[str, Any]] = {}
        health_state: dict[str, str] = {}
        for item in items:
            # One at a time, and one failure never stops the others.
            try:
                status, f_state, h_state = await self._async_check(
                    item, settings, contract_ok, now, generation
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Electric Fence could not check %s: %s",
                    item.domain,
                    type(err).__name__,
                )
                status = self._failed_status(item, settings)
                f_state = self._files.get(item.domain)
                h_state = self._health.get(item.domain)
            results.append(status)
            if f_state is not None:
                files_state[item.domain] = f_state
            if h_state is not None:
                health_state[item.domain] = h_state
        # An unreadable `custom_components` lists nothing; keep the findings.
        if items:
            try:
                await hass.async_add_executor_job(
                    self.runtime.store.drop_findings, {i.domain for i in items}
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Electric Fence could not drop findings: %s", type(err).__name__
                )
        self._results = results
        self._files = files_state
        self._health = health_state
        self._unchecked = [
            s.domain
            for s in results
            if s.files is not None and s.files.status == "unchecked"
        ]
        self._scanned = True
        self._scanned_at = now
        self.apply_repairs()
        return results

    def _failed_status(
        self, item: InstalledIntegration, settings: Settings
    ) -> IntegrationStatus:
        files = (
            FileResult(item.domain, item.source, "unchecked", reason="check_failed")
            if settings.verify_files
            else None
        )
        return IntegrationStatus(
            item.domain,
            item.domain,
            item.source,
            item.version,
            item.repository,
            files,
            HealthResult(item.domain, "unknown", "ok", item.last_updated, None),
        )

    async def _async_check(
        self,
        item: InstalledIntegration,
        settings: Settings,
        contract_ok: bool,
        now: datetime,
        generation: int,
    ) -> tuple[IntegrationStatus, dict[str, Any] | None, str | None]:
        hass = self.hass
        store = self.runtime.store
        domain = item.domain
        name = await _async_name(hass, domain)
        prev_files = await hass.async_add_executor_job(store.finding, domain, FILES)
        prev_health = await hass.async_add_executor_job(store.finding, domain, HEALTH)

        file_result: FileResult | None = None
        files_state = prev_files
        truncated = False
        if settings.verify_files:
            # Without a trusted Marketplace only manual baselines are checked; a
            # Marketplace integration may then look manual, so its earlier
            # finding says which it was.
            trusted = contract_ok or (
                item.source == "manual"
                and (prev_files or {}).get("source") != "marketplace"
            )
            if not trusted:
                file_result = FileResult(
                    domain, item.source, "unchecked", reason="contract_failed"
                )
            else:
                try:
                    file_result = await verify(hass, self.runtime, item)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug(
                        "Electric Fence could not verify %s: %s",
                        domain,
                        type(err).__name__,
                    )
                    file_result = FileResult(
                        domain, item.source, "unchecked", reason="check_failed"
                    )
                if file_result.status == "changed" and self._install_overlaps(
                    item, generation
                ):
                    # Core writes the new files before it records the new
                    # version: wait for the rescan rather than report a change.
                    file_result = FileResult(
                        domain,
                        item.source,
                        "unchecked",
                        reason="install_in_progress",
                        ref=file_result.ref,
                    )
            files_state = await self._async_files_finding(
                item, name, file_result, prev_files, now
            )
            file_result, truncated = _capped(file_result)

        health_state: str | None
        if contract_ok:
            health = health_of(item, now, settings.unmaintained_months)
            health_state = await self._async_health_finding(
                item, name, health, prev_health, settings, now
            )
        else:
            health = HealthResult(domain, "unknown", "ok", item.last_updated, None)
            health_state = _status_of(prev_health)

        status = IntegrationStatus(
            domain,
            name,
            item.source,
            item.version,
            item.repository,
            file_result,
            health,
            truncated,
        )
        return status, files_state, health_state

    def _install_overlaps(self, item: InstalledIntegration, generation: int) -> bool:
        """Return True when an install may have changed the files under a check."""
        if self._install_running() or self._install_generation != generation:
            return True
        if item.source != "marketplace":
            return False
        try:
            _manager, repos = marketplace_repositories(self.hass)
            current = repos[item.domain].data.installed_version
        except Exception:  # noqa: BLE001
            return False
        if current == item.installed_version:
            return False
        self._schedule_rescan()
        return True

    async def _async_files_finding(
        self,
        item: InstalledIntegration,
        name: str,
        result: FileResult,
        prev: dict[str, Any] | None,
        now: datetime,
    ) -> dict[str, Any] | None:
        """Record a transition of the file check; return the state to keep."""
        status = result.status
        if status == "unchecked":
            return prev
        report = result.report
        full = {k: list(getattr(report, k)) if report else [] for k in _RECORDED_LISTS}
        lists = {k: v[:FILE_LIST_CAP] for k, v in full.items()}
        state: dict[str, Any] = {
            "status": status,
            **lists,
            "count": sum(len(v) for v in full.values()),
            "source": item.source,
            "ref": result.ref,
        }
        before = _status_of(prev)
        details: dict[str, Any] = {"repository": item.repository, "ref": result.ref}
        action: ChangeAction | None = None
        if status == "changed":
            if before != "changed" or any(
                (prev or {}).get(k) != v for k, v in lists.items()
            ):
                action = ChangeAction.FILES_CHANGED
                details = {**lists, **details}
        elif status == "match":
            if before == "changed":
                action = ChangeAction.FILES_MATCH
        elif status == "baseline_recorded":
            action = ChangeAction.BASELINE_RECORDED
        elif status == "baseline_renewed":
            action = ChangeAction.BASELINE_RENEWED
            details["version_before"] = result.version_before
        if action is ChangeAction.FILES_CHANGED and result.version_before is not None:
            details["version_before"] = result.version_before
        if action is not None:
            self._record(ChangeKind.FILES, action, item.domain, name, details, now)
        if state != prev:
            await self.hass.async_add_executor_job(
                self.runtime.store.save_finding, item.domain, FILES, state, now
            )
        return state

    async def _async_health_finding(
        self,
        item: InstalledIntegration,
        name: str,
        health: HealthResult,
        prev: dict[str, Any] | None,
        settings: Settings,
        now: datetime,
    ) -> str:
        """Record a transition of the health check; return the state to keep."""
        status = health.status
        before = _status_of(prev)
        action: ChangeAction | None = None
        if status in _HEALTH_ACTIONS and status != before:
            action = _HEALTH_ACTIONS[status]
        elif status == "ok" and before in _HEALTH_ACTIONS:
            action = ChangeAction.RECOVERED
        if action is not None:
            details: dict[str, Any] = {
                "repository": item.repository,
                "last_updated": iso(health.last_updated),
                "reason": health.reason,
            }
            if status == "unmaintained":
                details["months"] = settings.unmaintained_months
            if action is ChangeAction.RECOVERED:
                details["previous"] = before
            self._record(
                ChangeKind.INTEGRATION_HEALTH, action, item.domain, name, details, now
            )
        state = {"status": status, "source": item.source}
        if state != prev:
            await self.hass.async_add_executor_job(
                self.runtime.store.save_finding, item.domain, HEALTH, state, now
            )
        return status

    def _record(
        self,
        kind: ChangeKind,
        action: ChangeAction,
        domain: str,
        name: str,
        details: dict[str, Any],
        at: datetime,
        *,
        actor_user_id: str | None = None,
        actor_name: str | None = None,
    ) -> None:
        self.runtime.worker.submit(
            SettingsChange(
                ts=at,
                kind=kind,
                action=action,
                target_id=domain,
                target_name=name,
                details=details,
                actor_user_id=actor_user_id,
                actor_name=actor_name,
                actor_ip=None,
            )
        )
