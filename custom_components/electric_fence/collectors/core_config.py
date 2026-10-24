"""Core config collector (H6): who changed the core settings.

Core fires `core_config_updated` with the changed keys and their new values
inside `async_update`. Only the keys are recorded, never the values.
"""

from typing import Any

from homeassistant.const import EVENT_CORE_CONFIG_UPDATE
from homeassistant.core import Event, callback
from homeassistant.util import dt as dt_util

from .. import attribution
from ..models import ChangeAction, ChangeKind, SettingsChange
from .base import BaseCollector

TARGET_NAME = "Core settings"


class CoreConfigCollector(BaseCollector):
    """Record core config updates with strict attribution."""

    name = "core_config"

    def _install(self) -> None:
        self._unsubs.append(
            self.hass.bus.async_listen(EVENT_CORE_CONFIG_UPDATE, self._on_update)
        )

    def _uninstall(self) -> None:
        """Nothing beyond the subscription, which the base removes."""

    @callback
    def _on_update(self, event: Event[dict[str, Any]]) -> None:
        """Handle a core config update (inside Core's call)."""
        self.guard.call(self._handle, event)

    def _handle(self, event: Event[dict[str, Any]]) -> None:
        if not event.data:
            # Core fires it without data at startup: nothing changed.
            return
        actor = attribution.credited_actor(
            ChangeKind.CORE_CONFIG, ChangeAction.UPDATED, None
        )
        self.runtime.worker.submit(
            SettingsChange(
                ts=dt_util.utcnow(),
                kind=ChangeKind.CORE_CONFIG,
                action=ChangeAction.UPDATED,
                target_id=None,
                target_name=TARGET_NAME,
                details={"keys": sorted(str(key) for key in event.data)},
                actor_user_id=actor.user_id if actor else None,
                actor_name=actor.user_name if actor else None,
                actor_ip=actor.ip if actor else None,
            )
        )
