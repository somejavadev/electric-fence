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
from homeassistant.components.marketplace.base import MarketplaceCommon, Repositories
from homeassistant.components.marketplace.repositories.integration import (
    IntegrationRepository,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from tests.marketplace_real import real_manager, real_repo


@pytest.fixture(autouse=True)
def _no_marketplace_unload(hass: HomeAssistant) -> Iterator[None]:
    """Fake Marketplace entries can't be unloaded by Core at teardown."""
    yield
    for entry in hass.config_entries.async_entries("marketplace"):
        entry.mock_state(hass, ConfigEntryState.NOT_LOADED)


def _load(hass: HomeAssistant, repo: object | None, **members: object) -> None:
    """Load a manager holding Core's real container, members overridable.

    `members` replace (or, with None, remove) what the container shows.
    """
    container = Repositories()
    if repo is not None:
        container.register(repo)  # type: ignore[arg-type]
    repos = SimpleNamespace(
        list_all=container.list_all,
        is_removed=container.is_removed,
        removed_repository=container.removed_repository,
    )
    for key, value in members.items():
        setattr(repos, key, value) if value is not None else delattr(repos, key)
    entry = MockConfigEntry(domain="marketplace")
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    entry.runtime_data = SimpleNamespace(repositories=repos, common=MarketplaceCommon())


def _real_repo() -> IntegrationRepository:
    repo = real_repo()
    repo.data.id = "1"
    return repo


def _load_real(hass: HomeAssistant, manager: SimpleNamespace) -> None:
    entry = MockConfigEntry(domain="marketplace")
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    entry.runtime_data = manager


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
    _load(hass, repo)  # registered with its real data first
    repo.data = SimpleNamespace(  # type: ignore[assignment]
        **{k: v for k, v in vars(repo.data).items() if k != "installed_commit"}
    )
    reasons = check_contracts(hass, INTEGRATION_CHECKS)
    assert len(reasons) == 1
    assert "installed_commit" in reasons[0]
    assert not marketplace_contract_ok(hass)


async def test_contract_fails_on_missing_manifest_field(hass: HomeAssistant) -> None:
    """A removed manifest field fails."""
    repo = _real_repo()
    repo.repository_manifest = SimpleNamespace(zip_release=False)  # type: ignore[assignment]
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

    class Boom:
        def __iter__(self) -> Iterator[object]:
            raise RuntimeError

    _load(hass, None, list_all=Boom())
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
    reasons = check_contracts(hass, INTEGRATION_CHECKS)
    assert reasons
    assert "reason" in reasons[0]


async def test_contract_passes_with_real_container(hass: HomeAssistant) -> None:
    """Core's real `Repositories` (list_all is a property) passes, removal included."""
    manager = real_manager([_real_repo()], removed={"owner/demo": ("why", "remove")})
    _load_real(hass, manager)
    assert check_contracts(hass, INTEGRATION_CHECKS) == []
    assert marketplace_contract_ok(hass)


async def test_contract_fails_when_list_all_is_a_method(hass: HomeAssistant) -> None:
    """A `list_all` that became a method no longer matches what is read."""
    manager = real_manager([_real_repo()])
    manager.repositories = SimpleNamespace(
        list_all=list,
        is_removed=manager.repositories.is_removed,
        removed_repository=manager.repositories.removed_repository,
    )
    _load_real(hass, manager)
    reasons = check_contracts(hass, INTEGRATION_CHECKS)
    assert reasons
    assert "list_all" in reasons[0]
