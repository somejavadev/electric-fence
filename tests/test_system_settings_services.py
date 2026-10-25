"""The logger and recorder services: recorded after Core's handler returns.

The recorder runs through phcc's `recorder_mock` (a real recorder on an
in-memory database). Nothing here restarts Home Assistant or touches the
network.
"""

# ruff: noqa: SLF001

from collections.abc import Iterator
from dataclasses import replace
import logging
from typing import Any
from unittest.mock import Mock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from pytest_homeassistant_custom_component.typing import (
    ClientSessionGenerator,
    WebSocketGenerator,
)

from custom_components.electric_fence import attribution
from custom_components.electric_fence.collectors.system_settings import (
    SERVICE_AREA_SPECS,
    ServiceSpec,
    SystemSettingsCollector,
)
from custom_components.electric_fence.const import DOMAIN, MIN_HA_VERSION
from custom_components.electric_fence.contracts import CONTRACTS
from custom_components.electric_fence.hooks import run_service_job, wrap_service
from custom_components.electric_fence.models import ChangeAction, ChangeKind
from custom_components.electric_fence.settings import Settings
from custom_components.electric_fence.storage import ChangeRow
from homeassistant.components.recorder import Recorder
from homeassistant.components.websocket_api import DOMAIN as WEBSOCKET_API_DOMAIN
from homeassistant.core import (
    Context,
    HassJob,
    HassJobType,
    HomeAssistant,
    ServiceCall,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.recorder import DATA_INSTANCE
from homeassistant.setup import async_setup_component
from tests.helpers import change_rows, flush_worker

IP = "127.0.0.1"
KINDS = (ChangeKind.LOGGER, ChangeKind.RECORDER, ChangeKind.BACKUP)
FAKE = "ef_svc_test"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    recorder_db_url: str, enable_custom_integrations: None
) -> None:
    """Prepare the recorder's database before `hass`, as `recorder_mock` needs."""


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
    return [r for r in change_rows(hass) if r.kind in KINDS]


async def _ws_call_service(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    domain: str,
    service: str,
    token: str | None = None,
    **service_data: Any,
) -> dict[str, Any]:
    ws = await hass_ws_client(hass, token) if token else await hass_ws_client(hass)
    await ws.send_json_auto_id(
        {
            "type": "call_service",
            "domain": domain,
            "service": service,
            "service_data": service_data,
        }
    )
    response: dict[str, Any] = await ws.receive_json()
    await ws.close()
    return response


async def _ws_call(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, command: str, **fields: Any
) -> dict[str, Any]:
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": command, **fields})
    response: dict[str, Any] = await ws.receive_json()
    await ws.close()
    return response


def _by_admin(row: ChangeRow, admin: MockUser, ip: str | None = IP) -> bool:
    return (
        row.actor_user_id == admin.id
        and row.actor_name == admin.name
        and row.actor_ip == ip
    )


def _is_system(row: ChangeRow) -> bool:
    return row.actor_user_id is None and row.actor_name is None and row.actor_ip is None


def _admin_context(admin: MockUser) -> Context:
    return Context(user_id=admin.id)


# The table.


def test_service_specs_table() -> None:
    """The six services, each with its kind, action and name."""
    table = {
        f"{s.domain}.{s.service}": (s.kind, s.action, s.target_name, s.always_record)
        for s in SystemSettingsCollector.SERVICE_SPECS
    }
    assert SystemSettingsCollector.SERVICE_SPECS is SERVICE_AREA_SPECS
    assert table == {
        "logger.set_level": (
            ChangeKind.LOGGER,
            ChangeAction.UPDATED,
            "Logging",
            False,
        ),
        "logger.set_default_level": (
            ChangeKind.LOGGER,
            ChangeAction.UPDATED,
            "Logging",
            False,
        ),
        "recorder.disable": (
            ChangeKind.RECORDER,
            ChangeAction.DISABLED,
            "Recorder",
            False,
        ),
        "recorder.enable": (
            ChangeKind.RECORDER,
            ChangeAction.ENABLED,
            "Recorder",
            False,
        ),
        "recorder.purge": (ChangeKind.RECORDER, ChangeAction.PURGED, "Recorder", True),
        "recorder.purge_entities": (
            ChangeKind.RECORDER,
            ChangeAction.PURGED,
            "Recorder",
            True,
        ),
    }


def test_service_keys() -> None:
    """The parameter names the readers take from `call.data`."""
    assert {
        f"{s.domain}.{s.service}": s.service_keys
        for s in SystemSettingsCollector.SERVICE_SPECS
        if s.service_keys
    } == {
        "recorder.purge": ("keep_days", "repack", "apply_filter"),
        "recorder.purge_entities": (
            "entity_id",
            "domains",
            "entity_globs",
            "keep_days",
        ),
    }


