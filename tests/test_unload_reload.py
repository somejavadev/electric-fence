"""Unload restores Core exactly; reload works without a restart (§4.6, §11.12)."""

import inspect
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components import electric_fence as fence
from custom_components.electric_fence import attribution
from custom_components.electric_fence.attribution import ADMIN_WS_COMMANDS
from custom_components.electric_fence.const import DOMAIN, MIN_HA_VERSION
from custom_components.electric_fence.notifier import (
    EVENT_LOGIN_FAILED,
    NOTIFICATION_ID,
)
from custom_components.electric_fence.sensor import WRITE_INTERVAL, FailedLoginsSensor
from custom_components.electric_fence.settings import Settings
from homeassistant.components import persistent_notification
from homeassistant.components.frontend import DATA_PANELS
from homeassistant.components.websocket_api import DOMAIN as WEBSOCKET_API_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_COMPONENT_LOADED
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import DATA_DISPATCHER
from homeassistant.setup import ATTR_COMPONENT
from homeassistant.util import dt as dt_util
from tests.helpers import (
    change_rows,
    create_hass_user,
    flush_worker,
    login_flow,
    store_rows,
)

SENSOR_ID = "sensor.electric_fence_failed_logins"


async def _setup(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> MockConfigEntry:
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", MIN_HA_VERSION)
    entry = MockConfigEntry(domain=DOMAIN, options=Settings().to_options())
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


def _hooked_attributes(hass: HomeAssistant) -> list[tuple[object, str]]:
    """Every object attribute Electric Fence wraps."""
    entries = hass.config_entries
    provider = hass.auth.get_auth_provider("homeassistant", None)
    assert provider is not None
    return [
        (hass.auth.login_flow, "async_init"),
        (hass.auth.login_flow, "async_configure"),
        (hass.auth, "async_create_refresh_token"),
        *(
            (mgr, attr)
            for mgr in (entries.flow, entries.options, entries.subentries)
            for attr in ("async_init", "async_configure")
        ),
        (entries, "async_remove"),
        (provider, "async_change_password"),
        (provider, "async_change_username"),
    ]


def _snapshot(hass: HomeAssistant) -> tuple[list[Any], dict[str, Any]]:
    """Return the raw attributes (instance or class) and the WS handler tuples."""
    attrs = [
        inspect.getattr_static(obj, attr) for obj, attr in _hooked_attributes(hass)
    ]
    handlers = hass.data[WEBSOCKET_API_DOMAIN]
    return attrs, {command: handlers[command] for command in ADMIN_WS_COMMANDS}


async def test_unload_restores_every_hook(
    hass: HomeAssistant, config: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After unload every hooked attribute and WS handler is Core's own object."""
    await create_hass_user(hass, "alice", "right")
    before_attrs, before_ws = _snapshot(hass)

    entry = await _setup(hass, monkeypatch)
    # Loaded: every one of them is wrapped (so the check below means something).
    loaded_attrs, loaded_ws = _snapshot(hass)
    for old, new in zip(before_attrs, loaded_attrs, strict=True):
        assert new is not old
    for command, handler in before_ws.items():
        assert loaded_ws[command] is not handler, command

    assert await hass.config_entries.async_unload(entry.entry_id)

    after_attrs, after_ws = _snapshot(hass)
    for (obj, attr), old, new in zip(
        _hooked_attributes(hass), before_attrs, after_attrs, strict=True
    ):
        assert new is old, f"{type(obj).__name__}.{attr}"
        if attr not in vars(obj):
            continue
        # Restored instance attributes are the very objects that were there.
        assert vars(obj)[attr] is old
    for command, handler in before_ws.items():
        assert after_ws[command] is handler, command


def _listener_counts(hass: HomeAssistant) -> tuple[dict[str, int], dict[Any, int]]:
    bus = {k: v for k, v in hass.bus.async_listeners().items() if v}
    dispatcher = {
        signal: len(targets)
        for signal, targets in hass.data.get(DATA_DISPATCHER, {}).items()
        if targets
    }
    return bus, dispatcher


def _tracking(real: Any, ended: list[bool]) -> Any:
    def tracked(*args: Any, **kwargs: Any) -> Any:
        unsub = real(*args, **kwargs)
        index = len(ended)
        ended.append(False)

        def end() -> None:
            ended[index] = True
            unsub()

        return end

    return tracked


def _track_timers(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[bool]]:
    """Record, per timer helper, whether each timer the setup started was ended."""
    ended: dict[str, list[bool]] = {}
    for name in ("async_track_time_interval", "async_call_later"):
        ended[name] = []
        monkeypatch.setattr(fence, name, _tracking(getattr(fence, name), ended[name]))
    return ended


async def test_unload_removes_panel_sensor_listeners(
    hass: HomeAssistant, config: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unload removes the panel, the sensor, and every listener and timer."""
    # A first load and unload sets up the dependencies (sensor, frontend, ...),
    # whose own listeners stay; the second cycle is compared.
    entry = await _setup(hass, monkeypatch)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    before = _listener_counts(hass)
    timers = _track_timers(monkeypatch)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    worker = entry.runtime_data.worker
    assert "electric-fence" in hass.data[DATA_PANELS]
    assert hass.states.get(SENSOR_ID) is not None
    assert worker._listeners  # noqa: SLF001
    assert _listener_counts(hass) != before
    assert timers == {
        "async_track_time_interval": [False],
        "async_call_later": [False],
    }

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert "electric-fence" not in hass.data[DATA_PANELS]
    assert hass.states.get(SENSOR_ID).state == "unavailable"
    assert not worker._listeners  # noqa: SLF001
    assert _listener_counts(hass) == before
    assert timers == {
        "async_track_time_interval": [True],
        "async_call_later": [True],
    }


async def test_reload_without_restart_records_again(
    hass: HomeAssistant,
    config: None,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a reload, logins and changes are recorded again, each exactly once."""
    await create_hass_user(hass, "alice", "right")
    entry = await _setup(hass, monkeypatch)
    client = await hass_client_no_auth()
    assert (await login_flow(client, "before", "x"))["errors"] == {
        "base": "invalid_auth"
    }
    await flush_worker(hass)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert "electric-fence" in hass.data[DATA_PANELS]

    assert (await login_flow(client, "after", "x"))["errors"] == {
        "base": "invalid_auth"
    }
    await hass.config.async_update(location_name="Reloaded")
    await flush_worker(hass)
    # The sensor writes at most once a second; let its trailing write run.
    async_fire_time_changed(hass, dt_util.utcnow() + WRITE_INTERVAL)
    await hass.async_block_till_done()

    rows = store_rows(hass)
    assert [(r.username_typed, r.count) for r in rows] == [("after", 1), ("before", 1)]
    assert [(c.kind, c.details) for c in change_rows(hass)] == [
        ("core_config", {"keys": ["location_name"]})
    ]
    assert hass.states.get(SENSOR_ID).state == "2"


async def test_unload_restores_ws_handlers_wrapped_after_config_loads(
    hass: HomeAssistant, config: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WS handlers wrapped when `config` loads late are restored to Core's tuples.

    `frontend` (a dependency) loads `config` first in practice, so the entry
    is set up as if `config` were not loaded yet, and its load event is then
    fired.
    """
    handlers = hass.data[WEBSOCKET_API_DOMAIN]
    core = {command: handlers[command] for command in ADMIN_WS_COMMANDS}
    monkeypatch.setattr(attribution, "_CONFIG", "config (not loaded yet)")
    entry = await _setup(hass, monkeypatch)
    for command, original in core.items():
        assert handlers[command] is original, command

    monkeypatch.setattr(attribution, "_CONFIG", "config")
    hass.bus.async_fire(EVENT_COMPONENT_LOADED, {ATTR_COMPONENT: "config"})
    await hass.async_block_till_done()
    for command, original in core.items():
        assert handlers[command] is not original, command
        assert handlers[command][1] is original[1], command

    assert await hass.config_entries.async_unload(entry.entry_id)

    for command, original in core.items():
        assert handlers[command] is original, command


async def test_unload_flushes_records_to_listeners(
    hass: HomeAssistant,
    config: None,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Records submitted just before unload still fire events and notify."""
    await create_hass_user(hass, "alice", "right")
    entry = await _setup(hass, monkeypatch)
    events = async_capture_events(hass, EVENT_LOGIN_FAILED)
    counted: list[int] = []
    real_add = FailedLoginsSensor._add  # noqa: SLF001

    def spy_add(self: FailedLoginsSensor, ts: Any, count: int) -> None:
        counted.append(count)
        real_add(self, ts, count)

    monkeypatch.setattr(FailedLoginsSensor, "_add", spy_add)
    assert (await login_flow(await hass_client_no_auth(), "last", "x"))["errors"] == {
        "base": "invalid_auth"
    }
    # Still pending in the worker (it writes once a second).
    assert events == []

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert [e.data["username"] for e in events] == ["la•••"]
    notifications = persistent_notification._async_get_or_create_notifications(  # noqa: SLF001
        hass
    )
    assert NOTIFICATION_ID in notifications
    # The sensor counted it (its throttled write is moot: it is unavailable).
    assert counted == [1]
