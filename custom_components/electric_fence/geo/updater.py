"""Keeps the GeoIP database present, fresh and open."""

import asyncio
from datetime import date, datetime, timedelta
import logging
from pathlib import Path
import time

from homeassistant.core import CALLBACK_TYPE, HassJob, HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.util import dt as dt_util

from ..settings import Settings
from .base import GeoDownloadError, GeoProvider, GeoReader, GeoStatus
from .dbip_lite import ATTRIBUTION as DBIP_ATTRIBUTION, DbIpLiteProvider
from .maxmind import MaxMindProvider

_LOGGER = logging.getLogger(__name__)

MAX_AGE = timedelta(days=35)
REFRESH_INTERVAL = timedelta(days=30)
# A failed download is retried once after this delay.
RETRY_DELAY = timedelta(hours=1)


def geoip_dir(hass: HomeAssistant) -> Path:
    """Return the folder that holds the GeoIP files."""
    return Path(hass.config.path(".storage", "electric_fence", "geoip"))


def make_provider(settings: Settings) -> GeoProvider | None:
    """Return the provider chosen in `settings`, or None for no GeoIP."""
    if settings.geoip_provider == "dbip_lite":
        return DbIpLiteProvider()
    if settings.geoip_provider == "maxmind":
        return MaxMindProvider(
            settings.maxmind_account_id, settings.maxmind_license_key
        )
    return None


def _file_date(path: Path) -> date | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=dt_util.UTC).date()
    except OSError:
        return None


class GeoUpdater:
    """Downloads the database when missing or old, then monthly."""

    def __init__(
        self, hass: HomeAssistant, settings: Settings, status: GeoStatus
    ) -> None:
        """Initialise (not started)."""
        self._hass = hass
        self._settings = settings
        self.status = status
        self.reader = GeoReader()
        self._task: asyncio.Task[None] | None = None
        self._unsub: CALLBACK_TYPE | None = None
        self._retry: CALLBACK_TYPE | None = None

    async def async_start(self) -> None:
        """Open the existing database, download if needed, schedule refreshes."""
        if self._unsub is None:
            self._unsub = async_track_time_interval(
                self._hass,
                self.async_refresh,
                REFRESH_INTERVAL,
                name="electric_fence geoip refresh",
                cancel_on_shutdown=True,
            )
        self._schedule(force=False)

    async def async_set_provider(self, settings: Settings) -> None:
        """Switch provider (or credentials) live."""
        self._settings = settings
        self._schedule(force=False)

    async def async_wait(self) -> None:
        """Wait for the running open/download to finish."""
        if (task := self._task) is not None:
            await asyncio.wait({task})

    async def async_stop(self) -> None:
        """Cancel timers and downloads, close the database."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        self._cancel_retry()
        await self._cancel()
        await self._hass.async_add_executor_job(self.reader.close)

    async def async_refresh(self, _now: datetime | None = None) -> None:
        """Download a fresh database now (the monthly timer calls this)."""
        self._schedule(force=True)

    async def _async_retry(self, _now: datetime) -> None:
        self._retry = None
        self._schedule(force=True, is_retry=True)

    def _cancel_retry(self) -> None:
        if self._retry is not None:
            self._retry()
            self._retry = None

    def _schedule(self, *, force: bool, is_retry: bool = False) -> None:
        # Callers always run on the loop; the previous task is replaced.
        self._cancel_retry()
        if (old := self._task) is not None:
            old.cancel()
        self._task = self._hass.async_create_background_task(
            self._async_refresh(force=force, is_retry=is_retry),
            "electric_fence geoip",
        )

    async def _cancel(self) -> None:
        if (task := self._task) is not None:
            self._task = None
            task.cancel()
            await asyncio.wait({task})

    async def _async_refresh(self, *, force: bool, is_retry: bool) -> None:
        provider = make_provider(self._settings)
        status = self.status
        status.last_error = None
        if provider is None:
            status.provider = "none"
            status.db_date = None
            status.attribution = None
            await self._hass.async_add_executor_job(self.reader.close)
            return
        status.provider = provider.key
        status.attribution = (
            DBIP_ATTRIBUTION if isinstance(provider, DbIpLiteProvider) else None
        )
        path = geoip_dir(self._hass) / provider.filename
        # Drop the previous provider's database until this one is open.
        await self._hass.async_add_executor_job(self.reader.close)
        status.db_date = None
        opened = await self._async_open(path)
        if not opened or force or self._is_stale(path):
            await self._async_download(provider, path, is_retry=is_retry)

    def _is_stale(self, path: Path) -> bool:
        try:
            return time.time() - path.stat().st_mtime > MAX_AGE.total_seconds()
        except OSError:
            return True

    async def _async_open(self, path: Path) -> bool:
        if not await self._hass.async_add_executor_job(path.exists):
            return False
        try:
            await self._hass.async_add_executor_job(self.reader.open, path)
        except Exception as err:  # noqa: BLE001
            self.status.last_error = f"database unreadable ({type(err).__name__})"
            return False
        self.status.db_date = await self._hass.async_add_executor_job(_file_date, path)
        return True

    def _retry_later(self, is_retry: bool) -> None:
        if not is_retry:
            self._retry = async_call_later(
                self._hass,
                RETRY_DELAY,
                HassJob(
                    self._async_retry,
                    "electric_fence geoip retry",
                    cancel_on_shutdown=True,
                ),
            )

    async def _async_download(
        self, provider: GeoProvider, path: Path, *, is_retry: bool
    ) -> None:
        session = async_get_clientsession(self._hass)
        try:
            await provider.async_download(session, path)
        except GeoDownloadError as err:
            # Keep serving the old file, if any.
            self.status.last_error = str(err)
            _LOGGER.warning("Electric Fence GeoIP update failed: %s", err)
            self._retry_later(is_retry)
            return
        except Exception as err:  # noqa: BLE001
            self.status.last_error = f"download failed ({type(err).__name__})"
            _LOGGER.warning(
                "Electric Fence GeoIP update failed: %s", type(err).__name__
            )
            self._retry_later(is_retry)
            return
        self.status.last_error = None
        await self._async_open(path)