def test_action_for() -> None:
    """A fixed action, or one chosen from the before and after readings."""
    spec = SERVICE_AREA_SPECS[0]
    assert spec.action_for({}, {}) is ChangeAction.UPDATED
    chosen = replace(
        spec,
        action=lambda before, after: (
            ChangeAction.ENABLED if after["on"] else ChangeAction.DISABLED
        ),
    )
    assert chosen.action_for({"on": False}, {"on": True}) is ChangeAction.ENABLED
    assert chosen.action_for({"on": True}, {"on": False}) is ChangeAction.DISABLED


# Recorder.


@pytest.fixture
async def fence_recorder(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    hass_admin_user: MockUser,
    monkeypatch: pytest.MonkeyPatch,
) -> SystemSettingsCollector:
    """Electric Fence and a real recorder."""
    await _setup_fence(hass, monkeypatch)
    return _collector(hass)


async def test_recorder_disable_by_admin(
    hass: HomeAssistant,
    fence_recorder: SystemSettingsCollector,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
) -> None:
    """The UI's `call_service` records the admin and the WebSocket client's IP."""
    response = await _ws_call_service(hass, hass_ws_client, "recorder", "disable")

    assert response["success"] is True
    assert hass.data[DATA_INSTANCE].enabled is False
    (row,) = await _rows(hass)
    assert row.kind is ChangeKind.RECORDER
    assert row.action is ChangeAction.DISABLED
    assert row.target_id is None
    assert row.target_name == "Recorder"
    assert _by_admin(row, hass_admin_user)
    assert row.details == {"changed": ["enabled"]}


async def test_recorder_disable_values(
    hass: HomeAssistant,
    fence_recorder: SystemSettingsCollector,
    hass_admin_user: MockUser,
) -> None:
    """With `record_values` on, the before and after state is recorded."""
    _set_record_values(hass, True)

    await hass.services.async_call(
        "recorder",
        "disable",
        {},
        blocking=True,
        context=_admin_context(hass_admin_user),
    )

    (row,) = await _rows(hass)
    assert row.details == {
        "changed": ["enabled"],
        "values": {"enabled": {"before": True, "after": False}},
    }
    # No WebSocket, request or scope: the IP is unknown.
    assert _by_admin(row, hass_admin_user, ip=None)


async def test_recorder_disable_twice_records_once(
    hass: HomeAssistant,
    fence_recorder: SystemSettingsCollector,
    hass_admin_user: MockUser,
) -> None:
    """Disabling a disabled recorder changes nothing and records nothing."""
    context = _admin_context(hass_admin_user)
    for _ in range(2):
        await hass.services.async_call(
            "recorder", "disable", {}, blocking=True, context=context
        )

    (row,) = await _rows(hass)
    assert row.action is ChangeAction.DISABLED


async def test_recorder_enable_after_disable(
    hass: HomeAssistant,
    fence_recorder: SystemSettingsCollector,
    hass_admin_user: MockUser,
) -> None:
    """Enabling again is recorded as enabled; enabling an enabled one is not."""
    context = _admin_context(hass_admin_user)
    for service in ("enable", "disable", "enable"):
        await hass.services.async_call(
            "recorder", service, {}, blocking=True, context=context
        )

    rows = await _rows(hass)
    assert [r.action for r in rows] == [ChangeAction.DISABLED, ChangeAction.ENABLED]
    assert hass.data[DATA_INSTANCE].enabled is True


async def test_recorder_purge_records_parameters(
    hass: HomeAssistant,
    fence_recorder: SystemSettingsCollector,
    hass_admin_user: MockUser,
) -> None:
    """A purge is always recorded, with the parameters Core used."""
    await hass.services.async_call(
        "recorder",
        "purge",
        {"keep_days": 30},
        blocking=True,
        context=_admin_context(hass_admin_user),
    )
    _set_record_values(hass, True)
    await hass.services.async_call(
        "recorder",
        "purge",
        {"keep_days": 30, "repack": True},
        blocking=True,
        context=_admin_context(hass_admin_user),
    )
    await async_wait_recording_done(hass)

    names, values = await _rows(hass)
    for row in (names, values):
        assert row.kind is ChangeKind.RECORDER
        assert row.action is ChangeAction.PURGED
        assert row.target_name == "Recorder"
        assert _by_admin(row, hass_admin_user, ip=None)
    # Core's schema fills in the defaults.
    assert names.details == {"changed": ["apply_filter", "keep_days", "repack"]}
    assert values.details == {
        "changed": ["apply_filter", "keep_days", "repack"],
        "values": {
            "apply_filter": {"before": None, "after": False},
            "keep_days": {"before": None, "after": 30},
            "repack": {"before": None, "after": True},
        },
    }


