"""Events and the persistent notification for login attempts and changes."""

import asyncio
from datetime import timedelta
import logging
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .attribution import auth_users
from .const import DOMAIN
from .failopen import FailOpen
from .models import (
    LoginAttempt,
    LoginReason,
    LoginResult,
    Record,
    SettingsChange,
    UserExists,
)
from .privacy import inline_text, mask_username, md_code
from .runtime import Runtime
from .storage import FenceStore

_LOGGER = logging.getLogger(__name__)

EVENT_LOGIN_FAILED = f"{DOMAIN}_login_failed"
EVENT_LOGIN_SUCCEEDED = f"{DOMAIN}_login_succeeded"
EVENT_SETTINGS_CHANGED = f"{DOMAIN}_settings_changed"
EVENT_IP_BANNED = f"{DOMAIN}_ip_banned"
NOTIFICATION_ID = f"{DOMAIN}_login_failed"
NOTIFICATION_TITLE = "Electric Fence: failed login"
PANEL_LINK = "[Open Electric Fence](/electric-fence)"

WINDOW = timedelta(hours=24)
TOKEN_WINDOW = timedelta(hours=1)
# How long a stop waits for the pending notification update.
STOP_TIMEOUT = 5.0

REASON_TEXT: dict[LoginReason, str] = {
    LoginReason.NO_SUCH_USER: "no such user",
    LoginReason.WRONG_PASSWORD: "wrong password",
    LoginReason.INVALID_CREDENTIALS: "invalid credentials",
    LoginReason.WRONG_2FA_CODE: "wrong two-factor code",
    LoginReason.INVALID_TOKEN: "invalid token",
    LoginReason.BLOCKED_INACTIVE: "blocked: inactive user",
    LoginReason.BLOCKED_LOCAL_ONLY: "blocked: local only",
}

# ISO 3166-1 alpha-2 code to English short name.
_COUNTRY_DATA = (
    "AD:Andorra|AE:United Arab Emirates|AF:Afghanistan|AG:Antigua and Barbuda|"
    "AL:Albania|AM:Armenia|AO:Angola|AR:Argentina|AT:Austria|AU:Australia|"
    "AZ:Azerbaijan|BA:Bosnia and Herzegovina|BB:Barbados|BD:Bangladesh|"
    "BE:Belgium|BF:Burkina Faso|BG:Bulgaria|BH:Bahrain|BI:Burundi|BJ:Benin|"
    "BN:Brunei|BO:Bolivia|BR:Brazil|BS:Bahamas|BT:Bhutan|BW:Botswana|"
    "BY:Belarus|BZ:Belize|CA:Canada|CD:DR Congo|CF:Central African Republic|"
    "CG:Republic of the Congo|CH:Switzerland|CI:Ivory Coast|CL:Chile|"
    "CM:Cameroon|CN:China|CO:Colombia|CR:Costa Rica|CU:Cuba|CV:Cape Verde|"
    "CY:Cyprus|CZ:Czechia|DE:Germany|DJ:Djibouti|DK:Denmark|DM:Dominica|"
    "DO:Dominican Republic|DZ:Algeria|EC:Ecuador|EE:Estonia|EG:Egypt|"
    "ER:Eritrea|ES:Spain|ET:Ethiopia|FI:Finland|FJ:Fiji|FR:France|GA:Gabon|"
    "GB:United Kingdom|GD:Grenada|GE:Georgia|GH:Ghana|GM:Gambia|GN:Guinea|"
    "GQ:Equatorial Guinea|GR:Greece|GT:Guatemala|GW:Guinea-Bissau|GY:Guyana|"
    "HK:Hong Kong|HN:Honduras|HR:Croatia|HT:Haiti|HU:Hungary|ID:Indonesia|"
    "IE:Ireland|IL:Israel|IN:India|IQ:Iraq|IR:Iran|IS:Iceland|IT:Italy|"
    "JM:Jamaica|JO:Jordan|JP:Japan|KE:Kenya|KG:Kyrgyzstan|KH:Cambodia|"
    "KM:Comoros|KN:Saint Kitts and Nevis|KP:North Korea|KR:South Korea|"
    "KW:Kuwait|KZ:Kazakhstan|LA:Laos|LB:Lebanon|LC:Saint Lucia|"
    "LI:Liechtenstein|LK:Sri Lanka|LR:Liberia|LS:Lesotho|LT:Lithuania|"
    "LU:Luxembourg|LV:Latvia|LY:Libya|MA:Morocco|MC:Monaco|MD:Moldova|"
    "ME:Montenegro|MG:Madagascar|MK:North Macedonia|ML:Mali|MM:Myanmar|"
    "MN:Mongolia|MO:Macao|MR:Mauritania|MT:Malta|MU:Mauritius|MV:Maldives|"
    "MW:Malawi|MX:Mexico|MY:Malaysia|MZ:Mozambique|NA:Namibia|NE:Niger|"
    "NG:Nigeria|NI:Nicaragua|NL:Netherlands|NO:Norway|NP:Nepal|NZ:New Zealand|"
    "OM:Oman|PA:Panama|PE:Peru|PG:Papua New Guinea|PH:Philippines|"
    "PK:Pakistan|PL:Poland|PS:Palestine|PT:Portugal|PY:Paraguay|QA:Qatar|"
    "RO:Romania|RS:Serbia|RU:Russia|RW:Rwanda|SA:Saudi Arabia|"
    "SB:Solomon Islands|SC:Seychelles|SD:Sudan|SE:Sweden|SG:Singapore|"
    "SI:Slovenia|SK:Slovakia|SL:Sierra Leone|SM:San Marino|SN:Senegal|"
    "SO:Somalia|SR:Suriname|SS:South Sudan|ST:Sao Tome and Principe|"
    "SV:El Salvador|SY:Syria|SZ:Eswatini|TD:Chad|TG:Togo|TH:Thailand|"
    "TJ:Tajikistan|TL:Timor-Leste|TM:Turkmenistan|TN:Tunisia|TO:Tonga|"
    "TR:Turkey|TT:Trinidad and Tobago|TW:Taiwan|TZ:Tanzania|UA:Ukraine|"
    "UG:Uganda|US:United States|UY:Uruguay|UZ:Uzbekistan|VA:Vatican City|"
    "VC:Saint Vincent and the Grenadines|VE:Venezuela|VN:Vietnam|VU:Vanuatu|"
    "WS:Samoa|XK:Kosovo|YE:Yemen|ZA:South Africa|ZM:Zambia|ZW:Zimbabwe"
)
COUNTRY_NAMES: dict[str, str] = dict(
    item.split(":", 1) for item in _COUNTRY_DATA.split("|")
)


