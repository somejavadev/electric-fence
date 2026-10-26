"""Fetch the published files of an integration and verify the installed copy."""

from dataclasses import dataclass
import hashlib
import logging
from tempfile import SpooledTemporaryFile
from typing import IO, Literal
import zipfile

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..runtime import Runtime
from .files import FileReport, TreeHashes, _skipped, compare, hash_tree, is_cache
from .inventory import InstalledIntegration

_LOGGER = logging.getLogger(__name__)

MAX_ARCHIVE_BYTES = 25 * 1024 * 1024
_TIMEOUT = aiohttp.ClientTimeout(total=60)
_CHUNK = 64 * 1024

UncheckedReason = Literal[
    "no_reference",
    "too_large",
    "unreachable",
    "not_found",
    "bad_archive",
    "rate_limited",
]


@dataclass(frozen=True)
class Unchecked:
    """The official copy could not be fetched or read."""

    reason: UncheckedReason


@dataclass(frozen=True)
class FileResult:
    """Outcome of verifying one integration."""

    domain: str
    source: str
    status: Literal[
        "match", "changed", "unchecked", "baseline_recorded", "baseline_renewed"
    ]
    report: FileReport | None = None
    reason: str | None = None
    ref: str | None = None
    version_before: str | None = None


def reference_url(item: InstalledIntegration) -> str | None:
    """Return where the published files of the installed version live."""
    repo = item.repository
    if not repo:
        return None
    if item.zip_release and item.installed_version and item.zip_filename:
        return (
            f"https://github.com/{repo}/releases/download/"
            f"{item.installed_version}/{item.zip_filename}"
        )
    if item.installed_version:
        return (
            f"https://codeload.github.com/{repo}/zip/refs/tags/{item.installed_version}"
        )
    if item.installed_commit:
        return f"https://codeload.github.com/{repo}/zip/{item.installed_commit}"
    return None


def _member_path(item: InstalledIntegration, name: str) -> str | None:
    """Map an archive member to a path inside the integration folder."""
    parts = name.split("/")
    if "" in parts[:-1] or ".." in parts or name.endswith("/"):
        return None
    if not item.zip_release:
        parts = parts[1:]
        if not item.content_in_root:
            prefix = ["custom_components", item.domain]
            if parts[:2] != prefix:
                return None
            parts = parts[2:]
    return "/".join(parts) or None


def _hash_archive(
    spool: IO[bytes], item: InstalledIntegration
) -> dict[str, str] | Unchecked:
    hashes: dict[str, str] = {}
    try:
        with zipfile.ZipFile(spool) as archive:
            for info in archive.infolist():
                rel = _member_path(item, info.filename)
                if rel is None or any(is_cache(part) for part in rel.split("/")):
                    continue
                digest = hashlib.sha256()
                with archive.open(info) as member:
                    while chunk := member.read(_CHUNK):
                        digest.update(chunk)
                hashes[rel] = digest.hexdigest()
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        OSError,
        ValueError,
        EOFError,
        NotImplementedError,
    ):
        return Unchecked("bad_archive")
    return hashes or Unchecked("bad_archive")


async def fetch_reference(
    hass: HomeAssistant, item: InstalledIntegration
) -> dict[str, str] | Unchecked:
    """Download the published archive and return the hashes of the integration."""
    url = reference_url(item)
    if url is None:
        return Unchecked("no_reference")
    session = async_get_clientsession(hass)
    try:
        async with session.get(url, timeout=_TIMEOUT) as response:
            if response.status == 404:
                return Unchecked("not_found")
            if response.status in (403, 429):
                return Unchecked("rate_limited")
            if response.status != 200:
                return Unchecked("unreachable")
            declared = response.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > MAX_ARCHIVE_BYTES:
                return Unchecked("too_large")
            # Held in memory: the cap keeps it small and nothing touches disk.
            with SpooledTemporaryFile(max_size=MAX_ARCHIVE_BYTES + _CHUNK) as spool:
                size = 0
                async for chunk in response.content.iter_chunked(_CHUNK):
                    size += len(chunk)
                    if size > MAX_ARCHIVE_BYTES:
                        return Unchecked("too_large")
                    spool.write(chunk)
                spool.seek(0)
                return await hass.async_add_executor_job(_hash_archive, spool, item)
    except aiohttp.ClientError, TimeoutError, OSError:
        return Unchecked("unreachable")
    except Exception:
        _LOGGER.debug("Fetching %s failed", item.domain, exc_info=True)
        return Unchecked("unreachable")


def _ignore(runtime: Runtime, item: InstalledIntegration) -> frozenset[str]:
    ignore = set(runtime.settings.ignored_for(item.domain))
    if item.persistent_directory:
        ignore.add(item.persistent_directory.strip("/") + "/")
    return frozenset(ignore)


def _symlink_report() -> FileReport:
    return FileReport([], [], ["."], [], ["."])


async def verify(
    hass: HomeAssistant, runtime: Runtime, item: InstalledIntegration
) -> FileResult:
    """Compare the installed files with the published copy or the baseline."""
    ignore = _ignore(runtime, item)
    store = runtime.store
    if item.source == "marketplace":
        return await _verify_marketplace(hass, runtime, item, ignore)

    actual = await hass.async_add_executor_job(hash_tree, item.path, ignore)
    version = item.version
    before, baseline = await hass.async_add_executor_job(store.baseline, item.domain)
    if before is None and not baseline:
        await hass.async_add_executor_job(
            store.save_baseline, item.domain, version, actual.files
        )
        return FileResult(item.domain, "manual", "baseline_recorded", ref=version)
    if before != version:
        await hass.async_add_executor_job(
            store.save_baseline, item.domain, version, actual.files
        )
        return FileResult(
            item.domain,
            "manual",
            "baseline_renewed",
            ref=version,
            version_before=before,
        )
    return _compared(item, baseline, actual, version)


def _compared(
    item: InstalledIntegration,
    expected: dict[str, str],
    actual: TreeHashes,
    ref: str | None,
) -> FileResult:
    report = compare(expected, actual)
    return FileResult(
        item.domain,
        item.source,
        "match" if report.ok else "changed",
        report=report,
        ref=ref,
    )


async def _verify_marketplace(
    hass: HomeAssistant,
    runtime: Runtime,
    item: InstalledIntegration,
    ignore: frozenset[str],
) -> FileResult:
    ref = item.installed_version or item.installed_commit
    repo = item.repository
    if item.symlinked:
        return FileResult(
            item.domain, "marketplace", "changed", report=_symlink_report(), ref=ref
        )
    if not repo or not ref:
        return FileResult(
            item.domain, "marketplace", "unchecked", reason="no_reference", ref=ref
        )
    store = runtime.store
    reference = await hass.async_add_executor_job(store.reference_hashes, repo, ref)
    if reference is None:
        fetched = await fetch_reference(hass, item)
        if isinstance(fetched, Unchecked):
            return FileResult(
                item.domain,
                "marketplace",
                "unchecked",
                reason=fetched.reason,
                ref=ref,
            )
        await hass.async_add_executor_job(store.save_reference, repo, ref, fetched)
        reference = fetched
    reference = {
        k: v for k, v in reference.items() if not _skipped(k, _prefixes(ignore), ignore)
    }
    actual = await hass.async_add_executor_job(hash_tree, item.path, ignore)
    return _compared(item, reference, actual, ref)


def _prefixes(ignore: frozenset[str]) -> tuple[str, ...]:
    return tuple(i for i in ignore if i.endswith("/"))
