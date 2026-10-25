"""Tests for the settings change collectors (H4, H5, H6)."""

# ruff: noqa: SLF001

from dataclasses import replace
from http import HTTPStatus
import threading
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser
from pytest_homeassistant_custom_component.typing import (
    ClientSessionGenerator,
    WebSocketGenerator,
)

from custom_components.electric_fence import COLLECTOR_TYPES
from custom_components.electric_fence.collectors.config_entries import (
    ConfigEntryCollector,
    EntrySnapshot,
)
from custom_components.electric_fence.collectors.core_config import CoreConfigCollector
from custom_components.electric_fence.collectors.users import (
    UserCollector,
    _credential_username,
)
from custom_components.electric_fence.const import DOMAIN
from custom_components.electric_fence.models import ChangeAction, ChangeKind
from custom_components.electric_fence.storage import ChangeRow
from homeassistant.auth.providers.homeassistant import HassAuthProvider, InvalidUser
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryDisabler,
    ConfigEntryState,
)
from homeassistant.const import EVENT_CORE_CONFIG_UPDATE
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util.unit_system import METRIC_SYSTEM
from tests.helpers import (
    CLIENT_ID,
    DUMMY,
    change_rows,
    create_hass_user,
    dummy_entry,
    dump_db,
    flush_worker,
    mock_dummy,
)
from tests.test_options_flow import GENERAL

IP = "127.0.0.1"


@pytest.fixture
async def fence(
    hass: HomeAssistant,
    local_auth: HassAuthProvider,
    hass_admin_user: MockUser,
    dummy: None,
    config: None,
    setup_entry: MockConfigEntry,
) -> MockConfigEntry:
    """Set up Electric Fence after `config`, `ef_dummy` and the provider."""
    return setup_entry


async def _rows(
    hass: HomeAssistant, kind: ChangeKind | None = None, *, after: int = 0
) -> list[ChangeRow]:
    """Return the stored changes of a kind, oldest first, from `after` on."""
    await hass.async_block_till_done()
    await flush_worker(hass)
    return [r for r in change_rows(hass)[after:] if kind is None or r.kind is kind]


async def _entry_rows(hass: HomeAssistant, *, after: int = 0) -> list[ChangeRow]:
    rows = await _rows(hass, ChangeKind.CONFIG_ENTRY, after=after)
    return [r for r in rows if r.details.get("domain") == DUMMY]


async def _mark(hass: HomeAssistant) -> int:
    """Return the number of changes stored so far."""
    return len(await _rows(hass))


def _by_admin(row: ChangeRow, admin: MockUser) -> bool:
    return (
        row.actor_user_id == admin.id
        and row.actor_name == admin.name
        and row.actor_ip == IP
    )


def _is_system(row: ChangeRow) -> bool:
    return row.actor_user_id is None and row.actor_name is None and row.actor_ip is None


def _collector[T](hass: HomeAssistant, kind: type[T]) -> T:
    entry = hass.config_entries.async_loaded_entries(DOMAIN)[0]
    return next(c for c in entry.runtime_data.collectors if isinstance(c, kind))


def test_collectors_registered() -> None:
    """The three settings collectors are installed by setup."""
    assert {ConfigEntryCollector, UserCollector, CoreConfigCollector} <= set(
        COLLECTOR_TYPES
    )
    assert ConfigEntryCollector.name == "config_entries"
    assert UserCollector.name == "users"
    assert CoreConfigCollector.name == "core_config"


# H4: config entries.


async def test_integration_added_by_admin_attributed(
    hass: HomeAssistant,
    fence: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    hass_admin_user: MockUser,
) -> None:
    """Adding an integration through the UI is credited to the admin."""
    client = await hass_client()

    resp = await client.post("/api/config/config_entries/flow", json={"handler": DUMMY})

    assert (await resp.json())["type"] == "create_entry"
    rows = await _entry_rows(hass)
    added = rows[0]
    assert added.action is ChangeAction.ADDED
    assert _by_admin(added, hass_admin_user)
    assert added.target_name == f"{DUMMY}: Dummy"
    assert added.details == {
        "domain": DUMMY,
        "changed": [],
        "data_keys_changed": [],
    }
    entry = hass.config_entries.async_entries(DUMMY)[0]
    assert added.target_id == entry.entry_id


@pytest.mark.parametrize("domain_loaded", [False, True])
async def test_update_during_setup_is_system(
    hass: HomeAssistant,
    fence: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    hass_admin_user: MockUser,
    domain_loaded: bool,
) -> None:
    """The integration's own update during its setup is System."""
    if domain_loaded:
        await dummy_entry(hass, {"quiet": True})
    mark = await _mark(hass)
    client = await hass_client()

    resp = await client.post("/api/config/config_entries/flow", json={"handler": DUMMY})

    assert (await resp.json())["type"] == "create_entry"
    added, update = await _entry_rows(hass, after=mark)
    assert added.action is ChangeAction.ADDED
    assert _by_admin(added, hass_admin_user)
    assert update.action is ChangeAction.UPDATED
    assert update.target_id == added.target_id
    assert update.actor_user_id is None
    assert _is_system(update)
    assert update.details["changed"] == ["data"]
    assert update.details["data_keys_changed"] == ["setups", "touched"]


async def test_reconfigure_recorded_as_reconfigured(
    hass: HomeAssistant,
    fence: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    hass_admin_user: MockUser,
) -> None:
    """A reconfigure is credited; the reload's own update is System UPDATED."""
    entry = await dummy_entry(hass)
    mark = await _mark(hass)
    client = await hass_client()

    resp = await client.post(
        "/api/config/config_entries/flow",
        json={"handler": DUMMY, "entry_id": entry.entry_id},
    )
    form = await resp.json()
    resp = await client.post(
        f"/api/config/config_entries/flow/{form['flow_id']}", json={}
    )

    assert (await resp.json())["reason"] == "reconfigure_successful"
    reconfigured, reload_update = await _entry_rows(hass, after=mark)
    assert reconfigured.action is ChangeAction.RECONFIGURED
    assert _by_admin(reconfigured, hass_admin_user)
    assert reconfigured.target_id == entry.entry_id
    assert reconfigured.details["changed"] == ["data"]
    assert reconfigured.details["data_keys_changed"] == ["reconfigured"]
    assert reload_update.action is ChangeAction.UPDATED
    assert _is_system(reload_update)