def country_text(code: str | None) -> str | None:
    """Return the English country name, or the code itself when unknown."""
    if not code:
        return None
    clean = inline_text(code)
    return COUNTRY_NAMES.get(clean.upper(), clean)


def _changed(details: dict[str, Any]) -> list[str]:
    """Return the changed setting names (a list of text), else empty."""
    names = details.get("changed")
    return [str(n) for n in names] if isinstance(names, list) else []


def _str(value: Any) -> str | None:
    return None if value is None else str(value)


def _exists(value: UserExists) -> bool | None:
    """Return the event form of `user_exists`: None when unknown."""
    if value is UserExists.YES:
        return True
    if value is UserExists.NO:
        return False
    return None


def display_username(record: LoginAttempt) -> tuple[str | None, bool]:
    """Return (display name, masked) of the typed username.

    The only place a typed username is turned into something shown outside the
    panel: existing users in full, everything else masked.
    """
    if record.username_typed is None:
        return None, False
    return mask_username(inline_text(record.username_typed), record.user_exists)


def _counts(store: FenceStore, token_ip: str | None) -> tuple[int, int | None]:
    """Return (failed and blocked in 24 hours, token failures in 1 hour)."""
    now = dt_util.utcnow()
    total = store.count_failed_since(now - WINDOW, include_blocked=True)
    tokens = (
        store.count_token_failures_since(token_ip, now - TOKEN_WINDOW)
        if token_ip is not None
        else None
    )
    return total, tokens


