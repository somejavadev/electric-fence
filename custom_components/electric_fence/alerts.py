"""Every Electric Fence notification: the bell and the owner's notify targets."""

import asyncio
from datetime import datetime, timedelta
import logging
from typing import Literal

from homeassistant.components import persistent_notification
from homeassistant.core import CALLBACK_TYPE, HassJob, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .attribution import auth_users
from .const import DOMAIN
from .failopen import FailOpen
from .models import LoginAttempt
from .notifier import NOTIFICATION_ID, NOTIFICATION_TITLE, PANEL_LINK, country_text
from .privacy import inline_text, md_code
from .protection.bans import BanView
from .protection.throttle import AttackView
from .runtime import Runtime

_LOGGER = logging.getLogger(__name__)

PUSH_INTERVAL = timedelta(minutes=10)
BAN_TITLE = "Electric Fence: IP banned"


def _plain(message: str) -> str:
    """Return the bell text as phone text: no panel link, no markdown code."""
    return message.replace(PANEL_LINK, "").replace("`", "").strip()


def _banned_text(count: int) -> str:
    noun = "address" if count == 1 else "addresses"
    return f"{count} {noun} banned since the last message."


def _when(value: datetime) -> str:
    return dt_util.as_local(value).strftime("%Y-%m-%d %H:%M")


class Alerter:
    """Sends the notifications, each kind behind its own switch."""

    def __init__(self, hass: HomeAssistant, runtime: Runtime) -> None:
        """Initialise the alerter."""
        self._hass = hass
        self._runtime = runtime
        self._guard = FailOpen("alerts")
        self._last_push: datetime | None = None
        # Ban pushes are limited like failed logins; the bell shows every ban.
        self._last_ban_push: datetime | None = None
        self._bans_unpushed = 0
        self._unsub_ban_push: CALLBACK_TYPE | None = None

    @callback
    def async_stop(self) -> None:
        """Cancel a pending ban summary."""
        self._cancel_ban_push()
        self._bans_unpushed = 0

    def _cancel_ban_push(self) -> None:
        if self._unsub_ban_push is not None:
            self._unsub_ban_push()
            self._unsub_ban_push = None

    @callback
    def failed_login(self, message: str) -> None:
        """Update the failed-login notification; push at most every 10 minutes."""
        self._guard.call(self._failed_login, message)

    @callback
    def ip_banned(self, view: BanView) -> None:
        """Announce a new ban."""
        self._guard.call(self._ip_banned, view)

    @callback
    def ban_ended(self, view: BanView, how: Literal["expired", "unbanned"]) -> None:
        """Announce the end of a ban."""
        self._guard.call(self._ban_ended, view, how)

    @callback
    def username_attack(self, view: AttackView) -> None:
        """Announce an attack on a username."""
        self._guard.call(self._username_attack, view)

    @callback
    def new_country(self, record: LoginAttempt) -> None:
        """Announce a login from a country not seen before."""
        self._guard.call(self._new_country, record)

    def _failed_login(self, message: str) -> None:
        if not self._runtime.settings.notify_failed_login:
            return
        now = dt_util.utcnow()
        push = self._last_push is None or now - self._last_push >= PUSH_INTERVAL
        if push:
            self._last_push = now
        self._send(NOTIFICATION_ID, NOTIFICATION_TITLE, message, push=push)

    def _ip_banned(self, view: BanView) -> None:
        if not self._runtime.settings.notify_ip_banned:
            return
        now = dt_util.utcnow()
        last = self._last_ban_push
        message = self._ban_text(view)
        self._send(f"{DOMAIN}_ip_banned", BAN_TITLE, message, push=False)
        if last is None or now - last >= PUSH_INTERVAL:
            self._last_ban_push = now
            # Bans still waiting for the summary go along with this one.
            waiting, self._bans_unpushed = self._bans_unpushed, 0
            self._cancel_ban_push()
            plain = _plain(message)
            if waiting:
                plain = f"{_banned_text(waiting + 1)} Latest: {plain}"
            self._push(BAN_TITLE, plain)
            return
        self._bans_unpushed += 1
        if self._unsub_ban_push is None:
            self._unsub_ban_push = async_call_later(
                self._hass,
                last + PUSH_INTERVAL - now,
                HassJob(
                    self._on_ban_push_due,
                    "electric_fence ban push",
                    cancel_on_shutdown=True,
                ),
            )

    @callback
    def _on_ban_push_due(self, _now: datetime) -> None:
        self._unsub_ban_push = None
        self._guard.call(self._push_ban_summary)

    def _push_ban_summary(self) -> None:
        count, self._bans_unpushed = self._bans_unpushed, 0
        if not count or not self._runtime.settings.notify_ip_banned:
            return
        self._last_ban_push = dt_util.utcnow()
        self._push(BAN_TITLE, _banned_text(count))

    def _ban_ended(self, view: BanView, how: Literal["expired", "unbanned"]) -> None:
        if not self._runtime.settings.notify_ban_ended:
            return
        self._send(
            f"{DOMAIN}_ban_ended",
            "Electric Fence: ban ended",
            f"The ban on {self._who(view)} {how}.",
        )

    def _username_attack(self, view: AttackView) -> None:
        if not self._runtime.settings.notify_username_attack:
            return
        countries = ", ".join(inline_text(c) for c in view.countries)
        where = f" ({countries})" if countries else ""
        name = md_code(view.display()[0])
        self._send(
            f"{DOMAIN}_username_attack",
            "Electric Fence: username under attack",
            f"Username {name} is under attack: {view.failures} failed logins "
            f"from {view.ip_count} addresses{where}. "
            "Logins for it are slowed down.",
        )

    def _new_country(self, record: LoginAttempt) -> None:
        if not self._runtime.settings.notify_new_country:
            return
        users = auth_users(self._hass)
        user = users.get(record.user_id) if users and record.user_id else None
        name = md_code(inline_text(user.name)) if user and user.name else "A user"
        country = country_text(record.country_code) or "a new country"
        ip = f" ({md_code(inline_text(record.ip))})" if record.ip else ""
        self._send(
            f"{DOMAIN}_new_country",
            "Electric Fence: new country login",
            f"{name} logged in from {country} for the first time{ip}.",
        )

    def _who(self, view: BanView) -> str:
        who = md_code(inline_text(str(view.ip)))
        if country := country_text(view.country_code):
            who += f", {country}"
        if view.hostname:
            who += f" ({md_code(inline_text(view.hostname))})"
        return who

    def _ban_text(self, view: BanView) -> str:
        after = f", after {view.failures} failed logins" if view.failures else ""
        expires = (
            "Permanent"
            if view.expires_at is None
            else (f"Expires {_when(view.expires_at)}")
        )
        return f"Banned {self._who(view)}{after}. {expires}."

    def _send(
        self, notification_id: str, title: str, message: str, *, push: bool = True
    ) -> None:
        persistent_notification.async_create(
            self._hass, message, title, notification_id
        )
        if push:
            self._push(title, _plain(message))

    def _push(self, title: str, plain: str) -> None:
        targets = [
            t
            for t in self._runtime.settings.notify_targets
            if self._hass.states.get(t) is not None
        ]
        if targets:
            self._hass.async_create_background_task(
                self._async_push(targets, title, plain),
                "electric_fence push",
            )

    async def _async_push(self, targets: list[str], title: str, plain: str) -> None:
        for target in targets:
            try:
                await self._hass.services.async_call(
                    "notify",
                    "send_message",
                    {"entity_id": target, "title": title, "message": plain},
                    blocking=False,
                )
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Electric Fence push failed: %s", type(err).__name__)
