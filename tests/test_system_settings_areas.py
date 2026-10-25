"""The system settings areas: backup, HTTP, network, analytics and logger.

Each command is driven through a real WebSocket client, as an admin, against
the real Core component. Nothing here restarts Home Assistant or sends data
anywhere: the restart service is mocked, backup deletion and restore are
patched on the manager, and analytics never sends.
"""

from collections.abc import Iterator
from dataclasses import replace
import json
import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockUser,
    async_mock_service,
)
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.electric_fence.collectors.system_settings import (
    SystemSettingsCollector,
)
from custom_components.electric_fence.const import DOMAIN, MIN_HA_VERSION
from custom_components.electric_fence.models import ChangeAction, ChangeKind
from custom_components.electric_fence.settings import Settings
from custom_components.electric_fence.storage import ChangeRow
from homeassistant.components.backup import BackupManager
from homeassistant.components.backup.manager import BackupNotFound
from homeassistant.components.http.config import DATA_STORE
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from tests.helpers import change_rows, flush_worker

IP = "127.0.0.1"
AREA_KINDS = (
    ChangeKind.BACKUP,
    ChangeKind.HTTP,
    ChangeKind.NETWORK,
    ChangeKind.ANALYTICS,
    ChangeKind.LOGGER,
)


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """Put back every logger level and the logger class the tests change."""
    klass = logging.getLoggerClass()
    root = logging.getLogger()
    levels = {
        name: logger.level
        for name, logger in logging.Logger.manager.loggerDict.items()
        if isinstance(logger, logging.Logger)
    }
    root_level = root.level
    yield
    logging.setLoggerClass(klass)
    root.setLevel(root_level)
    for name, logger in logging.Logger.manager.loggerDict.items():
        if isinstance(logger, logging.Logger):
            logger.setLevel(levels.get(name, logging.NOTSET))


