"""GeoIP building blocks: IP classification, provider interface, reader, status."""

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
import ipaddress
import os
from pathlib import Path
import tempfile
from typing import ClassVar

from aiohttp import ClientSession
import maxminddb

from ..models import Source

# Largest database accepted after decompression. Real country files are
# a few MB; this only stops a decompression bomb.
MAX_DB_BYTES = 256 * 1024 * 1024

_LOCAL_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",  # CGNAT
        "fc00::/7",  # unique local
        "fec0::/10",  # deprecated site-local
    )
)


class GeoDownloadError(Exception):
    """A GeoIP database could not be downloaded or validated.

    Messages are written by Electric Fence and never contain credentials.
    """


def classify_ip(ip: str | None) -> tuple[str | None, Source | None, bool]:
    """Return `(normalised_ip, source_if_local, is_public)`.

    IPv4-mapped IPv6 is normalised to IPv4. Private, loopback, link-local,
    ULA and CGNAT addresses are local. Other non-routable addresses
    (unspecified, multicast, reserved) are neither local nor public.
    Documentation ranges are treated as public, so test data can use them.
    """
    if not ip:
        return None, None, False
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return None, None, False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    text = str(addr)
    if (
        addr.is_loopback
        or addr.is_link_local
        or any(addr in net for net in _LOCAL_NETWORKS if net.version == addr.version)
    ):
        return text, Source.LOCAL, False
    if addr.is_unspecified or addr.is_multicast or addr.is_reserved:
        return text, None, False
    return text, None, True


@dataclass
class GeoStatus:
    """What the panel and diagnostics show about the GeoIP database."""

    provider: str
    db_date: date | None = None
    last_error: str | None = None
    attribution: str | None = None


class GeoProvider(ABC):
    """A source of a country-level `.mmdb` file."""

    key: ClassVar[str]
    filename: ClassVar[str]

    @abstractmethod
    async def async_download(self, session: ClientSession, dest: Path) -> None:
        """Download, validate and atomically install the database at `dest`.

        On failure `dest` is left untouched and `GeoDownloadError` is raised.
        """


def validate_mmdb(path: Path) -> None:
    """Raise `GeoDownloadError` unless `path` is a readable MaxMind database."""
    try:
        with maxminddb.open_database(path) as reader:
            reader.metadata()
    except (maxminddb.InvalidDatabaseError, ValueError, OSError) as err:
        raise GeoDownloadError("downloaded file is not a valid database") from err


def install_database(dest: Path, produce: Callable[[Path], None]) -> None:
    """Write via `produce` to a temp file next to `dest`, validate, replace.

    Runs in the executor. The temp file is always removed on failure.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".download-")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        produce(tmp)
        validate_mmdb(tmp)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)


class GeoReader:
    """Country lookups in one open `.mmdb` file."""

    def __init__(self) -> None:
        """Initialise with no database."""
        self._reader: maxminddb.Reader | None = None

    @property
    def is_open(self) -> bool:
        """Return True when a database is open."""
        return self._reader is not None

    def open(self, path: Path) -> None:
        """Open `path` (replacing any open database). Runs in the executor."""
        new = maxminddb.open_database(path)
        old, self._reader = self._reader, new
        if old is not None:
            old.close()

    def country(self, ip: str) -> str | None:
        """Return the ISO alpha-2 country code of `ip`, or None."""
        reader = self._reader
        if reader is None:
            return None
        try:
            data = reader.get(ip)
        except ValueError, maxminddb.InvalidDatabaseError:
            return None
        if not isinstance(data, dict):
            return None
        for key in ("country", "registered_country"):
            section = data.get(key)
            if isinstance(section, dict):
                code = section.get("iso_code")
                if isinstance(code, str) and len(code) == 2 and code.isalpha():
                    return code.upper()
        return None

    def close(self) -> None:
        """Close the database."""
        old, self._reader = self._reader, None
        if old is not None:
            old.close()
