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
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
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
            self._pending = {
                **current.to_options(),
                **user_input,
                "retention_days": int(user_input["retention_days"]),
            }
            if user_input["geoip_provider"] == "maxmind":
                return await self.async_step_maxmind()
            self._pending["maxmind_account_id"] = None
            self._pending["maxmind_license_key"] = None
            return self.async_create_entry(data=self._pending)
        schema = probatio.Schema(
            {
                probatio.Required(
                    "notify_failed_login", default=current.notify_failed_login
                ): bool,
                probatio.Required(
                    "ha_login_notification", default=current.ha_login_notification
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=["replace", "keep"],
                        translation_key="ha_login_notification",
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
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
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

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
