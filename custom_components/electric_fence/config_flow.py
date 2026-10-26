"""Config flow for Electric Fence."""

from typing import Any

from aiohttp import ClientError
import probatio

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import DOMAIN
from .geo.base import GeoDownloadError
from .geo.maxmind import MaxMindProvider
from .settings import Settings

DBIP_URL = "https://db-ip.com"
SECTION_NAMES = ("bans", "username_protection", "notifications")


class ElectricFenceConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Electric Fence config flow."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowHandler:
        """Return the options flow."""
        return OptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the setup, then create the single entry with defaults."""
        if user_input is None:
            return self.async_show_form(step_id="user")
        return self.async_create_entry(
            title="Electric Fence", data={}, options=Settings().to_options()
        )


class OptionsFlowHandler(OptionsFlow):
    """Edit Electric Fence settings."""

    def __init__(self) -> None:
        """Initialise the pending settings."""
        self._pending: dict[str, Any] = {}

    def _current(self) -> Settings:
        return Settings.from_options(self.config_entry.options)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the general settings."""
        current = self._current()
        if user_input is not None:
            flat = {k: v for k, v in user_input.items() if k not in SECTION_NAMES}
            for name in SECTION_NAMES:
                flat.update(user_input[name])
            self._pending = {
                **current.to_options(),
                **flat,
                "retention_days": int(user_input["retention_days"]),
            }
            if user_input["geoip_provider"] == "maxmind":
                return await self.async_step_maxmind()
            self._pending["maxmind_account_id"] = None
            self._pending["maxmind_license_key"] = None
            return self.async_create_entry(data=self._pending)

        def number(
            key: str, low: int, high: int, unit: str
        ) -> dict[probatio.Required, NumberSelector]:
            return {
                probatio.Required(key, default=getattr(current, key)): NumberSelector(
                    NumberSelectorConfig(
                        min=low,
                        max=high,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement=unit,
                    )
                )
            }

        def flag(key: str) -> dict[probatio.Required, type[bool]]:
            return {probatio.Required(key, default=getattr(current, key)): bool}

        schema = probatio.Schema(
            {
                probatio.Required(
                    "retention_days", default=current.retention_days
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=1,
                        max=3650,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="days",
                    )
                ),
                probatio.Required(
                    "geoip_provider", default=current.geoip_provider
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=["dbip_lite", "maxmind", "none"],
                        translation_key="geoip_provider",
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                probatio.Required("reverse_dns", default=current.reverse_dns): bool,
                probatio.Required("record_values", default=current.record_values): bool,
                **flag("mask_ips"),
                probatio.Required("bans"): section(
                    probatio.Schema(
                        {
                            **flag("auto_ban"),
                            **number("ban_threshold", 1, 100, "attempts"),
                            **number("ban_window_minutes", 1, 1440, "minutes"),
                            **number("ban_hours", 0, 8760, "hours"),
                            **flag("ban_repeat_offenders"),
                        }
                    ),
                    {"collapsed": True},
                ),
                probatio.Required("username_protection"): section(
                    probatio.Schema(
                        {
                            **flag("username_protection"),
                            **number("attack_threshold", 2, 1000, "attempts"),
                            **number("attack_min_ips", 1, 100, "addresses"),
                            **number("attack_window_minutes", 1, 1440, "minutes"),
                        }
                    ),
                    {"collapsed": True},
                ),
                probatio.Required("notifications"): section(
                    probatio.Schema(
                        {
                            **flag("notify_failed_login"),
                            **flag("notify_ip_banned"),
                            **flag("notify_username_attack"),
                            **flag("notify_new_country"),
                            **flag("notify_ban_ended"),
                            probatio.Required(
                                "ha_login_notification",
                                default=current.ha_login_notification,
                            ): SelectSelector(
                                SelectSelectorConfig(
                                    options=["replace", "keep"],
                                    translation_key="ha_login_notification",
                                    mode=SelectSelectorMode.DROPDOWN,
                                )
                            ),
                            probatio.Required(
                                "notify_targets",
                                default=list(current.notify_targets),
                            ): EntitySelector(
                                EntitySelectorConfig(domain="notify", multiple=True)
                            ),
                        }
                    ),
                    {"collapsed": False},
                ),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            # hassfest bans URLs in strings; the link comes in as a placeholder.
            description_placeholders={"dbip_url": DBIP_URL},
        )

    async def async_step_maxmind(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the MaxMind credentials and check them."""
        errors: dict[str, str] = {}
        current = self._current()
        if user_input is not None:
            account_id = user_input["maxmind_account_id"].strip()
            license_key = user_input["maxmind_license_key"].strip()
            provider = MaxMindProvider(account_id, license_key)
            try:
                valid = await provider.async_validate_credentials(
                    async_get_clientsession(self.hass)
                )
            except GeoDownloadError, ClientError:
                errors["base"] = "cannot_connect"
            else:
                if valid:
                    return self.async_create_entry(
                        data={
                            **self._pending,
                            "maxmind_account_id": account_id,
                            "maxmind_license_key": license_key,
                        }
                    )
                errors["base"] = "invalid_maxmind_credentials"
        schema = probatio.Schema(
            {
                probatio.Required(
                    "maxmind_account_id",
                    default=(user_input or {}).get(
                        "maxmind_account_id", current.maxmind_account_id or ""
                    ),
                ): TextSelector(),
                probatio.Required("maxmind_license_key"): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(
            step_id="maxmind", data_schema=schema, errors=errors
        )
