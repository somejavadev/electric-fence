"""The Electric Fence integration."""

from datetime import datetime, timedelta
import logging
from pathlib import Path
import shutil

from awesomeversion import AwesomeVersion

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform, __version__ as HA_VERSION
from homeassistant.core import HassJob, HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .attribution import AttributionCollector
from .collectors.auth_failures import AuthFailureCollector
from .collectors.base import BaseCollector
from .collectors.config_entries import ConfigEntryCollector
from .collectors.core_config import CoreConfigCollector
from .collectors.login import LoginCollector
from .collectors.users import UserCollector
from .const import MIN_HA_VERSION
from .geo.base import GeoStatus
from .geo.enrich import GeoEnricher
from .geo.updater import GeoUpdater
from .notifier import Notifier
from .runtime import Runtime
from .settings import Settings
from .storage import FenceStore
from .worker import FenceWorker

_LOGGER = logging.getLogger(__name__)

type ElectricFenceConfigEntry = ConfigEntry[Runtime]

DB_FILENAME = "electric_fence.db"
DATA_DIRNAME = "electric_fence"
PURGE_INTERVAL = timedelta(hours=24)
FIRST_PURGE_DELAY = timedelta(minutes=5)
PLATFORMS = [Platform.SENSOR]

# Each collector task appends its class here.
COLLECTOR_TYPES: list[type[BaseCollector]] = [
    LoginCollector,
    AuthFailureCollector,
    AttributionCollector,
    ConfigEntryCollector,
    UserCollector,
    CoreConfigCollector,
]


def _db_path(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(".storage", DB_FILENAME))


def _open_store(path: Path) -> FenceStore:
    path.parent.mkdir(parents=True, exist_ok=True)
    store = FenceStore(path)
    store.open()
    return store


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

    store = await hass.async_add_executor_job(_open_store, _db_path(hass))
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
    # Registered first, so it runs last (on_unload runs in reverse). HA runs
    # on_unload both on unload and when setup fails partway, so this is the
    # only teardown path.
    entry.async_on_unload(lambda: _async_teardown(hass, runtime))

    await worker.async_start()

    async def _async_shutdown() -> None:
        # Store what is queued before shutdown cancels background tasks.
        await worker.async_stop()
        await hass.async_add_executor_job(store.close)

    entry.async_on_unload(hass.async_add_shutdown_job(HassJob(_async_shutdown)))

    # Registered after the worker's shutdown job, so it stops first.
    entry.async_on_unload(geo_updater.async_stop)

    # Downloads wait for startup to finish, so they never slow it down.
    async def _async_start_geo(_hass: HomeAssistant) -> None:
        await geo_updater.async_start()

    entry.async_on_unload(async_at_started(hass, _async_start_geo))

    notifier = Notifier(hass, runtime)
    runtime.notifier = notifier
    entry.async_on_unload(notifier.async_stop)
    entry.async_on_unload(worker.add_listener(notifier.on_record))

    for collector_type in COLLECTOR_TYPES:
        collector = collector_type(hass, runtime)
        runtime.collectors.append(collector)
        collector.install()

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

    # Applies option changes live; it never reloads, so the hooks stay put.
    entry.async_on_unload(entry.add_update_listener(async_apply_settings))

    # Last, so nothing that can fail runs after the platform is forwarded.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)


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


async def async_apply_settings(
    hass: HomeAssistant, entry: ElectricFenceConfigEntry
) -> None:
    """Apply changed options live (update listener). Never reloads the entry."""
    runtime = entry.runtime_data
    old = runtime.settings
    settings = Settings.from_options(entry.options)
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
    if settings.retention_days < old.retention_days:
        try:
            await async_purge(hass, runtime)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Electric Fence purge failed: %s", type(err).__name__)


async def async_unload_entry(
    hass: HomeAssistant, entry: ElectricFenceConfigEntry
) -> None:
    """Unload a config entry.

    Collectors, worker and store are torn down by the on_unload callback
    registered in setup, which also covers a setup that failed partway.
    """
    await hass.config_entries.async_unload_platforms_or_raise(entry, PLATFORMS)


async def _async_teardown(hass: HomeAssistant, runtime: Runtime) -> None:
    """Uninstall collectors in reverse, stop the worker, close the store."""
    for collector in reversed(runtime.collectors):
        collector.uninstall()
    runtime.collectors.clear()
    await runtime.worker.async_stop()
    await hass.async_add_executor_job(runtime.store.close)


def _remove_data(config_dir: Path) -> None:
    storage = config_dir / ".storage"
    for suffix in ("", "-wal", "-shm"):
        (storage / f"{DB_FILENAME}{suffix}").unlink(missing_ok=True)
    shutil.rmtree(storage / DATA_DIRNAME, ignore_errors=True)


async def async_remove_entry(
    hass: HomeAssistant, entry: ElectricFenceConfigEntry
) -> None:
    """Delete the database and GeoIP files when the integration is removed."""
    await hass.async_add_executor_job(_remove_data, Path(hass.config.config_dir))
