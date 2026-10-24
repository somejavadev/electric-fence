"""Auth failure collector: H2+H3 (token failures, `http-login` replace mode).

Core's `process_wrong_login` creates or updates the `http-login` notification
synchronously inside the failing request, so this listener sees the request
through `current_request`. It runs on the request path: memory only, at most
one queued record, everything inside `self.guard` (fail open).

Login-flow failures are recorded by the login collector (H1), which marks the
request (`REQUEST_RECORDED`). A login-flow request Core counts without that
mark (a 4xx before any form, such as a bad client ID) is recorded here as
`invalid_credentials` with no username. Token failures are recorded here.

Replace mode dismisses `http-login` only when Electric Fence will notify about
this same failure (spec §5.2: never left without a notification): it was
recorded (by H1 with a notifying mark, or here), the login collector is
installed for login-flow paths, and storage had no error in the last five
minutes. The dismiss is scheduled with `call_soon`, so Core's own create call
finishes first. `ip-ban` is never touched.
"""

from collections.abc import Mapping
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
from .login import REQUEST_RECORDED, LoginCollector

# Core's `homeassistant.components.http.ban.NOTIFICATION_ID_LOGIN`, checked by
# the `wrong_login_notification` contract.
NOTIFICATION_ID_LOGIN = "http-login"

# Login-flow failures belong to H1, unless it did not see the request.
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
        if request.path.startswith(_LOGIN_FLOW_PATH):
            mark = (
                request.get(REQUEST_RECORDED) if isinstance(request, Mapping) else None
            )
            if mark is None:
                recorded = self.guard.call(
                    self._record, request, LoginReason.INVALID_CREDENTIALS
                )
            else:
                recorded = mark is True
            recorded = recorded and self._login_installed()
        else:
            recorded = self.guard.call(self._record, request, LoginReason.INVALID_TOKEN)
        # Read at event time, so a settings change applies live.
        settings = self.runtime.settings
        if (
            recorded
            and settings.ha_login_notification == "replace"
            and settings.notify_failed_login
            and self.runtime.worker.storage_healthy
        ):
            self.hass.loop.call_soon(
                self.guard.call,
                persistent_notification.async_dismiss,
                self.hass,
                NOTIFICATION_ID_LOGIN,
            )

    def _login_installed(self) -> bool:
        return any(
            isinstance(c, LoginCollector) and c.is_installed
            for c in self.runtime.collectors
        )

    def _record(self, request: web.Request, reason: LoginReason) -> bool:
        """Queue a failed attempt for the failing request; True if queued."""
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
        return self.runtime.worker.submit(
            LoginAttempt(
                ts=dt_util.utcnow(),
                result=LoginResult.FAILED,
                reason=reason,
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
