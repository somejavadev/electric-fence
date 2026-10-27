"""The Electric Fence integration."""

from datetime import datetime, timedelta
import logging
from pathlib import Path
import shutil

from awesomeversion import AwesomeVersion

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STOP,
    Platform,
    __version__ as HA_VERSION,
)
from homeassistant.core import Event, HassJob, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.hassio import is_hassio
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from .alerts import Alerter
from .attribution import AttributionCollector
from .collectors.apps import AppCollector
from .collectors.apps_outside import OutsideAppWatcher
from .collectors.auth_failures import AuthFailureCollector
from .collectors.base import BaseCollector
from .collectors.config_entries import ConfigEntryCollector
from .collectors.core_config import CoreConfigCollector
from .collectors.login import LoginCollector
from .collectors.system_settings import SystemSettingsCollector
from .collectors.users import UserCollector
from .const import DOMAIN, MIN_HA_VERSION, SIGNAL_SETTINGS_UPDATED
from .forwarding.forwarder import Forwarder
from .forwarding.transport import SyslogTransport, build_ssl_context
from .geo.base import GeoStatus
from .geo.enrich import GeoEnricher
from .geo.updater import GeoUpdater
from .integrity.scheduler import IntegrityScheduler
from .notifier import Notifier
from .panel import async_register_panel, async_unregister_panel
from .protection.backend import CoreBanBackend
from .protection.bans import BanManager
from .protection.network import normalise_ip
from .protection.policy import IpPolicy
from .protection.throttle import KNOWN_DEVICE_WINDOW, UsernameGuard
from .repairs import (
    async_clear_disabled_collector,
    async_clear_ip_ban_disabled,
    async_clear_schema_too_new,
    async_clear_storage_limit,
    async_clear_tls_check_required,
    async_report_schema_too_new,
    async_report_storage_limit,
    async_report_tls_check_required,
    async_sync_integration_issues,
)
from .runtime import Runtime
from .settings import Settings
from .storage import (
    SCHEMA_VERSION,
    FenceStore,
    StoreSchemaTooNewError,
    read_active_ban_ips,
)
from .websocket_api import async_end_subscriptions, async_register_commands
from .worker import FenceWorker

_LOGGER = logging.getLogger(__name__)

type ElectricFenceConfigEntry = ConfigEntry[Runtime]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

DB_FILENAME = "electric_fence.db"
DATA_DIRNAME = "electric_fence"
PURGE_INTERVAL = timedelta(hours=24)
FIRST_PURGE_DELAY = timedelta(minutes=5)
# The database size is checked this often; a flood of failed logins must not
# fill the disk between daily purges.
SIZE_CHECK_INTERVAL = timedelta(minutes=10)
MB_BYTES = 1024 * 1024
# The size repair goes away once the database is under this share of its limit.
_SIZE_CLEAR_FRACTION = 0.8
PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]

# Each collector task appends its class here.
COLLECTOR_TYPES: list[type[BaseCollector]] = [
    LoginCollector,
    AuthFailureCollector,
    AttributionCollector,
    SystemSettingsCollector,
    ConfigEntryCollector,
    UserCollector,
    CoreConfigCollector,
    AppCollector,
]


def _db_path(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(".storage", DB_FILENAME))


