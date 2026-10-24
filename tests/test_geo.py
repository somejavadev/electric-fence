"""Tests for GeoIP, reverse DNS and the enricher."""

import asyncio
from collections.abc import AsyncIterator
import gzip
import io
import os
from pathlib import Path
import socket
import tarfile
import time
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.electric_fence.geo.base import (
    GeoDownloadError,
    GeoReader,
    GeoStatus,
    classify_ip,
)
from custom_components.electric_fence.geo.enrich import GeoEnricher
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
    """Bytes of a database mapping 203.0.113.0/24 to RU and 2001:db8::/32 to NL."""
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
    assert classify_ip("::ffff:203.0.113.7") == ("203.0.113.7", None, True)
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


@pytest.mark.parametrize("ip", [None, "", "nonsense", "0.0.0.0", "224.0.0.1"])
def test_classify_unusable(ip: str | None) -> None:
    """Invalid, unspecified and multicast addresses are neither local nor public."""
    assert classify_ip(ip)[1:] == (None, False)


def test_classify_public_ipv6() -> None:
    """A global IPv6 address is public."""
    assert classify_ip("2001:db8::1") == ("2001:db8::1", None, True)


async def test_enrich_public_ip_sets_country_and_hostname(
    hass: HomeAssistant, updater: GeoUpdater
) -> None:
    """A public IP gets source, country and hostname."""
    rec = _attempt("203.0.113.7")
    with patch("socket.gethostbyaddr", return_value=("host.example.net", [], [])):
        await _enricher(hass, updater).async_enrich(rec)
    assert (rec.source, rec.country_code, rec.hostname) == (
        Source.REMOTE,
        "RU",
        "host.example.net",
    )
    rec6 = _attempt("2001:db8::5")
    await _enricher(hass, updater, reverse_dns=False).async_enrich(rec6)
    assert (rec6.country_code, rec6.hostname) == ("NL", None)
    mapped = _attempt("::ffff:203.0.113.7")
    await _enricher(hass, updater, reverse_dns=False).async_enrich(mapped)
    assert mapped.country_code == "RU"


async def test_enrich_keeps_existing_source(
    hass: HomeAssistant, updater: GeoUpdater
) -> None:
    """The enricher sets source only when it is None."""
    rec = _attempt("203.0.113.7", Source.CLOUD)
    local = _attempt("192.168.1.2", Source.CLOUD)
    enricher = _enricher(hass, updater, reverse_dns=False)
    await enricher.async_enrich(rec)
    await enricher.async_enrich(local)
    assert rec.source is Source.CLOUD
    assert local.source is Source.CLOUD
    assert rec.country_code == "RU"


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
    rec = _attempt("203.0.113.7")
    await _enricher(hass, upd, reverse_dns=False).async_enrich(rec)
    assert (rec.source, rec.country_code) == (Source.REMOTE, None)
    # No updater and no runtime at all.
    rec2 = _attempt("203.0.113.7")
    await _enricher(hass, None).async_enrich(rec2)
    await GeoEnricher(hass).async_enrich(rec2)
    assert rec2.country_code is None
    # No IP, and an address that is neither local nor public.
    for ip in (None, "0.0.0.0"):
        rec3 = _attempt(ip)
        await _enricher(hass, upd).async_enrich(rec3)
        assert rec3.source is None


async def test_enrich_survives_lookup_errors(
    hass: HomeAssistant, updater: GeoUpdater
) -> None:
    """A failing reader or resolver leaves the fields None."""
    rec = _attempt("203.0.113.7")
    with (
        patch.object(GeoReader, "country", side_effect=RuntimeError("boom")),
        patch("socket.gethostbyaddr", side_effect=socket.herror),
    ):
        await _enricher(hass, updater).async_enrich(rec)
    assert (rec.country_code, rec.hostname) == (None, None)


