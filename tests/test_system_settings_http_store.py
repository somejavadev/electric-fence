"""HTTP configs staged without `http/config/configure` (Core's SSL profile repair).

Core's repair stages a config with `HTTPConfigStore.async_set_pending` from
the repair flow's view, then restarts. The collector wraps that method on the
live store. These tests drive Core's real store, repair flow and WebSocket
command; the restart service is mocked, as Core's tests do.
"""

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockUser
from pytest_homeassistant_custom_component.components.repairs import (
    process_repair_fix_flow,
    start_repair_fix_flow,
)
from pytest_homeassistant_custom_component.typing import (
    ClientSessionGenerator,
    WebSocketGenerator,
)

from custom_components.electric_fence.collectors import system_settings
from custom_components.electric_fence.const import DOMAIN
from custom_components.electric_fence.contracts import CONTRACTS
from custom_components.electric_fence.models import ChangeAction, ChangeKind
from homeassistant.components.http.config import DATA_STORE, HTTPConfigStore
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from tests.test_system_settings_areas import (
    _by_admin,
    _collector,
    _configure,
    _rows,
    _set_record_values,
    _setup_fence,
    fence_http as fence_http,  # noqa: PLC0414
    restore_logging as restore_logging,  # noqa: PLC0414
)

ISSUE = "ssl_profile_outdated"


def _store(hass: HomeAssistant) -> HTTPConfigStore:
    store: HTTPConfigStore = hass.data[DATA_STORE]
    return store


def _profile_issue(hass: HomeAssistant) -> None:
    """Raise Core's SSL profile repair, as Core does for an outdated profile."""
    ir.async_create_issue(
        hass,
        "http",
        ISSUE,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE,
        translation_placeholders={"profile": "intermediate_v4"},
    )


async def _run_repair(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, upgrade: str
) -> None:
    assert await async_setup_component(hass, "repairs", {})
    _profile_issue(hass)
    client = await hass_client()
    data = await start_repair_fix_flow(client, "http", ISSUE)
    data = await process_repair_fix_flow(
        client, data["flow_id"], json={"next_step_id": f"confirm_{upgrade}"}
    )
    data = await process_repair_fix_flow(client, data["flow_id"])
    assert data["type"] == "create_entry"
    await hass.async_block_till_done()


async def test_repair_is_recorded_with_the_admin(
    hass: HomeAssistant,
    fence_http: list[Any],
    hass_client: ClientSessionGenerator,
    hass_admin_user: MockUser,
) -> None:
    """Core's repair staging a new profile is recorded against stable."""
    _set_record_values(hass, True)
    before = _store(hass).stable["ssl_profile"]

    await _run_repair(hass, hass_client, "modern_v6")

    assert _store(hass).pending is not None
    assert _store(hass).pending["ssl_profile"] == "modern_v6"
    assert len(fence_http) == 1
    (row,) = await _rows(hass)
    assert row.kind is ChangeKind.HTTP
    assert row.action is ChangeAction.UPDATED
    assert row.target_name == "HTTP"
    assert _by_admin(row, hass_admin_user)
    assert row.details == {
        "changed": ["ssl_profile"],
        "values": {"ssl_profile": {"before": before, "after": "modern_v6"}},
    }


async def test_repair_names_only_without_record_values(
    hass: HomeAssistant,
    fence_http: list[Any],
    hass_client: ClientSessionGenerator,
) -> None:
    """Without `record_values` only the setting's name is recorded."""
    await _run_repair(hass, hass_client, "modern_v6")
    (row,) = await _rows(hass)
    assert row.details == {"changed": ["ssl_profile"]}


async def test_websocket_command_is_recorded_once(
    hass: HomeAssistant,
    fence_http: list[Any],
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
) -> None:
    """The command's own wrapper records it; the store wrapper stays quiet."""
    response = await _configure(hass, hass_ws_client, {"ssl_profile": "modern_v6"})
    assert response["success"] is True
    (row,) = await _rows(hass)
    assert row.action is ChangeAction.UPDATED
    assert _by_admin(row, hass_admin_user)


