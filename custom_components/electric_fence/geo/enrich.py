"""The real enricher: source, country and hostname."""

import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant

from ..models import LoginAttempt, Source
from .base import classify_ip
from .rdns import ReverseDns

if TYPE_CHECKING:
    from ..runtime import Runtime

_LOGGER = logging.getLogger(__name__)


class GeoEnricher:
    """Adds source, country and hostname. Every failure leaves None."""

    def __init__(self, hass: HomeAssistant, runtime: Runtime | None = None) -> None:
        """Initialise. `runtime` may be attached later (it holds the worker)."""
        self._hass = hass
        self.runtime = runtime
        self._rdns = ReverseDns(hass)

    async def async_enrich(self, record: LoginAttempt) -> None:
        """Set `source` (only if unset), `country_code` and `hostname`."""
        ip, local_source, is_public = classify_ip(record.ip)
        if ip is None:
            return
        if local_source is not None:
            if record.source is None:
                record.source = local_source
            return
        if not is_public:
            return
        if record.source is None:
            record.source = Source.REMOTE
        runtime = self.runtime
        if runtime is None:
            return
        updater = runtime.geo_updater
        if updater is not None and updater.reader.is_open:
            try:
                record.country_code = await self._hass.async_add_executor_job(
                    updater.reader.country, ip
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("GeoIP lookup failed: %s", type(err).__name__)
        if runtime.settings.reverse_dns:
            record.hostname = await self._rdns.async_lookup(ip)
