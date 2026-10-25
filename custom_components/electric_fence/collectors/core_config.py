"""Core config collector (H6): who changed the core settings.

Core fires `core_config_updated` with the saved keys and their new values
inside `async_update`. A snapshot of `hass.config.as_dict()`, taken at
install and refreshed after each event, gives the values before; both
sides come from `as_dict()`, so a unit system compares as its JSON form
(the event carries its name). The saved keys that changed are recorded,
with redacted values only when `record_values` is on.
"""

from typing import Any

from homeassistant.const import EVENT_CORE_CONFIG_UPDATE
from homeassistant.core import Event, callback
from homeassistant.util import dt as dt_util

from .. import attribution
from ..models import ChangeAction, ChangeKind, SettingsChange
from ..redact import diff_details
from .base import BaseCollector

TARGET_NAME = "Core settings"


class CoreConfigCollector(BaseCollector):
    """Record core config updates with strict attribution."""

    name = "core_config"

    _snapshot: dict[str, Any]

    def _install(self) -> None:
        self._snapshot = self._current({})
        self._unsubs.append(
            self.hass.bus.async_listen(EVENT_CORE_CONFIG_UPDATE, self._on_update)
        )

    def _uninstall(self) -> None:
        self._snapshot = {}

    def _current(self, event_data: dict[str, Any]) -> dict[str, Any]:
        """Return the settings now, plus event keys `as_dict()` lacks."""
        return {**event_data, **self.hass.config.as_dict()}

    @callback
    def _on_update(self, event: Event[dict[str, Any]]) -> None:
        """Handle a core config update (inside Core's call)."""
        self.guard.call(self._handle, event)

    def _handle(self, event: Event[dict[str, Any]]) -> None:
        if not event.data:
            # Core fires it without data at startup: nothing changed.
            return
        before = self._snapshot
        current = self._current(event.data)
        self._snapshot = current
        keys = [str(key) for key in event.data]
        details = diff_details(
            {key: before.get(key) for key in keys},
            {key: current.get(key) for key in keys},
            include_values=self.runtime.settings.record_values,
        )
        # `keys` is the old field, kept for older panel builds.
        details["keys"] = list(details["changed"])
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
                details=details,
                actor_user_id=actor.user_id if actor else None,
                actor_name=actor.user_name if actor else None,
                actor_ip=actor.ip if actor else None,
            )
        )
