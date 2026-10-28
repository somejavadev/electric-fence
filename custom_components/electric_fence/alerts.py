"""Every Electric Fence notification: the bell and the owner's notify targets."""

import asyncio
from collections import deque
from collections.abc import Callable
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
from .settings import Settings

_LOGGER = logging.getLogger(__name__)

PUSH_INTERVAL = timedelta(minutes=10)
# Over all kinds together: a flood must not page the owner all night, or run
# up the bill of a paid notify service. The bell still shows everything.
PUSH_HOURLY_LIMIT = 6
PUSH_LIMIT_WINDOW = timedelta(hours=1)
BAN_TITLE = "Electric Fence: IP banned"
BAN_ENDED_TITLE = "Electric Fence: ban ended"
ATTACK_TITLE = "Electric Fence: username under attack"
NEW_COUNTRY_TITLE = "Electric Fence: new country login"
HELD_TITLE = "Electric Fence"


def _plain(message: str) -> str:
    """Return the bell text as phone text: no panel link, no markdown code."""
    return message.replace(PANEL_LINK, "").replace("`", "").strip()


def _since(count: int, one: str, many: str) -> str:
    """Return e.g. "3 bans ended since the last message."."""
    text = one if count == 1 else many
    return f"{count} {text} since the last message."


def _banned_text(count: int) -> str:
    return _since(count, "address banned", "addresses banned")


def _ended_text(count: int) -> str:
    return _since(count, "ban ended", "bans ended")


def _attack_text(count: int) -> str:
    return _since(count, "username came under attack", "usernames came under attack")


def _new_country_text(count: int) -> str:
    return _since(count, "new-country login", "new-country logins")


def _held_text(count: int) -> str:
    noun = "notification was" if count == 1 else "notifications were"
    return (
        f"{count} more {noun} not sent to your phone, to keep it quiet during "
        "a flood. Open Home Assistant to see them."
    )


def _when(value: datetime) -> str:
    return dt_util.as_local(value).strftime("%Y-%m-%d %H:%M")


