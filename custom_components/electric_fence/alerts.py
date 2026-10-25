"""Every Electric Fence notification: the bell and the owner's notify targets."""

import asyncio
from datetime import datetime, timedelta
import logging
from typing import Literal

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback
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


def _plain(message: str) -> str:
    """Return the bell text as phone text: no panel link, no markdown code."""
    return message.replace(PANEL_LINK, "").replace("`", "").strip()


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
        self._send(
            f"{DOMAIN}_ip_banned",
            "Electric Fence: IP banned",
            self._ban_text(view),
        )

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
        name = md_code(inline_text(view.username))
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
        if not push:
            return
        targets = [
            t
            for t in self._runtime.settings.notify_targets
            if self._hass.states.get(t) is not None
        ]
        if targets:
            self._hass.async_create_background_task(
                self._async_push(targets, title, _plain(message)),
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