def test_reader_edge_cases(tmp_path: Path, mmdb: bytes) -> None:
    """Closed readers and unknown addresses give None; reopening replaces."""
    reader = GeoReader()
    assert reader.country("203.0.113.7") is None
    path = tmp_path / "a.mmdb"
    path.write_bytes(mmdb)
    reader.open(path)
    reader.open(path)
    assert reader.country("8.8.8.8") is None
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
    assert upd.reader.country("203.0.113.7") == "RU"
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
    assert updater.reader.country("203.0.113.7") == "RU"
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
    assert upd.reader.country("2001:db8::1") == "NL"
    assert upd.status.attribution is None
    assert upd.status.last_error is None
    assert (geoip_dir(hass) / "GeoLite2-Country.mmdb").exists()
    await upd.async_stop()


@pytest.mark.parametrize(
    ("status", "content", "error"),
    [
        (401, b"", "rejected the credentials"),
        (500, b"", "HTTP 500"),
        (200, b"not a tar", "not a valid archive"),
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
    assert updater.reader.country("203.0.113.7") == "RU"
    aioclient_mock.get(DOWNLOAD_URL, content=_tar_gz({"x/GeoLite2-Country.mmdb": mmdb}))
    await updater.async_set_provider(
        Settings(
            geoip_provider="maxmind", maxmind_account_id="1", maxmind_license_key="k"
        )
    )
    await updater.async_wait()
    assert updater.status.provider == "maxmind"
    assert updater.status.attribution is None
    assert updater.reader.country("203.0.113.7") == "RU"
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
    assert upd.reader.country("203.0.113.7") == "RU"
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
    assert updater.reader.country("203.0.113.7") == "RU"


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
    """A slow resolver gives None after the timeout, and is not cached."""
    rdns = ReverseDns(hass, timeout=0.05)
    with patch("socket.gethostbyaddr", side_effect=lambda _: time.sleep(0.3)):
        assert await rdns.async_lookup("203.0.113.7") is None
    with patch("socket.gethostbyaddr", return_value=("ok.example", [], [])):
        assert await rdns.async_lookup("203.0.113.7") == "ok.example"


async def test_rdns_cached(hass: HomeAssistant) -> None:
    """Two lookups of one IP call the resolver once, misses included."""
    rdns = ReverseDns(hass)
    with patch("socket.gethostbyaddr", return_value=("h.example", [], [])) as call:
        assert await rdns.async_lookup("203.0.113.7") == "h.example"
        assert await rdns.async_lookup("203.0.113.7") == "h.example"
    assert call.call_count == 1
    with patch("socket.gethostbyaddr", side_effect=socket.herror) as miss:
        assert await rdns.async_lookup("203.0.113.8") is None
        assert await rdns.async_lookup("203.0.113.8") is None
    assert miss.call_count == 1


async def test_rdns_ttl_and_lru(hass: HomeAssistant) -> None:
    """Entries expire after the TTL; the oldest is evicted past maxsize."""
    rdns = ReverseDns(hass, ttl=0, maxsize=2)
    with patch("socket.gethostbyaddr", return_value=("h.example", [], [])) as call:
        await rdns.async_lookup("203.0.113.1")
        await rdns.async_lookup("203.0.113.1")
    assert call.call_count == 2
    rdns = ReverseDns(hass, maxsize=2)
    with patch("socket.gethostbyaddr", return_value=("h.example", [], [])) as call:
        for ip in ("203.0.113.1", "203.0.113.2", "203.0.113.3", "203.0.113.1"):
            await rdns.async_lookup(ip)
    assert call.call_count == 4


@pytest.mark.parametrize(
    "name", ["", "bad host", "evil\x1b[31m.example", "ünï.example", "a" * 300]
)
async def test_rdns_rejects_hostile_names(hass: HomeAssistant, name: str) -> None:
    """Odd PTR records are dropped rather than stored."""
    rdns = ReverseDns(hass)
    with patch("socket.gethostbyaddr", return_value=(name, [], [])):
        assert await rdns.async_lookup("203.0.113.7") is None
