"""Tests for GeoIP, reverse DNS and the enricher."""

import asyncio
import base64
from collections.abc import AsyncIterator
from datetime import timedelta
import gzip
import io
import os
from pathlib import Path
import re
import socket
import tarfile
import threading
import time
from unittest.mock import AsyncMock, Mock, PropertyMock, patch
import zlib

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.electric_fence.geo import base
from custom_components.electric_fence.geo.base import (
    GeoDownloadError,
    GeoReader,
    GeoStatus,
    classify_ip,
)
from custom_components.electric_fence.geo.enrich import RDNS_MAX_BACKLOG, GeoEnricher
from custom_components.electric_fence.geo.maxmind import DOWNLOAD_URL, MaxMindProvider
from custom_components.electric_fence.geo.rdns import ReverseDns
from custom_components.electric_fence.geo.updater import GeoUpdater, geoip_dir
from custom_components.electric_fence.models import (
    LoginAttempt,
    LoginReason,
    LoginResult,
    Source,
    UserExists,
)
from custom_components.electric_fence.settings import Settings
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from tests.fixtures.make_mmdb import build_mmdb_bytes
from tests.helpers import make_runtime

DBIP_NOW = dt_util.utc_from_timestamp(1792800000)  # 2026-10-24
DBIP_THIS = "https://download.db-ip.com/free/dbip-country-lite-2026-10.mmdb.gz"
DBIP_PREV = "https://download.db-ip.com/free/dbip-country-lite-2026-09.mmdb.gz"


@pytest.fixture
def mmdb(tmp_path: Path) -> bytes:
    """Bytes of a database mapping 8.8.8.0/24 to RU and 2606:4700::/32 to NL."""
    return build_mmdb_bytes(tmp_path)


def _attempt(ip: str | None, source: Source | None = None) -> LoginAttempt:
    return LoginAttempt(
        ts=dt_util.utcnow(),
        result=LoginResult.FAILED,
        reason=LoginReason.WRONG_PASSWORD,
        username_typed=None,
        user_exists=UserExists.UNKNOWN,
        user_id=None,
        provider=None,
        ip=ip,
        source=source,
        url=None,
        user_agent=None,
        client_id=None,
    )


@pytest.fixture
async def updater(hass: HomeAssistant, mmdb: bytes) -> AsyncIterator[GeoUpdater]:
    """Return an updater with its database installed and opened."""
    path = geoip_dir(hass) / "dbip-country-lite.mmdb"
    path.parent.mkdir(parents=True)
    path.write_bytes(mmdb)
    upd = GeoUpdater(hass, Settings(), GeoStatus("dbip_lite"))
    await upd.async_start()
    await upd.async_wait()
    yield upd
    await upd.async_stop()


def _enricher(
    hass: HomeAssistant, upd: GeoUpdater | None, **settings: object
) -> GeoEnricher:
    runtime = make_runtime(hass)
    runtime.settings = Settings(**settings)  # type: ignore[arg-type]
    runtime.geo_updater = upd
    return GeoEnricher(hass, runtime)


def test_classify_ipv4_mapped_ipv6() -> None:
    """IPv4-mapped IPv6 is normalised and classified as IPv4."""
    assert classify_ip("::ffff:8.8.8.7") == ("8.8.8.7", None, True)
    assert classify_ip("::ffff:192.168.1.2")[1] is Source.LOCAL


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "::1",
        "10.1.2.3",
        "172.30.32.1",
        "192.168.0.9",
        "100.64.1.1",
        "169.254.1.1",
        "fe80::1%eth0",
        "fd12:3456::1",
    ],
)
def test_classify_local(ip: str) -> None:
    """Private, loopback, link-local, ULA and CGNAT addresses are local."""
    norm, source, public = classify_ip(ip)
    assert (norm is not None, source, public) == (True, Source.LOCAL, False)


@pytest.mark.parametrize("ip", [None, "", "nonsense"])
def test_classify_invalid(ip: str | None) -> None:
    """Missing and invalid addresses give nothing."""
    assert classify_ip(ip) == (None, None, False)


@pytest.mark.parametrize(
    "ip",
    [
        "0.0.0.0",
        "203.0.113.7",
        "198.51.100.1",
        "192.0.2.1",
        "198.18.0.1",
        "192.0.0.8",
        "240.0.0.1",
        "2001:db8::1",
    ],
)
def test_classify_not_global(ip: str) -> None:
    """Neither local nor global addresses are not public (R30)."""
    assert classify_ip(ip) == (ip, None, False)


def test_classify_public_is_global() -> None:
    """Public means globally routable."""
    assert classify_ip("::ffff:8.8.8.7") == ("8.8.8.7", None, True)