class _Batch:
    """Pushes of one kind: at most one per PUSH_INTERVAL, the rest summed up.

    The first event of a quiet period is pushed as it is. Events within the
    interval after it are counted and pushed as one summary when it ends, or
    folded into the next event pushed after it.
    """

    def __init__(
        self,
        alerter: Alerter,
        title: str,
        summary: Callable[[int], str],
        enabled: Callable[[Settings], bool],
    ) -> None:
        self._alerter = alerter
        self._title = title
        self._summary = summary
        self._enabled = enabled
        self._last: datetime | None = None
        self._waiting = 0
        self._unsub: CALLBACK_TYPE | None = None

    def stop(self) -> None:
        """Cancel a pending summary."""
        self._cancel()
        self._waiting = 0

    def _cancel(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    def offer(self, plain: str) -> None:
        """Push this event now, or count it for the summary."""
        now = dt_util.utcnow()
        last = self._last
        if last is None or now - last >= PUSH_INTERVAL:
            self._last = now
            # Events still waiting for the summary go along with this one.
            waiting, self._waiting = self._waiting, 0
            self._cancel()
            if waiting:
                plain = f"{self._summary(waiting + 1)} Latest: {plain}"
            self._alerter.push(self._title, plain)
            return
        self._waiting += 1
        if self._unsub is None:
            self._unsub = async_call_later(
                self._alerter.hass,
                last + PUSH_INTERVAL - now,
                HassJob(
                    self._on_due,
                    "electric_fence push summary",
                    cancel_on_shutdown=True,
                ),
            )

    @callback
    def _on_due(self, _now: datetime) -> None:
        self._unsub = None
        self._alerter.guard.call(self._push_summary)

    def _push_summary(self) -> None:
        count, self._waiting = self._waiting, 0
        if not count or not self._enabled(self._alerter.settings):
            return
        self._last = dt_util.utcnow()
        self._alerter.push(self._title, self._summary(count))


class Alerter:
    """Sends the notifications, each kind behind its own switch."""

    def __init__(self, hass: HomeAssistant, runtime: Runtime) -> None:
        """Initialise the alerter."""
        self.hass = hass
        self._runtime = runtime
        self.guard = FailOpen("alerts")
        self._last_push: datetime | None = None
        # Each kind is limited on its own; the bell still shows every event.
        self._bans = _Batch(self, BAN_TITLE, _banned_text, _ip_banned_on)
        self._ended = _Batch(self, BAN_ENDED_TITLE, _ended_text, _ban_ended_on)
        self._attacks = _Batch(self, ATTACK_TITLE, _attack_text, _attack_on)
        self._countries = _Batch(
            self, NEW_COUNTRY_TITLE, _new_country_text, _new_country_on
        )
        # When recent pushes went out, for the hourly limit over all kinds.
        self._pushed: deque[datetime] = deque()
        self._held = 0
        self._unsub_held: CALLBACK_TYPE | None = None

    @property
    def settings(self) -> Settings:
        """Return the current settings."""
        return self._runtime.settings

    @callback
    def async_stop(self) -> None:
        """Cancel pending summaries."""
        for batch in (self._bans, self._ended, self._attacks, self._countries):
            batch.stop()
        if self._unsub_held is not None:
            self._unsub_held()
            self._unsub_held = None
        self._held = 0

    @callback
    def failed_login(self, message: str) -> None:
        """Update the failed-login notification; push at most every 10 minutes."""
        self.guard.call(self._failed_login, message)

    @callback
    def ip_banned(self, view: BanView) -> None:
        """Announce a new ban."""
        self.guard.call(self._ip_banned, view)

    @callback
    def ban_ended(self, view: BanView, how: Literal["expired", "unbanned"]) -> None:
        """Announce the end of a ban."""
        self.guard.call(self._ban_ended, view, how)

    @callback
    def username_attack(self, view: AttackView) -> None:
        """Announce an attack on a username."""
        self.guard.call(self._username_attack, view)

    @callback
    def new_country(self, record: LoginAttempt) -> None:
        """Announce a login from a country not seen before."""
        self.guard.call(self._new_country, record)

    def _failed_login(self, message: str) -> None:
        if not self.settings.notify_failed_login:
            return
        now = dt_util.utcnow()
        push = self._last_push is None or now - self._last_push >= PUSH_INTERVAL
        if push:
            self._last_push = now
        self._send(NOTIFICATION_ID, NOTIFICATION_TITLE, message, push=push)

    def _ip_banned(self, view: BanView) -> None:
        if not self.settings.notify_ip_banned:
            return
        message = self._ban_text(view)
        self._send(f"{DOMAIN}_ip_banned", BAN_TITLE, message, push=False)
        self._bans.offer(_plain(message))

    def _ban_ended(self, view: BanView, how: Literal["expired", "unbanned"]) -> None:
        if not self.settings.notify_ban_ended:
            return
        message = f"The ban on {self._who(view)} {how}."
        self._send(f"{DOMAIN}_ban_ended", BAN_ENDED_TITLE, message, push=False)
        self._ended.offer(_plain(message))

    def _username_attack(self, view: AttackView) -> None:
        if not self.settings.notify_username_attack:
            return
        countries = ", ".join(inline_text(c) for c in view.countries)
        where = f" ({countries})" if countries else ""
        name = md_code(view.display()[0])
        message = (
            f"Username {name} is under attack: {view.failures} failed logins "
            f"from {view.ip_count} addresses{where}. "
            "Logins for it are slowed down."
        )
        self._send(f"{DOMAIN}_username_attack", ATTACK_TITLE, message, push=False)
        self._attacks.offer(_plain(message))

    def _new_country(self, record: LoginAttempt) -> None:
        if not self.settings.notify_new_country:
            return
        users = auth_users(self.hass)
        user = users.get(record.user_id) if users and record.user_id else None
        name = md_code(inline_text(user.name)) if user and user.name else "A user"
        country = country_text(record.country_code) or "a new country"
        ip = f" ({md_code(inline_text(record.ip))})" if record.ip else ""
        message = f"{name} logged in from {country} for the first time{ip}."
        self._send(f"{DOMAIN}_new_country", NEW_COUNTRY_TITLE, message, push=False)
        self._countries.offer(_plain(message))

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
        persistent_notification.async_create(self.hass, message, title, notification_id)
        if push:
            self.push(title, _plain(message))

    def push(self, title: str, plain: str) -> None:
        """Push to the notify targets, within the hourly limit."""
        targets = [
            t
            for t in self.settings.notify_targets
            if self.hass.states.get(t) is not None
        ]
        if not targets:
            return
        now = dt_util.utcnow()
        while self._pushed and now - self._pushed[0] >= PUSH_LIMIT_WINDOW:
            self._pushed.popleft()
        if len(self._pushed) >= PUSH_HOURLY_LIMIT:
            self._held += 1
            if self._unsub_held is None:
                self._unsub_held = async_call_later(
                    self.hass,
                    self._pushed[0] + PUSH_LIMIT_WINDOW - now,
                    HassJob(
                        self._on_held_due,
                        "electric_fence held pushes",
                        cancel_on_shutdown=True,
                    ),
                )
            return
        self._pushed.append(now)
        self.hass.async_create_background_task(
            self._async_push(targets, title, plain),
            "electric_fence push",
        )

    @callback
    def _on_held_due(self, _now: datetime) -> None:
        self._unsub_held = None
        count, self._held = self._held, 0
        if count:
            self.guard.call(self.push, HELD_TITLE, _held_text(count))

    async def _async_push(self, targets: list[str], title: str, plain: str) -> None:
        for target in targets:
            try:
                await self.hass.services.async_call(
                    "notify",
                    "send_message",
                    {"entity_id": target, "title": title, "message": plain},
                    blocking=False,
                )
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Electric Fence push failed: %s", type(err).__name__)


def _ip_banned_on(settings: Settings) -> bool:
    return settings.notify_ip_banned


def _ban_ended_on(settings: Settings) -> bool:
    return settings.notify_ban_ended


def _attack_on(settings: Settings) -> bool:
    return settings.notify_username_attack


def _new_country_on(settings: Settings) -> bool:
    return settings.notify_new_country
