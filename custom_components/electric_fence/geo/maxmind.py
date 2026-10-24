"""MaxMind GeoLite2-Country database."""

import asyncio
import io
from pathlib import Path
import tarfile

from aiohttp import ClientError, ClientSession, ClientTimeout, encode_basic_auth

from .base import MAX_DB_BYTES, GeoDownloadError, GeoProvider, install_database

# MaxMind's authenticated permalink download. Confirm against MaxMind's docs.
DOWNLOAD_URL = (
    "https://download.maxmind.com/geoip/databases/GeoLite2-Country/download"
    "?suffix=tar.gz"
)
ATTRIBUTION = None
_TIMEOUT = ClientTimeout(total=300)


def _extract_mmdb(archive: bytes, tmp: Path) -> None:
    """Write the `.mmdb` member of a tar.gz to `tmp`."""
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            for member in tar:
                if member.isfile() and member.name.endswith(".mmdb"):
                    if member.size > MAX_DB_BYTES:
                        raise GeoDownloadError("database in archive is too large")
                    src = tar.extractfile(member)
                    if src is None:
                        break
                    tmp.write_bytes(src.read())
                    return
    except (tarfile.TarError, EOFError, OSError) as err:
        raise GeoDownloadError("downloaded file is not a valid archive") from err
    raise GeoDownloadError("archive contains no database")


class MaxMindProvider(GeoProvider):
    """MaxMind GeoLite2-Country with an account ID and license key."""

    key = "maxmind"
    filename = "GeoLite2-Country.mmdb"

    def __init__(self, account_id: str | None, license_key: str | None) -> None:
        """Store the credentials (never logged)."""
        self._headers = {
            "Authorization": encode_basic_auth(account_id or "", license_key or "")
        }

    async def async_validate_credentials(self, session: ClientSession) -> bool:
        """Return False if MaxMind rejects the credentials (HTTP 401 or 403)."""
        try:
            async with session.head(
                DOWNLOAD_URL, headers=self._headers, timeout=_TIMEOUT
            ) as resp:
                if resp.status in (401, 403):
                    return False
                if resp.status >= 400:
                    raise GeoDownloadError(f"MaxMind returned HTTP {resp.status}")
        except (ClientError, TimeoutError) as err:
            raise GeoDownloadError(
                f"MaxMind request failed ({type(err).__name__})"
            ) from err
        return True

    async def async_download(self, session: ClientSession, dest: Path) -> None:
        """Download and install GeoLite2-Country."""
        try:
            async with session.get(
                DOWNLOAD_URL, headers=self._headers, timeout=_TIMEOUT
            ) as resp:
                if resp.status in (401, 403):
                    raise GeoDownloadError("MaxMind rejected the credentials")
                if resp.status != 200:
                    raise GeoDownloadError(f"MaxMind returned HTTP {resp.status}")
                payload = await resp.read()
        except (ClientError, TimeoutError) as err:
            raise GeoDownloadError(
                f"MaxMind download failed ({type(err).__name__})"
            ) from err
        await asyncio.get_running_loop().run_in_executor(None, _install, dest, payload)


def _install(dest: Path, payload: bytes) -> None:
    install_database(dest, lambda tmp: _extract_mmdb(payload, tmp))