async def test_recorder_purge_without_keep_days(
    hass: HomeAssistant,
    fence_recorder: SystemSettingsCollector,
    hass_admin_user: MockUser,
) -> None:
    """A parameter not given (Core uses its setting) is left out."""
    await hass.services.async_call(
        "recorder", "purge", {}, blocking=True, context=_admin_context(hass_admin_user)
    )
    await async_wait_recording_done(hass)

    (row,) = await _rows(hass)
    assert row.details == {"changed": ["apply_filter", "repack"]}


async def test_recorder_purge_entities_records_entity_ids(
    hass: HomeAssistant,
    fence_recorder: SystemSettingsCollector,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
) -> None:
    """Entity IDs given as a target are recorded (through redaction)."""
    _set_record_values(hass, True)
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id(
        {
            "type": "call_service",
            "domain": "recorder",
            "service": "purge_entities",
            "target": {"entity_id": ["sensor.kitchen"]},
            "service_data": {"keep_days": 2},
        }
    )
    response = await ws.receive_json()
    await ws.close()
    await async_wait_recording_done(hass)

    assert response["success"] is True
    (row,) = await _rows(hass)
    assert row.action is ChangeAction.PURGED
    assert _by_admin(row, hass_admin_user)
    assert row.details == {
        "changed": ["domains", "entity_globs", "entity_id", "keep_days"],
        "values": {
            "domains": {"before": None, "after": []},
            "entity_globs": {"before": None, "after": []},
            "entity_id": {"before": None, "after": ["sensor.kitchen"]},
            "keep_days": {"before": None, "after": 2},
        },
    }


async def test_service_from_automation_is_system(
    hass: HomeAssistant, fence_recorder: SystemSettingsCollector
) -> None:
    """A call without a user (automation, script, integration) is System."""
    await hass.services.async_call(
        "recorder", "disable", {}, blocking=True, context=Context()
    )

    (row,) = await _rows(hass)
    assert row.action is ChangeAction.DISABLED
    assert _is_system(row)


async def test_service_without_context_is_system(
    hass: HomeAssistant, fence_recorder: SystemSettingsCollector
) -> None:
    """Core makes a context without a user when none is given."""
    await hass.services.async_call("recorder", "disable", {}, blocking=True)

    (row,) = await _rows(hass)
    assert _is_system(row)


async def test_non_blocking_call_records(
    hass: HomeAssistant,
    fence_recorder: SystemSettingsCollector,
    hass_admin_user: MockUser,
) -> None:
    """A call that runs in the background is recorded once it finishes."""
    await hass.services.async_call(
        "recorder", "disable", {}, context=_admin_context(hass_admin_user)
    )

    (row,) = await _rows(hass)
    assert _by_admin(row, hass_admin_user, ip=None)


async def test_non_admin_records_nothing(
    hass: HomeAssistant,
    fence_recorder: SystemSettingsCollector,
    hass_read_only_user: MockUser,
) -> None:
    """Core rejects a non-admin; the exception passes through, nothing is recorded."""
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            "recorder",
            "disable",
            {},
            blocking=True,
            context=Context(user_id=hass_read_only_user.id),
        )

    assert hass.data[DATA_INSTANCE].enabled is True
    assert await _rows(hass) == []


async def test_non_admin_ws_records_nothing(
    hass: HomeAssistant,
    fence_recorder: SystemSettingsCollector,
    hass_ws_client: WebSocketGenerator,
    hass_read_only_access_token: str,
) -> None:
    """The client gets Core's own refusal; nothing is recorded."""
    response = await _ws_call_service(
        hass, hass_ws_client, "recorder", "disable", hass_read_only_access_token
    )

    assert response["success"] is False
    # Core answers `Unauthorized` from `call_service` as a Home Assistant error.
    assert response["error"]["code"] == "home_assistant_error"
    assert await _rows(hass) == []


async def test_failed_service_records_nothing(
    hass: HomeAssistant,
    fence_recorder: SystemSettingsCollector,
    hass_admin_user: MockUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core's handler raising: the same exception propagates, nothing is recorded."""
    err = HomeAssistantError("disk full")

    def _raise(enable: bool) -> None:
        hass.data[DATA_INSTANCE].enabled = enable
        raise err

    monkeypatch.setattr(hass.data[DATA_INSTANCE], "set_enable", _raise)

    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            "recorder",
            "disable",
            {},
            blocking=True,
            context=_admin_context(hass_admin_user),
        )

    assert raised.value is err
    assert await _rows(hass) == []