async def test_options_change_recorded_as_options_changed(
    hass: HomeAssistant,
    fence: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    hass_admin_user: MockUser,
) -> None:
    """An options flow is credited, listing the option keys (values off)."""
    entry = await dummy_entry(hass)
    mark = await _mark(hass)
    client = await hass_client()

    resp = await client.post(
        "/api/config/config_entries/options/flow", json={"handler": entry.entry_id}
    )
    form = await resp.json()
    resp = await client.post(
        f"/api/config/config_entries/options/flow/{form['flow_id']}", json={}
    )

    assert (await resp.json())["type"] == "create_entry"
    (row,) = await _entry_rows(hass, after=mark)
    assert row.action is ChangeAction.OPTIONS_CHANGED
    assert _by_admin(row, hass_admin_user)
    assert row.details == {
        "domain": DUMMY,
        "changed": ["options.opt"],
        "data_keys_changed": [],
    }


async def test_options_change_with_reloading_listener_credited(
    hass: HomeAssistant,
    fence: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    hass_admin_user: MockUser,
) -> None:
    """The update listener's reload starts first; the admin still gets credit.

    Core starts the listener before it dispatches the options change, so the
    whole reload, with the setup's own update, is dispatched first, already
    showing the new options. The options change is held back for the
    admin's dispatch; the setup's update is System.
    """
    mock_dummy(hass, reloading=True)
    entry = await dummy_entry(hass)
    mark = await _mark(hass)
    client = await hass_client()

    resp = await client.post(
        "/api/config/config_entries/options/flow", json={"handler": entry.entry_id}
    )
    form = await resp.json()
    resp = await client.post(
        f"/api/config/config_entries/options/flow/{form['flow_id']}", json={}
    )

    assert (await resp.json())["type"] == "create_entry"
    assert entry.data["setups"] == 2
    # The reload ran ahead of the admin's dispatch, so its row comes first.
    reload_update, options = await _entry_rows(hass, after=mark)
    assert options.action is ChangeAction.OPTIONS_CHANGED
    assert _by_admin(options, hass_admin_user)
    assert options.details["changed"] == ["options.opt"]
    assert reload_update.action is ChangeAction.UPDATED
    assert _is_system(reload_update)
    assert reload_update.details["changed"] == ["data"]


async def test_ws_update_with_reloading_listener_credited(
    hass: HomeAssistant,
    fence: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A WebSocket scope reaches the listener's task, but only the change counts.

    The WebSocket scope has no owner task, so the reload running ahead of
    the dispatch could be credited; while the change is held back, it isn't.
    """
    mock_dummy(hass, reloading=True)
    entry = await dummy_entry(hass)
    mark = await _mark(hass)
    original = hass.config_entries.async_update_entry

    def _update(entry_: Any, **kwargs: Any) -> bool:
        # A WS options change isn't possible: add one to the title change.
        if "title" in kwargs:
            kwargs["options"] = {"opt": "ws"}
        return original(entry_, **kwargs)

    monkeypatch.setattr(hass.config_entries, "async_update_entry", _update)
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id(
        {"type": "config_entries/update", "entry_id": entry.entry_id, "title": "T"}
    )
    assert (await ws.receive_json())["success"]

    assert entry.data["setups"] == 2
    reload_update, update = await _entry_rows(hass, after=mark)
    assert _is_system(reload_update)
    assert reload_update.details["changed"] == ["data"]
    assert update.action is ChangeAction.UPDATED
    assert update.details["changed"] == ["options.opt", "title"]
    assert _by_admin(update, hass_admin_user)


async def test_stale_hold_back_expires(
    hass: HomeAssistant,
    fence: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
) -> None:
    """Fields changed without a dispatch are held back one loop turn only."""
    entry = await dummy_entry(hass, {"quiet": True})
    mark = await _mark(hass)
    collector = _collector(hass, ConfigEntryCollector)
    object.__setattr__(entry, "title", "Sneaky")

    assert await hass.config_entries.async_reload(entry.entry_id)
    assert entry.entry_id in collector._pending
    await hass.async_block_till_done()
    assert collector._pending == {}

    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id(
        {"type": "config_entries/update", "entry_id": entry.entry_id, "title": "T"}
    )
    assert (await ws.receive_json())["success"]

    # The expired change is kept, as System; the next change is the admin's.
    expired, row = await _entry_rows(hass, after=mark)
    assert expired.action is ChangeAction.UPDATED
    assert expired.details["changed"] == ["title"]
    assert expired.target_name == f"{DUMMY}: Sneaky"
    assert _is_system(expired)
    assert _by_admin(row, hass_admin_user)


async def test_expired_hold_back_of_removed_entry_dropped(
    hass: HomeAssistant, fence: MockConfigEntry
) -> None:
    """A hold-back of an entry gone by expiry records nothing more."""
    entry = await dummy_entry(hass, {"quiet": True})
    collector = _collector(hass, ConfigEntryCollector)
    object.__setattr__(entry, "title", "Sneaky")
    assert await hass.config_entries.async_reload(entry.entry_id)
    pending = collector._pending[entry.entry_id]
    mark = await _mark(hass)

    collector._pending.clear()
    hass.config_entries._entries.pop(entry.entry_id)
    collector._pending[entry.entry_id] = pending
    collector._expire(entry.entry_id, pending)

    assert await _rows(hass, after=mark) == []
    assert collector._pending == {}


async def test_hold_back_with_suspending_unload(
    hass: HomeAssistant,
    fence: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    hass_admin_user: MockUser,
) -> None:
    """A reload whose unload suspends: the admin's change is still credited."""
    mock_dummy(hass, reloading=True, suspending_unload=True)
    entry = await dummy_entry(hass)
    mark = await _mark(hass)
    client = await hass_client()

    resp = await client.post(
        "/api/config/config_entries/options/flow", json={"handler": entry.entry_id}
    )
    form = await resp.json()
    resp = await client.post(
        f"/api/config/config_entries/options/flow/{form['flow_id']}", json={}
    )

    assert (await resp.json())["type"] == "create_entry"
    assert entry.data["setups"] == 2
    options, reload_update = await _entry_rows(hass, after=mark)
    assert options.action is ChangeAction.OPTIONS_CHANGED
    assert options.details["changed"] == ["options.opt"]
    assert _by_admin(options, hass_admin_user)
    assert reload_update.details["changed"] == ["data"]
    assert _is_system(reload_update)


async def test_ws_listener_change_before_reload_never_credited(
    hass: HomeAssistant,
    fence: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
) -> None:
    """A listener's own change, before any state dispatch, is System (R29).

    It runs in the listener's task, which inherits the WebSocket scope; the
    admin is never credited with the listener's keys.
    """
    ran: list[bool] = []

    async def _setup(hass: HomeAssistant, entry: ConfigEntry) -> None:
        async def _on_update(hass: HomeAssistant, entry: ConfigEntry) -> None:
            if ran:
                return
            ran.append(True)
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, "listener_key": True}
            )
            await hass.config_entries.async_reload(entry.entry_id)

        entry.async_on_unload(entry.add_update_listener(_on_update))

    mock_dummy(hass, setup_entry=_setup)
    entry = await dummy_entry(hass)
    mark = await _mark(hass)
    ws = await hass_ws_client(hass)

    await ws.send_json_auto_id(
        {"type": "config_entries/update", "entry_id": entry.entry_id, "title": "T"}
    )
    assert (await ws.receive_json())["success"]

    assert ran
    assert entry.data["listener_key"] is True
    rows = await _entry_rows(hass, after=mark)
    assert rows
    listener_rows = [
        r for r in rows if "listener_key" in r.details["data_keys_changed"]
    ]
    assert listener_rows
    assert all(_is_system(r) for r in listener_rows)
    for row in rows:
        assert _is_system(row) or "listener_key" not in row.details["data_keys_changed"]


