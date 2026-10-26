"""App changes made through Home Assistant (Supervisor installs only).

Core's real `supervisor/api` and `hassio/update/addon` commands and app
services run against a stand-in Supervisor client (`tests.helpers`).
"""

import asyncio
from dataclasses import replace
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser
from pytest_homeassistant_custom_component.typing import (
    ClientSessionGenerator,
    WebSocketGenerator,
)

from custom_components.electric_fence import COLLECTOR_TYPES
from custom_components.electric_fence.collectors import apps
from custom_components.electric_fence.collectors.apps import (
    AppCollector,
    AppOp,
    classify,
)
from custom_components.electric_fence.const import DOMAIN, MIN_HA_VERSION
from custom_components.electric_fence.contracts import CONTRACTS, Contract
from custom_components.electric_fence.models import ChangeAction, ChangeKind
from custom_components.electric_fence.settings import Settings
from custom_components.electric_fence.storage import ChangeRow
from homeassistant.components.hassio.handler import HassioAPIError
from homeassistant.components.websocket_api import DOMAIN as WEBSOCKET_API_DOMAIN
from homeassistant.core import Context, HomeAssistant, ServiceCall
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.setup import async_setup_component
from tests.helpers import (
    change_rows,
    flush_worker,
    make_runtime,
    setup_fake_hassio,
    supervisor_client,
)

IP = "127.0.0.1"
A = ChangeAction
REPO = "https://github.com/x/apps"


def _info(
    *,
    version: str | None = "9.0",
    name: str = "Mosquitto broker",
    options: dict[str, Any] | None = None,
    **settings: Any,
) -> SimpleNamespace:
    """Return what `addons.addon_info(slug)` returns, as far as we read it."""
    fields: dict[str, Any] = {
        "boot": "auto",
        "watchdog": False,
        "auto_update": False,
        "ingress_panel": True,
        "network": None,
        "audio_input": None,
        "audio_output": None,
        "protected": True,
    }
    fields.update(settings)
    return SimpleNamespace(name=name, version=version, options=options or {}, **fields)


@pytest.fixture
def client() -> Mock:
    """Return the Supervisor client both Core and the collector use."""
    found = supervisor_client()
    found.addons.addon_info.return_value = _info()
    return found


@pytest.fixture
async def send(
    hass: HomeAssistant, client: Mock, monkeypatch: pytest.MonkeyPatch
) -> AsyncMock:
    """Core's Supervisor commands and services, before Electric Fence."""
    return await setup_fake_hassio(hass, client, monkeypatch)