async def test_websocket_withdraw_is_recorded_once(
    hass: HomeAssistant,
    fence_http: list[Any],
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Withdrawing over the command records one withdrawal, not two."""
    await _configure(hass, hass_ws_client, {"ssl_profile": "modern_v6"})
    await _configure(hass, hass_ws_client, None)
    rows = await _rows(hass)
    assert [r.action for r in rows] == [ChangeAction.UPDATED, ChangeAction.WITHDRAWN]


async def test_call_outside_a_request_is_system(
    hass: HomeAssistant, fence_http: list[Any]
) -> None:
    """Staged by Core itself, with no request in this task: System."""
    store = _store(hass)
    await store.async_set_pending({**store.stable, "ssl_profile": "modern_v6"})
    (row,) = await _rows(hass)
    assert row.action is ChangeAction.UPDATED
    assert (row.actor_user_id, row.actor_name, row.actor_ip) == (None, None, None)


async def test_dropping_the_pending_config_is_withdrawn(
    hass: HomeAssistant, fence_http: list[Any]
) -> None:
    """Clearing a pending config records a withdrawal back to stable."""
    _set_record_values(hass, True)
    store = _store(hass)
    stable_profile = store.stable["ssl_profile"]
    await store.async_set_pending({**store.stable, "ssl_profile": "modern_v6"})
    await store.async_set_pending(None)
    rows = await _rows(hass)
    assert [r.action for r in rows] == [ChangeAction.UPDATED, ChangeAction.WITHDRAWN]
    assert rows[1].details == {
        "changed": ["ssl_profile"],
        "values": {"ssl_profile": {"before": "modern_v6", "after": stable_profile}},
    }


async def test_no_change_records_nothing(
    hass: HomeAssistant, fence_http: list[Any]
) -> None:
    """Stable again, nothing pending, or the same untried pending: no record."""
    store = _store(hass)
    await store.async_set_pending(dict(store.stable))
    await store.async_set_pending(None)
    staged = {**store.stable, "ssl_profile": "modern_v6"}
    await store.async_set_pending(dict(staged))
    await store.async_set_pending(dict(staged))
    rows = await _rows(hass)
    assert [r.action for r in rows] == [ChangeAction.UPDATED]


async def test_core_error_passes_through_and_records_nothing(
    hass: HomeAssistant, fence_http: list[Any]
) -> None:
    """Core's exception reaches the caller unchanged; nothing is recorded."""
    store = _store(hass)
    with (
        patch.object(
            HTTPConfigStore, "_async_persist", AsyncMock(side_effect=OSError("disk"))
        ),
        pytest.raises(OSError, match="disk"),
    ):
        await store.async_set_pending({**store.stable, "ssl_profile": "modern_v6"})
    assert await _rows(hass) == []


async def test_our_failure_lets_core_run(
    hass: HomeAssistant, fence_http: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure in the wrapper is counted, and Core's call still happens."""
    collector = _collector(hass)
    errors = collector.guard.errors

    def boom() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(system_settings, "request_actor", boom)
    store = _store(hass)
    await store.async_set_pending({**store.stable, "ssl_profile": "modern_v6"})
    assert store.pending is not None
    assert store.pending["ssl_profile"] == "modern_v6"
    assert collector.guard.errors == errors + 1
    assert await _rows(hass) == []


async def test_unload_restores_the_store_method(
    hass: HomeAssistant, fence_http: list[Any]
) -> None:
    """Unloading Electric Fence puts Core's method back."""
    store = _store(hass)
    assert "async_set_pending" in vars(store)
    entry = hass.config_entries.async_loaded_entries(DOMAIN)[0]
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert "async_set_pending" not in vars(store)


# The contract.


def _check(hass: HomeAssistant) -> str | None:
    return CONTRACTS["http_store_set_pending"].check(hass)


async def test_contract_passes_on_core(hass: HomeAssistant) -> None:
    """Core's store matches; without `http` the contract does not apply."""
    assert _check(hass) is None
    assert await async_setup_component(hass, "http", {})
    assert _check(hass) is None


async def test_contract_fails_without_store(hass: HomeAssistant) -> None:
    """The store is reached through `hass.data[DATA_STORE]`."""
    assert await async_setup_component(hass, "http", {})
    hass.data.pop(DATA_STORE)
    assert _check(hass) == "the HTTP config store is missing"


async def test_contract_fails_without_created_at(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kept pending config is told apart by its `created_at`."""
    from homeassistant.components.http import config as http_config  # noqa: PLC0415

    assert await async_setup_component(hass, "http", {})
    monkeypatch.delattr(http_config, "HTTP_CONFIG_CREATED_AT")
    assert _check(hass) == "config.HTTP_CONFIG_CREATED_AT is missing"


@pytest.mark.parametrize("attr", ["stable", "pending"])
async def test_contract_fails_without_store_member(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, attr: str
) -> None:
    """The wrapper reads the stable and pending configs."""
    assert await async_setup_component(hass, "http", {})
    monkeypatch.delattr(HTTPConfigStore, attr)
    assert _check(hass) == f"HTTPConfigStore.{attr} is missing"


async def test_store_gone_records_nothing(
    hass: HomeAssistant, fence_http: list[Any]
) -> None:
    """If the store is not in `hass.data` when called, nothing is recorded."""
    store = _store(hass)
    del hass.data[DATA_STORE]
    await store.async_set_pending({**store.stable, "ssl_profile": "modern_v6"})
    hass.data[DATA_STORE] = store
    assert store.pending is not None
    assert await _rows(hass) == []


async def test_store_gone_after_the_call_records_nothing(
    hass: HomeAssistant, fence_http: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store removed while Core's call runs: nothing is recorded."""
    store = _store(hass)
    original = HTTPConfigStore.async_set_pending

    async def drop_then_set(self: HTTPConfigStore, config: Any) -> None:
        del hass.data[DATA_STORE]
        await original(self, config)

    monkeypatch.setattr(HTTPConfigStore, "async_set_pending", drop_then_set)
    # The wrapper on the instance wraps the bound method it found at install,
    # so re-install it over the patched class method.
    entry = hass.config_entries.async_loaded_entries(DOMAIN)[0]
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await store.async_set_pending({**store.stable, "ssl_profile": "modern_v6"})
    hass.data[DATA_STORE] = store
    assert await _rows(hass) == []


async def test_no_store_at_install_wraps_nothing(
    hass: HomeAssistant, fence_http: list[Any]
) -> None:
    """Without a store when installing, there is nothing to wrap."""
    store = _store(hass)
    entry = hass.config_entries.async_loaded_entries(DOMAIN)[0]
    assert await hass.config_entries.async_unload(entry.entry_id)
    # Every contract passes, so only the missing store stops the wrap.
    with (
        patch(
            "custom_components.electric_fence.collectors.base.check_contracts",
            return_value=[],
        ),
        patch.object(system_settings, "check_contracts", return_value=[]),
    ):
        del hass.data[DATA_STORE]
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert _collector(hass).is_installed
    hass.data[DATA_STORE] = store
    assert "async_set_pending" not in vars(store)


async def test_contract_fails_for_changed_signature(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`async_set_pending` takes the config as its only parameter."""
    assert await async_setup_component(hass, "http", {})

    async def renamed(self: HTTPConfigStore, new_config: Any) -> None:
        """Stand-in with a renamed parameter."""

    monkeypatch.setattr(HTTPConfigStore, "async_set_pending", renamed)
    assert (
        _check(hass) == "HTTPConfigStore.async_set_pending has an unexpected signature"
    )


async def test_contract_fails_for_sync_method(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wrapper awaits Core's method."""
    assert await async_setup_component(hass, "http", {})

    def sync(self: HTTPConfigStore, config: Any) -> None:
        """Stand-in that is not a coroutine function."""

    monkeypatch.setattr(HTTPConfigStore, "async_set_pending", sync)
    assert (
        _check(hass) == "HTTPConfigStore.async_set_pending is not a coroutine function"
    )


async def test_failed_store_contract_keeps_the_rest(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the store hook is disabled; the repair issue says so."""
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "websocket_api", {})

    async def renamed(self: HTTPConfigStore, new_config: Any) -> None:
        """Stand-in with a renamed parameter."""

    monkeypatch.setattr(HTTPConfigStore, "async_set_pending", renamed)
    await _setup_fence(hass, monkeypatch)
    collector = _collector(hass)

    assert collector.is_installed
    assert collector.disabled_reason == [
        "HTTP config store: HTTPConfigStore.async_set_pending has an unexpected"
        " signature"
    ]
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, "collector_disabled_system_settings"
    )
    assert issue is not None
    assert "async_set_pending" not in vars(_store(hass))