async def test_untracked_fields_not_recorded(
    hass: HomeAssistant, fence: MockConfigEntry
) -> None:
    """A change of unreported fields only (unique ID, version) is no row."""
    entry = await dummy_entry(hass, {"quiet": True})
    mark = await _mark(hass)

    hass.config_entries.async_update_entry(entry, unique_id="u1", version=2)

    assert await _rows(hass, after=mark) == []


async def test_follow_up_of_untracked_fields_not_recorded(
    hass: HomeAssistant, fence: MockConfigEntry
) -> None:
    """A held-back change's follow-up of unreported fields is no row either."""
    entry = await dummy_entry(hass, {"quiet": True})
    mark = await _mark(hass)
    collector = _collector(hass, ConfigEntryCollector)
    before = collector._snapshots[entry.entry_id]
    object.__setattr__(entry, "title", "Held")

    assert await hass.config_entries.async_reload(entry.entry_id)
    # Dispatched while the title change is held back.
    hass.config_entries.async_update_entry(entry, unique_id="u2")
    assert collector._pending[entry.entry_id][0] is before

    # Only the expired title change (R28), nothing for the unique ID.
    (expired,) = await _rows(hass, after=mark)
    assert expired.details["changed"] == ["title"]
    assert _is_system(expired)


async def test_entry_state_change_not_recorded(
    hass: HomeAssistant, fence: MockConfigEntry
) -> None:
    """A reload changes only the state: no row."""
    entry = await dummy_entry(hass, {"quiet": True})
    mark = await _mark(hass)

    assert await hass.config_entries.async_reload(entry.entry_id)

    assert entry.state is ConfigEntryState.LOADED
    assert await _rows(hass, after=mark) == []


async def test_disable_and_enable_recorded(
    hass: HomeAssistant,
    fence: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
) -> None:
    """Disabling and enabling are credited; the setup's update is System."""
    entry = await dummy_entry(hass)
    mark = await _mark(hass)
    ws = await hass_ws_client(hass)

    for disabled_by in ("user", None):
        await ws.send_json_auto_id(
            {
                "type": "config_entries/disable",
                "entry_id": entry.entry_id,
                "disabled_by": disabled_by,
            }
        )
        assert (await ws.receive_json())["success"]

    disabled, enabled, setup_update = await _entry_rows(hass, after=mark)
    assert disabled.action is ChangeAction.DISABLED
    assert disabled.details["changed"] == ["disabled_by"]
    assert _by_admin(disabled, hass_admin_user)
    assert enabled.action is ChangeAction.ENABLED
    assert _by_admin(enabled, hass_admin_user)
    assert setup_update.action is ChangeAction.UPDATED
    assert _is_system(setup_update)


async def test_entry_removed_attributed(
    hass: HomeAssistant,
    fence: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    hass_admin_user: MockUser,
) -> None:
    """Deleting an entry is credited, with its name."""
    entry = await dummy_entry(hass)
    mark = await _mark(hass)
    client = await hass_client()

    resp = await client.delete(f"/api/config/config_entries/entry/{entry.entry_id}")

    assert resp.status == HTTPStatus.OK
    (removed,) = await _entry_rows(hass, after=mark)
    assert removed.action is ChangeAction.REMOVED
    assert removed.target_id == entry.entry_id
    assert removed.target_name == f"{DUMMY}: Mock Title"
    assert _by_admin(removed, hass_admin_user)
    assert entry.entry_id not in _collector(hass, ConfigEntryCollector)._snapshots


