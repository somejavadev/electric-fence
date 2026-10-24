"""DB-IP Lite country database."""

import asyncio
from datetime import date, timedelta
from pathlib import Path
import zlib

from aiohttp import ClientError, ClientSession, ClientTimeout

from homeassistant.util import dt as dt_util

from .base import MAX_DB_BYTES, GeoDownloadError, GeoProvider, install_database

URL_TEMPLATE = (
    "https://download.db-ip.com/free/dbip-country-lite-{year}-{month:02d}.mmdb.gz"
)
ATTRIBUTION = "IP geolocation by DB-IP"
_TIMEOUT = ClientTimeout(total=300)


def _previous_month(today: date) -> date:
    first = today.replace(day=1)
    return (first - timedelta(days=1)).replace(day=1)


def gunzip_limited(data: bytes) -> bytes:
    """Decompress gzip `data`, refusing more than `MAX_DB_BYTES`."""
    decomp = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = decomp.decompress(data, MAX_DB_BYTES + 1)
        out += decomp.flush()
    except zlib.error as err:
        raise GeoDownloadError("downloaded file is not valid gzip") from err
    if len(out) > MAX_DB_BYTES or decomp.unconsumed_tail or not decomp.eof:
        raise GeoDownloadError("downloaded file is not valid gzip")
    return out


class DbIpLiteProvider(GeoProvider):
    """DB-IP Lite: free, no account, needs attribution."""

    key = "dbip_lite"
    filename = "dbip-country-lite.mmdb"

    async def async_download(self, session: ClientSession, dest: Path) -> None:
        """Download this month's file, or last month's if not yet published."""
        today = dt_util.utcnow().date()
        months = (today.replace(day=1), _previous_month(today))
        payload: bytes | None = None
        for month in months:
            url = URL_TEMPLATE.format(year=month.year, month=month.month)
            try:
                async with session.get(url, timeout=_TIMEOUT) as resp:
                    if resp.status == 404:
                        continue
                    if resp.status != 200:
                        raise GeoDownloadError(f"DB-IP returned HTTP {resp.status}")
                    payload = await resp.read()
            except (ClientError, TimeoutError) as err:
                raise GeoDownloadError(
                    f"DB-IP download failed ({type(err).__name__})"
                ) from err
            break
        if payload is None:
            raise GeoDownloadError("DB-IP has no database for this or last month")
        await asyncio.get_running_loop().run_in_executor(None, _install, dest, payload)


def _install(dest: Path, payload: bytes) -> None:
    def produce(tmp: Path) -> None:
        tmp.write_bytes(gunzip_limited(payload))

    install_database(dest, produce)
