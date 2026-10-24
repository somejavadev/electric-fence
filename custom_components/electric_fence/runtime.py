"""Runtime data for Electric Fence."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .collectors.base import BaseCollector
from .settings import Settings
from .storage import FenceStore
from .worker import Enricher, FenceWorker

if TYPE_CHECKING:
    # Module added by Task 12 (notifier); that task drops the ignore.
    from .geo.base import GeoStatus
    from .geo.updater import GeoUpdater
    from .notifier import Notifier  # type: ignore[import-not-found]


@dataclass
class Runtime:
    """Per-entry runtime state."""

    settings: Settings
    store: FenceStore
    worker: FenceWorker
    enricher: Enricher
    collectors: list[BaseCollector]
    ha_version: str
    notifier: Notifier | None = None
    geo_status: GeoStatus | None = None
    geo_updater: GeoUpdater | None = None