async def test_entry_ws_update_and_subentry_attributed(
    hass: HomeAssistant,
    fence: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
) -> None:
    """Title, preference and subentry changes are credited UPDATEDs."""
    entry = await dummy_entry(hass, {"quiet": True})
    mark = await _mark(hass)
    ws = await hass_ws_client(hass)
    client = await hass_client()

    await ws.send_json_auto_id(
        {
            "type": "config_entries/update",
            "entry_id": entry.entry_id,
            "title": "Renamed",
            "pref_disable_new_entities": True,
        }
    )
    assert (await ws.receive_json())["success"]
    resp = await client.post(
        "/api/config/config_entries/subentries/flow",
        json={"handler": [entry.entry_id, "thing"]},
    )
    assert (await resp.json())["type"] == "create_entry"

    title, subentry = await _entry_rows(hass, after=mark)
    assert title.action is ChangeAction.UPDATED
    assert title.details["changed"] == ["pref_disable_new_entities", "title"]
    assert title.target_name == f"{DUMMY}: Renamed"
    assert _by_admin(title, hass_admin_user)
    assert subentry.action is ChangeAction.UPDATED
    assert subentry.details["changed"] == ["subentries"]
    assert _by_admin(subentry, hass_admin_user)


async def test_subentry_content_change_recorded(
    hass: HomeAssistant, fence: MockConfigEntry
) -> None:
    """A subentry's content counts, not only its ID."""
    entry = await dummy_entry(hass, {"quiet": True})
    mark = await _mark(hass)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "thing"), context={"source": "user"}
    )
    assert result["type"] == "create_entry"
    subentry = next(iter(entry.subentries.values()))

    hass.config_entries.async_update_subentry(entry, subentry, title="Renamed")

    *_, row = await _entry_rows(hass, after=mark)
    assert row.details["changed"] == ["subentries"]
    assert _is_system(row)


async def test_change_without_scope_is_system(
    hass: HomeAssistant, fence: MockConfigEntry
) -> None:
    """A change made by code, with no admin action, is System."""
    entry = await dummy_entry(hass, {"quiet": True})
    mark = await _mark(hass)

    hass.config_entries.async_update_entry(
        entry, title="By code", options={"a": 1}, data={"quiet": True, "b": 2}
    )
    await hass.config_entries.async_set_disabled_by(
        entry.entry_id, ConfigEntryDisabler.USER
    )

    update, disabled = await _entry_rows(hass, after=mark)
    assert update.action is ChangeAction.UPDATED
    assert update.details == {
        "domain": DUMMY,
        "changed": ["data", "options.a", "title"],
        "data_keys_changed": ["b"],
    }
    assert disabled.action is ChangeAction.DISABLED
    assert _is_system(update)
    assert _is_system(disabled)


async def test_secrets_never_in_details(
    hass: HomeAssistant,
    fence: MockConfigEntry,
    hass_client: ClientSessionGenerator,
) -> None:
    """Data values, old or new, never reach the database."""
    entry = await dummy_entry(hass, {"api_key": "SECRET123"})
    mark = await _mark(hass)
    client = await hass_client()

    resp = await client.post(
        "/api/config/config_entries/flow",
        json={"handler": DUMMY, "entry_id": entry.entry_id},
    )
    form = await resp.json()
    resp = await client.post(
        f"/api/config/config_entries/flow/{form['flow_id']}",
        json={"api_key": "SECRET456"},
    )

    assert (await resp.json())["reason"] == "reconfigure_successful"
    rows = await _entry_rows(hass, after=mark)
    reconfigured = next(r for r in rows if r.action is ChangeAction.RECONFIGURED)
    assert reconfigured.details["data_keys_changed"] == ["api_key", "reconfigured"]
    assert entry.data["api_key"] == "SECRET456"
    db = dump_db(hass)
    assert "SECRET123" not in db
    assert "SECRET456" not in db


