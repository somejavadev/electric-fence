"""MaxMind GeoLite2-Country database."""

from pathlib import Path
import tarfile

from aiohttp import ClientError, ClientSession, ClientTimeout, encode_basic_auth

from . import base
from .base import (
    GeoDownloadError,
    GeoProvider,
    copy_limited,
    gunzip_to,
    install_response,
)

# MaxMind's authenticated permalink download. Confirm against MaxMind's docs.
DOWNLOAD_URL = (
    "https://download.maxmind.com/geoip/databases/GeoLite2-Country/download"
    "?suffix=tar.gz"
)
ATTRIBUTION = None
_TIMEOUT = ClientTimeout(total=300)


def _extract_mmdb(raw: Path, out: Path) -> None:
    """Write the `.mmdb` member of the downloaded tar.gz to `out`."""
    tar_path = out.with_name(out.name + ".tar")
    try:
        gunzip_to(raw, tar_path)
        with tarfile.open(tar_path, mode="r:") as tar:
            for member in tar:
                if member.isfile() and member.name.endswith(".mmdb"):
                    if member.size > base.MAX_DB_BYTES:
                        raise GeoDownloadError("downloaded database is too large")
                    if (src := tar.extractfile(member)) is None:
                        break
                    with src:
                        copy_limited(src, out)
                    return
    except (tarfile.TarError, EOFError) as err:
        raise GeoDownloadError("downloaded file is not a valid archive") from err
    finally:
        tar_path.unlink(missing_ok=True)
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
        """Return False if MaxMind rejects the credentials (HTTP 401 or 403).

        Redirects are not followed: the permalink answers 401 for bad
        credentials before it redirects to a presigned storage URL, so any
        2xx or 3xx answer means the credentials were accepted.
        """
        try:
            async with session.head(
                DOWNLOAD_URL,
                headers=self._headers,
                allow_redirects=False,
                timeout=_TIMEOUT,
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
        # The presigned redirect target is another origin; aiohttp drops the
        # Authorization header when following it.
        try:
            async with session.get(
                DOWNLOAD_URL, headers=self._headers, timeout=_TIMEOUT
            ) as resp:
                if resp.status in (401, 403):
                    raise GeoDownloadError("MaxMind rejected the credentials")
                if resp.status != 200:
                    raise GeoDownloadError(f"MaxMind returned HTTP {resp.status}")
                await install_response(resp, dest, _extract_mmdb)
        except (ClientError, TimeoutError) as err:
            raise GeoDownloadError(
                f"MaxMind download failed ({type(err).__name__})"
            ) from err