async def _setup_fence(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> MockConfigEntry:
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", MIN_HA_VERSION)
    entry = MockConfigEntry(domain=DOMAIN, options=Settings().to_options())
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _collector(hass: HomeAssistant) -> SystemSettingsCollector:
    entry = hass.config_entries.async_loaded_entries(DOMAIN)[0]
    return next(
        c
        for c in entry.runtime_data.collectors
        if isinstance(c, SystemSettingsCollector)
    )


def _set_record_values(hass: HomeAssistant, value: bool) -> None:
    runtime = hass.config_entries.async_loaded_entries(DOMAIN)[0].runtime_data
    runtime.settings = replace(runtime.settings, record_values=value)


async def _rows(hass: HomeAssistant) -> list[ChangeRow]:
    await hass.async_block_till_done()
    await flush_worker(hass)
    return [r for r in change_rows(hass) if r.kind in AREA_KINDS]


async def _call(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, command: str, **fields: Any
) -> dict[str, Any]:
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": command, **fields})
    response: dict[str, Any] = await ws.receive_json()
    while response["type"] == "event":
        response = await ws.receive_json()
    await ws.close()
    return response


def _by_admin(row: ChangeRow, admin: MockUser) -> bool:
    return (
        row.actor_user_id == admin.id
        and row.actor_name == admin.name
        and row.actor_ip == IP
    )


# The table.


def test_specs_table() -> None:
    """Every command of the areas is recorded with its kind, action and name."""
    table = {
        spec.command: (spec.component, spec.kind, spec.action, spec.target_name)
        for spec in SystemSettingsCollector.WS_SPECS
    }
    configure = table.pop("http/config/configure")
    assert configure[:2] == ("http", ChangeKind.HTTP)
    assert configure[3] == "HTTP"
    assert callable(configure[2])
    assert configure[2]({"config": None}) is ChangeAction.WITHDRAWN
    assert configure[2]({"config": {}}) is ChangeAction.UPDATED
    assert table == {
        "backup/config/update": (
            "backup",
            ChangeKind.BACKUP,
            ChangeAction.UPDATED,
            "Backups",
        ),
        "backup/delete": ("backup", ChangeKind.BACKUP, ChangeAction.REMOVED, "Backup"),
        "backup/restore": (
            "backup",
            ChangeKind.BACKUP,
            ChangeAction.RESTORE_REQUESTED,
            "Backup",
        ),
        "http/config/promote": (
            "http",
            ChangeKind.HTTP,
            ChangeAction.CONFIRMED,
            "HTTP",
        ),
        "network/configure": (
            "network",
            ChangeKind.NETWORK,
            ChangeAction.UPDATED,
            "Network",
        ),
        "analytics/preferences": (
            "analytics",
            ChangeKind.ANALYTICS,
            ChangeAction.UPDATED,
            "Analytics",
        ),
        "logger/log_level": (
            "logger",
            ChangeKind.LOGGER,
            ChangeAction.UPDATED,
            "Logging",
        ),
        "logger/integration_log_level": (
            "logger",
            ChangeKind.LOGGER,
            ChangeAction.UPDATED,
            "Logging",
        ),
    }
    by_command = {spec.command: spec for spec in SystemSettingsCollector.WS_SPECS}
    assert by_command["backup/restore"].record_before is True
    assert by_command["backup/delete"].always_record is True
    assert by_command["backup/delete"].record_before is False


# Backup.


@pytest.fixture
async def backup(hass: HomeAssistant) -> BackupManager:
    """Set up the backup component (not on a Supervisor), as Core's tests do."""
    with (
        patch("homeassistant.components.backup.is_hassio", return_value=False),
        patch("homeassistant.components.backup.backup.is_hassio", return_value=False),
        patch("homeassistant.components.backup.services.is_hassio", return_value=False),
    ):
        assert await async_setup_component(hass, "backup", {})
        await hass.async_block_till_done(wait_background_tasks=True)
    manager: BackupManager = hass.data["backup"]
    return manager


@pytest.fixture
async def fence_backup(
    hass: HomeAssistant,
    backup: BackupManager,
    hass_admin_user: MockUser,
    monkeypatch: pytest.MonkeyPatch,
) -> SystemSettingsCollector:
    """Electric Fence set up after the backup component."""
    await _setup_fence(hass, monkeypatch)
    return _collector(hass)


async def test_backup_disable_automatic_backups(
    hass: HomeAssistant,
    fence_backup: SystemSettingsCollector,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
) -> None:
    """Turning automatic backups off records the schedule, before and after."""
    response = await _call(
        hass, hass_ws_client, "backup/config/update", schedule={"recurrence": "daily"}
    )
    assert response["success"] is True
    _set_record_values(hass, True)
    response = await _call(
        hass, hass_ws_client, "backup/config/update", schedule={"recurrence": "never"}
    )
    assert response["success"] is True

    enabled, disabled = await _rows(hass)
    assert enabled.details == {"changed": ["schedule"]}
    assert disabled.kind is ChangeKind.BACKUP
    assert disabled.action is ChangeAction.UPDATED
    assert disabled.target_name == "Backups"
    assert _by_admin(disabled, hass_admin_user)
    assert disabled.details["changed"] == ["schedule"]
    schedule = disabled.details["values"]["schedule"]
    assert schedule["before"]["recurrence"] == "daily"
    assert schedule["after"]["recurrence"] == "never"


async def test_backup_create_password_hidden(
    hass: HomeAssistant,
    fence_backup: SystemSettingsCollector,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """The backup encryption password is recorded as changed, never its value."""
    _set_record_values(hass, True)
    response = await _call(
        hass,
        hass_ws_client,
        "backup/config/update",
        create_backup={"password": "hunter2", "name": "Nightly"},
    )
    assert response["success"] is True

    (row,) = await _rows(hass)
    assert row.details["changed"] == ["create_backup"]
    create = row.details["values"]["create_backup"]
    assert create["before"]["password"] == {"hidden": True}
    assert create["after"]["password"] == {"hidden": True}
    assert create["before"]["name"] is None
    assert create["after"]["name"] == "Nightly"
    assert "hunter2" not in json.dumps(row.details)


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_backup_list_change_in_place_recorded(
    hass: HomeAssistant,
    fence_backup: SystemSettingsCollector,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """A setting Core changes in place (a shared list) still shows its before."""
    _set_record_values(hass, True)
    await _call(
        hass,
        hass_ws_client,
        "backup/config/update",
        schedule={"recurrence": "custom_days", "days": ["mon"]},
    )
    data = hass.data["backup"].config.data

    def _add_day(**_: Any) -> None:
        # Change the stored list in place, as a later Core might.
        data.schedule.days.append("tue")

    with patch.object(hass.data["backup"].config, "update", _add_day):
        await _call(hass, hass_ws_client, "backup/config/update", schedule={})

    _first, second = await _rows(hass)
    days = second.details["values"]["schedule"]
    assert days["before"]["days"] == ["mon"]
    assert days["after"]["days"] == ["mon", "tue"]


async def test_backup_no_change_records_nothing(
    hass: HomeAssistant,
    fence_backup: SystemSettingsCollector,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Saving the backup settings unchanged records nothing."""
    response = await _call(
        hass, hass_ws_client, "backup/config/update", schedule={"recurrence": "never"}
    )
    assert response["success"] is True
    assert await _rows(hass) == []


async def test_backup_delete(
    hass: HomeAssistant,
    fence_backup: SystemSettingsCollector,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
) -> None:
    """Deleting a backup records its ID once Core answered."""
    with patch.object(
        BackupManager, "async_delete_backup", AsyncMock(return_value={})
    ) as delete:
        off = await _call(hass, hass_ws_client, "backup/delete", backup_id="abc123")
        _set_record_values(hass, True)
        on = await _call(hass, hass_ws_client, "backup/delete", backup_id="def456")

    assert off["success"] is on["success"] is True
    assert delete.await_count == 2
    first, second = await _rows(hass)
    assert first.kind is ChangeKind.BACKUP
    assert first.action is ChangeAction.REMOVED
    assert first.target_name == "Backup"
    assert _by_admin(first, hass_admin_user)
    assert first.details == {"changed": ["backup_id"]}
    assert second.details == {
        "changed": ["backup_id"],
        "values": {"backup_id": {"before": None, "after": "def456"}},
    }


async def test_backup_delete_error_records_nothing(
    hass: HomeAssistant,
    fence_backup: SystemSettingsCollector,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """A delete that raises records nothing."""
    with patch.object(
        BackupManager,
        "async_delete_backup",
        AsyncMock(side_effect=BackupNotFound("gone")),
    ):
        response = await _call(
            hass, hass_ws_client, "backup/delete", backup_id="abc123"
        )

    assert response["success"] is False
    assert await _rows(hass) == []


@pytest.mark.parametrize("outcome", ["success", "not_found"])
async def test_backup_restore_requested(
    hass: HomeAssistant,
    fence_backup: SystemSettingsCollector,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
    outcome: str,
) -> None:
    """A restore is recorded before Core runs it, whatever happens, no password."""
    _set_record_values(hass, True)
    restore = AsyncMock(
        return_value=None,
        side_effect=BackupNotFound("gone") if outcome == "not_found" else None,
    )
    with patch.object(BackupManager, "async_restore_backup", restore):
        response = await _call(
            hass,
            hass_ws_client,
            "backup/restore",
            backup_id="abc123",
            agent_id="backup.local",
            password="hunter2",
            restore_folders=["media"],
        )

    assert response["success"] is (outcome == "success")
    restore.assert_awaited_once()
    (row,) = await _rows(hass)
    assert row.kind is ChangeKind.BACKUP
    assert row.action is ChangeAction.RESTORE_REQUESTED
    assert row.target_name == "Backup"
    assert _by_admin(row, hass_admin_user)
    assert row.details["changed"] == [
        "agent_id",
        "backup_id",
        "restore_database",
        "restore_folders",
        "restore_homeassistant",
    ]
    assert row.details["values"]["backup_id"] == {"before": None, "after": "abc123"}
    assert row.details["values"]["restore_folders"] == {
        "before": None,
        "after": ["media"],
    }
    assert row.details["values"]["restore_database"] == {
        "before": None,
        "after": True,
    }
    assert "hunter2" not in json.dumps(row.details)
    assert "password" not in json.dumps(row.details)


# HTTP.


@pytest.fixture
async def fence_http(
    hass: HomeAssistant, hass_admin_user: MockUser, monkeypatch: pytest.MonkeyPatch
) -> list[Any]:
    """Electric Fence and HTTP; return the mocked restart calls."""
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "websocket_api", {})
    await _setup_fence(hass, monkeypatch)
    # Staging a config restarts Home Assistant; mocked, as Core's tests do.
    return async_mock_service(hass, "homeassistant", "restart")


async def _configure(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, config: Any
) -> dict[str, Any]:
    response = await _call(hass, hass_ws_client, "http/config/configure", config=config)
    await hass.async_block_till_done()
    return response


async def test_http_ban_threshold(
    hass: HomeAssistant,
    fence_http: list[Any],
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
) -> None:
    """Staging a new ban threshold records it against the stable config."""
    _set_record_values(hass, True)
    default = hass.data[DATA_STORE].stable["login_attempts_threshold"]

    response = await _configure(hass, hass_ws_client, {"login_attempts_threshold": 7})

    assert response["success"] is True
    assert response["result"] == {"restart": True}
    assert len(fence_http) == 1
    (row,) = await _rows(hass)
    assert row.kind is ChangeKind.HTTP
    assert row.action is ChangeAction.UPDATED
    assert row.target_name == "HTTP"
    assert _by_admin(row, hass_admin_user)
    assert row.details == {
        "changed": ["login_attempts_threshold"],
        "values": {"login_attempts_threshold": {"before": default, "after": 7}},
    }


async def test_http_withdraw_pending(
    hass: HomeAssistant,
    fence_http: list[Any],
    hass_ws_client: WebSocketGenerator,
) -> None:
    """`config: None` withdraws the pending config; with none pending, nothing."""
    _set_record_values(hass, True)
    default = hass.data[DATA_STORE].stable["login_attempts_threshold"]
    await _configure(hass, hass_ws_client, {"login_attempts_threshold": 7})

    withdrawn = await _configure(hass, hass_ws_client, None)
    again = await _configure(hass, hass_ws_client, None)

    assert withdrawn["result"] == {"restart": True}
    assert again["result"] == {"restart": False}
    _staged, row = await _rows(hass)
    assert row.kind is ChangeKind.HTTP
    assert row.action is ChangeAction.WITHDRAWN
    assert row.details == {
        "changed": ["login_attempts_threshold"],
        "values": {"login_attempts_threshold": {"before": 7, "after": default}},
    }


async def test_http_same_pending_again_records_nothing(
    hass: HomeAssistant,
    fence_http: list[Any],
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Staging the config that is already pending changes nothing."""
    await _configure(hass, hass_ws_client, {"login_attempts_threshold": 7})
    again = await _configure(hass, hass_ws_client, {"login_attempts_threshold": 7})

    assert again["result"] == {"restart": False}
    (row,) = await _rows(hass)
    assert row.details == {"changed": ["login_attempts_threshold"]}


async def test_http_stable_config_clears_pending(
    hass: HomeAssistant,
    fence_http: list[Any],
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Staging the stable config again drops the pending one: recorded."""
    _set_record_values(hass, True)
    default = hass.data[DATA_STORE].stable["login_attempts_threshold"]
    await _configure(hass, hass_ws_client, {"login_attempts_threshold": 7})
    back = await _configure(hass, hass_ws_client, {})
    unchanged = await _configure(hass, hass_ws_client, {})

    assert back["result"] == {"restart": True}
    assert unchanged["result"] == {"restart": False}
    assert hass.data[DATA_STORE].pending is None
    _staged, row = await _rows(hass)
    assert row.action is ChangeAction.UPDATED
    assert row.details["values"] == {
        "login_attempts_threshold": {"before": 7, "after": default}
    }


async def test_http_failed_pending_tried_again_records(
    hass: HomeAssistant,
    fence_http: list[Any],
    hass_ws_client: WebSocketGenerator,
) -> None:
    """A pending config that failed its trial and is staged again is recorded."""
    await _configure(hass, hass_ws_client, {"login_attempts_threshold": 7})
    pending = hass.data[DATA_STORE].pending
    assert pending is not None
    pending["error"] = "apply_failed"

    again = await _configure(hass, hass_ws_client, {"login_attempts_threshold": 7})

    assert again["result"] == {"restart": True}
    assert len(await _rows(hass)) == 2


async def test_http_rejected_config_records_nothing(
    hass: HomeAssistant,
    fence_http: list[Any],
    hass_ws_client: WebSocketGenerator,
) -> None:
    """A config Core rejects (invalid) records nothing."""
    response = await _configure(
        hass,
        hass_ws_client,
        {"use_x_forwarded_for": True},
    )

    assert response["success"] is False
    assert await _rows(hass) == []
    assert fence_http == []


async def test_http_promote(
    hass: HomeAssistant,
    fence_http: list[Any],
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
) -> None:
    """Promoting the pending config records the stable config, before and after."""
    default = hass.data[DATA_STORE].stable["login_attempts_threshold"]
    await _configure(hass, hass_ws_client, {"login_attempts_threshold": 7})
    _set_record_values(hass, True)

    response = await _call(hass, hass_ws_client, "http/config/promote")
    nothing = await _call(hass, hass_ws_client, "http/config/promote")

    assert response["success"] is True
    assert nothing["success"] is False
    _staged, row = await _rows(hass)
    assert row.kind is ChangeKind.HTTP
    assert row.action is ChangeAction.CONFIRMED
    assert row.target_name == "HTTP"
    assert _by_admin(row, hass_admin_user)
    assert row.details == {
        "changed": ["login_attempts_threshold"],
        "values": {"login_attempts_threshold": {"before": default, "after": 7}},
    }


async def test_http_store_missing_records_nothing(
    hass: HomeAssistant,
    fence_http: list[Any],
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Without the HTTP config store, nothing is read and nothing recorded."""
    store = hass.data.pop(DATA_STORE)
    try:
        assert _collector(hass).WS_SPECS
        for spec in _collector(hass).WS_SPECS:
            if spec.component == "http":
                assert spec.read(hass, {"config": None}) is None
                assert spec.read(hass, {"config": {}}) is None
                after = spec.read_after or spec.read
                assert after(hass, {"config": None}) is None
                assert after(hass, {"config": {}}) is None
    finally:
        hass.data[DATA_STORE] = store


# Network.


async def test_network_adapters(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Choosing network adapters records the selection, before and after."""
    assert await async_setup_component(hass, "network", {})
    await _setup_fence(hass, monkeypatch)
    _set_record_values(hass, True)

    response = await _call(
        hass,
        hass_ws_client,
        "network/configure",
        config={"configured_adapters": ["eth0"]},
    )

    assert response["success"] is True
    (row,) = await _rows(hass)
    assert row.kind is ChangeKind.NETWORK
    assert row.action is ChangeAction.UPDATED
    assert row.target_name == "Network"
    assert _by_admin(row, hass_admin_user)
    assert row.details == {
        "changed": ["configured_adapters"],
        "values": {"configured_adapters": {"before": [], "after": ["eth0"]}},
    }


async def test_network_not_loaded_reads_nothing(hass: HomeAssistant) -> None:
    """The network reader needs the loaded network singleton."""
    spec = next(
        s for s in SystemSettingsCollector.WS_SPECS if s.command == "network/configure"
    )
    assert spec.read(hass, {}) is None
    hass.data["network"] = hass.loop.create_future()
    assert spec.read(hass, {}) is None
    hass.data.pop("network").cancel()


# Analytics.


async def test_analytics_toggle(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turning basic analytics on records it, False to True; nothing is sent."""
    with patch(
        "homeassistant.components.analytics.analytics.Analytics.send_analytics",
        AsyncMock(),
    ) as send:
        assert await async_setup_component(hass, "analytics", {"analytics": {}})
        await _setup_fence(hass, monkeypatch)
        _set_record_values(hass, True)

        response = await _call(
            hass, hass_ws_client, "analytics/preferences", preferences={"base": True}
        )
        await hass.async_block_till_done()

    assert response["success"] is True
    send.assert_not_called()
    (row,) = await _rows(hass)
    assert row.kind is ChangeKind.ANALYTICS
    assert row.action is ChangeAction.UPDATED
    assert row.target_name == "Analytics"
    assert _by_admin(row, hass_admin_user)
    assert row.details == {
        "changed": ["base"],
        "values": {"base": {"before": False, "after": True}},
    }


async def test_analytics_not_loaded_reads_nothing(hass: HomeAssistant) -> None:
    """The analytics reader needs the analytics component's object."""
    spec = next(
        s
        for s in SystemSettingsCollector.WS_SPECS
        if s.command == "analytics/preferences"
    )
    assert spec.read(hass, {}) is None


async def test_backup_not_loaded_reads_nothing(hass: HomeAssistant) -> None:
    """The backup config reader needs the backup manager."""
    spec = next(
        s
        for s in SystemSettingsCollector.WS_SPECS
        if s.command == "backup/config/update"
    )
    assert spec.read(hass, {}) is None


# Logger.


@pytest.fixture
async def fence_logger(
    hass: HomeAssistant, hass_admin_user: MockUser, monkeypatch: pytest.MonkeyPatch
) -> SystemSettingsCollector:
    """Electric Fence and the logger component."""
    assert await async_setup_component(hass, "logger", {})
    await _setup_fence(hass, monkeypatch)
    return _collector(hass)


async def test_logger_level(
    hass: HomeAssistant,
    fence_logger: SystemSettingsCollector,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
) -> None:
    """Debug logging for an integration records the level, before and after."""
    _set_record_values(hass, True)
    before = logging.getLevelName(
        logging.getLogger("homeassistant.components.http").getEffectiveLevel()
    )
    assert before != "DEBUG"

    response = await _call(
        hass,
        hass_ws_client,
        "logger/integration_log_level",
        integration="http",
        level="DEBUG",
        persistence="none",
    )

    assert response["success"] is True
    (row,) = await _rows(hass)
    assert row.kind is ChangeKind.LOGGER
    assert row.action is ChangeAction.UPDATED
    assert row.target_name == "Logging"
    assert _by_admin(row, hass_admin_user)
    assert row.details == {
        "changed": ["http"],
        "values": {"http": {"before": before, "after": "DEBUG"}},
    }


async def test_logger_ws_records_once(
    hass: HomeAssistant,
    fence_logger: SystemSettingsCollector,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """A module log level change is recorded exactly once."""
    _set_record_values(hass, True)
    module = "custom_components.electric_fence.test_module"
    before = logging.getLevelName(logging.getLogger(module).getEffectiveLevel())

    response = await _call(
        hass,
        hass_ws_client,
        "logger/log_level",
        module=module,
        level="CRITICAL",
        persistence="none",
    )

    assert response["success"] is True
    (row,) = await _rows(hass)
    assert row.kind is ChangeKind.LOGGER
    assert row.details == {
        "changed": [module],
        "values": {module: {"before": before, "after": "CRITICAL"}},
    }


async def test_logger_unknown_integration_records_nothing(
    hass: HomeAssistant,
    fence_logger: SystemSettingsCollector,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Core's 'Integration not found' records nothing."""
    response = await _call(
        hass,
        hass_ws_client,
        "logger/integration_log_level",
        integration="no_such_integration_xyz",
        level="DEBUG",
        persistence="none",
    )

    assert response["success"] is False
    assert await _rows(hass) == []


async def test_areas_loaded_after_fence_are_wrapped(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A component loading after Electric Fence still gets recorded."""
    await _setup_fence(hass, monkeypatch)
    assert await async_setup_component(hass, "logger", {})
    await hass.async_block_till_done()

    await _call(
        hass,
        hass_ws_client,
        "logger/integration_log_level",
        integration="http",
        level="DEBUG",
        persistence="none",
    )

    (row,) = await _rows(hass)
    assert row.details == {"changed": ["http"]}
    assert _collector(hass).disabled_reason == []


def test_logger_reader_creates_no_logger() -> None:
    """Reading a level never creates a logger; a new one inherits its parent's."""
    spec = next(
        s for s in SystemSettingsCollector.WS_SPECS if s.command == "logger/log_level"
    )
    parent = logging.getLogger("custom_components.electric_fence")
    module = "custom_components.electric_fence.no_such_module.deeper"
    parent.setLevel(logging.ERROR)

    assert spec.read({}, {"module": module}) == {module: "ERROR"}  # type: ignore[arg-type]
    assert spec.read({}, {"module": "ef_no_such_root_xyz"}) == {  # type: ignore[arg-type]
        "ef_no_such_root_xyz": logging.getLevelName(
            logging.getLogger().getEffectiveLevel()
        )
    }
    assert module not in logging.Logger.manager.loggerDict
    assert "ef_no_such_root_xyz" not in logging.Logger.manager.loggerDict
