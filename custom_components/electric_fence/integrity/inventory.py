"""List installed custom integrations and where each came from."""

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import logging
from pathlib import Path
from typing import Any, Literal

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RemovedInfo:
    """Why the Marketplace catalog removed a repository."""

    reason: str | None
    removal_type: str | None


@dataclass(frozen=True)
class InstalledIntegration:
    """A custom integration folder and its Marketplace record, if any."""

    domain: str
    path: Path
    version: str | None
    source: Literal["marketplace", "manual"]
    repository: str | None = None
    installed_version: str | None = None
    installed_commit: str | None = None
    zip_release: bool = False
    zip_filename: str | None = None
    content_in_root: bool = False
    persistent_directory: str | None = None
    archived: bool = False
    removed: RemovedInfo | None = None
    last_updated: datetime | None = None
    symlinked: bool = False


def _parse_last_updated(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _manifest_version(folder: Path) -> str | None:
    try:
        version = json.loads((folder / "manifest.json").read_text("utf-8")).get(
            "version"
        )
    except OSError, ValueError, AttributeError:
        return None
    return version if isinstance(version, str) else None


def _scan(root: Path) -> list[tuple[str, Path, str | None, bool]]:
    try:
        folders = sorted(root.iterdir())
    except OSError:
        return []
    found: list[tuple[str, Path, str | None, bool]] = []
    for folder in folders:
        if folder.is_symlink():
            found.append((folder.name, folder, None, True))
        elif folder.is_dir() and (folder / "manifest.json").is_file():
            found.append((folder.name, folder, _manifest_version(folder), False))
    return found


def _marketplace_repositories(hass: HomeAssistant) -> tuple[Any, dict[str, Any]]:
    """Return the manager and its installed integration repositories by domain."""
    for entry in hass.config_entries.async_loaded_entries("marketplace"):
        manager = entry.runtime_data
        try:
            repos = {
                r.data.domain: r
                for r in manager.repositories.list_all
                if r.data.installed
                and r.data.category == "integration"
                and r.data.domain
            }
        except Exception:
            _LOGGER.warning("Marketplace shape not understood", exc_info=True)
            return None, {}
        return manager, repos
    return None, {}


def _marketplace_fields(manager: Any, repo: Any) -> dict[str, Any]:
    data = repo.data
    manifest = repo.repository_manifest
    repositories = manager.repositories
    removed = None
    if repositories.is_removed(data.full_name):
        info = repositories.removed_repository(data.full_name)
        removed = RemovedInfo(info.reason, info.removal_type)
    archived = bool(data.archived) or data.full_name in (
        manager.common.archived_repositories
    )
    return {
        "source": "marketplace",
        "repository": data.full_name,
        "installed_version": data.installed_version,
        "installed_commit": data.installed_commit,
        "zip_release": bool(manifest.zip_release),
        "zip_filename": manifest.filename,
        "content_in_root": bool(manifest.content_in_root),
        "persistent_directory": manifest.persistent_directory,
        "archived": archived,
        "removed": removed,
        "last_updated": _parse_last_updated(data.last_updated),
    }


async def async_inventory(hass: HomeAssistant) -> list[InstalledIntegration]:
    """List custom integrations, matched to the Marketplace when it is loaded."""
    root = Path(hass.config.path("custom_components"))
    folders = await hass.async_add_executor_job(_scan, root)
    manager, repos = _marketplace_repositories(hass)
    result: list[InstalledIntegration] = []
    for domain, path, version, symlinked in folders:
        fields: dict[str, Any] = {"source": "manual"}
        if manager is not None and (repo := repos.get(domain)) is not None:
            try:
                fields = _marketplace_fields(manager, repo)
            except Exception:
                _LOGGER.warning(
                    "Marketplace record of %s unreadable", domain, exc_info=True
                )
        result.append(
            InstalledIntegration(domain, path, version, symlinked=symlinked, **fields)
        )
    return result