def test_classify_public_ipv6() -> None:
    """A global IPv6 address is public."""
    assert classify_ip("2606:4700::1") == ("2606:4700::1", None, True)


async def test_enrich_public_ip_sets_country_and_hostname(
    hass: HomeAssistant, updater: GeoUpdater
) -> None:
    """A public IP gets source, country and hostname."""
    rec = _attempt("8.8.8.7")
    with patch("socket.gethostbyaddr", return_value=("host.example.net", [], [])):
        await _enricher(hass, updater).async_enrich(rec)
    assert (rec.source, rec.country_code, rec.hostname) == (
        Source.REMOTE,
        "US",
        "host.example.net",
    )
    rec6 = _attempt("2606:4700::5")
    await _enricher(hass, updater, reverse_dns=False).async_enrich(rec6)
    assert (rec6.country_code, rec6.hostname) == ("NL", None)
    mapped = _attempt("::ffff:8.8.8.7")
    await _enricher(hass, updater, reverse_dns=False).async_enrich(mapped)
    assert mapped.country_code == "US"


async def test_enrich_keeps_existing_source(
    hass: HomeAssistant, updater: GeoUpdater
) -> None:
    """The enricher sets source only when it is None."""
    rec = _attempt("8.8.8.7", Source.CLOUD)
    local = _attempt("192.168.1.2", Source.CLOUD)
    enricher = _enricher(hass, updater, reverse_dns=False)
    await enricher.async_enrich(rec)
    await enricher.async_enrich(local)
    assert rec.source is Source.CLOUD
    assert local.source is Source.CLOUD
    assert rec.country_code == "US"


async def test_enrich_local_ip_no_lookup(
    hass: HomeAssistant, updater: GeoUpdater
) -> None:
    """Local addresses are not looked up."""
    rec = _attempt("192.168.1.2")
    with patch("socket.gethostbyaddr") as rdns:
        await _enricher(hass, updater).async_enrich(rec)
    rdns.assert_not_called()
    assert (rec.source, rec.country_code, rec.hostname) == (Source.LOCAL, None, None)


async def test_enrich_missing_db_leaves_country_none(hass: HomeAssistant) -> None:
    """Without a database the country stays None and nothing fails."""
    upd = GeoUpdater(hass, Settings(), GeoStatus("dbip_lite"))
    rec = _attempt("8.8.8.7")
    await _enricher(hass, upd, reverse_dns=False).async_enrich(rec)
    assert (rec.source, rec.country_code) == (Source.REMOTE, None)
    # No updater and no runtime at all.
    rec2 = _attempt("8.8.8.7")
    await _enricher(hass, None).async_enrich(rec2)
    await GeoEnricher(hass).async_enrich(rec2)
    assert rec2.country_code is None
    # No IP.
    rec3 = _attempt(None)
    await _enricher(hass, upd).async_enrich(rec3)
    assert rec3.source is None


async def test_enrich_non_global_is_remote_without_lookup(
    hass: HomeAssistant, updater: GeoUpdater
) -> None:
    """A documentation-range address is REMOTE and never looked up (R30)."""
    rec = _attempt("203.0.113.7")
    with patch("socket.gethostbyaddr") as rdns:
        await _enricher(hass, updater).async_enrich(rec)
    rdns.assert_not_called()
    assert (rec.source, rec.country_code, rec.hostname) == (Source.REMOTE, None, None)
    cloud = _attempt("203.0.113.7", Source.CLOUD)
    await _enricher(hass, updater).async_enrich(cloud)
    assert cloud.source is Source.CLOUD


async def test_enrich_survives_lookup_errors(
    hass: HomeAssistant, updater: GeoUpdater
) -> None:
    """A failing reader or resolver leaves the fields None."""
    rec = _attempt("8.8.8.7")
    with (
        patch.object(GeoReader, "country", side_effect=RuntimeError("boom")),
        patch("socket.gethostbyaddr", side_effect=socket.herror),
    ):
        await _enricher(hass, updater).async_enrich(rec)
    assert (rec.country_code, rec.hostname) == (None, None)


def test_reader_edge_cases(tmp_path: Path, mmdb: bytes) -> None:
    """Closed readers and unknown addresses give None; reopening replaces."""
    reader = GeoReader()
    assert reader.country("8.8.8.7") is None
    path = tmp_path / "a.mmdb"
    path.write_bytes(mmdb)
    reader.open(path)
    reader.open(path)
    assert reader.country("1.1.1.1") is None
    assert reader.country("not an ip") is None
    reader.close()
    assert not reader.is_open
    reader.close()


