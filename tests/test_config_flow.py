"""Tests for the Electric Fence config flow."""

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.electric_fence.const import DOMAIN
from custom_components.electric_fence.settings import Settings
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType


async def test_user_flow_creates_entry_with_defaults(hass: HomeAssistant) -> None:
    """The user step creates an entry carrying the default settings."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Electric Fence"
    assert Settings.from_options(result["options"]) == Settings()


async def test_single_instance_only(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """A second instance is refused."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"
