"""Contract for the Marketplace object shape the integration inventory reads."""

from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.electric_fence.contracts import (
    CONTRACTS,
    INTEGRATION_CHECKS,
    check_contracts,
    marketplace_contract_ok,
)
from homeassistant.components.marketplace.repositories.base import (
    RepositoryData,
    RepositoryManifest,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant


@pytest.fixture(autouse=True)
def _no_marketplace_unload(hass: HomeAssistant) -> Iterator[None]:
    """Fake Marketplace entries can't be unloaded by Core at teardown."""
    yield
    for entry in hass.config_entries.async_entries("marketplace"):
        entry.mock_state(hass, ConfigEntryState.NOT_LOADED)


def _load(hass: HomeAssistant, repo: object | None, **manager: object) -> None:
    repos = SimpleNamespace(
        list_all=lambda: [] if repo is None else [repo],
        is_removed=lambda name: False,
        removed_repository=lambda name: None,
    )
    for key, value in manager.items():
        setattr(repos, key, value) if value is not None else delattr(repos, key)
    entry = MockConfigEntry(domain="marketplace")
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    entry.runtime_data = SimpleNamespace(
        repositories=repos, common=SimpleNamespace(archived_repositories=set())
    )


def _real_repo() -> SimpleNamespace:
    return SimpleNamespace(
        data=RepositoryData(), repository_manifest=RepositoryManifest.from_dict({})
    )


async def test_contract_registered() -> None:
    """The contract disables `integration_checks`."""
    assert CONTRACTS["marketplace_repositories"].collector == INTEGRATION_CHECKS
    assert INTEGRATION_CHECKS == "integration_checks"


async def test_contract_passes_without_marketplace(hass: HomeAssistant) -> None:
    """No loaded Marketplace is not a failure."""
    assert check_contracts(hass, INTEGRATION_CHECKS) == []
    assert marketplace_contract_ok(hass)


async def test_contract_passes_with_core_objects(hass: HomeAssistant) -> None:
    """Core's own repository data classes match."""
    _load(hass, _real_repo())
    assert check_contracts(hass, INTEGRATION_CHECKS) == []
    assert marketplace_contract_ok(hass)


async def test_contract_fails_on_missing_data_field(hass: HomeAssistant) -> None:
    """A removed repository field fails with a clear reason."""
    repo = _real_repo()
    repo.data = SimpleNamespace(
        **{k: v for k, v in vars(repo.data).items() if k != "installed_commit"}
    )
    _load(hass, repo)
    reasons = check_contracts(hass, INTEGRATION_CHECKS)
    assert len(reasons) == 1
    assert "installed_commit" in reasons[0]
    assert not marketplace_contract_ok(hass)


async def test_contract_fails_on_missing_manifest_field(hass: HomeAssistant) -> None:
    """A removed manifest field fails."""
    repo = _real_repo()
    repo.repository_manifest = SimpleNamespace(zip_release=False)
    _load(hass, repo)
    reasons = check_contracts(hass, INTEGRATION_CHECKS)
    assert reasons
    assert "filename" in reasons[0]


async def test_contract_fails_on_missing_manager_method(hass: HomeAssistant) -> None:
    """A missing `is_removed` fails even without repositories."""
    _load(hass, None, is_removed=None)
    reasons = check_contracts(hass, INTEGRATION_CHECKS)
    assert reasons
    assert "is_removed" in reasons[0]


async def test_contract_fails_when_list_all_raises(hass: HomeAssistant) -> None:
    """A raising `list_all` fails the contract."""

    def boom() -> list[object]:
        raise RuntimeError

    _load(hass, None, list_all=boom)
    # `common` is required too
    hass.config_entries.async_loaded_entries("marketplace")[
        0
    ].runtime_data.common = SimpleNamespace(archived_repositories=set())
    reasons = check_contracts(hass, INTEGRATION_CHECKS)
    assert reasons
    assert "list_all" in reasons[0]


async def test_contract_fails_on_removed_shape(hass: HomeAssistant) -> None:
    """A removed-repository record without `reason` fails."""
    repo = _real_repo()
    repo.data.full_name = "o/r"
    _load(
        hass, repo, is_removed=lambda name: True, removed_repository=lambda n: object()
    )
    hass.config_entries.async_loaded_entries("marketplace")[
        0
    ].runtime_data.common = SimpleNamespace(archived_repositories=set())
    reasons = check_contracts(hass, INTEGRATION_CHECKS)
    assert reasons
    assert "reason" in reasons[0]