class Notifier:
    """Fires the events and keeps the persistent notification current."""

    def __init__(self, hass: HomeAssistant, runtime: Runtime) -> None:
        """Initialise the notifier."""
        self._hass = hass
        self._runtime = runtime
        self._event_guard = FailOpen("notifier events")
        self._notify_guard = FailOpen("notifier notification")
        self._latest: LoginAttempt | None = None
        self._task: asyncio.Task[None] | None = None
        self._last_count: int | None = None

    @callback
    def on_record(self, record: Record) -> None:
        """Handle a stored record (a worker listener)."""
        self._event_guard.call(self._fire, record)
        if (
            isinstance(record, LoginAttempt)
            and record.result is not LoginResult.SUCCESS
            and self._runtime.settings.notify_failed_login
        ):
            self._latest = record
            if self._task is None:
                # Not eager: the task must not finish (and reset `_task`)
                # before it is stored here.
                self._task = self._hass.async_create_task(
                    self._async_run(), "electric_fence notification", eager_start=False
                )

    async def async_stop(self) -> None:
        """Let a pending notification update finish (bounded), then stop."""
        task = self._task
        if task is not None:
            await asyncio.wait({task}, timeout=STOP_TIMEOUT)
            task.cancel()
        self._latest = None
        self._task = None

    def _fire(self, record: Record) -> None:
        if isinstance(record, SettingsChange):
            self._hass.bus.async_fire(
                EVENT_SETTINGS_CHANGED,
                {
                    "kind": str(record.kind),
                    "action": str(record.action),
                    "target": record.target_id,
                    "target_name": record.target_name,
                    "changed_by_user_id": record.actor_user_id,
                    "changed_by_name": record.actor_name,
                    "ip": record.actor_ip,
                    # Names only: never `values`.
                    "changed": _changed(record.details),
                },
            )
        elif record.result is LoginResult.SUCCESS:
            self._fire_success(record)
        else:
            self._fire_failed(record)

    def _fire_success(self, record: LoginAttempt) -> None:
        # An unconfirmed success never got its token: not a login yet.
        if not record.confirmed:
            return
        # The real name from the auth store in memory, never the typed text.
        # `hass.auth.async_get_user` is a coroutine, which a sync listener
        # can't await; Task 9's in-memory lookup is the same data.
        users = auth_users(self._hass)
        user = users.get(record.user_id) if users and record.user_id else None
        self._hass.bus.async_fire(
            EVENT_LOGIN_SUCCEEDED,
            {
                "user_id": record.user_id,
                "username": inline_text(user.name)
                if user is not None and user.name
                else None,
                "ip": record.ip,
                "country": record.country_code,
                "source": _str(record.source),
                "client": record.client_name or record.client_id,
                "mfa_used": record.mfa_module is not None,
            },
        )

    def _fire_failed(self, record: LoginAttempt) -> None:
        username, masked = display_username(record)
        exists = record.user_exists
        self._hass.bus.async_fire(
            EVENT_LOGIN_FAILED,
            {
                "username": username,
                "username_masked": masked,
                "user_exists": _exists(exists),
                "user_id": record.user_id if exists is UserExists.YES else None,
                "reason": str(record.reason),
                "ip": record.ip,
                "country": record.country_code,
                "hostname": record.hostname,
                "source": _str(record.source),
                "client": record.client_name or record.client_id,
                "count": record.count,
            },
        )

    async def _async_run(self) -> None:
        """Update the notification for the newest record, until none is left."""
        try:
            while (record := self._latest) is not None:
                self._latest = None
                await self._async_update(record)
        finally:
            self._task = None

    async def _async_update(self, record: LoginAttempt) -> None:
        if not self._runtime.settings.notify_failed_login:
            return
        token_ip = (
            record.ip
            if record.reason is LoginReason.INVALID_TOKEN and record.ip
            else None
        )
        total: int | None = self._last_count
        tokens: int | None = None
        try:
            total, tokens = await self._hass.async_add_executor_job(
                _counts, self._runtime.store, token_ip
            )
            self._last_count = total
        except Exception as err:  # noqa: BLE001
            # Keep the last known count; the notification is still worth it.
            _LOGGER.debug("Electric Fence count failed: %s", type(err).__name__)
        message = self._notify_guard.call(build_message, record, total, tokens)
        if message is None:
            return
        persistent_notification.async_create(
            self._hass, message, NOTIFICATION_TITLE, NOTIFICATION_ID
        )


def build_message(record: LoginAttempt, total: int | None, tokens: int | None) -> str:
    """Return the notification text for the newest attempt."""
    ip = md_code(inline_text(record.ip)) if record.ip else "an unknown address"
    parts: list[str] = []
    if record.reason is LoginReason.INVALID_TOKEN:
        parts.append(f"{tokens or record.count} invalid token attempts from {ip}.")
    else:
        username, _ = display_username(record)
        who = md_code(username) if username is not None else "an unknown user"
        reason = REASON_TEXT.get(record.reason, str(record.reason))
        where = f"from {ip}"
        if country := country_text(record.country_code):
            where += f", {country}"
        if record.hostname:
            where += f" ({md_code(inline_text(record.hostname))})"
        parts.append(f"Failed login for {who} ({reason}) {where}.")
    if total is not None:
        parts.append(f"{total} failed logins in the last 24 hours.")
    parts.append(PANEL_LINK)
    return " ".join(parts)
