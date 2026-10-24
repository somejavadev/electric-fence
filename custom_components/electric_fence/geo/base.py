"""GeoIP building blocks: IP classification, provider interface, reader, status."""

from abc import ABC, abstractmethod
import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from functools import partial
import ipaddress
import os
from pathlib import Path
import tempfile
from typing import IO, ClassVar
import zlib

from aiohttp import ClientResponse, ClientSession
import maxminddb

from ..models import Source

# Largest database accepted after decompression, and largest compressed
# download. Real country files are a few MB; these only stop abuse.
MAX_DB_BYTES = 256 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
# Read, decompress and write in pieces of this size, so memory stays small.
CHUNK_SIZE = 64 * 1024

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
    ULA and CGNAT addresses are local. Public means globally routable;
    addresses that are neither local nor global (documentation ranges,
    reserved, multicast, ...) are not public.
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
    return text, None, addr.is_global


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


async def stream_to_temp(resp: ClientResponse, dest: Path) -> Path:
    """Stream a response body into a temp file next to `dest`, size-capped.

    The temp file is removed if anything fails.
    """
    loop = asyncio.get_running_loop()
    declared = resp.headers.get("Content-Length")
    if (
        declared is not None
        and declared.isdigit()
        and int(declared) > MAX_DOWNLOAD_BYTES
    ):
        raise GeoDownloadError("download is too large")
    tmp = await loop.run_in_executor(None, _make_temp, dest.parent, ".download-")
    try:
        handle = await loop.run_in_executor(None, tmp.open, "wb")
        try:
            total = 0
            async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise GeoDownloadError("download is too large")
                await loop.run_in_executor(None, handle.write, chunk)
        finally:
            await loop.run_in_executor(None, handle.close)
    except BaseException:
        await loop.run_in_executor(None, partial(tmp.unlink, missing_ok=True))
        raise
    return tmp


def _make_temp(directory: Path, prefix: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=directory, prefix=prefix)
    os.close(fd)
    return Path(name)


def gunzip_to(src: Path, dst: Path) -> None:
    """Decompress gzip `src` into `dst`, failing as soon as it exceeds the limit."""
    decomp = zlib.decompressobj(16 + zlib.MAX_WBITS)
    total = 0
    try:
        with src.open("rb") as fin, dst.open("wb") as fout:
            while block := fin.read(CHUNK_SIZE):
                pending = block
                while True:
                    data = decomp.decompress(pending, CHUNK_SIZE)
                    total += len(data)
                    if total > MAX_DB_BYTES:
                        raise GeoDownloadError("downloaded database is too large")
                    fout.write(data)
                    pending = decomp.unconsumed_tail
                    if not pending and len(data) < CHUNK_SIZE:
                        break
    except zlib.error as err:
        raise GeoDownloadError("downloaded file is not valid gzip") from err
    if not decomp.eof:
        raise GeoDownloadError("downloaded file is not valid gzip")


def copy_limited(src: IO[bytes], dst: Path) -> None:
    """Copy `src` to `dst`, failing as soon as it exceeds the limit."""
    total = 0
    with dst.open("wb") as fout:
        while block := src.read(CHUNK_SIZE):
            total += len(block)
            if total > MAX_DB_BYTES:
                raise GeoDownloadError("downloaded database is too large")
            fout.write(block)


def _finish(raw: Path, dest: Path, extract: Callable[[Path, Path], None]) -> None:
    """Extract `raw` to a second temp file, validate it, replace `dest`."""
    out = _make_temp(dest.parent, ".extract-")
    try:
        extract(raw, out)
        validate_mmdb(out)
        out.replace(dest)
    finally:
        out.unlink(missing_ok=True)


async def install_response(
    resp: ClientResponse, dest: Path, extract: Callable[[Path, Path], None]
) -> None:
    """Download `resp` and install the database it contains at `dest`.

    `extract(raw_file, output_file)` runs in the executor. On any failure
    `dest` is untouched and no temp file remains.
    """
    loop = asyncio.get_running_loop()
    raw = await stream_to_temp(resp, dest)
    try:
        await loop.run_in_executor(None, _finish, raw, dest, extract)
    finally:
        await loop.run_in_executor(None, partial(raw.unlink, missing_ok=True))


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