async def test_dbip_download_falls_back_to_previous_month(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, mmdb: bytes
) -> None:
    """A 404 for this month falls back to last month's file."""
    aioclient_mock.get(DBIP_THIS, status=404)
    aioclient_mock.get(DBIP_PREV, content=gzip.compress(mmdb))
    upd = GeoUpdater(hass, Settings(), GeoStatus("dbip_lite"))
    with patch("homeassistant.util.dt.utcnow", return_value=DBIP_NOW):
        await upd.async_start()
        await upd.async_wait()
    assert [str(c[1]) for c in aioclient_mock.mock_calls] == [DBIP_THIS, DBIP_PREV]
    assert upd.reader.country("8.8.8.7") == "US"
    assert upd.status.last_error is None
    assert upd.status.db_date is not None
    assert upd.status.attribution == "IP geolocation by DB-IP"
    assert list(geoip_dir(hass).iterdir()) == [
        geoip_dir(hass) / "dbip-country-lite.mmdb"
    ]
    await upd.async_stop()


@pytest.mark.parametrize(
    ("this_status", "prev_status", "error"),
    [(404, 404, "no database"), (500, 200, "HTTP 500")],
)
async def test_dbip_download_failures(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    this_status: int,
    prev_status: int,
    error: str,
) -> None:
    """Missing months and HTTP errors are reported in the status."""
    aioclient_mock.get(DBIP_THIS, status=this_status)
    aioclient_mock.get(DBIP_PREV, status=prev_status)
    upd = GeoUpdater(hass, Settings(), GeoStatus("dbip_lite"))
    with patch("homeassistant.util.dt.utcnow", return_value=DBIP_NOW):
        await upd.async_start()
        await upd.async_wait()
    assert upd.status.last_error is not None
    assert error in upd.status.last_error
    assert not upd.reader.is_open
    await upd.async_stop()


