"""Auth failure collector: H2+H3 (token failures, `http-login` replace mode).

Core's `process_wrong_login` creates or updates the `http-login` notification
synchronously inside the failing request, so this listener sees the request
through `current_request`. It runs on the request path: memory only, at most
one queued record, everything inside `self.guard` (fail open). Login-flow
failures are recorded by the login collector (H1), so they are skipped here.

The dismiss of `http-login` (replace mode) is scheduled with `call_soon`, so
Core's own create call finishes first. `ip-ban` is never touched.
"""

from ipaddress import ip_address

from aiohttp import hdrs, web

from homeassistant.components import persistent_notification
from homeassistant.components.persistent_notification import Notification, UpdateType
from homeassistant.core import callback
from homeassistant.helpers.http import current_request
from homeassistant.helpers.network import is_cloud_connection
from homeassistant.util import dt as dt_util
from homeassistant.util.network import is_local

from ..models import LoginAttempt, LoginReason, LoginResult, Source, UserExists
from ..privacy import sanitise_text
from .base import BaseCollector

# Core's `homeassistant.components.http.ban.NOTIFICATION_ID_LOGIN`, checked by
# the `wrong_login_notification` contract.
NOTIFICATION_ID_LOGIN = "http-login"

# Login-flow failures belong to H1.
_LOGIN_FLOW_PATH = "/auth/login_flow"


class AuthFailureCollector(BaseCollector):
    """Record token failures and optionally dismiss Core's `http-login`."""

    name = "auth_failures"

    def _install(self) -> None:
        self._unsubs.append(
            persistent_notification.async_register_callback(self.hass, self._on_update)
        )

    def _uninstall(self) -> None:
        """Nothing beyond the subscription, which the base removes."""

    @callback
    def _on_update(
        self, update_type: UpdateType, notifications: dict[str, Notification]
    ) -> None:
        """Handle a persistent notification update (inside Core's request)."""
        self.guard.call(self._handle, update_type, notifications)

    def _handle(
        self, update_type: UpdateType, notifications: dict[str, Notification]
    ) -> None:
        if (
            update_type not in (UpdateType.ADDED, UpdateType.UPDATED)
            or NOTIFICATION_ID_LOGIN not in notifications
        ):
            return
        request = current_request.get()
        if request is None:
            return
        if not request.path.startswith(_LOGIN_FLOW_PATH):
            self.guard.call(self._record, request)
        # Read at event time, so a settings change applies live.
        settings = self.runtime.settings
        if settings.ha_login_notification == "replace" and settings.notify_failed_login:
            self.hass.loop.call_soon(
                self.guard.call,
                persistent_notification.async_dismiss,
                self.hass,
                NOTIFICATION_ID_LOGIN,
            )

    def _record(self, request: web.Request) -> None:
        """Queue an invalid-token attempt for the failing request."""
        remote = request.remote
        source: Source | None = None
        if is_cloud_connection(self.hass):
            source = Source.CLOUD
        elif remote is not None:
            try:
                ip = ip_address(remote)
            except ValueError:
                pass
            else:
                source = Source.LOCAL if is_local(ip) else Source.REMOTE
        self.runtime.worker.submit(
            LoginAttempt(
                ts=dt_util.utcnow(),
                result=LoginResult.FAILED,
                reason=LoginReason.INVALID_TOKEN,
                username_typed=None,
                user_exists=UserExists.UNKNOWN,
                user_id=None,
                provider=None,
                ip=remote,
                source=source,
                url=sanitise_text(request.path),
                user_agent=sanitise_text(request.headers.get(hdrs.USER_AGENT)),
                client_id=None,
            )
        )
