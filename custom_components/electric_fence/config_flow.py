"""Config flow for Electric Fence."""

import ssl
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
SECTION_NAMES = (
    "bans",
    "username_protection",
    "notifications",
    "integration_checks",
    "forwarding",
)


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

    async def _forwarding_errors(self, flat: dict[str, Any]) -> dict[str, str]:
        """Check the forwarding settings; errors are keyed by the section."""
        if flat["forward_enabled"] and not str(flat["forward_host"]).strip():
            return {"forwarding": "forward_host_required"}
        pem = str(flat["forward_ca_pem"])
        if pem.strip():
            try:
                await self.hass.async_add_executor_job(
                    lambda: ssl.create_default_context(cadata=pem)
                )
            except ssl.SSLError, ValueError:
                return {"forwarding": "forward_ca_invalid"}
        return {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the general settings."""
        current = self._current()
        shown = current.to_options()
        shown["ignored_files"] = "\n".join(current.ignored_files)
        errors: dict[str, str] = {}
        if user_input is not None:
            flat = {k: v for k, v in user_input.items() if k not in SECTION_NAMES}
            for name in SECTION_NAMES:
                flat.update(user_input[name])
            shown.update(flat)
            errors = await self._forwarding_errors(flat)
        if user_input is not None and not errors:
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
            key: str, low: int, high: int, unit: str | None = None
        ) -> dict[probatio.Required, NumberSelector]:
            return {
                probatio.Required(key, default=shown[key]): NumberSelector(
                    NumberSelectorConfig(
                        min=low,
                        max=high,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        **({"unit_of_measurement": unit} if unit else {}),
                    )
                )
            }

        def flag(key: str) -> dict[probatio.Required, type[bool]]:
            return {probatio.Required(key, default=shown[key]): bool}

        def text(
            key: str, multiline: bool = False
        ) -> dict[probatio.Optional, TextSelector]:
            # The frontend refuses a required field holding "", and leaves a
            # cleared optional one out; the default turns that back into "".
            return {
                probatio.Optional(
                    key, default="", description={"suggested_value": shown[key]}
                ): TextSelector(TextSelectorConfig(multiline=multiline))
            }

        schema = probatio.Schema(
            {
                probatio.Required(
                    "retention_days", default=shown["retention_days"]
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
                    "max_db_mb", default=shown["max_db_mb"]
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=20,
                        max=10000,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="MB",
                    )
                ),
                probatio.Required(
                    "geoip_provider", default=shown["geoip_provider"]
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=["dbip_lite", "maxmind", "none"],
                        translation_key="geoip_provider",
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                probatio.Required("reverse_dns", default=shown["reverse_dns"]): bool,
                probatio.Required(
                    "record_values", default=shown["record_values"]
                ): bool,
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
                                default=shown["ha_login_notification"],
                            ): SelectSelector(
                                SelectSelectorConfig(
                                    options=["replace", "keep"],
                                    translation_key="ha_login_notification",
                                    mode=SelectSelectorMode.DROPDOWN,
                                )
                            ),
                            probatio.Required(
                                "notify_targets",
                                default=list(shown["notify_targets"]),
                            ): EntitySelector(
                                EntitySelectorConfig(domain="notify", multiple=True)
                            ),
                        }
                    ),
                    {"collapsed": False},
                ),
                probatio.Required("integration_checks"): section(
                    probatio.Schema(
                        {
                            **flag("verify_files"),
                            **flag("integration_repairs"),
                            **number("unmaintained_months", 1, 36, "months"),
                            **number("scan_delay_minutes", 1, 1440, "minutes"),
                            **text("ignored_files", multiline=True),
                        }
                    ),
                    {"collapsed": True},
                ),
                probatio.Required("forwarding"): section(
                    probatio.Schema(
                        {
                            **flag("forward_enabled"),
                            **text("forward_host"),
                            **number("forward_port", 1, 65535),
                            probatio.Required(
                                "forward_protocol", default=shown["forward_protocol"]
                            ): SelectSelector(
                                SelectSelectorConfig(
                                    options=["tcp", "udp", "tls"],
                                    translation_key="forward_protocol",
                                    mode=SelectSelectorMode.DROPDOWN,
                                )
                            ),
                            **flag("forward_verify_tls"),
                            **text("forward_ca_pem", multiline=True),
                        }
                    ),
                    {"collapsed": True},
                ),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
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
