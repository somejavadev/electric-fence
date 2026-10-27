"""Core's own Marketplace classes, built without network, for the tests."""

from types import SimpleNamespace
from typing import Any

from homeassistant.components.marketplace.base import MarketplaceCommon, Repositories
from homeassistant.components.marketplace.repositories.integration import (
    IntegrationRepository,
)


def real_repo(full_name: str = "owner/demo", **overrides: Any) -> IntegrationRepository:
    """Return a Core integration repository, installed, with its real data classes."""
    manager = SimpleNamespace(core=SimpleNamespace(config_path="/config"))
    repo = IntegrationRepository(manager, full_name)  # type: ignore[arg-type]
    values: dict[str, Any] = {
        "installed": True,
        "category": "integration",
        "domain": "demo",
        "installed_version": "1.2.0",
        "installed_commit": "abc",
        "archived": False,
        "last_updated": 0,
    }
    manifest_values: dict[str, Any] = {
        "zip_release": True,
        "filename": "demo.zip",
        "content_in_root": True,
        "persistent_directory": "store",
    }
    values.update(overrides)
    for key, value in values.items():
        setattr(repo.data, key, value)
    for key, value in manifest_values.items():
        setattr(repo.repository_manifest, key, value)
    return repo


def real_manager(
    repos: list[IntegrationRepository],
    *,
    removed: dict[str, tuple[str | None, str | None]] | None = None,
    archived: set[str] | None = None,
) -> SimpleNamespace:
    """Return a manager-shaped object holding Core's real container classes.

    Repositories go in through `Repositories.register`, removals through
    `Repositories.removed_repository`, the archived set on `MarketplaceCommon`.
    """
    repositories = Repositories()
    for number, repo in enumerate(repos, start=1):
        repo.data.id = str(number)
        repositories.register(repo)
    for name, (reason, removal_type) in (removed or {}).items():
        record = repositories.removed_repository(name)
        record.reason = reason
        record.removal_type = removal_type
    common = MarketplaceCommon()
    common.archived_repositories.update(archived or ())
    return SimpleNamespace(repositories=repositories, common=common)
