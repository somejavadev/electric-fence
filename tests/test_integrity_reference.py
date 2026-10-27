"""Official file set and verification."""

from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
import zipfile

import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.electric_fence.integrity.inventory import InstalledIntegration
from custom_components.electric_fence.integrity.reference import (
    Unchecked,
    accept_baseline,
    fetch_reference,
    reference_url,
    verify,
)
from custom_components.electric_fence.runtime import Runtime
from custom_components.electric_fence.settings import Settings
from custom_components.electric_fence.storage import FenceStore
from homeassistant.core import HomeAssistant
from tests.test_storage_audit import store as store  # noqa: PLC0414

REPO = "owner/demo"
RELEASE = f"https://github.com/{REPO}/releases/download/1.2.0/demo.zip"
TAG = f"https://codeload.github.com/{REPO}/zip/refs/tags/1.2.0"
COMMIT = f"https://codeload.github.com/{REPO}/zip/abc123"


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _item(tmp_path: Path, **kwargs: Any) -> InstalledIntegration:
    folder = tmp_path / "demo"
    folder.mkdir(exist_ok=True)
    if not (folder / "__init__.py").exists():
        (folder / "__init__.py").write_bytes(b"print(1)\n")
        (folder / "manifest.json").write_text(json.dumps({"domain": "demo"}))
    values: dict[str, Any] = {
        "domain": "demo",
        "path": folder,
        "version": "1.2.0",
        "source": "marketplace",
        "repository": REPO,
        "installed_version": "1.2.0",
        "zip_release": True,
        "zip_filename": "demo.zip",
    }
    values.update(kwargs)
    return InstalledIntegration(**values)


def _members(item: InstalledIntegration) -> dict[str, bytes]:
    return {
        "__init__.py": (item.path / "__init__.py").read_bytes(),
        "manifest.json": (item.path / "manifest.json").read_bytes(),
    }


@pytest.fixture
def runtime(store: FenceStore) -> Runtime:
    """Return a runtime stand-in holding settings and a store."""
    return cast(Runtime, SimpleNamespace(settings=Settings(), store=store))


def test_reference_url_variants(tmp_path: Path) -> None:
    """Release zip, tag archive, commit archive, none."""
    assert reference_url(_item(tmp_path)) == RELEASE
    assert reference_url(_item(tmp_path, zip_release=False)) == TAG
    assert (
        reference_url(
            _item(
                tmp_path,
                zip_release=False,
                installed_version=None,
                installed_commit="abc123",
            )
        )
        == COMMIT
    )
    assert reference_url(_item(tmp_path, installed_version=None)) is None