def _open_store(path: Path) -> FenceStore:
    path.parent.mkdir(parents=True, exist_ok=True)
    store = FenceStore(path)
    store.open()
    return store


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the admin WebSocket commands, once per Home Assistant run."""
    async_register_commands(hass)
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: ElectricFenceConfigEntry
) -> None:
    """Set up Electric Fence from a config entry."""
    if AwesomeVersion(HA_VERSION) < MIN_HA_VERSION:
        message = (
            f"Electric Fence requires Home Assistant {MIN_HA_VERSION} or newer "
            f"(running {HA_VERSION})"
        )
        _LOGGER.error("%s", message)
        raise ConfigEntryError(message)

    try:
        store = await hass.async_add_executor_job(_open_store, _db_path(hass))
    except StoreSchemaTooNewError as err:
        async_report_schema_too_new(hass, err.version, SCHEMA_VERSION)
        message = (
            f"The Electric Fence database (schema {err.version}) was written by a "
            f"newer version of Electric Fence, which supports schema "
            f"{SCHEMA_VERSION} at most; it was left unchanged"
        )
        _LOGGER.error("%s", message)
        raise ConfigEntryError(message) from err
    async_clear_schema_too_new(hass)
    _async_drop_tls_check_option(hass, entry)
    enricher = GeoEnricher(hass)
    worker = FenceWorker(hass, store, enricher)
    settings = Settings.from_options(entry.options)
    geo_status = GeoStatus(provider=settings.geoip_provider)
    geo_updater = GeoUpdater(hass, settings, geo_status)
    runtime = Runtime(
        settings=settings,
        store=store,
        worker=worker,
        enricher=enricher,
        collectors=[],
        ha_version=HA_VERSION,
        geo_status=geo_status,
        geo_updater=geo_updater,
    )
    enricher.runtime = runtime
    entry.runtime_data = runtime
    if not settings.record_values:
        # Retries a strip that failed or was interrupted when the option went
        # off. Idempotent, and cheap when there is nothing to remove.
        await _async_strip_values(hass, runtime)
    # Registered first, so it runs last (on_unload runs in reverse). HA runs
    # on_unload both on unload and when setup fails partway, so this is the
    # only teardown path.
    entry.async_on_unload(lambda: _async_teardown(hass, runtime))

    await worker.async_start()

    async def _async_shutdown() -> None:
        # Store what is queued before shutdown cancels background tasks.
        await _async_stop_integrity(runtime)
        await _async_stop_app_watcher(runtime)
        await worker.async_stop()
        await hass.async_add_executor_job(store.close)

    entry.async_on_unload(hass.async_add_shutdown_job(HassJob(_async_shutdown)))

    async def _async_flush_on_stop(_event: Event) -> None:
        # A second safety net. The shutdown job above is the primary flush
        # (Core runs it in stage 1, before this event); this covers a STOP
        # without the shutdown jobs. A no-op once the worker has stopped.
        try:
            await worker.async_flush()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Electric Fence flush on stop failed: %s", type(err).__name__
            )

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_flush_on_stop)
    )

    # Registered after the worker's shutdown job, so it stops first.
    entry.async_on_unload(geo_updater.async_stop)

    # Downloads wait for startup to finish, so they never slow it down.
    async def _async_start_geo(_hass: HomeAssistant) -> None:
        await geo_updater.async_start()

    entry.async_on_unload(async_at_started(hass, _async_start_geo))

    notifier = Notifier(hass, runtime)
    runtime.notifier = notifier
    # On unload, `async_unload_entry` has already stopped (flushed) the worker,
    # so the notifier, sensor and live feed saw every last record.
    entry.async_on_unload(notifier.async_stop)
    entry.async_on_unload(worker.add_listener(notifier.on_record))
    entry.async_on_unload(lambda: async_end_subscriptions(hass, entry.entry_id))

    alerter = Alerter(hass, runtime)
    runtime.alerts = alerter
    entry.async_on_unload(alerter.async_stop)

    bans = BanManager(hass, runtime, CoreBanBackend(hass))
    runtime.bans = bans
    entry.async_on_unload(bans.async_stop)
    await bans.async_start()

    ip_policy = IpPolicy(hass, runtime)
    runtime.ip_policy = ip_policy
    username_guard = UsernameGuard(hass, runtime)
    runtime.username_guard = username_guard
    # Recent successes: trusted (address, username) pairs for the IP policy,
    # known devices for the username guard.
    try:
        successes = await hass.async_add_executor_job(
            store.last_successes_since,
            dt_util.utcnow() - max(settings.trusted_window, KNOWN_DEVICE_WINDOW),
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning(
            "Electric Fence could not load recent logins: %s", type(err).__name__
        )
    else:
        ip_policy.seed_trusted(successes)
        username_guard.seed_known(successes)
    entry.async_on_unload(worker.add_listener(ip_policy.on_record))

    await username_guard.async_start()
    entry.async_on_unload(worker.add_listener(username_guard.on_record))

    await _async_start_forwarder(hass, runtime)

    async def _async_stop_forwarder() -> None:
        if runtime.forwarder is not None:
            await runtime.forwarder.stop()

    entry.async_on_unload(_async_stop_forwarder)

    async def _async_drain_on_stop(_event: Event) -> None:
        await _async_stop_forwarder()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_drain_on_stop)
    )

    for collector_type in COLLECTOR_TYPES:
        collector = collector_type(hass, runtime)
        runtime.collectors.append(collector)
        collector.install()

    if is_hassio(hass):
        watcher = OutsideAppWatcher(hass, runtime)
        runtime.app_watcher = watcher

        @callback
        def _start_app_watcher(_hass: HomeAssistant) -> None:
            watcher.start_in_background()

        entry.async_on_unload(async_at_started(hass, _start_app_watcher))

    _start_integrity(hass, runtime)

    async def _async_purge(_now: datetime | None = None) -> None:
        await async_purge(hass, runtime)

    entry.async_on_unload(
        async_track_time_interval(
            hass,
            _async_purge,
            PURGE_INTERVAL,
            name="electric_fence purge",
            cancel_on_shutdown=True,
        )
    )
    entry.async_on_unload(
        async_call_later(
            hass,
            FIRST_PURGE_DELAY,
            HassJob(
                _async_purge, "electric_fence first purge", cancel_on_shutdown=True
            ),
        )
    )

    async def _async_enforce_size(_now: datetime | None = None) -> None:
        await async_enforce_size(hass, runtime)

    entry.async_on_unload(
        async_track_time_interval(
            hass,
            _async_enforce_size,
            SIZE_CHECK_INTERVAL,
            name="electric_fence size check",
            cancel_on_shutdown=True,
        )
    )

    # Applies option changes live; it never reloads, so the hooks stay put.
    entry.async_on_unload(entry.add_update_listener(async_apply_settings))

    integration = await async_get_integration(hass, DOMAIN)
    version = str(integration.version)
    await async_register_panel(hass, version)
    entry.async_on_unload(lambda: async_unregister_panel(hass, version))

    # Last, so nothing that can fail runs after the platform is forwarded.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)


def _async_drop_tls_check_option(
    hass: HomeAssistant, entry: ElectricFenceConfigEntry
) -> None:
    """Drop the removed "Verify the certificate" option, once.

    TLS forwarding now always checks the certificate, so an entry that had the
    check off for TLS gets a repair issue explaining how to fix forwarding.
    """
    if "forward_verify_tls" not in entry.options:
        return
    options = dict(entry.options)
    verify = options.pop("forward_verify_tls")
    hass.config_entries.async_update_entry(entry, options=options)
    if (
        verify is False
        and options.get("forward_enabled")
        and options.get("forward_protocol") == "tls"
    ):
        async_report_tls_check_required(hass)


async def async_enforce_size(hass: HomeAssistant, runtime: Runtime) -> None:
    """Keep the database under its size limit; never raises.

    The oldest failed logins are deleted first, and a repair issue says so.
    It goes away once the database is well under the limit again.
    """
    limit_mb = runtime.settings.max_db_mb
    limit = limit_mb * MB_BYTES
    store = runtime.store
    try:
        deleted = await hass.async_add_executor_job(store.trim_failed, limit)
        used = await hass.async_add_executor_job(store.used_bytes)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Electric Fence size check failed: %s", type(err).__name__)
        return
    if deleted:
        _LOGGER.warning(
            "Electric Fence database reached its %d MB limit; removed the %d "
            "oldest failed logins",
            limit_mb,
            deleted,
        )
        async_report_storage_limit(hass, limit_mb)
    elif used < limit * _SIZE_CLEAR_FRACTION:
        async_clear_storage_limit(hass)


async def async_purge(hass: HomeAssistant, runtime: Runtime) -> None:
    """Delete records older than the retention period."""
    cutoff = dt_util.utcnow() - timedelta(days=runtime.settings.retention_days)
    try:
        deleted = await hass.async_add_executor_job(
            runtime.store.purge_older_than, cutoff
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Electric Fence purge failed: %s", type(err).__name__)
        return
    _LOGGER.debug("Electric Fence purged %d rows", deleted)


async def _async_strip_values(
    hass: HomeAssistant, runtime: Runtime, *, flush: bool = False
) -> None:
    """Remove recorded values from stored changes; log, never raise."""
    try:
        if flush:
            await runtime.worker.async_flush()
        await hass.async_add_executor_job(runtime.store.strip_change_values)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning(
            "Electric Fence could not remove stored values: %s", type(err).__name__
        )


def _forward_target(settings: Settings) -> tuple[object, ...] | None:
    """Return what the forwarder depends on, None when forwarding is off.

    `record_values` is left out: events read it when they are queued.
    """
    if not (settings.forward_enabled and settings.forward_host):
        return None
    return (
        settings.forward_host,
        settings.forward_port,
        settings.forward_protocol,
        settings.forward_ca_pem,
    )


async def _async_build_forwarder(
    hass: HomeAssistant, runtime: Runtime
) -> Forwarder | None:
    """Return a forwarder for the current settings (not started), None when off."""
    settings = runtime.settings
    if _forward_target(settings) is None:
        return None
    ssl_context = None
    if settings.forward_protocol == "tls":
        try:
            ssl_context = await hass.async_add_executor_job(
                build_ssl_context, settings.forward_ca_pem
            )
        except Exception as err:  # noqa: BLE001
            # The transport then fails to connect, which the sensor shows.
            _LOGGER.warning(
                "Electric Fence could not set up TLS for forwarding: %s",
                type(err).__name__,
            )

    def factory() -> SyslogTransport:
        return SyslogTransport(
            settings.forward_host,
            settings.forward_port,
            settings.forward_protocol,
            ssl_context,
        )

    return Forwarder(hass, runtime, factory)


async def _async_start_forwarder(hass: HomeAssistant, runtime: Runtime) -> None:
    """Start forwarding when it is on. Never raises."""
    try:
        forwarder = await _async_build_forwarder(hass, runtime)
        if forwarder is not None:
            forwarder.start()
        runtime.forwarder = forwarder
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning(
            "Electric Fence could not start forwarding: %s", type(err).__name__
        )


async def _async_restart_forwarder(hass: HomeAssistant, runtime: Runtime) -> None:
    """Replace the forwarder; events still queued move to the new one.

    Restarts run one at a time, so overlapping option updates neither
    orphan a running forwarder nor reorder the queued events.
    """
    async with runtime.forward_lock:
        try:
            new = await _async_build_forwarder(hass, runtime)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Electric Fence could not start forwarding: %s", type(err).__name__
            )
            new = None
        # Read after the build, so the swap has no await and no record
        # falls between.
        old = runtime.forwarder
        if old is not None:
            old.detach()
        if new is not None:
            new.start()
        runtime.forwarder = new
        if old is not None:
            await old.stop()
            if new is not None:
                new.adopt(old.take_queue())


async def async_apply_settings(
    hass: HomeAssistant, entry: ElectricFenceConfigEntry
) -> None:
    """Apply changed options live (update listener). Never reloads the entry."""
    runtime = entry.runtime_data
    old = runtime.settings
    settings = Settings.from_options(entry.options)
    # Saving the settings means the TLS notice has been acted on.
    async_clear_tls_check_required(hass)
    # Collectors, notifier and sensor read runtime.settings on every use.
    runtime.settings = settings
    if runtime.geo_updater is not None and (
        settings.geoip_provider != old.geoip_provider
        or settings.maxmind_account_id != old.maxmind_account_id
        or settings.maxmind_license_key != old.maxmind_license_key
    ):
        try:
            await runtime.geo_updater.async_set_provider(settings)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Electric Fence could not switch GeoIP provider: %s",
                type(err).__name__,
            )
    if _forward_target(settings) != _forward_target(old):
        await _async_restart_forwarder(hass, runtime)
    if old.record_values and not settings.record_values:
        # Flush first so rows still queued with values are covered too.
        await _async_strip_values(hass, runtime, flush=True)
    if (
        settings.integration_repairs != old.integration_repairs
        or settings.verify_files != old.verify_files
        or settings.unmaintained_months != old.unmaintained_months
    ) and runtime.integrity is not None:
        # A scan already scheduled keeps its time when `scan_delay_minutes`
        # changes; the next one uses the new delay.
        try:
            runtime.integrity.apply_repairs()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Electric Fence could not update integration repairs: %s",
                type(err).__name__,
            )
    if settings.max_db_mb < old.max_db_mb:
        await async_enforce_size(hass, runtime)
    if settings.retention_days < old.retention_days:
        try:
            await async_purge(hass, runtime)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Electric Fence purge failed: %s", type(err).__name__)
    async_dispatcher_send(hass, SIGNAL_SETTINGS_UPDATED)


async def async_unload_entry(
    hass: HomeAssistant, entry: ElectricFenceConfigEntry
) -> None:
    """Unload a config entry.

    Stops recording first and flushes the worker while the notifier, the
    sensor and the live feed still listen, so the last records still fire
    their events. The rest (listeners, store) is torn down by the on_unload
    callbacks registered in setup, which also cover a setup that failed
    partway.
    """
    await _async_stop_recording(entry.runtime_data)
    # Not run on shutdown. While the entry is unloaded nothing would keep
    # these repairs current, so they go; the first scan after setup restores
    # them.
    async_sync_integration_issues(hass, set())
    await hass.config_entries.async_unload_platforms_or_raise(entry, PLATFORMS)


async def _async_stop_recording(runtime: Runtime) -> None:
    """Uninstall collectors in reverse, then stop (flush) the worker."""
    # First, so no new attempt is delayed. Attempts waiting in the slow-down
    # are released and continue with Core's step on the next loop iteration;
    # uninstalling the hook below doesn't affect calls already inside it.
    if runtime.username_guard is not None:
        runtime.username_guard.async_stop()
    for collector in reversed(runtime.collectors):
        collector.uninstall()
    runtime.collectors.clear()
    await _async_stop_integrity(runtime)
    await _async_stop_app_watcher(runtime)
    await runtime.worker.async_stop()


def _start_integrity(hass: HomeAssistant, runtime: Runtime) -> None:
    """Schedule the integration checks. Never raises."""
    try:
        scheduler = IntegrityScheduler(hass, runtime)
        scheduler.start()
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning(
            "Electric Fence could not schedule integration checks: %s",
            type(err).__name__,
        )
        return
    runtime.integrity = scheduler


async def _async_stop_integrity(runtime: Runtime) -> None:
    """Stop the integration checks; repairs stay. Never raises."""
    scheduler, runtime.integrity = runtime.integrity, None
    if scheduler is None:
        return
    try:
        await scheduler.stop()
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning(
            "Electric Fence could not stop the integration checks: %s",
            type(err).__name__,
        )


async def _async_stop_app_watcher(runtime: Runtime) -> None:
    """Stop the outside app watcher, recording its pending burst. Never raises."""
    watcher, runtime.app_watcher = runtime.app_watcher, None
    if watcher is None:
        return
    try:
        await watcher.stop()
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning(
            "Electric Fence could not stop the app watcher: %s", type(err).__name__
        )


async def _async_teardown(hass: HomeAssistant, runtime: Runtime) -> None:
    """Stop recording (if still running), then close the store."""
    await _async_stop_recording(runtime)
    await hass.async_add_executor_job(runtime.store.close)


def _remove_data(config_dir: Path) -> None:
    storage = config_dir / ".storage"
    for suffix in ("", "-wal", "-shm"):
        (storage / f"{DB_FILENAME}{suffix}").unlink(missing_ok=True)
    shutil.rmtree(storage / DATA_DIRNAME, ignore_errors=True)


async def _async_lift_own_bans(hass: HomeAssistant) -> None:
    """Lift Electric Fence's active bans in Core; other bans stay. Never raises."""
    try:
        ips = await hass.async_add_executor_job(read_active_ban_ips, _db_path(hass))
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning(
            "Electric Fence could not read its bans on removal: %s",
            type(err).__name__,
        )
        return
    backend = CoreBanBackend(hass)
    for ip in ips:
        if (addr := normalise_ip(ip)) is None:
            continue
        try:
            await backend.async_remove(addr)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Electric Fence could not lift a ban on removal: %s",
                type(err).__name__,
            )


async def async_remove_entry(
    hass: HomeAssistant, entry: ElectricFenceConfigEntry
) -> None:
    """Lift our bans, then delete the database and GeoIP files."""
    await _async_lift_own_bans(hass)
    async_clear_disabled_collector(hass, "protection")
    async_clear_ip_ban_disabled(hass)
    async_sync_integration_issues(hass, set())
    await hass.async_add_executor_job(_remove_data, Path(hass.config.config_dir))