async def test_download_network_error_reported(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A connection error is reported without its message."""
    aioclient_mock.get(DBIP_THIS, exc=TimeoutError("secret detail"))
    upd = GeoUpdater(hass, Settings(), GeoStatus("dbip_lite"))
    with patch("homeassistant.util.dt.utcnow", return_value=DBIP_NOW):
        await upd.async_start()
        await upd.async_wait()
    assert upd.status.last_error == "DB-IP download failed (TimeoutError)"
    await upd.async_stop()


async def test_download_is_atomic_on_corrupt_file(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    updater: GeoUpdater,
    mmdb: bytes,
) -> None:
    """A corrupt download keeps the old file and sets last_error."""
    path = geoip_dir(hass) / "dbip-country-lite.mmdb"
    old_mtime = time.time() - 40 * 86400
    path.touch()
    os.utime(path, (old_mtime, old_mtime))
    for url, body in (
        (DBIP_THIS, b"definitely not gzip"),
        (DBIP_PREV, b"unused"),
    ):
        aioclient_mock.get(url, content=body)
    with patch("homeassistant.util.dt.utcnow", return_value=DBIP_NOW):
        await updater.async_set_provider(Settings())
        await updater.async_wait()
    assert path.read_bytes() == mmdb
    assert updater.status.last_error == "downloaded file is not valid gzip"
    assert updater.reader.country("8.8.8.7") == "US"
    assert list(geoip_dir(hass).iterdir()) == [path]

    # Valid gzip of something that is not a database.
    aioclient_mock.clear_requests()
    aioclient_mock.get(DBIP_THIS, content=gzip.compress(b"not a database"))
    with patch("homeassistant.util.dt.utcnow", return_value=DBIP_NOW):
        await updater.async_set_provider(Settings())
        await updater.async_wait()
    assert path.read_bytes() == mmdb
    assert updater.status.last_error == "downloaded file is not a valid database"


def _tar_gz(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def test_maxmind_download(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, mmdb: bytes
) -> None:
    """MaxMind's tar.gz is unpacked, validated and opened."""
    archive = _tar_gz(
        {
            "GeoLite2-Country_20261020/README.txt": b"hi",
            "GeoLite2-Country_20261020/GeoLite2-Country.mmdb": mmdb,
        }
    )
    aioclient_mock.get(DOWNLOAD_URL, content=archive)
    settings = Settings(
        geoip_provider="maxmind", maxmind_account_id="123", maxmind_license_key="KEY"
    )
    upd = GeoUpdater(hass, settings, GeoStatus("maxmind"))
    await upd.async_start()
    await upd.async_wait()
    assert upd.reader.country("2606:4700::1") == "NL"
    assert upd.status.attribution is None
    assert upd.status.last_error is None
    assert (geoip_dir(hass) / "GeoLite2-Country.mmdb").exists()
    await upd.async_stop()


@pytest.mark.parametrize(
    ("status", "content", "error"),
    [
        (401, b"", "rejected the credentials"),
        (500, b"", "HTTP 500"),
        (200, b"not gzip", "not valid gzip"),
        (200, gzip.compress(b"not a tar"), "not a valid archive"),
        (200, _tar_gz({"readme.txt": b"x"}), "no database"),
    ],
)
async def test_maxmind_download_failures(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
    status: int,
    content: bytes,
    error: str,
) -> None:
    """Failures are reported without leaking the license key."""
    aioclient_mock.get(DOWNLOAD_URL, status=status, content=content)
    settings = Settings(
        geoip_provider="maxmind",
        maxmind_account_id="123",
        maxmind_license_key="SECRETKEY",
    )
    upd = GeoUpdater(hass, settings, GeoStatus("maxmind"))
    await upd.async_start()
    await upd.async_wait()
    assert upd.status.last_error is not None
    assert error in upd.status.last_error
    assert "SECRETKEY" not in caplog.text
    assert "SECRETKEY" not in upd.status.last_error
    await upd.async_stop()


async def test_maxmind_invalid_credentials_rejected(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """HTTP 401 and 403 mean invalid credentials; 200 means valid."""
    session = async_get_clientsession(hass)
    provider = MaxMindProvider("123", "bad")
    aioclient_mock.head(DOWNLOAD_URL, status=401)
    assert await provider.async_validate_credentials(session) is False
    aioclient_mock.clear_requests()
    aioclient_mock.head(DOWNLOAD_URL, status=403)
    assert await provider.async_validate_credentials(session) is False
    aioclient_mock.clear_requests()
    aioclient_mock.head(DOWNLOAD_URL, status=200)
    assert await provider.async_validate_credentials(session) is True
    aioclient_mock.clear_requests()
    aioclient_mock.head(DOWNLOAD_URL, status=503)
    with pytest.raises(GeoDownloadError):
        await provider.async_validate_credentials(session)
    aioclient_mock.clear_requests()
    aioclient_mock.head(DOWNLOAD_URL, exc=TimeoutError)
    with pytest.raises(GeoDownloadError):
        await provider.async_validate_credentials(session)


async def test_provider_switch_live(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    updater: GeoUpdater,
    mmdb: bytes,
) -> None:
    """Switching provider swaps the database and status; none closes it."""
    assert updater.reader.country("8.8.8.7") == "US"
    aioclient_mock.get(DOWNLOAD_URL, content=_tar_gz({"x/GeoLite2-Country.mmdb": mmdb}))
    await updater.async_set_provider(
        Settings(
            geoip_provider="maxmind", maxmind_account_id="1", maxmind_license_key="k"
        )
    )
    await updater.async_wait()
    assert updater.status.provider == "maxmind"
    assert updater.status.attribution is None
    assert updater.reader.country("8.8.8.7") == "US"
    assert aioclient_mock.call_count == 1

    await updater.async_set_provider(Settings(geoip_provider="none"))
    await updater.async_wait()
    assert updater.status.provider == "none"
    assert updater.status.db_date is None
    assert not updater.reader.is_open
    assert aioclient_mock.call_count == 1


async def test_unreadable_existing_file_is_redownloaded(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, mmdb: bytes
) -> None:
    """A corrupt file on disk is replaced by a fresh download."""
    path = geoip_dir(hass) / "dbip-country-lite.mmdb"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"garbage")
    aioclient_mock.get(DBIP_THIS, content=gzip.compress(mmdb))
    upd = GeoUpdater(hass, Settings(), GeoStatus("dbip_lite"))
    with patch("homeassistant.util.dt.utcnow", return_value=DBIP_NOW):
        await upd.async_start()
        await upd.async_wait()
    assert upd.status.last_error is None
    assert upd.reader.country("8.8.8.7") == "US"
    await upd.async_stop()


async def test_fresh_file_not_downloaded_and_monthly_refresh(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    updater: GeoUpdater,
    mmdb: bytes,
) -> None:
    """A recent file is only opened; the 30-day timer forces a download."""
    assert aioclient_mock.call_count == 0
    assert updater.status.db_date is not None
    aioclient_mock.get(DBIP_THIS, content=gzip.compress(mmdb))
    with patch("homeassistant.util.dt.utcnow", return_value=DBIP_NOW):
        await updater.async_refresh(DBIP_NOW)
        await updater.async_wait()
    assert aioclient_mock.call_count == 1
    assert updater.reader.country("8.8.8.7") == "US"


async def test_unexpected_download_error_reported(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-download exception is reported by type only."""
    upd = GeoUpdater(hass, Settings(), GeoStatus("dbip_lite"))
    with patch(
        "custom_components.electric_fence.geo.dbip_lite.DbIpLiteProvider.async_download",
        side_effect=RuntimeError("secret"),
    ):
        await upd.async_start()
        await upd.async_wait()
    assert upd.status.last_error == "download failed (RuntimeError)"
    assert "secret" not in caplog.text
    await upd.async_stop()


async def test_stop_cancels_running_download(hass: HomeAssistant) -> None:
    """Stopping cancels an in-flight download."""
    started = asyncio.Event()
    hang_cancelled = asyncio.Event()

    async def hang(*_: object) -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        finally:
            hang_cancelled.set()

    upd = GeoUpdater(hass, Settings(), GeoStatus("dbip_lite"))
    with patch(
        "custom_components.electric_fence.geo.dbip_lite.DbIpLiteProvider.async_download",
        hang,
    ):
        await upd.async_start()
        await started.wait()
        await upd.async_stop()
    assert hang_cancelled.is_set()


async def test_rdns_timeout_returns_none(hass: HomeAssistant) -> None:
    """A slow resolver gives None after the timeout, cached as a miss."""
    rdns = ReverseDns(hass, timeout=0.05)
    with patch("socket.gethostbyaddr", side_effect=lambda _: time.sleep(0.3)):
        assert await rdns.async_lookup("8.8.8.7") is None
    with patch("socket.gethostbyaddr", return_value=("ok.example", [], [])) as call:
        assert await rdns.async_lookup("8.8.8.7") is None
    call.assert_not_called()


async def test_rdns_concurrent_lookups_share_one_resolution(
    hass: HomeAssistant,
) -> None:
    """Concurrent lookups of one IP run the resolver once."""
    rdns = ReverseDns(hass)

    def slow(_: str) -> tuple[str, list[str], list[str]]:
        time.sleep(0.1)
        return ("h.example", [], [])

    with patch("socket.gethostbyaddr", side_effect=slow) as call:
        results = await asyncio.gather(
            *(rdns.async_lookup("8.8.8.7") for _ in range(3))
        )
    assert results == ["h.example"] * 3
    assert call.call_count == 1


async def test_rdns_waiter_cancel_keeps_resolution(hass: HomeAssistant) -> None:
    """Cancelling one waiter doesn't break the shared resolution."""
    rdns = ReverseDns(hass)

    def slow(_: str) -> tuple[str, list[str], list[str]]:
        time.sleep(0.1)
        return ("h.example", [], [])

    with patch("socket.gethostbyaddr", side_effect=slow) as call:
        first = asyncio.ensure_future(rdns.async_lookup("8.8.8.7"))
        await asyncio.sleep(0.01)
        second = asyncio.ensure_future(rdns.async_lookup("8.8.8.7"))
        await asyncio.sleep(0.01)
        first.cancel()
        assert await second == "h.example"
    assert call.call_count == 1


async def test_enrich_rdns_failure_leaves_hostname_none(
    hass: HomeAssistant, updater: GeoUpdater
) -> None:
    """An unexpected rDNS error leaves the hostname None, country kept."""
    rec = _attempt("8.8.8.7")
    enricher = _enricher(hass, updater)
    with patch.object(ReverseDns, "async_lookup", side_effect=RuntimeError("boom")):
        await enricher.async_enrich(rec)
    assert (rec.country_code, rec.hostname) == ("US", None)


async def test_rdns_cached(hass: HomeAssistant) -> None:
    """Two lookups of one IP call the resolver once, misses included."""
    rdns = ReverseDns(hass)
    with patch("socket.gethostbyaddr", return_value=("h.example", [], [])) as call:
        assert await rdns.async_lookup("8.8.8.7") == "h.example"
        assert await rdns.async_lookup("8.8.8.7") == "h.example"
    assert call.call_count == 1
    with patch("socket.gethostbyaddr", side_effect=socket.herror) as miss:
        assert await rdns.async_lookup("8.8.8.8") is None
        assert await rdns.async_lookup("8.8.8.8") is None
    assert miss.call_count == 1


async def test_rdns_ttl_and_lru(hass: HomeAssistant) -> None:
    """Entries expire after the TTL; the oldest is evicted past maxsize."""
    rdns = ReverseDns(hass, ttl=0, maxsize=2)
    with patch("socket.gethostbyaddr", return_value=("h.example", [], [])) as call:
        await rdns.async_lookup("8.8.8.1")
        await rdns.async_lookup("8.8.8.1")
    assert call.call_count == 2
    rdns = ReverseDns(hass, maxsize=2)
    with patch("socket.gethostbyaddr", return_value=("h.example", [], [])) as call:
        for ip in ("8.8.8.1", "8.8.8.2", "8.8.8.3", "8.8.8.1"):
            await rdns.async_lookup(ip)
    assert call.call_count == 4


@pytest.mark.parametrize(
    "name", ["", "bad host", "evil\x1b[31m.example", "ünï.example", "a" * 300]
)
async def test_rdns_rejects_hostile_names(hass: HomeAssistant, name: str) -> None:
    """Odd PTR records are dropped rather than stored."""
    rdns = ReverseDns(hass)
    with patch("socket.gethostbyaddr", return_value=(name, [], [])):
        assert await rdns.async_lookup("8.8.8.7") is None


# --- Fix round 1 -----------------------------------------------------------

BASE = "custom_components.electric_fence.geo.base"
DBIP_ANY = re.compile(r"https://download\.db-ip\.com/free/dbip-country-lite-.*")


def _basic(account: str, key: str) -> str:
    return "Basic " + base64.b64encode(f"{account}:{key}".encode()).decode()


async def _run_dbip(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, *, body: bytes
) -> GeoUpdater:
    aioclient_mock.get(DBIP_THIS, content=body)
    upd = GeoUpdater(hass, Settings(), GeoStatus("dbip_lite"))
    with patch("homeassistant.util.dt.utcnow", return_value=DBIP_NOW):
        await upd.async_start()
        await upd.async_wait()
    return upd


async def test_gunzip_bomb_is_bounded(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    updater: GeoUpdater,
    mmdb: bytes,
) -> None:
    """An over-limit gzip fails early, in small chunks, keeping the old file."""
    path = geoip_dir(hass) / "dbip-country-lite.mmdb"
    os.utime(path, (1, 1))
    aioclient_mock.get(DBIP_THIS, content=gzip.compress(bytes(5_000_000)))
    sizes: list[int] = []
    real = zlib.decompressobj

    class Spy:
        def __init__(self, *args: int) -> None:
            self._d = real(*args)

        def decompress(self, data: bytes, max_length: int = 0) -> bytes:
            out = self._d.decompress(data, max_length)
            sizes.append(len(out))
            return out

        def __getattr__(self, name: str) -> object:
            return getattr(self._d, name)

    with (
        patch(f"{BASE}.MAX_DB_BYTES", 100_000),
        patch(f"{BASE}.CHUNK_SIZE", 8192),
        patch(f"{BASE}.zlib.decompressobj", Spy),
        patch("homeassistant.util.dt.utcnow", return_value=DBIP_NOW),
    ):
        await updater.async_set_provider(Settings())
        await updater.async_wait()
    assert updater.status.last_error == "downloaded database is too large"
    assert sizes
    assert max(sizes) <= 8192
    assert path.read_bytes() == mmdb
    assert list(geoip_dir(hass).iterdir()) == [path]


async def test_truncated_gzip_rejected(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A truncated gzip stream is rejected."""
    data = gzip.compress(bytes(100_000))[:-20]
    upd = await _run_dbip(hass, aioclient_mock, body=data)
    assert upd.status.last_error == "downloaded file is not valid gzip"
    await upd.async_stop()


async def test_tar_member_bomb_is_bounded(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An oversized database inside the tar is rejected."""
    aioclient_mock.get(
        DOWNLOAD_URL, content=_tar_gz({"x/GeoLite2-Country.mmdb": bytes(300_000)})
    )
    settings = Settings(
        geoip_provider="maxmind", maxmind_account_id="1", maxmind_license_key="k"
    )
    upd = GeoUpdater(hass, settings, GeoStatus("maxmind"))
    with patch(f"{BASE}.MAX_DB_BYTES", 100_000):
        await upd.async_start()
        await upd.async_wait()
    assert upd.status.last_error == "downloaded database is too large"
    assert list(geoip_dir(hass).iterdir()) == []
    await upd.async_stop()


async def test_download_size_cap_while_streaming(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A body larger than the download limit is rejected, leaving no temp."""
    aioclient_mock.get(DBIP_THIS, content=gzip.compress(b"x") + bytes(50_000))
    upd = GeoUpdater(hass, Settings(), GeoStatus("dbip_lite"))
    with (
        patch(f"{BASE}.MAX_DOWNLOAD_BYTES", 10_000),
        patch("homeassistant.util.dt.utcnow", return_value=DBIP_NOW),
    ):
        await upd.async_start()
        await upd.async_wait()
    assert upd.status.last_error == "download is too large"
    assert list(geoip_dir(hass).iterdir()) == []
    await upd.async_stop()


async def test_download_size_cap_content_length(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A Content-Length above the limit is rejected before reading."""
    aioclient_mock.get(
        DBIP_THIS, content=b"tiny", headers={"Content-Length": "999999999"}
    )
    upd = GeoUpdater(hass, Settings(), GeoStatus("dbip_lite"))
    with patch("homeassistant.util.dt.utcnow", return_value=DBIP_NOW):
        await upd.async_start()
        await upd.async_wait()
    assert upd.status.last_error == "download is too large"
    await upd.async_stop()


async def test_validate_credentials_302_is_valid(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A redirect answer means the credentials were accepted; none is followed."""
    session = async_get_clientsession(hass)
    aioclient_mock.head(
        DOWNLOAD_URL, status=302, headers={"Location": "https://r2.example/x"}
    )
    assert await MaxMindProvider("1", "k").async_validate_credentials(session)
    assert aioclient_mock.call_count == 1


async def test_validate_credentials_disables_redirects(hass: HomeAssistant) -> None:
    """The HEAD request is sent with allow_redirects=False."""
    session = Mock()
    resp = AsyncMock()
    resp.status = 200
    session.head.return_value.__aenter__ = AsyncMock(return_value=resp)
    session.head.return_value.__aexit__ = AsyncMock(return_value=None)
    assert await MaxMindProvider("1", "k").async_validate_credentials(session)
    assert session.head.call_args.kwargs["allow_redirects"] is False


async def test_maxmind_sends_basic_auth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, mmdb: bytes
) -> None:
    """GET and HEAD carry Authorization: Basic base64(account:key)."""
    session = async_get_clientsession(hass)
    provider = MaxMindProvider("123", "KEY")
    aioclient_mock.head(DOWNLOAD_URL, status=200)
    aioclient_mock.get(DOWNLOAD_URL, content=_tar_gz({"x/a.mmdb": mmdb}))
    await provider.async_validate_credentials(session)
    await provider.async_download(session, geoip_dir(hass) / "m.mmdb")
    methods = {c[0]: c[3] for c in aioclient_mock.mock_calls}
    assert methods["HEAD"]["Authorization"] == _basic("123", "KEY")
    assert methods["GET"]["Authorization"] == _basic("123", "KEY")


@pytest.mark.usefixtures("socket_enabled")
async def test_cross_origin_redirect_drops_authorization(
    hass: HomeAssistant, mmdb: bytes, tmp_path: Path
) -> None:
    """The presigned host never sees the Authorization header."""
    seen: dict[str, str | None] = {}

    async def presigned(request: web.Request) -> web.Response:
        seen["presigned"] = request.headers.get("Authorization")
        return web.Response(body=_tar_gz({"x/GeoLite2-Country.mmdb": mmdb}))

    store = web.Application()
    store.router.add_get("/file", presigned)
    store_server = TestServer(store)
    await store_server.start_server()

    async def permalink(request: web.Request) -> web.Response:
        seen["permalink"] = request.headers.get("Authorization")
        raise web.HTTPFound(str(store_server.make_url("/file")))

    api = web.Application()
    api.router.add_get("/download", permalink)
    api_server = TestServer(api)
    await api_server.start_server()
    dest = tmp_path / "m.mmdb"
    try:
        async with ClientSession() as session:
            with patch(
                "custom_components.electric_fence.geo.maxmind.DOWNLOAD_URL",
                str(api_server.make_url("/download")),
            ):
                await MaxMindProvider("123", "KEY").async_download(session, dest)
    finally:
        await api_server.close()
        await store_server.close()
    assert seen["permalink"] == _basic("123", "KEY")
    assert seen["presigned"] is None
    assert dest.exists()


async def test_validation_failure_after_payload_written(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    updater: GeoUpdater,
    mmdb: bytes,
) -> None:
    """Garbage that unpacks fine but fails validation keeps old file, no temp."""
    path = geoip_dir(hass) / "dbip-country-lite.mmdb"
    os.utime(path, (1, 1))
    aioclient_mock.get(DBIP_THIS, content=gzip.compress(b"garbage" * 100))
    with (
        patch("homeassistant.util.dt.utcnow", return_value=DBIP_NOW),
        patch(f"{BASE}.validate_mmdb", wraps=base.validate_mmdb) as validate,
    ):
        await updater.async_set_provider(Settings())
        await updater.async_wait()
    validate.assert_called_once()
    assert path.read_bytes() == mmdb
    assert list(geoip_dir(hass).iterdir()) == [path]
    assert updater.status.last_error == "downloaded file is not a valid database"


async def test_failed_download_retried_after_one_hour(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, mmdb: bytes
) -> None:
    """A failed download is retried once, an hour later (R31)."""
    aioclient_mock.get(DBIP_ANY, status=500)
    upd = GeoUpdater(hass, Settings(), GeoStatus("dbip_lite"))
    await upd.async_start()
    await upd.async_wait()
    assert upd.status.last_error is not None
    calls = aioclient_mock.call_count

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=59))
    await hass.async_block_till_done()
    await upd.async_wait()
    assert aioclient_mock.call_count == calls

    aioclient_mock.clear_requests()
    aioclient_mock.get(DBIP_ANY, content=gzip.compress(mmdb))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=1, minutes=1))
    await hass.async_block_till_done()
    await upd.async_wait()
    assert upd.status.last_error is None
    assert upd.reader.country("8.8.8.7") == "US"
    await upd.async_stop()


async def test_retry_happens_once(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A failed retry does not schedule another one."""
    aioclient_mock.get(DBIP_ANY, status=500)
    upd = GeoUpdater(hass, Settings(), GeoStatus("dbip_lite"))
    await upd.async_start()
    await upd.async_wait()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=1, minutes=1))
    await hass.async_block_till_done()
    await upd.async_wait()
    calls = aioclient_mock.call_count
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=3))
    await hass.async_block_till_done()
    await upd.async_wait()
    assert aioclient_mock.call_count == calls
    await upd.async_stop()


async def test_enrich_skips_rdns_under_backlog(
    hass: HomeAssistant, updater: GeoUpdater
) -> None:
    """With many records waiting, hostnames are skipped so the queue drains."""
    enricher = _enricher(hass, updater)
    assert enricher.runtime is not None
    with (
        patch.object(
            type(enricher.runtime.worker),
            "queued",
            new_callable=PropertyMock,
            return_value=RDNS_MAX_BACKLOG + 1,
        ),
        patch("socket.gethostbyaddr") as rdns,
    ):
        rec = _attempt("8.8.8.7")
        await enricher.async_enrich(rec)
    rdns.assert_not_called()
    assert (rec.country_code, rec.hostname) == ("US", None)


async def test_rdns_running_lookups_capped(hass: HomeAssistant) -> None:
    """Past the cap, a lookup returns None at once and isn't cached.

    Lookups still blocked in a thread after their timeout count too, so a
    hostile resolver can't use up Home Assistant's executor.
    """
    rdns = ReverseDns(hass, timeout=0.01, max_running=2)
    release = threading.Event()

    def stuck(_: str) -> tuple[str, list[str], list[str]]:
        release.wait(5)
        return ("late.example", [], [])

    try:
        with patch("socket.gethostbyaddr", side_effect=stuck) as call:
            assert await rdns.async_lookup("8.8.8.1") is None  # timed out
            assert await rdns.async_lookup("8.8.8.2") is None  # timed out
            assert await rdns.async_lookup("8.8.8.3") is None  # capped
            assert call.call_count == 2
    finally:
        release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    for _ in range(50):
        if rdns.running == 0:
            break
        await asyncio.sleep(0.01)
    assert rdns.running == 0
    # The capped address wasn't cached as a miss: it is looked up now.
    with patch("socket.gethostbyaddr", return_value=("h.example", [], [])):
        assert await rdns.async_lookup("8.8.8.3") == "h.example"


async def test_rdns_ipv6_miss_cached_per_prefix(hass: HomeAssistant) -> None:
    """A miss for one IPv6 address answers for its whole /64."""
    rdns = ReverseDns(hass)
    with patch("socket.gethostbyaddr", side_effect=socket.herror) as miss:
        assert await rdns.async_lookup("2606:4700:1:2::1") is None
        assert await rdns.async_lookup("2606:4700:1:2::99") is None
    assert miss.call_count == 1
    with patch("socket.gethostbyaddr", return_value=("h.example", [], [])):
        assert await rdns.async_lookup("2606:4700:1:3::1") == "h.example"


async def test_rdns_prefix_misses_bounded(hass: HomeAssistant) -> None:
    """The /64 miss cache drops its oldest entries; junk addresses have none."""
    rdns = ReverseDns(hass, maxsize=1)
    with patch("socket.gethostbyaddr", side_effect=socket.herror):
        assert await rdns.async_lookup("2606:4700:1:1::1") is None
        assert await rdns.async_lookup("2606:4700:1:2::1") is None
        assert await rdns.async_lookup("not an ip") is None
    with patch("socket.gethostbyaddr", return_value=("h.example", [], [])):
        assert await rdns.async_lookup("2606:4700:1:1::2") == "h.example"
