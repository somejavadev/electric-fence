"""Runtime data for Electric Fence."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .collectors.base import BaseCollector
from .settings import Settings
from .storage import FenceStore
from .worker import Enricher, FenceWorker

if TYPE_CHECKING:
    from .alerts import Alerter
    from .collectors.apps_outside import OutsideAppWatcher
    from .forwarding.forwarder import Forwarder
    from .geo.base import GeoStatus
    from .geo.updater import GeoUpdater
    from .notifier import Notifier
    from .protection.bans import BanManager
    from .protection.policy import IpPolicy
    from .protection.throttle import UsernameGuard


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
    bans: BanManager | None = None
    ip_policy: IpPolicy | None = None
    username_guard: UsernameGuard | None = None
    alerts: Alerter | None = None
    forwarder: Forwarder | None = None
    app_watcher: OutsideAppWatcher | None = None