@pytest.fixture
async def entry(
    hass: HomeAssistant, send: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> MockConfigEntry:
    """Electric Fence, set up after the Supervisor commands are registered."""
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", MIN_HA_VERSION)
    found = MockConfigEntry(domain=DOMAIN, options=Settings().to_options())
    found.add_to_hass(hass)
    assert await hass.config_entries.async_setup(found.entry_id)
    await hass.async_block_till_done()
    return found


def _collector(entry: MockConfigEntry) -> AppCollector:
    return next(c for c in entry.runtime_data.collectors if isinstance(c, AppCollector))


def _set_record_values(entry: MockConfigEntry, value: bool) -> None:
    runtime = entry.runtime_data
    runtime.settings = replace(runtime.settings, record_values=value)


async def _rows(hass: HomeAssistant) -> list[ChangeRow]:
    await hass.async_block_till_done()
    await flush_worker(hass)
    return [r for r in change_rows(hass) if r.kind == ChangeKind.APP]


async def _api(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    endpoint: str,
    method: str = "post",
    data: dict[str, Any] | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    ws = await hass_ws_client(hass, token) if token else await hass_ws_client(hass)
    msg: dict[str, Any] = {
        "type": "supervisor/api",
        "endpoint": endpoint,
        "method": method,
    }
    if data is not None:
        msg["data"] = data
    await ws.send_json_auto_id(msg)
    response: dict[str, Any] = await ws.receive_json()
    return response


_TABLE: list[tuple[str, str, dict[str, Any] | None, AppOp | None]] = [
    (
        "POST",
        "/store/addons/core_ssh/install",
        None,
        AppOp(A.INSTALLED, "core_ssh", None, ()),
    ),
    (
        "POST",
        "/addons/core_ssh/uninstall",
        None,
        AppOp(A.UNINSTALLED, "core_ssh", None, ()),
    ),
    (
        "POST",
        "/store/addons/core_ssh/update",
        None,
        AppOp(A.UPDATED, "core_ssh", None, ()),
    ),
    ("POST", "/addons/core_ssh/update", None, AppOp(A.UPDATED, "core_ssh", None, ())),
    ("POST", "/addons/core_ssh/start", None, AppOp(A.STARTED, "core_ssh", None, ())),
    ("POST", "/addons/core_ssh/stop", None, AppOp(A.STOPPED, "core_ssh", None, ())),
    (
        "POST",
        "/addons/core_ssh/restart",
        None,
        AppOp(A.RESTARTED, "core_ssh", None, ()),
    ),
    ("POST", "/addons/core_ssh/rebuild", None, AppOp(A.REBUILT, "core_ssh", None, ())),
    (
        "POST",
        "/addons/core_ssh/options",
        {"options": {"a": 1}},
        AppOp(A.OPTIONS_CHANGED, "core_ssh", None, ("options",)),
    ),
    (
        "POST",
        "/addons/core_ssh/options",
        {"boot": "manual", "watchdog": True},
        AppOp(A.SETTINGS_CHANGED, "core_ssh", None, ("boot", "watchdog")),
    ),
    (
        "POST",
        "/addons/core_ssh/options",
        {"audio_input": "x"},
        AppOp(A.SETTINGS_CHANGED, "core_ssh", None, ("audio_input",)),
    ),
    (
        "POST",
        "/addons/core_ssh/security",
        {"protected": False},
        AppOp(A.SETTINGS_CHANGED, "core_ssh", None, ("protected",)),
    ),
    (
        "POST",
        "/store/repositories",
        {"repository": REPO},
        AppOp(A.REPOSITORY_ADDED, None, REPO, ()),
    ),
    (
        "DELETE",
        "/store/repositories/a0d7b954",
        None,
        AppOp(A.REPOSITORY_REMOVED, None, "a0d7b954", ()),
    ),
    (
        "post",
        "/addons/core_ssh/restart/",
        None,
        AppOp(A.RESTARTED, "core_ssh", None, ()),
    ),
    ("GET", "/addons/core_ssh/info", None, None),
    ("POST", "/addons/core_ssh/info", None, None),
    (
        "POST",
        "/addons/core_ssh/install",
        None,
        AppOp(A.INSTALLED, "core_ssh", None, ()),
    ),
    (
        "POST",
        "/store/addons/core_ssh/install",
        {"background": True},
        AppOp(A.INSTALLED, "core_ssh", None, (), background=True),
    ),
    (
        "POST",
        "/store/addons/core_ssh/update",
        {"background": True, "backup": False},
        AppOp(A.UPDATED, "core_ssh", None, (), background=True),
    ),
    (
        "POST",
        "/addons/core_ssh/restart",
        {"background": True},
        AppOp(A.RESTARTED, "core_ssh", None, ()),
    ),
    ("POST", "/addons/core_ssh/options/validate", {"options": {}}, None),
    ("POST", "/addons/core_ssh/logs", None, None),
    ("GET", "/addons/core_ssh/start", None, None),
    ("GET", "/store/repositories", None, None),
    ("POST", "/store/repositories", {"other": 1}, None),
    ("POST", "/core/restart", None, None),
    ("POST", "/addons/a/b/start", None, None),
]


@pytest.mark.parametrize(("method", "endpoint", "data", "expected"), _TABLE)
def test_classify_table(
    method: str, endpoint: str, data: dict[str, Any] | None, expected: AppOp | None
) -> None:
    """Every mutating app endpoint maps to its action; anything else is None."""
    assert classify(endpoint, method, data) == expected


def test_classify_without_body() -> None:
    """Without a body, options and repository adds are decided from state."""
    assert classify("/addons/x/options", "POST", None) == AppOp(
        A.OPTIONS_CHANGED, "x", None, ()
    )
    assert classify("/store/repositories", "POST", None) == AppOp(
        A.REPOSITORY_ADDED, None, None, ()
    )


async def test_ws_install_recorded_with_user(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    send: AsyncMock,
    client: Mock,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
) -> None:
    """An admin installs an app over `supervisor/api`: recorded with the user."""
    response = await _api(hass, hass_ws_client, "/store/addons/core_ssh/install")
    assert response["success"] is True
    send.assert_awaited_once()
    [row] = await _rows(hass)
    assert row.kind == "app"
    assert row.action == "installed"
    assert row.target_id == "core_ssh"
    assert row.target_name == "Mosquitto broker"
    assert row.actor_user_id == hass_admin_user.id
    assert row.actor_ip == IP
    assert row.details == {"version_before": None, "version_after": "9.0"}


async def test_failed_supervisor_call_not_recorded(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    send: AsyncMock,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """A Supervisor error reaches the client unchanged and records nothing."""
    send.side_effect = HassioAPIError("no space left")
    for endpoint in ("/addons/core_ssh/update", "/addons/core_ssh/restart"):
        response = await _api(hass, hass_ws_client, endpoint)
        assert response["success"] is False
        assert response["error"] == {
            "code": "unknown_error",
            "message": "no space left",
        }
    assert await _rows(hass) == []


async def test_non_admin_refused_not_recorded(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    send: AsyncMock,
    hass_ws_client: WebSocketGenerator,
    hass_read_only_access_token: str,
) -> None:
    """Core refuses a non-admin; nothing is recorded."""
    response = await _api(
        hass,
        hass_ws_client,
        "/addons/core_ssh/stop",
        token=hass_read_only_access_token,
    )
    assert response["error"]["code"] == "unauthorized"
    send.assert_not_awaited()
    assert await _rows(hass) == []


@pytest.mark.parametrize("record_values", [False, True])
async def test_options_values_follow_setting(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: Mock,
    hass_ws_client: WebSocketGenerator,
    record_values: bool,
) -> None:
    """Option names always; values only with `record_values`, secrets masked."""
    _set_record_values(entry, record_values)
    new = {"password": "s3cret", "log_level": "debug"}
    client.addons.addon_info.side_effect = [
        _info(options={"password": "old", "log_level": "info"}),
        _info(options=new),
    ]
    response = await _api(
        hass, hass_ws_client, "/addons/core_mosquitto/options", data={"options": new}
    )
    assert response["success"] is True
    [row] = await _rows(hass)
    assert row.action == "options_changed"
    assert row.target_id == "core_mosquitto"
    assert row.details["changed"] == ["options"]
    if not record_values:
        assert "values" not in row.details
        return
    values = row.details["values"]["options"]
    assert values["before"]["log_level"] == "info"
    assert values["after"]["log_level"] == "debug"
    assert values["after"]["password"] == {"hidden": True}
    assert "s3cret" not in repr(row.details)


async def test_settings_change_recorded(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: Mock,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Boot and watchdog are settings: only what changed is listed."""
    client.addons.addon_info.side_effect = [
        _info(boot="auto", watchdog=False),
        _info(boot="manual", watchdog=False),
    ]
    await _api(
        hass,
        hass_ws_client,
        "/addons/core_ssh/options",
        data={"boot": "manual", "watchdog": False},
    )
    [row] = await _rows(hass)
    assert row.action == "settings_changed"
    assert row.details == {"changed": ["boot"]}


async def test_options_unchanged_not_recorded(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Saving the same options again changes nothing: nothing is recorded."""
    await _api(hass, hass_ws_client, "/addons/core_ssh/options", data={"options": {}})
    assert await _rows(hass) == []


async def test_options_without_state_records_keys(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: Mock,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """When Supervisor can't be read, the request's keys are recorded."""
    client.addons.addon_info.side_effect = RuntimeError("down")
    response = await _api(
        hass, hass_ws_client, "/addons/core_ssh/options", data={"options": {"a": 1}}
    )
    assert response["success"] is True
    [row] = await _rows(hass)
    assert row.action == "options_changed"
    assert row.target_name == "core_ssh"
    assert row.details == {"changed": ["options"]}


async def test_update_versions_recorded(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: Mock,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """An update records the versions before and after."""
    client.addons.addon_info.side_effect = [_info(version="1.0"), _info(version="2.0")]
    await _api(hass, hass_ws_client, "/store/addons/core_ssh/update")
    [row] = await _rows(hass)
    assert row.action == "updated"
    assert row.details == {"version_before": "1.0", "version_after": "2.0"}


async def test_read_failure_fails_open(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: Mock,
    send: AsyncMock,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """A failing Supervisor read never changes the answer; the change is kept."""
    send.return_value = {"result": "ok", "data": {"job": "x"}}
    client.addons.addon_info.side_effect = RuntimeError("down")
    response = await _api(hass, hass_ws_client, "/store/addons/core_ssh/update")
    assert response == {
        "id": response["id"],
        "type": "result",
        "success": True,
        "result": {"job": "x"},
    }
    [row] = await _rows(hass)
    assert row.target_name == "core_ssh"
    assert row.details == {"version_before": None, "version_after": None}


async def test_classify_failure_fails_open(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    send: AsyncMock,
    hass_ws_client: WebSocketGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A classify error passes the call straight to Core."""
    monkeypatch.setattr(apps, "classify", Mock(side_effect=RuntimeError))
    response = await _api(hass, hass_ws_client, "/addons/core_ssh/restart")
    assert response["success"] is True
    send.assert_awaited_once()
    assert await _rows(hass) == []


async def test_get_passes_straight_through(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: Mock,
    send: AsyncMock,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Reads go to Core with no Supervisor call of ours."""
    send.return_value = {"result": "ok", "data": {"name": "x"}}
    response = await _api(hass, hass_ws_client, "/addons/core_ssh/info", "get")
    assert response["result"] == {"name": "x"}
    client.addons.addon_info.assert_not_awaited()
    client.store.repositories_list.assert_not_awaited()
    assert await _rows(hass) == []


async def test_repository_added(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
) -> None:
    """Adding a repository records its URL."""
    response = await _api(
        hass, hass_ws_client, "/store/repositories", data={"repository": REPO}
    )
    assert response["success"] is True
    [row] = await _rows(hass)
    assert row.action == "repository_added"
    assert row.target_id == REPO
    assert row.target_name == REPO
    assert row.details == {"repository": REPO}
    assert row.actor_user_id == hass_admin_user.id


async def test_repository_url_credentials_masked(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """A token in a private repository's URL is never stored."""
    url = "https://user:ghp_secret@github.com/x/apps"
    await _api(hass, hass_ws_client, "/store/repositories", data={"repository": url})
    [row] = await _rows(hass)
    assert "ghp_secret" not in repr(row)
    assert row.target_id == "https://•••@github.com/x/apps"


async def test_repository_removed(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: Mock,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Removing a repository by slug records its URL."""
    client.store.repositories_list.return_value = [
        SimpleNamespace(slug="a0d7b954", url=REPO)
    ]
    await _api(hass, hass_ws_client, "/store/repositories/a0d7b954", "delete")
    [row] = await _rows(hass)
    assert row.action == "repository_removed"
    assert row.target_id == REPO
    assert row.details == {"repository": REPO}


async def test_ws_update_addon_recorded(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: Mock,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
) -> None:
    """`hassio/update/addon` records an update with the user."""
    client.addons.addon_info.side_effect = [_info(version="1.0"), _info(version="2.0")]
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id(
        {"type": "hassio/update/addon", "addon": "core_ssh", "backup": False}
    )
    response = await ws.receive_json()
    assert response["success"] is True
    client.store.update_addon.assert_awaited_once()
    [row] = await _rows(hass)
    assert row.action == "updated"
    assert row.target_id == "core_ssh"
    assert row.actor_user_id == hass_admin_user.id
    assert row.actor_ip == IP
    assert row.details == {"version_before": "1.0", "version_after": "2.0"}


@pytest.mark.parametrize(
    ("service", "key", "action"),
    [
        ("addon_stop", "addon", "stopped"),
        ("addon_start", "addon", "started"),
        ("addon_restart", "addon", "restarted"),
        ("app_stop", "app", "stopped"),
        ("app_start", "app", "started"),
        ("app_restart", "app", "restarted"),
    ],
)
async def test_service_stop_recorded(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_admin_user: MockUser,
    service: str,
    key: str,
    action: str,
) -> None:
    """The app services record the calling user."""
    await hass.services.async_call(
        "hassio",
        service,
        {key: "core_mosquitto"},
        blocking=True,
        context=Context(user_id=hass_admin_user.id),
    )
    [row] = await _rows(hass)
    assert row.action == action
    assert row.target_id == "core_mosquitto"
    assert row.target_name == "Mosquitto broker"
    assert row.actor_user_id == hass_admin_user.id
    assert row.actor_name == hass_admin_user.name


async def test_service_failure_not_recorded(
    hass: HomeAssistant, entry: MockConfigEntry, client: Mock
) -> None:
    """A failing service raises as before and records nothing."""
    from aiohasupervisor import SupervisorError  # noqa: PLC0415

    from homeassistant.exceptions import HomeAssistantError  # noqa: PLC0415

    client.addons.stop_addon.side_effect = SupervisorError("busy")
    with pytest.raises(HomeAssistantError, match="busy"):
        await hass.services.async_call(
            "hassio", "addon_stop", {"addon": "core_ssh"}, blocking=True
        )
    assert await _rows(hass) == []


async def test_service_by_automation_is_system(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """A call without a user is System."""
    await hass.services.async_call(
        "hassio", "addon_restart", {"addon": "core_ssh"}, blocking=True
    )
    [row] = await _rows(hass)
    assert row.actor_user_id is None
    assert row.actor_name is None


async def _fake_update_component(hass: HomeAssistant) -> list[ServiceCall]:
    """Register an `update.install` service and mark `update` loaded."""
    calls: list[ServiceCall] = []

    async def install(call: ServiceCall) -> None:
        calls.append(call)

    hass.services.async_register("update", "install", install)
    hass.config.components.add("update")
    hass.bus.async_fire("component_loaded", {"component": "update"})
    await hass.async_block_till_done()
    return calls


async def test_update_entity_install_recorded(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: Mock,
    entity_registry: er.EntityRegistry,
    hass_admin_user: MockUser,
) -> None:
    """`update.install` on an app's update entity records an update."""
    app = entity_registry.async_get_or_create(
        "update", "hassio", "core_ssh_version_latest"
    )
    core = entity_registry.async_get_or_create(
        "update", "hassio", "home_assistant_core_version_latest"
    )
    other = entity_registry.async_get_or_create("update", "hacs", "x_version_latest")
    for found in (app, core, other):
        hass.states.async_set(found.entity_id, "on")
    calls = await _fake_update_component(hass)
    client.addons.addon_info.side_effect = [_info(version="1.0"), _info(version="2.0")]
    await hass.services.async_call(
        "update",
        "install",
        {"entity_id": [app.entity_id, core.entity_id, other.entity_id]},
        blocking=True,
        context=Context(user_id=hass_admin_user.id),
    )
    assert len(calls) == 1
    [row] = await _rows(hass)
    assert row.action == "updated"
    assert row.target_id == "core_ssh"
    assert row.actor_user_id == hass_admin_user.id
    assert row.details == {"version_before": "1.0", "version_after": "2.0"}


async def test_update_entity_install_failure_not_recorded(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """A failing `update.install` records nothing and raises unchanged."""
    app = entity_registry.async_get_or_create(
        "update", "hassio", "core_ssh_version_latest"
    )

    async def install(call: ServiceCall) -> None:
        raise RuntimeError("nope")

    hass.services.async_register("update", "install", install)
    hass.config.components.add("update")
    hass.bus.async_fire("component_loaded", {"component": "update"})
    await hass.async_block_till_done()
    with pytest.raises(RuntimeError, match="nope"):
        await hass.services.async_call(
            "update", "install", {"entity_id": app.entity_id}, blocking=True
        )
    assert await _rows(hass) == []


async def test_rest_proxy_refuses_app_changes(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
) -> None:
    """Core's `/api/hassio` proxy refuses app changes, so it has nothing to record.

    `HassIOView` only lets admins reach backups, logs, changelogs and
    documentation; the `hassio_rest_proxy` contract keeps that true.
    """
    from homeassistant.components.hassio.http import HassIOView  # noqa: PLC0415

    hass.http.register_view(HassIOView("127.0.0.1:1", Mock()))
    http = await hass_client()
    resp = await http.post("/api/hassio/addons/core_mosquitto/restart")
    assert resp.status == 401
    assert await _rows(hass) == []
    assert CONTRACTS["hassio_rest_proxy"].check(hass) is None


async def test_not_loaded_without_supervisor(hass: HomeAssistant) -> None:
    """Without Supervisor, `install()` returns False and wraps nothing."""
    assert await async_setup_component(hass, "websocket_api", {})
    handlers = dict(hass.data[WEBSOCKET_API_DOMAIN])
    collector = AppCollector(hass, make_runtime(hass))
    assert collector.install() is False
    assert collector.is_installed is False
    assert collector.disabled_reason == []
    assert dict(hass.data[WEBSOCKET_API_DOMAIN]) == handlers
    assert ir.async_get(hass).issues == {}


async def test_collector_registered(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """The collector is one of Electric Fence's and installed with Supervisor."""
    assert AppCollector in COLLECTOR_TYPES
    collector = _collector(entry)
    assert collector.is_installed
    assert collector.disabled_reason == []


async def test_unload_restores_core(
    hass: HomeAssistant, send: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unloading puts Core's very handlers and service jobs back."""
    calls = await _fake_update_component(hass)
    assert calls == []
    handlers = hass.data[WEBSOCKET_API_DOMAIN]
    commands = ("supervisor/api", "hassio/update/addon")
    services = hass.services._services  # noqa: SLF001
    keys = [("hassio", name) for name in apps.APP_SERVICES] + [("update", "install")]
    before_ws = {c: handlers[c] for c in commands}
    before_jobs = {k: services[k[0]][k[1]].job for k in keys}
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", MIN_HA_VERSION)
    found = MockConfigEntry(domain=DOMAIN, options=Settings().to_options())
    found.add_to_hass(hass)
    assert await hass.config_entries.async_setup(found.entry_id)
    await hass.async_block_till_done()
    for command in commands:
        assert handlers[command][0] is not before_ws[command][0]
    for key in keys:
        assert services[key[0]][key[1]].job is not before_jobs[key], key
    assert await hass.config_entries.async_unload(found.entry_id)
    for command in commands:
        assert handlers[command] is before_ws[command]
    for key in keys:
        assert services[key[0]][key[1]].job is before_jobs[key], key


async def test_failing_contract_disables_only_apps(
    hass: HomeAssistant,
    send: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing contract disables the app collector with a repair issue."""
    monkeypatch.setitem(
        CONTRACTS,
        "supervisor_ws_api",
        Contract("supervisor_ws_api", "apps", lambda _hass: "supervisor/api moved"),
    )
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", MIN_HA_VERSION)
    found = MockConfigEntry(domain=DOMAIN, options=Settings().to_options())
    found.add_to_hass(hass)
    assert await hass.config_entries.async_setup(found.entry_id)
    await hass.async_block_till_done()
    collector = _collector(found)
    assert collector.is_installed is False
    assert collector.disabled_reason == ["supervisor/api moved"]
    others = [c for c in found.runtime_data.collectors if c is not collector]
    assert all(c.is_installed for c in others)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "collector_disabled_apps")


async def test_failing_rest_proxy_contract_keeps_collector(
    hass: HomeAssistant,
    send: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If Core's proxy ever lets app changes through, a repair says so.

    The other sources keep recording.
    """
    monkeypatch.setitem(
        CONTRACTS,
        "hassio_rest_proxy",
        Contract("hassio_rest_proxy", "apps_rest_proxy", lambda _hass: "open"),
    )
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", MIN_HA_VERSION)
    found = MockConfigEntry(domain=DOMAIN, options=Settings().to_options())
    found.add_to_hass(hass)
    assert await hass.config_entries.async_setup(found.entry_id)
    await hass.async_block_till_done()
    collector = _collector(found)
    assert collector.is_installed is True
    assert collector.disabled_reason == ["rest proxy: open"]
    assert ir.async_get(hass).async_get_issue(DOMAIN, "collector_disabled_apps")


async def test_options_without_body_decided_from_state(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: Mock,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Without a body, Supervisor's state says what changed."""
    client.addons.addon_info.side_effect = [
        _info(boot="auto"),
        _info(boot="manual"),
        _info(options={"a": 1}),
        _info(options={"a": 2}, boot="manual"),
    ]
    await _api(hass, hass_ws_client, "/addons/core_ssh/options")
    await _api(hass, hass_ws_client, "/addons/core_ssh/options")
    first, second = await _rows(hass)
    assert first.action == "settings_changed"
    assert first.details == {"changed": ["boot"]}
    assert second.action == "options_changed"
    assert second.details == {"changed": ["boot", "options"]}


async def test_repository_added_without_body(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: Mock,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Without a body, the new repository is found in the store's list."""
    old = SimpleNamespace(slug="core", url="https://github.com/home-assistant/addons")
    client.store.repositories_list.side_effect = [
        [old],
        [old, SimpleNamespace(slug="a0d7b954", url=REPO)],
    ]
    await _api(hass, hass_ws_client, "/store/repositories")
    [row] = await _rows(hass)
    assert row.action == "repository_added"
    assert row.target_id == REPO


async def test_update_entity_install_all(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: Mock,
    entity_registry: er.EntityRegistry,
) -> None:
    """`entity_id: all` updates every app's update entity."""
    for slug in ("core_ssh", "core_mosquitto"):
        found = entity_registry.async_get_or_create(
            "update", "hassio", f"{slug}_version_latest"
        )
        hass.states.async_set(found.entity_id, "on")
    entity_registry.async_get_or_create(
        "update", "hassio", "home_assistant_os_version_latest"
    )
    await _fake_update_component(hass)
    client.addons.addon_info.side_effect = [_info(version="1.0")] * 2 + [
        _info(version="2.0")
    ] * 2
    await hass.services.async_call(
        "update", "install", {"entity_id": "all"}, blocking=True
    )
    rows = await _rows(hass)
    assert sorted(r.target_id or "" for r in rows) == ["core_mosquitto", "core_ssh"]


async def test_late_update_contract_failure_disables(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing `update` contract when it loads disables the collector."""
    monkeypatch.setitem(
        CONTRACTS,
        "hassio_update_install",
        Contract("hassio_update_install", "apps", lambda _hass: "install moved"),
    )
    await _fake_update_component(hass)
    collector = _collector(entry)
    assert collector.is_installed is False
    assert collector.disabled_reason == ["install moved"]
    assert ir.async_get(hass).async_get_issue(DOMAIN, "collector_disabled_apps")


async def test_handler_error_after_read_answered(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Core's handler raising after our read is answered as Core would."""
    handlers = hass.data[WEBSOCKET_API_DOMAIN]
    wrapper, schema = handlers["supervisor/api"]
    original = Mock(side_effect=RuntimeError("boom"))
    wrap = _collector(entry)._wrap_api  # noqa: SLF001
    handlers["supervisor/api"] = (wrap(original), schema)
    response = await _api(hass, hass_ws_client, "/addons/core_ssh/update")
    assert response["success"] is False
    assert response["error"]["code"] == "unknown_error"
    original.assert_called_once()
    assert await _rows(hass) == []
    handlers["supervisor/api"] = (wrapper, schema)


async def test_update_entity_skips_unavailable_and_disabled(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: Mock,
    entity_registry: er.EntityRegistry,
) -> None:
    """Core skips unavailable, disabled and up-to-date entities: no record."""
    unavailable = entity_registry.async_get_or_create(
        "update", "hassio", "core_a_version_latest"
    )
    hass.states.async_set(unavailable.entity_id, "unavailable")
    disabled = entity_registry.async_get_or_create(
        "update",
        "hassio",
        "core_b_version_latest",
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    current = entity_registry.async_get_or_create(
        "update", "hassio", "core_c_version_latest"
    )
    hass.states.async_set(current.entity_id, "off")
    await _fake_update_component(hass)
    for target in ({"entity_id": "all"}, {"entity_id": unavailable.entity_id}):
        await hass.services.async_call("update", "install", target, blocking=True)
    await hass.services.async_call(
        "update", "install", {"entity_id": disabled.entity_id}, blocking=True
    )
    assert await _rows(hass) == []
    client.addons.addon_info.assert_not_awaited()


async def test_update_same_version_not_recorded(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: Mock,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """An update that leaves the version as it was records nothing."""
    client.addons.addon_info.side_effect = [_info(version="1.0"), _info(version="1.0")]
    response = await _api(hass, hass_ws_client, "/store/addons/core_ssh/update")
    assert response["success"] is True
    assert await _rows(hass) == []


async def test_update_entity_reads_concurrently(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: Mock,
    entity_registry: er.EntityRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slow Supervisor reads delay the user's update by one timeout at most."""
    monkeypatch.setattr(apps, "READ_TIMEOUT", 0.2)
    for n in range(5):
        found = entity_registry.async_get_or_create(
            "update", "hassio", f"core_{n}_version_latest"
        )
        hass.states.async_set(found.entity_id, "on")
    started: list[float] = []

    async def install(call: ServiceCall) -> None:
        started.append(time.monotonic())

    hass.services.async_register("update", "install", install)
    hass.config.components.add("update")
    hass.bus.async_fire("component_loaded", {"component": "update"})
    await hass.async_block_till_done()

    async def slow(slug: str) -> SimpleNamespace:
        await asyncio.sleep(5)
        return _info()

    client.addons.addon_info.side_effect = slow
    begin = time.monotonic()
    await hass.services.async_call(
        "update", "install", {"entity_id": "all"}, blocking=True
    )
    assert len(started) == 1
    # Five sequential reads would take 1 s.
    assert started[0] - begin < 0.6
    rows = await _rows(hass)
    assert len(rows) == 5
    assert all(
        r.details == {"version_before": None, "version_after": None} for r in rows
    )


async def test_background_install_recorded_as_accepted(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: Mock,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
) -> None:
    """A background job is recorded as accepted, never with a version after."""
    client.addons.addon_info.side_effect = [_info(version="1.0")]
    await _api(
        hass,
        hass_ws_client,
        "/store/addons/core_ssh/install",
        data={"background": True},
    )
    await _api(
        hass,
        hass_ws_client,
        "/store/addons/core_ssh/update",
        data={"background": True},
    )
    install, update = await _rows(hass)
    assert install.action == "installed"
    assert install.details == {"version_before": None, "background": True}
    assert install.actor_user_id == hass_admin_user.id
    assert update.action == "updated"
    assert update.details == {"version_before": "1.0", "background": True}
    assert client.addons.addon_info.await_count == 1


async def test_password_typed_options_masked(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: Mock,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Options the app's schema types as password are hidden too."""
    _set_record_values(entry, True)
    schema = [
        {"name": "mqtt_login", "type": "string", "format": "password"},
        {"name": "log_level", "type": "string"},
    ]
    client.addons.addon_info.side_effect = [
        _info(options={"mqtt_login": "hunter2", "log_level": "info"}, schema=schema),
        _info(options={"mqtt_login": "hunter3", "log_level": "debug"}, schema=schema),
    ]
    await _api(
        hass,
        hass_ws_client,
        "/addons/core_ssh/options",
        data={"options": {"mqtt_login": "hunter3", "log_level": "debug"}},
    )
    [row] = await _rows(hass)
    values = row.details["values"]["options"]
    assert values["after"] == {"mqtt_login": {"hidden": True}, "log_level": "debug"}
    assert "hunter" not in repr(row.details)


async def test_password_only_change_hidden(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: Mock,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """A change only in a password-typed option is listed, its values hidden."""
    _set_record_values(entry, True)
    schema = [{"name": "login", "type": "password"}]
    client.addons.addon_info.side_effect = [
        _info(options={"login": "a"}, schema=schema),
        _info(options={"login": "b"}, schema=schema),
    ]
    await _api(
        hass,
        hass_ws_client,
        "/addons/core_ssh/options",
        data={"options": {"login": "b"}},
    )
    [row] = await _rows(hass)
    assert row.details == {
        "changed": ["options"],
        "values": {"options": {"hidden": True}},
    }