async def test_recorder_ws_error_is_cores(
    hass: HomeAssistant,
    fence_recorder: SystemSettingsCollector,
    hass_ws_client: WebSocketGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over the WebSocket, the client gets Core's error; nothing is recorded."""

    def _raise(enable: bool) -> None:
        raise HomeAssistantError("disk full")

    monkeypatch.setattr(hass.data[DATA_INSTANCE], "set_enable", _raise)

    response = await _ws_call_service(hass, hass_ws_client, "recorder", "disable")

    assert response["success"] is False
    assert response["error"]["code"] == "home_assistant_error"
    assert response["error"]["message"] == "disk full"
    assert await _rows(hass) == []


async def test_reader_failure_fails_open(
    hass: HomeAssistant,
    fence_recorder: SystemSettingsCollector,
    hass_admin_user: MockUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reader raising is swallowed: Core's service runs, nothing is recorded."""
    collector = fence_recorder
    instance = hass.data.pop(DATA_INSTANCE)

    class _Broken:
        @property
        def enabled(self) -> bool:
            raise RuntimeError("boom")

        def set_enable(self, enable: bool) -> None:
            instance.set_enable(enable)

    hass.data[DATA_INSTANCE] = _Broken()  # type: ignore[assignment]
    try:
        await hass.services.async_call(
            "recorder",
            "disable",
            {},
            blocking=True,
            context=_admin_context(hass_admin_user),
        )
    finally:
        hass.data[DATA_INSTANCE] = instance

    assert instance.enabled is False
    assert collector.guard.errors == 1
    assert await _rows(hass) == []


async def test_after_failure_fails_open(
    hass: HomeAssistant,
    fence_recorder: SystemSettingsCollector,
    hass_admin_user: MockUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recording failing after Core's handler returned is swallowed."""
    collector = fence_recorder
    monkeypatch.setattr(collector.runtime.worker, "submit", Mock(side_effect=OSError))

    await hass.services.async_call(
        "recorder",
        "disable",
        {},
        blocking=True,
        context=_admin_context(hass_admin_user),
    )

    assert hass.data[DATA_INSTANCE].enabled is False
    assert collector.guard.errors == 1


async def test_recorder_missing_records_nothing(
    hass: HomeAssistant,
    fence_recorder: SystemSettingsCollector,
    hass_admin_user: MockUser,
) -> None:
    """The enabled reader gives None without a recorder instance."""
    spec = next(s for s in SERVICE_AREA_SPECS if s.service == "disable")
    call = ServiceCall(hass, "recorder", "disable", {})
    instance = hass.data.pop(DATA_INSTANCE)
    try:
        assert spec.read(hass, call) is None
    finally:
        hass.data[DATA_INSTANCE] = instance
    assert spec.read(hass, call) == {"enabled": True}


# IP sources.


async def test_ip_from_rest_api_request(
    hass: HomeAssistant,
    fence_recorder: SystemSettingsCollector,
    hass_client: ClientSessionGenerator,
    hass_admin_user: MockUser,
) -> None:
    """A REST API call has the request's IP."""
    assert await async_setup_component(hass, "api", {})
    client = await hass_client()

    resp = await client.post("/api/services/recorder/disable", json={})

    assert resp.status == 200
    (row,) = await _rows(hass)
    assert _by_admin(row, hass_admin_user)


async def test_ip_from_action_scope(
    hass: HomeAssistant,
    fence_recorder: SystemSettingsCollector,
    hass_admin_user: MockUser,
) -> None:
    """An admin action scope of the same user gives its IP."""
    with attribution.open_scope(
        user=hass_admin_user,
        ip="10.9.8.7",
        action=attribution.ACTION_WS,
        details={},
        owner=None,
    ):
        await hass.services.async_call(
            "recorder",
            "disable",
            {},
            blocking=True,
            context=_admin_context(hass_admin_user),
        )

    (row,) = await _rows(hass)
    assert _by_admin(row, hass_admin_user, ip="10.9.8.7")


async def test_ip_not_taken_from_another_users_scope(
    hass: HomeAssistant,
    fence_recorder: SystemSettingsCollector,
    hass_admin_user: MockUser,
    hass_owner_user: MockUser,
) -> None:
    """The IP of a scope, connection or request of someone else is not used."""
    with attribution.open_scope(
        user=hass_owner_user,
        ip="10.9.8.7",
        action=attribution.ACTION_WS,
        details={},
        owner=None,
    ):
        await hass.services.async_call(
            "recorder",
            "disable",
            {},
            blocking=True,
            context=_admin_context(hass_admin_user),
        )

    (row,) = await _rows(hass)
    assert _by_admin(row, hass_admin_user, ip=None)


def test_call_ip_sources(
    hass: HomeAssistant, hass_admin_user: MockUser, hass_owner_user: MockUser
) -> None:
    """Scope first, then the WebSocket connection, then the request."""
    from homeassistant.components.http.const import KEY_HASS_USER  # noqa: PLC0415
    from homeassistant.components.websocket_api import current_connection  # noqa: PLC0415
    from homeassistant.helpers.http import current_request  # noqa: PLC0415

    connection = Mock(remote="10.0.0.2", user=hass_admin_user)
    request = Mock(remote="10.0.0.3")
    request.get = {KEY_HASS_USER: hass_admin_user}.get
    other_request = Mock(remote="10.0.0.4")
    other_request.get = {KEY_HASS_USER: hass_owner_user}.get
    anonymous = Mock(remote="10.0.0.5")
    anonymous.get = {}.get

    assert attribution.call_ip(hass_admin_user.id) is None
    token = current_request.set(request)
    try:
        assert attribution.call_ip(hass_admin_user.id) == "10.0.0.3"
        conn_token = current_connection.set(connection)
        try:
            assert attribution.call_ip(hass_admin_user.id) == "10.0.0.2"
            # Another user's connection: fall back to the request.
            assert attribution.call_ip(hass_owner_user.id) is None
        finally:
            current_connection.reset(conn_token)
        conn_token = current_connection.set(Mock(remote="10.0.0.6", user=None))
        try:
            assert attribution.call_ip(hass_admin_user.id) == "10.0.0.3"
        finally:
            current_connection.reset(conn_token)
    finally:
        current_request.reset(token)
    for req in (other_request, anonymous):
        token = current_request.set(req)
        try:
            assert attribution.call_ip(hass_admin_user.id) is None
        finally:
            current_request.reset(token)


# Logger.


@pytest.fixture
async def fence_logger(
    hass: HomeAssistant, hass_admin_user: MockUser, monkeypatch: pytest.MonkeyPatch
) -> SystemSettingsCollector:
    """Electric Fence and the logger component."""
    assert await async_setup_component(hass, "logger", {})
    await _setup_fence(hass, monkeypatch)
    return _collector(hass)


async def test_logger_set_level(
    hass: HomeAssistant,
    fence_logger: SystemSettingsCollector,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
) -> None:
    """`logger.set_level` records each named logger's level, before and after."""
    _set_record_values(hass, True)
    module = "custom_components.electric_fence.svc_module"
    before = logging.getLevelName(logging.getLogger(module).getEffectiveLevel())
    assert before != "CRITICAL"

    response = await _ws_call_service(
        hass, hass_ws_client, "logger", "set_level", **{module: "critical"}
    )

    assert response["success"] is True
    (row,) = await _rows(hass)
    assert row.kind is ChangeKind.LOGGER
    assert row.action is ChangeAction.UPDATED
    assert row.target_name == "Logging"
    assert _by_admin(row, hass_admin_user)
    assert row.details == {
        "changed": [module],
        "values": {module: {"before": before, "after": "CRITICAL"}},
    }


async def test_logger_set_level_unchanged_records_nothing(
    hass: HomeAssistant,
    fence_logger: SystemSettingsCollector,
    hass_admin_user: MockUser,
) -> None:
    """Setting a logger to the level it has records nothing."""
    module = "custom_components.electric_fence.svc_same"
    logging.getLogger(module).setLevel(logging.WARNING)

    await hass.services.async_call(
        "logger",
        "set_level",
        {module: "warning"},
        blocking=True,
        context=_admin_context(hass_admin_user),
    )

    assert await _rows(hass) == []


async def test_logger_set_default_level(
    hass: HomeAssistant,
    fence_logger: SystemSettingsCollector,
    hass_admin_user: MockUser,
) -> None:
    """`logger.set_default_level` records the root level."""
    _set_record_values(hass, True)
    before = logging.getLevelName(logging.getLogger().level)
    assert before != "CRITICAL"

    await hass.services.async_call(
        "logger",
        "set_default_level",
        {"level": "critical"},
        blocking=True,
        context=_admin_context(hass_admin_user),
    )

    (row,) = await _rows(hass)
    assert row.kind is ChangeKind.LOGGER
    assert row.details == {
        "changed": ["default"],
        "values": {"default": {"before": before, "after": "CRITICAL"}},
    }


async def test_logger_ws_log_level_records_once(
    hass: HomeAssistant,
    fence_logger: SystemSettingsCollector,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """`logger/log_level` calls Core's helpers, not the service: one record."""
    services = hass.services._services["logger"]
    assert services["set_level"].job.target.__name__ == "service_wrapper"
    module = "custom_components.electric_fence.svc_ws_module"

    response = await _ws_call(
        hass,
        hass_ws_client,
        "logger/log_level",
        module=module,
        level="CRITICAL",
        persistence="none",
    )

    assert response["success"] is True
    (row,) = await _rows(hass)
    assert row.details == {"changed": [module]}


# Loading order, uninstall and the contract.


async def test_services_loaded_after_fence_are_wrapped(
    hass: HomeAssistant,
    hass_admin_user: MockUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A component loading after Electric Fence still gets its services recorded."""
    await _setup_fence(hass, monkeypatch)
    assert "logger" in _collector(hass)._waiting
    assert await async_setup_component(hass, "logger", {})
    await hass.async_block_till_done()

    await hass.services.async_call(
        "logger",
        "set_default_level",
        {"level": "critical"},
        blocking=True,
        context=_admin_context(hass_admin_user),
    )

    (row,) = await _rows(hass)
    assert row.details == {"changed": ["default"]}
    assert _collector(hass).disabled_reason == []


async def test_uninstall_restores_service_jobs(
    hass: HomeAssistant,
    hass_admin_user: MockUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unloading puts Core's jobs back on the services."""
    assert await async_setup_component(hass, "logger", {})
    services = hass.services._services["logger"]
    originals = {name: service.job for name, service in services.items()}
    entry = await _setup_fence(hass, monkeypatch)
    collector = _collector(hass)
    assert all(services[name].job is not job for name, job in originals.items())

    assert await hass.config_entries.async_unload(entry.entry_id)

    assert {name: service.job for name, service in services.items()} == originals
    assert collector._wrapped_services == set()


def _services_check(hass: HomeAssistant) -> str | None:
    return CONTRACTS["system_settings_services"].check(hass)


async def test_services_contract_passes(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """With logger and recorder loaded, every service and job is as expected."""
    assert await async_setup_component(hass, "logger", {})
    contract = CONTRACTS["system_settings_services"]
    # Its own collector name: a failure disables the service part only.
    assert contract.collector == "system_settings_services"
    assert _services_check(hass) is None


async def test_services_contract_skips_components_not_loaded(
    hass: HomeAssistant,
) -> None:
    """Services of components not loaded yet are checked when they load."""
    assert _services_check(hass) is None


async def test_services_contract_fails_without_services_mapping(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registry's private mapping is pinned."""
    monkeypatch.setattr(hass.services, "_services", None)
    assert _services_check(hass) == "ServiceRegistry._services is not a mapping"


async def test_services_contract_fails_for_unregistered_service(
    hass: HomeAssistant,
) -> None:
    """A loaded component missing a service fails the contract."""
    assert await async_setup_component(hass, "logger", {})
    hass.services.async_remove("logger", "set_level")
    assert _services_check(hass) == "services not registered: logger.set_level"


async def test_services_contract_fails_for_other_service_object(
    hass: HomeAssistant,
) -> None:
    """The registered object must be Core's `Service` with a `HassJob`."""
    assert await async_setup_component(hass, "logger", {})
    services = hass.services._services["logger"]
    service = services["set_level"]
    services["set_level"] = object()  # type: ignore[assignment]
    assert _services_check(hass) == "logger.set_level is not a Service"
    services["set_level"] = service
    original = service.job
    service.job = object()  # type: ignore[assignment]
    try:
        assert _services_check(hass) == "logger.set_level: Service.job is not a HassJob"
    finally:
        service.job = original


async def test_services_contract_fails_for_renamed_parameter(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """A parameter renamed in Core's service schema fails the contract."""
    import probatio  # noqa: PLC0415

    service = hass.services._services["recorder"]["purge"]
    original = service.schema
    service.schema = probatio.Schema({probatio.Optional("keep_days"): int})
    try:
        assert _services_check(hass) == (
            "recorder.purge: service fields missing: repack, apply_filter"
        )
        service.schema = None
        assert _services_check(hass) == (
            "recorder.purge: the service schema has no field names"
        )
    finally:
        service.schema = original


async def test_services_contract_fails_without_recorder_instance(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """The recorder instance with its `enabled` flag is pinned."""
    instance = hass.data.pop(DATA_INSTANCE)
    try:
        assert _services_check(hass) == "recorder: the recorder instance is missing"
        hass.data[DATA_INSTANCE] = object()  # type: ignore[assignment]
        assert _services_check(hass) == "recorder: Recorder.enabled is missing"
    finally:
        hass.data[DATA_INSTANCE] = instance


async def test_services_contract_fails_without_execute_service(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core runs `Service.job` from `ServiceRegistry._execute_service`."""
    from homeassistant.core import ServiceRegistry  # noqa: PLC0415

    monkeypatch.delattr(ServiceRegistry, "_execute_service")
    assert _services_check(hass) == "ServiceRegistry._execute_service is missing"


async def test_failed_services_contract_keeps_websocket_part(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the service wrapping is disabled; the repair issue is raised."""
    assert await async_setup_component(hass, "logger", {})
    services = hass.services._services["logger"]
    originals = {name: service.job for name, service in services.items()}
    hass.services.async_remove("logger", "set_level")

    await _setup_fence(hass, monkeypatch)
    collector = _collector(hass)

    assert collector.is_installed
    assert collector.disabled_reason == [
        "services: services not registered: logger.set_level"
    ]
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, "collector_disabled_system_settings"
    )
    assert issue is not None
    assert services["set_default_level"].job is originals["set_default_level"]
    # The WebSocket commands are still recorded.
    response = await _ws_call(
        hass,
        hass_ws_client,
        "logger/integration_log_level",
        integration="http",
        level="DEBUG",
        persistence="none",
    )
    assert response["success"] is True
    (row,) = await _rows(hass)
    assert row.details == {"changed": ["http"]}


async def test_late_services_contract_failure_unwraps_services(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    hass_admin_user: MockUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A component loading late that breaks the contract unwraps the services."""
    entry = await _setup_fence(hass, monkeypatch)
    collector = _collector(hass)
    recorder_services = hass.services._services["recorder"]
    assert recorder_services["disable"].job.target.__name__ == "service_wrapper"

    # Core's logger loads without one of its services.
    def _no_set_level(hass: HomeAssistant) -> None:
        from homeassistant.components.logger import services  # noqa: PLC0415

        services.async_register_admin_service(
            hass,
            "logger",
            "set_default_level",
            services._async_service_handler,
            schema=services.SERVICE_SET_DEFAULT_LEVEL_SCHEMA,
        )

    monkeypatch.setattr(
        "homeassistant.components.logger.async_setup_services", _no_set_level
    )
    assert await async_setup_component(hass, "logger", {})
    await hass.async_block_till_done()

    assert collector.is_installed
    assert collector.disabled_reason == [
        "services: services not registered: logger.set_level"
    ]
    assert collector._wrapped_services == set()
    assert (
        getattr(recorder_services["disable"].job.target, "__name__", None)
        != "service_wrapper"
    )
    assert hass.data[WEBSOCKET_API_DOMAIN]["logger/log_level"][0].__wrapped__
    # Another late component neither wraps services nor raises again.
    collector._on_component_loaded(Mock(data={"component": "analytics"}))
    assert collector._wrapped_services == set()

    await hass.services.async_call(
        "recorder",
        "disable",
        {},
        blocking=True,
        context=_admin_context(hass_admin_user),
    )
    assert await _rows(hass) == []
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_services_contract_raising_disables_services(
    hass: HomeAssistant,
    hass_admin_user: MockUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A contract check that raises counts as a failure of the services part."""
    from custom_components.electric_fence.contracts import Contract  # noqa: PLC0415

    def _boom(hass: HomeAssistant) -> str | None:
        raise RuntimeError("boom")

    monkeypatch.setitem(
        CONTRACTS,
        "system_settings_services",
        Contract("system_settings_services", "system_settings_services", _boom),
    )
    await _setup_fence(hass, monkeypatch)

    assert _collector(hass).disabled_reason == [
        "services: system_settings_services check raised RuntimeError"
    ]


# The hook.


async def _job_service(hass: HomeAssistant, job_type: HassJobType) -> list[Any]:
    """Register `ef_svc_test.run` with a handler of the given job type."""
    calls: list[Any] = []

    async def coro(call: ServiceCall) -> Any:
        calls.append(call.data["n"])
        return {"n": call.data["n"]}

    @callback
    def cb(call: ServiceCall) -> Any:
        calls.append(call.data["n"])
        return {"n": call.data["n"]}

    def sync(call: ServiceCall) -> Any:
        calls.append(call.data["n"])
        return {"n": call.data["n"]}

    handler = {
        HassJobType.Coroutinefunction: coro,
        HassJobType.Callback: cb,
        HassJobType.Executor: sync,
    }[job_type]
    from homeassistant.core import SupportsResponse  # noqa: PLC0415

    hass.services.async_register(
        FAKE, "run", handler, supports_response=SupportsResponse.OPTIONAL
    )
    assert hass.services._services[FAKE]["run"].job.job_type is job_type
    return calls


@pytest.mark.parametrize(
    "job_type",
    [HassJobType.Coroutinefunction, HassJobType.Callback, HassJobType.Executor],
)
async def test_wrap_service_runs_each_job_type(
    hass: HomeAssistant, job_type: HassJobType
) -> None:
    """The wrapper runs Core's job as Core would and returns its response."""
    calls = await _job_service(hass, job_type)
    seen: list[Any] = []

    def make(original: Any) -> Any:
        async def wrapper(call: ServiceCall) -> Any:
            seen.append("before")
            result = await original(call)
            seen.append(result)
            return result

        return wrapper

    handle = wrap_service(hass, FAKE, "run", make)
    response = await hass.services.async_call(
        FAKE, "run", {"n": 1}, blocking=True, return_response=True
    )

    assert response == {"n": 1}
    assert calls == [1]
    assert seen == ["before", {"n": 1}]
    handle.restore()
    assert hass.services._services[FAKE]["run"].job.job_type is job_type


async def test_run_service_job_matches_core(hass: HomeAssistant) -> None:
    """`run_service_job` runs a job the way Core's `_execute_service` does."""
    calls: list[int] = []

    def sync(call: ServiceCall) -> int:
        calls.append(1)
        return 7

    call = ServiceCall(hass, FAKE, "run", {})
    assert await run_service_job(hass, HassJob(sync), call) == 7
    assert calls == [1]


async def test_wrap_service_restore_order(hass: HomeAssistant) -> None:
    """Restoring under a later wrapper leaves ours as a passthrough."""
    calls = await _job_service(hass, HassJobType.Coroutinefunction)
    service = hass.services._services[FAKE]["run"]
    original = service.job
    seen: list[str] = []

    def make(name: str) -> Any:
        def build(original: Any) -> Any:
            async def wrapper(call: ServiceCall) -> Any:
                seen.append(name)
                return await original(call)

            return wrapper

        return build

    inner = wrap_service(hass, FAKE, "run", make("inner"))
    outer = wrap_service(hass, FAKE, "run", make("outer"))
    inner.restore()
    await hass.services.async_call(FAKE, "run", {"n": 1}, blocking=True)
    assert seen == ["outer"]
    outer.restore()
    # The outer put back the inner job, now a passthrough.
    assert service.job is not original
    await hass.services.async_call(FAKE, "run", {"n": 2}, blocking=True)
    assert seen == ["outer"]
    assert calls == [1, 2]

    service.job = original
    handle = wrap_service(hass, FAKE, "run", make("again"))
    handle.restore()
    handle.restore()
    assert service.job is original


async def test_wrapper_passes_exception_and_cancel(hass: HomeAssistant) -> None:
    """Any exception of Core's job, cancellation included, passes unchanged."""
    import asyncio  # noqa: PLC0415

    errors = [ValueError("x"), asyncio.CancelledError()]

    async def coro(call: ServiceCall) -> None:
        raise errors[call.data["i"]]

    hass.services.async_register(FAKE, "fail", coro)
    collector = SystemSettingsCollector(hass, Mock())
    spec = ServiceSpec(
        FAKE,
        "fail",
        ChangeKind.LOGGER,
        ChangeAction.UPDATED,
        "Test",
        lambda hass, call: {"a": 1},
        always_record=True,
    )
    handle = wrap_service(hass, FAKE, "fail", collector._wrap_service(spec))
    for i, err in enumerate(errors):
        with pytest.raises(type(err)) as raised:
            await hass.services._execute_service(
                hass.services._services[FAKE]["fail"],
                ServiceCall(hass, FAKE, "fail", {"i": i}),
            )
        assert raised.value is err
    assert not collector.runtime.worker.submit.called
    handle.restore()


async def test_wrapper_reader_none_records_nothing(hass: HomeAssistant) -> None:
    """A reader giving None before or after records nothing."""

    async def coro(call: ServiceCall) -> None:
        hass.data[f"{FAKE}_after"] = call.data.get("after")

    hass.services.async_register(FAKE, "maybe", coro)
    collector = SystemSettingsCollector(hass, Mock())
    collector.runtime.settings.record_values = False

    def read(hass: HomeAssistant, call: ServiceCall) -> Any:
        if f"{FAKE}_after" not in hass.data:
            return call.data.get("before")
        return hass.data[f"{FAKE}_after"]

    spec = ServiceSpec(
        FAKE, "maybe", ChangeKind.LOGGER, ChangeAction.UPDATED, "Test", read
    )
    handle = wrap_service(hass, FAKE, "maybe", collector._wrap_service(spec))
    # Before {"a": 1}, after None.
    await hass.services.async_call(FAKE, "maybe", {"before": {"a": 1}}, blocking=True)
    hass.data.pop(f"{FAKE}_after")
    # Before None.
    await hass.services.async_call(FAKE, "maybe", {}, blocking=True)
    hass.data.pop(f"{FAKE}_after")
    await hass.services.async_call(
        FAKE, "maybe", {"before": {"a": 1}, "after": {"a": 2}}, blocking=True
    )
    handle.restore()

    (change,) = [c.args[0] for c in collector.runtime.worker.submit.call_args_list]
    assert change.details == {"changed": ["a"]}


async def test_failed_ws_contract_wraps_no_services(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The WebSocket contract failing disables everything, services included."""
    assert await async_setup_component(hass, "logger", {})
    services = hass.services._services["logger"]
    originals = {name: service.job for name, service in services.items()}
    del hass.data[WEBSOCKET_API_DOMAIN]["logger/log_level"]
    collector = SystemSettingsCollector(hass, Mock())

    assert collector.install() is False

    assert collector.disabled_reason == [
        "WebSocket commands not registered: logger/log_level"
    ]
    assert {name: service.job for name, service in services.items()} == originals


async def test_unwrap_survives_failing_restore(hass: HomeAssistant) -> None:
    """A service handle failing to restore is counted, the rest still restored."""
    assert await async_setup_component(hass, "logger", {})
    collector = SystemSettingsCollector(hass, Mock())
    assert collector.install()
    broken = Mock()
    broken.restore.side_effect = RuntimeError("boom")
    collector._service_handles.insert(0, broken)

    collector.uninstall()

    assert collector.guard.errors == 1
    services = hass.services._services["logger"]
    assert all(
        getattr(s.job.target, "__name__", None) != "service_wrapper"
        for s in services.values()
    )