async def test_scope_label_needs_credit(
    hass: HomeAssistant,
    fence: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another entry changed during an options flow is a System UPDATED."""
    entry = await dummy_entry(hass, {"quiet": True})
    other = await dummy_entry(hass, {"quiet": True})
    mark = await _mark(hass)
    original = hass.config_entries.async_update_entry

    def _update(entry_: Any, **kwargs: Any) -> bool:
        if entry_ is entry:
            original(other, title="Side effect")
        return original(entry_, **kwargs)

    monkeypatch.setattr(hass.config_entries, "async_update_entry", _update)
    client = await hass_client()
    resp = await client.post(
        "/api/config/config_entries/options/flow", json={"handler": entry.entry_id}
    )
    form = await resp.json()
    resp = await client.post(
        f"/api/config/config_entries/options/flow/{form['flow_id']}", json={}
    )

    assert (await resp.json())["type"] == "create_entry"
    side, options = await _entry_rows(hass, after=mark)
    assert side.target_id == other.entry_id
    assert side.action is ChangeAction.UPDATED
    assert _is_system(side)
    assert options.action is ChangeAction.OPTIONS_CHANGED
    assert not _is_system(options)


async def test_unknown_entry_update_seeds_snapshot(
    hass: HomeAssistant, fence: MockConfigEntry
) -> None:
    """An UPDATED for an entry never seen is not recorded, only remembered."""
    entry = await dummy_entry(hass, {"quiet": True})
    mark = await _mark(hass)
    collector = _collector(hass, ConfigEntryCollector)
    del collector._snapshots[entry.entry_id]

    hass.config_entries.async_update_entry(entry, title="First")
    hass.config_entries.async_update_entry(entry, title="Second")

    (row,) = await _rows(hass, after=mark)
    assert row.target_name == f"{DUMMY}: Second"


async def test_target_name_falls_back_to_domain(
    hass: HomeAssistant, fence: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the loaded integration, the domain names it; text is sanitised."""
    entry = await dummy_entry(hass, {"quiet": True})
    mark = await _mark(hass)
    hass.config_entries.async_update_entry(entry, title="A\x00B\nC")
    rows = await _entry_rows(hass, after=mark)
    assert rows[-1].target_name == f"{DUMMY}: ABC"

    monkeypatch.delitem(hass.data["integrations"], DUMMY)
    hass.config_entries.async_update_entry(entry, title="Plain")
    rows = await _entry_rows(hass, after=mark)
    assert rows[-1].target_name == f"{DUMMY}: Plain"


async def test_config_entry_listener_fails_open(
    hass: HomeAssistant, fence: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A collector bug never breaks Core's change; it is counted."""
    entry = await dummy_entry(hass, {"quiet": True})
    collector = _collector(hass, ConfigEntryCollector)
    errors = collector.guard.errors

    def _boom(*args: Any) -> Any:
        raise RuntimeError("bug")

    monkeypatch.setattr(collector, "_handle", _boom)
    assert hass.config_entries.async_update_entry(entry, title="Still works")
    assert entry.title == "Still works"
    assert collector.guard.errors == errors + 1


# H5: users, passwords and usernames.


async def test_user_created_updated_removed_attributed(
    hass: HomeAssistant,
    fence: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
) -> None:
    """Admin user changes are credited; a removed user keeps its name."""
    ws = await hass_ws_client(hass)

    await ws.send_json_auto_id({"type": "config/auth/create", "name": "Carol"})
    msg = await ws.receive_json()
    assert msg["success"], msg
    user_id = msg["result"]["user"]["id"]
    await ws.send_json_auto_id(
        {
            "type": "config/auth/update",
            "user_id": user_id,
            "name": "Caroline",
            "is_active": False,
            "local_only": True,
            "group_ids": ["system-admin"],
        }
    )
    assert (await ws.receive_json())["success"]
    # Saving without changes records nothing.
    await ws.send_json_auto_id(
        {"type": "config/auth/update", "user_id": user_id, "name": "Caroline"}
    )
    assert (await ws.receive_json())["success"]
    await ws.send_json_auto_id({"type": "config/auth/delete", "user_id": user_id})
    assert (await ws.receive_json())["success"]

    added, updated, removed = await _rows(hass, ChangeKind.USER)
    assert added.action is ChangeAction.ADDED
    assert added.target_name == "Carol"
    assert updated.action is ChangeAction.UPDATED
    assert updated.details == {
        "changed": ["group_ids", "is_active", "local_only", "name"]
    }
    assert removed.action is ChangeAction.REMOVED
    assert removed.target_name == "Caroline"
    for row in (added, updated, removed):
        assert row.target_id == user_id
        assert _by_admin(row, hass_admin_user)


async def test_user_added_by_code_is_system(
    hass: HomeAssistant, fence: MockConfigEntry
) -> None:
    """Users made outside an admin action are System."""
    user = await hass.auth.async_create_user("Bot")
    await hass.auth.async_update_user(user, name="Robot")
    await hass.auth.async_remove_user(user)

    rows = await _rows(hass, ChangeKind.USER)
    assert [r.action for r in rows] == [
        ChangeAction.ADDED,
        ChangeAction.UPDATED,
        ChangeAction.REMOVED,
    ]
    assert all(_is_system(r) for r in rows)


async def test_user_events_for_unknown_users_ignored(
    hass: HomeAssistant, fence: MockConfigEntry
) -> None:
    """Events naming no known user record nothing, or no name."""
    collector = _collector(hass, UserCollector)
    hass.bus.async_fire("user_added", {"user_id": "nope"})
    hass.bus.async_fire("user_updated", {"user_id": "nope"})
    hass.bus.async_fire("user_removed", {})
    user = await hass.auth.async_create_user("Late")
    del collector._users[user.id]
    # Not cached: only remembered.
    await hass.auth.async_update_user(user, name="Later")
    del collector._users[user.id]
    hass.bus.async_fire("user_removed", {"user_id": user.id})

    added, removed = await _rows(hass, ChangeKind.USER)
    assert added.action is ChangeAction.ADDED
    assert removed.action is ChangeAction.REMOVED
    assert removed.target_name is None
    assert collector.guard.errors == 0


async def test_password_change_attributed(
    hass: HomeAssistant,
    fence: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
) -> None:
    """An owner's password change for a user is credited to the owner."""
    hass_admin_user.is_owner = True
    user = await create_hass_user(hass, "erin", "old-secret", admin=False)
    ws = await hass_ws_client(hass)

    await ws.send_json_auto_id(
        {
            "type": "config/auth_provider/homeassistant/admin_change_password",
            "user_id": user.id,
            "password": "new-secret",
        }
    )
    msg = await ws.receive_json()

    assert msg["success"], msg
    *_, row = await _rows(hass, ChangeKind.USER)
    assert row.action is ChangeAction.PASSWORD_CHANGED
    assert row.target_id == user.id
    assert row.target_name == "erin"
    assert row.details == {"changed": ["password"]}
    assert _by_admin(row, hass_admin_user)
    db = dump_db(hass)
    assert "new-secret" not in db
    assert "old-secret" not in db


async def test_own_password_change_attributed(
    hass: HomeAssistant,
    fence: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """A user changing their own password is credited to themselves."""
    user = await create_hass_user(hass, "jill", "old-secret", admin=False)
    refresh_token = await hass.auth.async_create_refresh_token(
        user, CLIENT_ID, credential=user.credentials[0]
    )
    ws = await hass_ws_client(hass, hass.auth.async_create_access_token(refresh_token))

    await ws.send_json_auto_id(
        {
            "type": "config/auth_provider/homeassistant/change_password",
            "current_password": "old-secret",
            "new_password": "new-secret",
        }
    )
    msg = await ws.receive_json()

    assert msg["success"], msg
    *_, row = await _rows(hass, ChangeKind.USER)
    assert row.action is ChangeAction.PASSWORD_CHANGED
    assert row.target_id == user.id
    assert row.actor_user_id == user.id
    assert row.actor_ip == IP


async def test_own_profile_rename_attributed(
    hass: HomeAssistant,
    fence: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """A user renaming themselves (`person/update_own_profile`) is credited."""
    # Loaded after Electric Fence: its command is wrapped once it loads.
    assert await async_setup_component(hass, "person", {})
    user = await create_hass_user(hass, "kim", "secret", admin=False)
    refresh_token = await hass.auth.async_create_refresh_token(
        user, CLIENT_ID, credential=user.credentials[0]
    )
    ws = await hass_ws_client(hass, hass.auth.async_create_access_token(refresh_token))

    await ws.send_json_auto_id(
        {"type": "person/update_own_profile", "name": "Kimberly"}
    )
    msg = await ws.receive_json()

    assert msg["success"], msg
    *_, row = await _rows(hass, ChangeKind.USER)
    assert row.action is ChangeAction.UPDATED
    assert row.target_id == user.id
    assert row.actor_user_id == user.id
    assert row.actor_ip == IP


async def test_username_change_attributed(
    hass: HomeAssistant,
    fence: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
) -> None:
    """An owner's username change is a credited UPDATED of the user."""
    hass_admin_user.is_owner = True
    user = await create_hass_user(hass, "frank", "secret", admin=False)
    ws = await hass_ws_client(hass)

    await ws.send_json_auto_id(
        {
            "type": "config/auth_provider/homeassistant/admin_change_username",
            "user_id": user.id,
            "username": "francis",
        }
    )
    msg = await ws.receive_json()

    assert msg["success"], msg
    *_, row = await _rows(hass, ChangeKind.USER)
    assert row.action is ChangeAction.UPDATED
    assert row.details == {"changed": ["username"]}
    assert row.target_id == user.id
    assert _by_admin(row, hass_admin_user)


async def test_password_change_failure_passes_through(
    hass: HomeAssistant, fence: MockConfigEntry, local_auth: HassAuthProvider
) -> None:
    """Core's exception passes through unchanged and nothing is recorded."""
    before = len(await _rows(hass, ChangeKind.USER))

    with pytest.raises(InvalidUser):
        await local_auth.async_change_password("nobody", "x")

    assert len(await _rows(hass, ChangeKind.USER)) == before


async def test_password_change_by_code_is_system(
    hass: HomeAssistant, fence: MockConfigEntry, local_auth: HassAuthProvider
) -> None:
    """A password changed by code is System; an unknown user has no target."""
    user = await create_hass_user(hass, "gina", "secret")
    await local_auth.async_change_password("gina", "other")
    await local_auth.async_add_auth("unlinked", "secret")
    await local_auth.async_change_password(username="unlinked", new_password="x")

    *_, row, unlinked = await _rows(hass, ChangeKind.USER)
    assert row.action is ChangeAction.PASSWORD_CHANGED
    assert row.target_id == user.id
    assert _is_system(row)
    assert unlinked.target_id is None
    assert unlinked.target_name is None


async def test_provider_wrappers_fail_open(
    hass: HomeAssistant,
    fence: MockConfigEntry,
    local_auth: HassAuthProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recording bug never breaks the password change."""
    await create_hass_user(hass, "hank", "secret")
    collector = _collector(hass, UserCollector)

    def _boom(*args: Any) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(collector, "_record_credential_change", _boom)
    await local_auth.async_change_password("hank", "other")

    assert collector.guard.errors == 1
    assert local_auth.data is not None
    await hass.async_add_executor_job(local_auth.data.validate_login, "hank", "other")


async def test_unload_restores_provider_methods(
    hass: HomeAssistant, fence: MockConfigEntry, local_auth: HassAuthProvider
) -> None:
    """Unloading puts the provider's own methods back."""
    assert "async_change_password" in vars(local_auth)

    assert await hass.config_entries.async_unload(fence.entry_id)

    assert "async_change_password" not in vars(local_auth)
    assert "async_change_username" not in vars(local_auth)


async def test_users_without_hass_provider(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """With no `homeassistant` provider, users are still recorded."""
    assert hass.auth.get_auth_provider("homeassistant", None) is None
    collector = _collector(hass, UserCollector)
    assert collector.disabled_reason == []

    await hass.auth.async_create_user("Ivy")

    (row,) = await _rows(hass, ChangeKind.USER)
    assert row.action is ChangeAction.ADDED


# H6: core config.


async def test_core_config_update_attributed(
    hass: HomeAssistant,
    fence: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
) -> None:
    """A core config update is credited and stores only the keys."""
    ws = await hass_ws_client(hass)

    await ws.send_json_auto_id(
        {"type": "config/core/update", "location_name": "Hut-XYZZY"}
    )
    msg = await ws.receive_json()

    assert msg["success"], msg
    (row,) = await _rows(hass, ChangeKind.CORE_CONFIG)
    assert row.action is ChangeAction.UPDATED
    assert row.details == {"changed": ["location_name"], "keys": ["location_name"]}
    assert row.target_id is None
    assert row.target_name == "Core settings"
    assert _by_admin(row, hass_admin_user)
    assert "Hut-XYZZY" not in dump_db(hass)


async def test_core_config_startup_event_without_data_ignored(
    hass: HomeAssistant, fence: MockConfigEntry
) -> None:
    """Core fires the event with no data at startup: no row."""
    hass.bus.async_fire_internal(EVENT_CORE_CONFIG_UPDATE)
    hass.bus.async_fire_internal(EVENT_CORE_CONFIG_UPDATE, {})

    assert await _rows(hass, ChangeKind.CORE_CONFIG) == []


async def test_core_config_update_by_code_is_system(
    hass: HomeAssistant, fence: MockConfigEntry
) -> None:
    """A core config update made by code is System."""
    await hass.config.async_update(location_name="Shed", elevation=7)

    (row,) = await _rows(hass, ChangeKind.CORE_CONFIG)
    assert row.details == {
        "changed": ["elevation", "location_name"],
        "keys": ["elevation", "location_name"],
    }
    assert _is_system(row)


def test_hash_of_mixed_type_keys() -> None:
    """Data with keys JSON can't sort still hashes, by `str(key)` order."""

    def _data(entry_data: dict[Any, Any]) -> Any:
        return EntrySnapshot.of(MockConfigEntry(domain=DUMMY, data=entry_data)).data

    assert _data({1: "a", "b": 2}) == _data({"b": 2, 1: "a"})
    nested = {"x": [{1: "a", "b": 2}]}
    assert _data(nested) == _data({"x": [{"b": 2, 1: "a"}]})
    assert _data(nested) != _data({"x": [{1: "a", "b": 3}]})


# Before and after values (record_values).

WEBHOOK = "https://ha.example/api/webhook/0123456789abcdef0123456789abcdef"


def _set_record_values(hass: HomeAssistant, value: bool) -> None:
    runtime = hass.config_entries.async_loaded_entries(DOMAIN)[0].runtime_data
    runtime.settings = replace(runtime.settings, record_values=value)


@pytest.mark.parametrize("record_values", [False, True])
async def test_core_rename_values(
    hass: HomeAssistant,
    fence: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
    record_values: bool,
) -> None:
    """A core rename shows before and after only when values are on."""
    await hass.config.async_update(location_name="Home")
    mark = await _mark(hass)
    _set_record_values(hass, record_values)
    ws = await hass_ws_client(hass)

    await ws.send_json_auto_id(
        {"type": "config/core/update", "location_name": "Home test"}
    )
    assert (await ws.receive_json())["success"]

    (row,) = await _rows(hass, ChangeKind.CORE_CONFIG, after=mark)
    assert _by_admin(row, hass_admin_user)
    expected: dict[str, Any] = {
        "changed": ["location_name"],
        "keys": ["location_name"],
    }
    if record_values:
        expected["values"] = {"location_name": {"before": "Home", "after": "Home test"}}
    assert row.details == expected


async def test_core_coordinates_hidden(
    hass: HomeAssistant, fence: MockConfigEntry
) -> None:
    """Coordinates are hidden; the unit system compares its JSON form."""
    _set_record_values(hass, True)
    latitude = hass.config.latitude + 1.234567
    units = "us_customary" if hass.config.units is METRIC_SYSTEM else "metric"

    await hass.config.async_update(latitude=latitude, unit_system=units)

    (row,) = await _rows(hass, ChangeKind.CORE_CONFIG)
    assert row.details["changed"] == ["latitude", "unit_system"]
    assert row.details["values"]["latitude"] == {"hidden": True}
    unit_values = row.details["values"]["unit_system"]
    assert unit_values["before"] != unit_values["after"]
    assert unit_values["after"] == hass.config.units.as_dict()
    assert str(latitude) not in dump_db(hass)


async def test_core_unchanged_save_and_unknown_key(
    hass: HomeAssistant, fence: MockConfigEntry
) -> None:
    """A save without a change is no row; a key not in as_dict is compared."""
    _set_record_values(hass, True)
    await hass.config.async_update(location_name=hass.config.location_name)
    hass.bus.async_fire_internal(EVENT_CORE_CONFIG_UPDATE, {"future": "a"})
    # The same value again: no row, but the snapshot still moves on.
    hass.bus.async_fire_internal(EVENT_CORE_CONFIG_UPDATE, {"future": "a"})
    hass.bus.async_fire_internal(EVENT_CORE_CONFIG_UPDATE, {"future": "b"})

    first, second = await _rows(hass, ChangeKind.CORE_CONFIG)
    assert first.details["values"] == {"future": {"before": None, "after": "a"}}
    assert second.details["values"] == {"future": {"before": "a", "after": "b"}}


@pytest.mark.parametrize("record_values", [False, True])
async def test_entry_options_values(
    hass: HomeAssistant, fence: MockConfigEntry, record_values: bool
) -> None:
    """Option values are shown, redacted; secrets in URLs are masked."""
    entry = await dummy_entry(hass, {"quiet": True})
    hass.config_entries.async_update_entry(entry, options={"scan_interval": 30})
    mark = await _mark(hass)
    _set_record_values(hass, record_values)

    hass.config_entries.async_update_entry(
        entry, options={"scan_interval": 60, "webhook_url": WEBHOOK}
    )

    (row,) = await _entry_rows(hass, after=mark)
    expected: dict[str, Any] = {
        "domain": DUMMY,
        "changed": ["options.scan_interval", "options.webhook_url"],
        "data_keys_changed": [],
    }
    if record_values:
        expected["values"] = {
            "options.scan_interval": {"before": 30, "after": 60},
            "options.webhook_url": {
                "before": None,
                "after": "https://ha.example/api/webhook/•••",
            },
        }
    assert row.details == expected
    assert "0123456789abcdef0123456789abcdef" not in dump_db(hass)


async def test_entry_fields_values(hass: HomeAssistant, fence: MockConfigEntry) -> None:
    """Title, disabled-by and preferences carry values; subentries names."""
    entry = await dummy_entry(hass, {"quiet": True})
    mark = await _mark(hass)
    _set_record_values(hass, True)

    hass.config_entries.async_update_entry(
        entry, title="Renamed", pref_disable_polling=True
    )
    await hass.config_entries.async_set_disabled_by(
        entry.entry_id, ConfigEntryDisabler.USER
    )
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "thing"), context={"source": "user"}
    )
    assert result["type"] == "create_entry"

    update, disabled, subentry = await _entry_rows(hass, after=mark)
    assert update.details["values"] == {
        "pref_disable_polling": {"before": False, "after": True},
        "title": {"before": "Mock Title", "after": "Renamed"},
    }
    assert disabled.details["values"] == {
        "disabled_by": {"before": None, "after": "user"}
    }
    assert subentry.details == {
        "domain": DUMMY,
        "changed": ["subentries"],
        "data_keys_changed": [],
    }


async def test_entry_data_values_never_stored(
    hass: HomeAssistant, fence: MockConfigEntry
) -> None:
    """A data change names its keys only, even with values on."""
    entry = await dummy_entry(hass, {"quiet": True, "host": "old-host-XYZZY"})
    mark = await _mark(hass)
    _set_record_values(hass, True)

    hass.config_entries.async_update_entry(
        entry, data={"quiet": True, "host": "new-host-XYZZY"}
    )

    (row,) = await _entry_rows(hass, after=mark)
    assert row.details == {
        "domain": DUMMY,
        "changed": ["data"],
        "data_keys_changed": ["host"],
    }
    assert "XYZZY" not in dump_db(hass)


async def test_entry_option_values_not_shared(
    hass: HomeAssistant, fence: MockConfigEntry
) -> None:
    """The snapshot keeps its own copy, or a shallow one it can't deep-copy."""
    lock = threading.Lock()
    entry = MockConfigEntry(domain=DUMMY, options={"a": {"b": 1}, "lock": lock})
    snapshot = EntrySnapshot.of(entry)
    assert snapshot.option_values["lock"] is lock
    plain = EntrySnapshot.of(MockConfigEntry(domain=DUMMY, options={"a": {"b": 1}}))
    entry_options = {"a": {"b": 1}}
    copied = EntrySnapshot.of(MockConfigEntry(domain=DUMMY, options=entry_options))
    entry_options["a"]["b"] = 2
    assert copied.option_values == plain.option_values == {"a": {"b": 1}}


@pytest.mark.parametrize("record_values", [False, True])
async def test_user_rename_values(
    hass: HomeAssistant,
    fence: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
    record_values: bool,
) -> None:
    """A user rename shows the names; groups compare as sorted lists."""
    user = await hass.auth.async_create_user("Carol")
    mark = await _mark(hass)
    _set_record_values(hass, record_values)
    ws = await hass_ws_client(hass)

    await ws.send_json_auto_id(
        {
            "type": "config/auth/update",
            "user_id": user.id,
            "name": "Caroline",
            "group_ids": ["system-admin"],
        }
    )
    assert (await ws.receive_json())["success"]

    (row,) = await _rows(hass, ChangeKind.USER, after=mark)
    assert _by_admin(row, hass_admin_user)
    expected: dict[str, Any] = {"changed": ["group_ids", "name"]}
    if record_values:
        expected["values"] = {
            "group_ids": {"before": [], "after": ["system-admin"]},
            "name": {"before": "Carol", "after": "Caroline"},
        }
    assert row.details == expected


async def test_username_and_password_values(
    hass: HomeAssistant, fence: MockConfigEntry, local_auth: HassAuthProvider
) -> None:
    """A username shows its values; a password never does."""
    user = await create_hass_user(hass, "olga", "old-secret")
    _set_record_values(hass, True)

    await local_auth.async_change_username(
        credential=user.credentials[0], new_username="olgaa"
    )
    await local_auth.async_change_password("olgaa", "new-secret")

    *_, username, password = await _rows(hass, ChangeKind.USER)
    assert username.details == {
        "changed": ["username"],
        "values": {"username": {"before": "olga", "after": "olgaa"}},
    }
    assert password.details == {"changed": ["password"]}
    db = dump_db(hass)
    assert "old-secret" not in db
    assert "new-secret" not in db


async def test_toggling_record_values_is_recorded(
    hass: HomeAssistant,
    fence: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    hass_admin_user: MockUser,
) -> None:
    """Turning values on is an admin's options change of Electric Fence."""
    mark = await _mark(hass)
    client = await hass_client()

    resp = await client.post(
        "/api/config/config_entries/options/flow", json={"handler": fence.entry_id}
    )
    form = await resp.json()
    resp = await client.post(
        f"/api/config/config_entries/options/flow/{form['flow_id']}",
        json={**GENERAL, "record_values": True},
    )

    assert (await resp.json())["type"] == "create_entry"
    rows = [
        r
        for r in await _rows(hass, ChangeKind.CONFIG_ENTRY, after=mark)
        if r.target_id == fence.entry_id
    ]
    (row,) = rows
    assert row.action is ChangeAction.OPTIONS_CHANGED
    assert _by_admin(row, hass_admin_user)
    assert row.details["domain"] == DOMAIN
    assert "options.record_values" in row.details["changed"]
    assert fence.runtime_data.settings.record_values is True


def test_credential_username_without_data() -> None:
    """Something that is no credential gives no old username."""
    assert _credential_username(None) is None
    assert _credential_username(MockUser()) is None


async def test_entry_option_named_like_a_field(
    hass: HomeAssistant, fence: MockConfigEntry
) -> None:
    """Options are namespaced: an option `title` never hides the entry title."""
    entry = await dummy_entry(hass, {"quiet": True})
    hass.config_entries.async_update_entry(
        entry, options={"title": "Opt", "disabled_by": None, "data": 1}
    )
    mark = await _mark(hass)
    _set_record_values(hass, True)

    # (a) The entry renamed, the `title` option unchanged.
    hass.config_entries.async_update_entry(entry, title="Renamed")
    # (b) The `title` option changed, the entry title unchanged.
    hass.config_entries.async_update_entry(
        entry, options={"title": "Opt 2", "disabled_by": None, "data": 1}
    )

    renamed, option = await _entry_rows(hass, after=mark)
    assert renamed.details["changed"] == ["title"]
    assert renamed.details["values"] == {
        "title": {"before": "Mock Title", "after": "Renamed"}
    }
    assert option.details["changed"] == ["options.title"]
    assert option.details["values"] == {
        "options.title": {"before": "Opt", "after": "Opt 2"}
    }


async def test_entry_sensitive_option_hidden_under_namespace(
    hass: HomeAssistant, fence: MockConfigEntry
) -> None:
    """The name rule still sees `api_key` behind the `options.` prefix."""
    entry = await dummy_entry(hass, {"quiet": True})
    mark = await _mark(hass)
    _set_record_values(hass, True)

    hass.config_entries.async_update_entry(entry, options={"api_key": "plain words"})

    (row,) = await _entry_rows(hass, after=mark)
    assert row.details["changed"] == ["options.api_key"]
    assert row.details["values"] == {"options.api_key": {"hidden": True}}
    assert "plain words" not in dump_db(hass)


async def test_entry_option_changed_by_hash_only_is_namespaced(
    hass: HomeAssistant, fence: MockConfigEntry
) -> None:
    """A change `==` misses (1 to 1.0) is still named, with its namespace."""
    entry = await dummy_entry(hass, {"quiet": True})
    collector = _collector(hass, ConfigEntryCollector)
    old = EntrySnapshot.of(MockConfigEntry(domain=DUMMY, options={"n": 1}))
    new = EntrySnapshot.of(MockConfigEntry(domain=DUMMY, options={"n": 1.0}))
    mark = await _mark(hass)

    # Core itself sees no change here, so the snapshots are given directly.
    collector._record_update(entry, old, new, credit=False)

    (row,) = await _entry_rows(hass, after=mark)
    assert row.details["changed"] == ["options.n"]
