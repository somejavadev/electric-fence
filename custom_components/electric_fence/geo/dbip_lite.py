"""DB-IP Lite country database."""

from datetime import date, timedelta
from pathlib import Path

from aiohttp import ClientError, ClientSession, ClientTimeout

from homeassistant.util import dt as dt_util

from .base import GeoDownloadError, GeoProvider, gunzip_to, install_response

URL_TEMPLATE = (
    "https://download.db-ip.com/free/dbip-country-lite-{year}-{month:02d}.mmdb.gz"
)
ATTRIBUTION = "IP geolocation by DB-IP"
_TIMEOUT = ClientTimeout(total=300)


def _previous_month(today: date) -> date:
    first = today.replace(day=1)
    return (first - timedelta(days=1)).replace(day=1)


class DbIpLiteProvider(GeoProvider):
    """DB-IP Lite: free, no account, needs attribution."""

    key = "dbip_lite"
    filename = "dbip-country-lite.mmdb"

    async def async_download(self, session: ClientSession, dest: Path) -> None:
        """Download this month's file, or last month's if not yet published."""
        today = dt_util.utcnow().date()
        for month in (today.replace(day=1), _previous_month(today)):
            url = URL_TEMPLATE.format(year=month.year, month=month.month)
            try:
                async with session.get(url, timeout=_TIMEOUT) as resp:
                    if resp.status == 404:
                        continue
                    if resp.status != 200:
                        raise GeoDownloadError(f"DB-IP returned HTTP {resp.status}")
                    await install_response(resp, dest, gunzip_to)
            except (ClientError, TimeoutError) as err:
                raise GeoDownloadError(
                    f"DB-IP download failed ({type(err).__name__})"
                ) from err
            return
        raise GeoDownloadError("DB-IP has no database for this or last month")
