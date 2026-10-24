"""Config flow for Electric Fence."""

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN
from .settings import Settings


class ElectricFenceConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Electric Fence config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the setup, then create the single entry with defaults."""
        if user_input is None:
            return self.async_show_form(step_id="user")
        return self.async_create_entry(
            title="Electric Fence", data={}, options=Settings().to_options()
        )
