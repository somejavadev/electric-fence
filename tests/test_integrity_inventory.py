"""Inventory of installed custom integrations."""

from collections.abc import Iterator
from datetime import UTC, datetime
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.electric_fence.contracts import (
    INTEGRATION_CHECKS,
    check_contracts,
    marketplace_contract_ok,
)
from custom_components.electric_fence.integrity.files import hash_tree
from custom_components.electric_fence.integrity.inventory import (
    RemovedInfo,
    async_inventory,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant


@pytest.fixture(autouse=True)
def _no_marketplace_unload(hass: HomeAssistant) -> Iterator[None]:
    """Fake Marketplace entries can't be unloaded by Core at teardown."""
    yield
    for entry in hass.config_entries.async_entries("marketplace"):
        entry.mock_state(hass, ConfigEntryState.NOT_LOADED)


def _integration(hass: HomeAssistant, name: str, version: str | None = "1.0.0") -> None:
    folder = Path(hass.config.path("custom_components")) / name
    folder.mkdir(parents=True)
    manifest: dict[str, Any] = {"domain": name}
    if version is not None:
        manifest["version"] = version
    (folder / "manifest.json").write_text(json.dumps(manifest))


def _repo(**data: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "installed": True,
        "category": "integration",
        "domain": "demo",
        "full_name": "owner/demo",
        "installed_version": "1.2.0",
        "installed_commit": "abc",
        "archived": False,
        "last_updated": 0,
    }
    base.update(data)
    manifest = SimpleNamespace(
        zip_release=True,
        filename="demo.zip",
        content_in_root=True,
        persistent_directory="store",
    )
    return SimpleNamespace(data=SimpleNamespace(**base), repository_manifest=manifest)


def _marketplace(
    hass: HomeAssistant,
    repos: list[SimpleNamespace],
    *,
    removed: dict[str, RemovedInfo] | None = None,
    archived: set[str] | None = None,
) -> SimpleNamespace:
    removed = removed or {}
    calls: list[str] = []

    def removed_repository(name: str) -> SimpleNamespace:
        calls.append(name)
        info = removed[name]
        return SimpleNamespace(reason=info.reason, removal_type=info.removal_type)

    manager = SimpleNamespace(
        repositories=SimpleNamespace(
            list_all=lambda: repos,
            is_removed=lambda name: name in removed,
            removed_repository=removed_repository,
        ),
        common=SimpleNamespace(archived_repositories=archived or set()),
        removed_calls=calls,
    )
    entry = MockConfigEntry(domain="marketplace")
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    entry.runtime_data = manager
    return manager


async def test_inventory_manual_without_marketplace(hass: HomeAssistant) -> None:
    """Folders with a manifest are manual; others are skipped."""
    _integration(hass, "alpha", "2.3.4")
    (Path(hass.config.path("custom_components")) / "empty").mkdir()

    result = await async_inventory(hass)

    assert len(result) == 1
    item = result[0]
    assert item.domain == "alpha"
    assert item.source == "manual"
    assert item.version == "2.3.4"
    assert item.path == Path(hass.config.path("custom_components")) / "alpha"
    assert item.repository is None
    assert item.removed is None
    assert not item.zip_release


async def test_inventory_bad_manifest(hass: HomeAssistant) -> None:
    """An unreadable manifest gives version None, not an error."""
    folder = Path(hass.config.path("custom_components")) / "bad"
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text("{nope")
    result = await async_inventory(hass)
    assert [(i.domain, i.version) for i in result] == [("bad", None)]


async def test_inventory_marketplace_match(hass: HomeAssistant) -> None:
    """A matching installed repository fills every marketplace field."""
    _integration(hass, "demo")
    _integration(hass, "other")
    _marketplace(
        hass,
        [
            _repo(last_updated="2026-03-04T05:06:07+00:00"),
            _repo(domain="gone", installed=False),
            _repo(domain="other", category="plugin"),
        ],
    )

    result = {i.domain: i for i in await async_inventory(hass)}

    demo = result["demo"]
    assert demo.source == "marketplace"
    assert demo.repository == "owner/demo"
    assert demo.installed_version == "1.2.0"
    assert demo.installed_commit == "abc"
    assert demo.zip_release is True
    assert demo.zip_filename == "demo.zip"
    assert demo.content_in_root is True
    assert demo.persistent_directory == "store"
    assert demo.archived is False
    assert demo.removed is None
    assert demo.last_updated == datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
    assert result["other"].source == "manual"


async def test_inventory_archived_removed_and_naive_date(hass: HomeAssistant) -> None:
    """Archived comes from the set too; removed is read only when flagged."""
    _integration(hass, "demo")
    _integration(hass, "kept")
    manager = _marketplace(
        hass,
        [
            _repo(last_updated="2026-03-04T05:06:07"),
            _repo(domain="kept", full_name="owner/kept", last_updated="garbage"),
        ],
        removed={"owner/demo": RemovedInfo("malware", "critical")},
        archived={"owner/demo"},
    )

    result = {i.domain: i for i in await async_inventory(hass)}

    assert result["demo"].archived is True
    assert result["demo"].removed == RemovedInfo("malware", "critical")
    assert result["demo"].last_updated == datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
    assert result["kept"].removed is None
    assert result["kept"].last_updated is None
    assert manager.removed_calls == ["owner/demo"]


async def test_inventory_marketplace_broken_falls_back_to_manual(
    hass: HomeAssistant,
) -> None:
    """A marketplace object of the wrong shape never breaks the inventory."""
    _integration(hass, "demo")
    entry = MockConfigEntry(domain="marketplace")
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    entry.runtime_data = object()
    result = await async_inventory(hass)
    assert [(i.domain, i.source) for i in result] == [("demo", "manual")]
    assert not marketplace_contract_ok(hass)


async def test_inventory_symlinked_folder(hass: HomeAssistant) -> None:
    """A symlinked folder is listed, flagged, and nothing behind it is read."""
    outside = Path(hass.config.path("outside"))
    outside.mkdir()
    (outside / "manifest.json").write_text('{"version": "9"}')
    (outside / "f.py").write_bytes(b"x")
    root = Path(hass.config.path("custom_components"))
    root.mkdir(exist_ok=True)
    (root / "foo").symlink_to(outside)
    _marketplace(hass, [_repo(domain="foo", full_name="o/foo")])
    real_open = open
    opened: list[str] = []

    def spy(file: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    with patch("builtins.open", spy):
        result = await async_inventory(hass)
        hashes = await hass.async_add_executor_job(
            hash_tree, result[0].path, frozenset()
        )

    assert result[0].symlinked is True
    assert result[0].version is None
    assert result[0].source == "marketplace"
    assert hashes.symlinks == ["."]
    assert not any("outside" in p for p in opened)


async def test_contract_fails_without_common(hass: HomeAssistant) -> None:
    """A manager without `common` fails the contract."""
    manager = _marketplace(hass, [_repo()])
    del manager.common
    assert not marketplace_contract_ok(hass)
    assert "archived_repositories" in check_contracts(hass, INTEGRATION_CHECKS)[0]
