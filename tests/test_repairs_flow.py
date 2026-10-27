"""The fix flow that accepts the installed files of a manual install."""

from typing import Any
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.electric_fence.const import DOMAIN
from custom_components.electric_fence.repairs import (
    INTEGRATION_FILES_CHANGED_MANUAL,
    async_report_integration_issue,
)
from homeassistant.auth.models import User
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

ISSUE = "integration_files_changed_demo"


@pytest.fixture
async def issue(hass: HomeAssistant, setup_entry: MockConfigEntry) -> AsyncMock:
    """Raise the manual files issue; return the patched accept call."""
    assert await async_setup_component(hass, "repairs", {})
    async_report_integration_issue(
        hass,
        ISSUE,
        INTEGRATION_FILES_CHANGED_MANUAL,
        {"name": "Demo", "files": "a\\.py"},
        {"domain": "demo"},
    )
    accept = AsyncMock(return_value=True)
    scheduler = setup_entry.runtime_data.integrity
    assert scheduler is not None
    scheduler.async_accept_files = accept  # type: ignore[method-assign]
    return accept


async def _fix(client: Any) -> dict[str, Any]:
    resp = await client.post(
        "/api/repairs/issues/fix", json={"handler": DOMAIN, "issue_id": ISSUE}
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["step_id"] == "confirm"
    assert data["description_placeholders"] == {"name": "Demo", "files": "a\\.py"}
    resp = await client.post(f"/api/repairs/issues/fix/{data['flow_id']}", json={})
    assert resp.status == 200
    return await resp.json()


async def test_accept_files_from_repair(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    hass_admin_user: User,
    issue: AsyncMock,
) -> None:
    """Confirming accepts the files in the admin's name; the issue goes away."""
    result = await _fix(await hass_client())
    assert result["type"] == "create_entry"
    issue.assert_awaited_once_with("demo", hass_admin_user.id, hass_admin_user.name)
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE) is None


async def test_accept_refused_aborts(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, issue: AsyncMock
) -> None:
    """When the files can't be accepted, the flow aborts and the issue stays."""
    issue.return_value = False
    result = await _fix(await hass_client())
    assert result["type"] == "abort"
    assert result["reason"] == "accept_failed"
    assert result["description_placeholders"] == {"name": "Demo", "files": "a\\.py"}
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE) is not None