async def test_matching_release_zip(
    hass: HomeAssistant,
    tmp_path: Path,
    runtime: Runtime,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Identical files match."""
    item = _item(tmp_path)
    aioclient_mock.get(RELEASE, content=_zip(_members(item)))
    result = await verify(hass, runtime, item)
    assert result.status == "match"
    assert result.report is not None
    assert result.report.ok


async def test_changed_file_detected(
    hass: HomeAssistant,
    tmp_path: Path,
    runtime: Runtime,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A differing byte is reported."""
    item = _item(tmp_path)
    members = _members(item)
    members["__init__.py"] = b"print(2)\n"
    aioclient_mock.get(RELEASE, content=_zip(members))
    result = await verify(hass, runtime, item)
    assert result.status == "changed"
    assert result.report is not None
    assert result.report.changed == ["__init__.py"]


async def test_archive_trimmed_to_domain(
    hass: HomeAssistant,
    tmp_path: Path,
    runtime: Runtime,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Only the integration folder of a source archive is compared."""
    item = _item(tmp_path, zip_release=False)
    members = {
        f"demo-1.2.0/custom_components/demo/{k}": v for k, v in _members(item).items()
    }
    members["demo-1.2.0/README.md"] = b"readme"
    members["demo-1.2.0/custom_components/other/x.py"] = b"x"
    members["demo-1.2.0/custom_components/demo/__pycache__/a.pyc"] = b"c"
    aioclient_mock.get(TAG, content=_zip(members))
    result = await fetch_reference(hass, item)
    assert result == {k: _sha(v) for k, v in _members(item).items()}
    assert (await verify(hass, runtime, item)).status == "match"


async def test_sourceless_bytecode_is_compared(
    hass: HomeAssistant,
    tmp_path: Path,
    runtime: Runtime,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A `.pyc` outside `__pycache__` is importable, so it is hashed on both sides."""
    item = _item(tmp_path, zip_release=False)
    (item.path / "extra.pyc").write_bytes(b"installed")
    members = {
        f"demo-1.2.0/custom_components/demo/{k}": v for k, v in _members(item).items()
    }
    members["demo-1.2.0/custom_components/demo/extra.pyc"] = b"published"
    aioclient_mock.get(TAG, content=_zip(members))
    result = await verify(hass, runtime, item)
    assert result.status == "changed"
    assert result.report is not None
    assert result.report.changed == ["extra.pyc"]


async def test_content_in_root(
    hass: HomeAssistant,
    tmp_path: Path,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """With content in root the archive root after the first part is the folder."""
    item = _item(tmp_path, zip_release=False, content_in_root=True)
    members = {f"demo-1.2.0/{k}": v for k, v in _members(item).items()}
    members["demo-1.2.0/sub/y.py"] = b"y"
    aioclient_mock.get(TAG, content=_zip(members))
    result = await fetch_reference(hass, item)
    assert isinstance(result, dict)
    assert set(result) == {"__init__.py", "manifest.json", "sub/y.py"}


@pytest.mark.parametrize(
    ("kwargs", "reason", "item_kwargs"),
    [
        ({"status": 404}, "not_found", {}),
        ({"status": 429}, "rate_limited", {}),
        ({"status": 403}, "rate_limited", {}),
        ({"exc": TimeoutError()}, "unreachable", {}),
        (
            {"content": b"x", "headers": {"Content-Length": "30000000"}},
            "too_large",
            {},
        ),
        ({"content": b"\0" * (26 * 1024 * 1024)}, "too_large", {}),
        ({"content": b"garbage"}, "bad_archive", {}),
        ({}, "no_reference", {"installed_version": None}),
    ],
)
async def test_unchecked_reasons(
    hass: HomeAssistant,
    tmp_path: Path,
    runtime: Runtime,
    aioclient_mock: AiohttpClientMocker,
    kwargs: dict[str, Any],
    reason: str,
    item_kwargs: dict[str, Any],
) -> None:
    """A reference that cannot be fetched is unchecked, never changed."""
    item = _item(tmp_path, **item_kwargs)
    aioclient_mock.get(RELEASE, **kwargs)
    result = await verify(hass, runtime, item)
    assert result.status == "unchecked"
    assert result.reason == reason
    assert result.report is None
    assert runtime.store.reference_hashes(REPO, "1.2.0") is None


async def test_reference_cached(
    hass: HomeAssistant,
    tmp_path: Path,
    runtime: Runtime,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The second verify of the same ref makes no HTTP call."""
    item = _item(tmp_path)
    aioclient_mock.get(RELEASE, content=_zip(_members(item)))
    await verify(hass, runtime, item)
    assert aioclient_mock.call_count == 1
    assert (await verify(hass, runtime, item)).status == "match"
    assert aioclient_mock.call_count == 1


async def test_ignored_and_persistent(
    hass: HomeAssistant,
    tmp_path: Path,
    store: FenceStore,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Ignored files and the persistent directory are left out of the tree."""
    item = _item(tmp_path, persistent_directory="data")
    (item.path / "data").mkdir()
    (item.path / "data" / "db").write_bytes(b"d")
    (item.path / "extra.txt").write_bytes(b"e")
    aioclient_mock.get(RELEASE, content=_zip(_members(item)))
    rt = cast(
        Runtime,
        SimpleNamespace(
            settings=Settings(ignored_files=("demo/extra.txt",)), store=store
        ),
    )
    assert (await verify(hass, rt, item)).status == "match"


async def test_symlinked_marketplace(
    hass: HomeAssistant,
    tmp_path: Path,
    runtime: Runtime,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A symlinked folder is changed, without any download."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    item = _item(tmp_path, path=link, symlinked=True)
    result = await verify(hass, runtime, item)
    assert result.status == "changed"
    assert result.report is not None
    assert result.report.symlinks == ["."]
    assert aioclient_mock.call_count == 0


async def test_manual_baseline_then_change(
    hass: HomeAssistant, tmp_path: Path, runtime: Runtime
) -> None:
    """A manual install is compared with its own baseline."""
    item = _item(tmp_path, source="manual", repository=None, installed_version=None)
    first = await verify(hass, runtime, item)
    assert first.status == "baseline_recorded"
    assert (await verify(hass, runtime, item)).status == "match"
    (item.path / "__init__.py").write_bytes(b"changed")
    changed = await verify(hass, runtime, item)
    assert changed.status == "changed"
    assert changed.report is not None
    assert changed.report.changed == ["__init__.py"]


async def test_manual_version_change_needs_acceptance(
    hass: HomeAssistant, tmp_path: Path, runtime: Runtime
) -> None:
    """A new version is compared with the old baseline until it is accepted.

    Bumping the manifest version must not make tampered files the baseline.
    """
    item = _item(tmp_path, source="manual", repository=None, installed_version=None)
    assert (await verify(hass, runtime, item)).status == "baseline_recorded"
    (item.path / "__init__.py").write_bytes(b"tampered")
    bumped = _item(
        tmp_path,
        source="manual",
        repository=None,
        installed_version=None,
        version="1.3.0",
    )
    for _ in range(2):  # stays changed: nothing is renewed by itself
        result = await verify(hass, runtime, bumped)
        assert result.status == "changed"
        assert result.version_before == "1.2.0"
        assert result.ref == "1.3.0"
        assert result.report is not None
        assert result.report.changed == ["__init__.py"]

    assert await accept_baseline(hass, runtime, bumped) == 2
    accepted = await verify(hass, runtime, bumped)
    assert accepted.status == "match"
    assert accepted.version_before is None


async def test_manual_version_change_same_files(
    hass: HomeAssistant, tmp_path: Path, runtime: Runtime
) -> None:
    """With identical files (manifest ignored), only the version is updated."""
    rt = cast(
        Runtime,
        SimpleNamespace(
            settings=Settings(ignored_files=("demo/manifest.json",)),
            store=runtime.store,
        ),
    )
    item = _item(tmp_path, source="manual", repository=None, installed_version=None)
    assert (await verify(hass, rt, item)).status == "baseline_recorded"
    bumped = replace(item, version="1.3.0")
    first = await verify(hass, rt, bumped)
    assert (first.status, first.version_before) == ("match", None)
    assert runtime.store.baseline("demo")[0] == "1.3.0"


async def test_manual_empty_folder_baseline_stable(
    hass: HomeAssistant, tmp_path: Path, runtime: Runtime
) -> None:
    """A folder with no hashable files records its baseline once."""
    item = _item(tmp_path, source="manual", repository=None, installed_version=None)
    rt = cast(
        Runtime,
        SimpleNamespace(
            settings=Settings(ignored_files=("demo/__init__.py", "demo/manifest.json")),
            store=runtime.store,
        ),
    )
    assert (await verify(hass, rt, item)).status == "baseline_recorded"
    assert (await verify(hass, rt, item)).status == "match"


async def test_manual_ignore_added_after_baseline(
    hass: HomeAssistant, tmp_path: Path, runtime: Runtime
) -> None:
    """A file ignored after the baseline was taken is skipped, not missing."""
    item = _item(tmp_path, source="manual", repository=None, installed_version=None)
    (item.path / "data").mkdir()
    (item.path / "data" / "cache.json").write_text("a")
    assert (await verify(hass, runtime, item)).status == "baseline_recorded"
    (item.path / "data" / "cache.json").write_text("b")
    assert (await verify(hass, runtime, item)).status == "changed"
    for ignored in ("demo/data/cache.json", "demo/data/"):
        rt = cast(
            Runtime,
            SimpleNamespace(
                settings=Settings(ignored_files=(ignored,)), store=runtime.store
            ),
        )
        result = await verify(hass, rt, item)
        assert result.status == "match", ignored
        assert result.report is not None
        assert result.report.missing == []


async def test_symlinked_manual(
    hass: HomeAssistant, tmp_path: Path, runtime: Runtime
) -> None:
    """A symlinked manual folder is never followed."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    item = _item(tmp_path, path=link, symlinked=True, source="manual", repository=None)
    result = await verify(hass, runtime, item)
    assert runtime.store.baseline("demo") == (None, {})
    assert result.status == "changed"
    assert result.report is not None
    assert result.report.symlinks == ["."]


async def test_archive_without_domain_files_is_bad(
    hass: HomeAssistant,
    tmp_path: Path,
    runtime: Runtime,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """An archive with nothing for the domain is not cached as empty."""
    item = _item(tmp_path, zip_release=False)
    aioclient_mock.get(TAG, content=_zip({"demo-1.2.0/README.md": b"r"}))
    result = await verify(hass, runtime, item)
    assert (result.status, result.reason) == ("unchecked", "bad_archive")
    assert runtime.store.reference_hashes(REPO, "1.2.0") is None


async def test_release_without_filename_uses_archive(
    hass: HomeAssistant,
    tmp_path: Path,
    runtime: Runtime,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """zip_release without a zip filename falls back to the tag archive."""
    item = _item(tmp_path, zip_filename=None)
    members = {
        f"demo-1.2.0/custom_components/demo/{k}": v for k, v in _members(item).items()
    }
    aioclient_mock.get(TAG, content=_zip(members))
    assert reference_url(item) == TAG
    assert (await verify(hass, runtime, item)).status == "match"


async def test_folder_name_differs_from_domain(
    hass: HomeAssistant,
    tmp_path: Path,
    runtime: Runtime,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The first directory of custom_components is the integration, as in Core."""
    item = _item(tmp_path, zip_release=False)
    members = {
        f"demo-1.2.0/custom_components/alpha/{k}": v for k, v in _members(item).items()
    }
    members["demo-1.2.0/custom_components/demo/__init__.py"] = b"decoy"
    aioclient_mock.get(TAG, content=_zip(members))
    assert (await verify(hass, runtime, item)).status == "match"


async def test_zip_limits(
    hass: HomeAssistant,
    tmp_path: Path,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Too many members or too much expanded data is too_large."""
    item = _item(tmp_path)
    many = {f"f{i}": b"" for i in range(50_001)}
    aioclient_mock.get(RELEASE, content=_zip(many))
    assert await fetch_reference(hass, item) == Unchecked("too_large")
    aioclient_mock.clear_requests()
    big = _zip({"a": b"\0" * (60 * 1024 * 1024), "b": b"\0" * (60 * 1024 * 1024)})
    aioclient_mock.get(RELEASE, content=big)
    assert await fetch_reference(hass, item) == Unchecked("too_large")


async def test_member_names_normalised(
    hass: HomeAssistant,
    tmp_path: Path,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Dot parts and empty parts are dropped, traversal names ignored."""
    item = _item(tmp_path)
    aioclient_mock.get(
        RELEASE,
        content=_zip(
            {
                "./__init__.py": b"a",
                "sub//b.py": b"b",
                "../evil.py": b"e",
                "a/../c.py": b"c",
            }
        ),
    )
    assert set(cast(dict[str, str], await fetch_reference(hass, item))) == {
        "__init__.py",
        "sub/b.py",
    }
