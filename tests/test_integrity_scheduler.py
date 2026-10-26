"""Scheduled integration checks: findings, repairs and posture."""

import asyncio
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.electric_fence import posture
from custom_components.electric_fence.const import DOMAIN, MIN_HA_VERSION
from custom_components.electric_fence.integrity import scheduler
from custom_components.electric_fence.integrity.files import FileReport
from custom_components.electric_fence.integrity.health import HealthResult, Status
from custom_components.electric_fence.integrity.inventory import InstalledIntegration
from custom_components.electric_fence.integrity.reference import FileResult
from custom_components.electric_fence.integrity.scheduler import IntegrityScheduler
from custom_components.electric_fence.models import ChangeAction, ChangeKind
from custom_components.electric_fence.notifier import EVENT_SETTINGS_CHANGED
from custom_components.electric_fence.settings import Settings
from custom_components.electric_fence.storage import ChangeFilter, FenceStore
from homeassistant.components.marketplace.const import SIGNAL_REPOSITORY_INSTALLED
from homeassistant.components.marketplace.enums import MarketplaceSignal
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from tests.helpers import change_rows, flush_worker


def make_item(domain: str = "demo", **kwargs: Any) -> InstalledIntegration:
    """Return an installed Marketplace integration (or `source="manual"`)."""
    values: dict[str, Any] = {
        "domain": domain,
        "path": Path("/nonexistent") / domain,
        "version": "1.0.0",
        "source": "marketplace",
        "repository": f"owner/{domain}",
        "installed_version": "1.0.0",
    }
    values.update(kwargs)
    return InstalledIntegration(**values)


def files(
    status: str = "match",
    domain: str = "demo",
    *,
    changed: list[str] | None = None,
    missing: list[str] | None = None,
    added: list[str] | None = None,
    reason: str | None = None,
) -> FileResult:
    """Return a file check result."""
    report = None
    if status in ("match", "changed"):
        report = FileReport(changed or [], missing or [], added or [], [], [])
    return FileResult(
        domain,
        "marketplace",
        status,  # type: ignore[arg-type]
        report=report,
        reason=reason,
        ref="1.0.0",
    )


def health(status: Status = "ok", domain: str = "demo") -> HealthResult:
    """Return a health result."""
    level = {"removed": "critical", "archived": "warning", "unmaintained": "notice"}
    return HealthResult(domain, status, level.get(status, "ok"), None, None)  # type: ignore[arg-type]


@dataclass
class Fake:
    """What the patched inventory, file check and health check return."""

    items: list[InstalledIntegration] = field(default_factory=lambda: [make_item()])
    files: dict[str, FileResult] = field(default_factory=dict)
    health: dict[str, HealthResult] = field(default_factory=dict)
    contract: bool = True
    inventory: AsyncMock = field(default_factory=AsyncMock)
    verify: AsyncMock = field(default_factory=AsyncMock)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Fake:
    """Patch the checks the scheduler runs."""
    state = Fake()

    async def inventory(_hass: HomeAssistant) -> list[InstalledIntegration]:
        return list(state.items)

    async def verify(_hass: HomeAssistant, _rt: Any, it: InstalledIntegration) -> Any:
        result = state.files.get(it.domain, files(domain=it.domain))
        if isinstance(result, Exception):
            raise result
        return result

    def health_of(it: InstalledIntegration, _now: Any, _months: int) -> HealthResult:
        return state.health.get(it.domain, health(domain=it.domain))

    state.inventory.side_effect = inventory
    state.verify.side_effect = verify
    monkeypatch.setattr(scheduler, "async_inventory", state.inventory)
    monkeypatch.setattr(scheduler, "verify", state.verify)
    monkeypatch.setattr(scheduler, "health_of", health_of)
    monkeypatch.setattr(scheduler, "marketplace_contract_ok", lambda _h: state.contract)
    return state


async def setup_with(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, **options: Any
) -> MockConfigEntry:
    """Set up Electric Fence with some settings changed."""
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", MIN_HA_VERSION)
    entry = MockConfigEntry(
        domain=DOMAIN, options=replace(Settings(), **options).to_options()
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def integrity(entry: MockConfigEntry) -> IntegrityScheduler:
    """Return the scheduler of the entry."""
    sched = entry.runtime_data.integrity
    assert sched is not None
    return sched


async def tick(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, delta: timedelta
) -> None:
    """Move time on and let timers and their scans finish."""
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        sched = entry.runtime_data.integrity
        if sched is not None and (task := sched._task) is not None:  # noqa: SLF001
            await asyncio.wait({task})


def file_rows(hass: HomeAssistant, kind: ChangeKind = ChangeKind.FILES) -> list[Any]:
    """Return the stored integration records of one kind, oldest first."""
    return [r for r in change_rows(hass) if r.kind == kind]


def our_issues(hass: HomeAssistant) -> set[str]:
    """Return the issue IDs Electric Fence raised for integrations."""
    return {
        issue_id
        for (domain, issue_id) in ir.async_get(hass).issues
        if domain == DOMAIN and issue_id.startswith("integration_")
    }


async def posture_items(
    hass: HomeAssistant, entry: MockConfigEntry
) -> dict[str, dict[str, Any]]:
    """Return the integration posture items by ID."""
    runtime = entry.runtime_data
    items = await posture.async_check(hass, runtime, runtime.store)
    return {i["id"]: i for i in items if i["id"].startswith("integration_")}


async def test_first_scan_after_delay(
    hass: HomeAssistant,
    fake: Fake,
    freezer: FrozenDateTimeFactory,
    setup_entry: MockConfigEntry,
) -> None:
    """No scan at startup; one after 30 minutes, then one every 24 hours."""
    await hass.async_block_till_done()
    assert fake.inventory.await_count == 0

    await tick(hass, freezer, timedelta(minutes=29))
    assert fake.inventory.await_count == 0
    await tick(hass, freezer, timedelta(minutes=1))
    assert fake.inventory.await_count == 1
    assert [s.domain for s in integrity(setup_entry).results()] == ["demo"]

    await tick(hass, freezer, timedelta(hours=23, minutes=59))
    assert fake.inventory.await_count == 1
    await tick(hass, freezer, timedelta(minutes=1))
    assert fake.inventory.await_count == 2


async def test_delay_setting_respected(
    hass: HomeAssistant,
    fake: Fake,
    monkeypatch: pytest.MonkeyPatch,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The first scan waits `scan_delay_minutes`."""
    await setup_with(hass, monkeypatch, scan_delay_minutes=90)

    await tick(hass, freezer, timedelta(minutes=89))
    assert fake.inventory.await_count == 0
    await tick(hass, freezer, timedelta(minutes=1))
    assert fake.inventory.await_count == 1


@pytest.mark.parametrize(
    ("signal", "payload"),
    [
        (SIGNAL_REPOSITORY_INSTALLED, object()),
        (
            "marketplace_repository_install_progress",
            {"repository": "owner/demo", "progress": False},
        ),
    ],
)
async def test_marketplace_signal_rescans(
    hass: HomeAssistant,
    fake: Fake,
    freezer: FrozenDateTimeFactory,
    setup_entry: MockConfigEntry,
    signal: str,
    payload: Any,
) -> None:
    """An install or update schedules one scan a delay later; signals don't stack."""
    await tick(hass, freezer, timedelta(minutes=30))
    assert fake.inventory.await_count == 1

    async_dispatcher_send(hass, signal, payload)
    await tick(hass, freezer, timedelta(minutes=10))
    async_dispatcher_send(hass, signal, payload)
    await tick(hass, freezer, timedelta(minutes=19))
    assert fake.inventory.await_count == 1
    await tick(hass, freezer, timedelta(minutes=1))
    assert fake.inventory.await_count == 2
    await tick(hass, freezer, timedelta(minutes=30))
    assert fake.inventory.await_count == 2


async def test_install_progress_running_does_not_rescan(
    hass: HomeAssistant,
    fake: Fake,
    freezer: FrozenDateTimeFactory,
    setup_entry: MockConfigEntry,
) -> None:
    """Only the end of an install schedules a scan, not its progress."""
    await tick(hass, freezer, timedelta(minutes=30))
    async_dispatcher_send(
        hass,
        "marketplace_repository_install_progress",
        {"repository": "owner/demo", "progress": 40},
    )
    await tick(hass, freezer, timedelta(minutes=30))
    assert fake.inventory.await_count == 1


async def test_records_on_transition_only(
    hass: HomeAssistant, fake: Fake, setup_entry: MockConfigEntry
) -> None:
    """The same `changed` twice records once; a later `match` records once."""
    events = async_capture_events(hass, EVENT_SETTINGS_CHANGED)
    fake.files["demo"] = files("changed", changed=["a.py"], added=["x.py"])
    sched = integrity(setup_entry)

    await sched.scan()
    await sched.scan()
    await flush_worker(hass)

    rows = file_rows(hass)
    assert [r.action for r in rows] == [ChangeAction.FILES_CHANGED]
    row = rows[0]
    assert row.target_id == "demo"
    assert row.target_name == "demo"
    assert row.actor_user_id is None
    assert row.actor_name is None
    assert row.details == {
        "changed": ["a.py"],
        "missing": [],
        "added": ["x.py"],
        "unreadable": [],
        "repository": "owner/demo",
        "ref": "1.0.0",
    }
    assert our_issues(hass) == {"integration_files_changed_demo"}

    fake.files["demo"] = files("changed", changed=["a.py", "b.py"])
    await sched.scan()
    fake.files["demo"] = files("match")
    await sched.scan()
    await sched.scan()
    await flush_worker(hass)

    assert [r.action for r in file_rows(hass)] == [
        ChangeAction.FILES_CHANGED,
        ChangeAction.FILES_CHANGED,
        ChangeAction.FILES_MATCH,
    ]
    assert our_issues(hass) == set()
    await hass.async_block_till_done()
    assert [e.data["action"] for e in events] == [
        "files_changed",
        "files_changed",
        "files_match",
    ]


async def test_baselines_recorded(
    hass: HomeAssistant, fake: Fake, setup_entry: MockConfigEntry
) -> None:
    """A recorded and a renewed baseline each record their action."""
    fake.items = [make_item(source="manual", repository=None)]
    sched = integrity(setup_entry)
    fake.files["demo"] = files("baseline_recorded")
    await sched.scan()
    fake.files["demo"] = files("match")
    await sched.scan()
    fake.files["demo"] = files("baseline_renewed")
    await sched.scan()
    await flush_worker(hass)

    assert [r.action for r in file_rows(hass)] == [
        ChangeAction.BASELINE_RECORDED,
        ChangeAction.BASELINE_RENEWED,
    ]


async def test_unchecked_keeps_state_no_repair(
    hass: HomeAssistant, fake: Fake, setup_entry: MockConfigEntry
) -> None:
    """`unchecked` after `changed` records nothing and keeps the repair."""
    sched = integrity(setup_entry)
    fake.files["demo"] = files("changed", changed=["a.py"])
    await sched.scan()
    fake.files["demo"] = files("unchecked", reason="unreachable")
    results = await sched.scan()
    await flush_worker(hass)

    assert [r.action for r in file_rows(hass)] == [ChangeAction.FILES_CHANGED]
    assert our_issues(hass) == {"integration_files_changed_demo"}
    assert results[0].files is not None
    assert results[0].files.status == "unchecked"
    items = await posture_items(hass, setup_entry)
    assert items["integration_files_changed"]["level"] == "warning"
    assert items["integration_checks_unchecked"] == {
        "id": "integration_checks_unchecked",
        "level": "info",
        "params": {"integrations": ["demo"]},
    }


async def test_unchecked_first_time_raises_nothing(
    hass: HomeAssistant, fake: Fake, setup_entry: MockConfigEntry
) -> None:
    """A copy that can't be fetched is never a change and never a repair."""
    fake.files["demo"] = files("unchecked", reason="not_found")
    await integrity(setup_entry).scan()
    await flush_worker(hass)

    assert file_rows(hass) == []
    assert our_issues(hass) == set()


async def test_repair_placeholders(
    hass: HomeAssistant, fake: Fake, setup_entry: MockConfigEntry
) -> None:
    """The files repair names the first 10 paths and how many more."""
    fake.files["demo"] = files("changed", changed=[f"f{i:02}.py" for i in range(13)])
    await integrity(setup_entry).scan()

    issue = ir.async_get(hass).async_get_issue(DOMAIN, "integration_files_changed_demo")
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == "integration_files_changed"
    assert issue.translation_placeholders == {
        "name": "demo",
        "files": ", ".join(f"f{i:02}\\.py" for i in range(10)) + " and 3 more",
    }


async def test_repair_placeholders_escape_markdown(
    hass: HomeAssistant, fake: Fake, setup_entry: MockConfigEntry
) -> None:
    """A file name can't add a link or HTML to the repair, which is Markdown."""
    fake.files["demo"] = files("changed", added=["[x](http://evil)\n<b>.py"])
    await integrity(setup_entry).scan()

    issue = ir.async_get(hass).async_get_issue(DOMAIN, "integration_files_changed_demo")
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["files"] == (
        "\\[x\\]\\(http://evil\\) \\<b\\>\\.py"
    )


async def test_health_transitions(
    hass: HomeAssistant, fake: Fake, setup_entry: MockConfigEntry
) -> None:
    """Archived, unmaintained and back to ok each record once, with repairs."""
    sched = integrity(setup_entry)
    fake.health["demo"] = health("archived")
    await sched.scan()
    await sched.scan()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "integration_archived_demo")
    assert issue is not None
    assert issue.translation_placeholders == {"name": "demo", "months": "4"}

    fake.health["demo"] = health("unmaintained")
    await sched.scan()
    assert our_issues(hass) == {"integration_unmaintained_demo"}
    items = await posture_items(hass, setup_entry)
    assert items["integration_health"]["level"] == "warning"
    assert items["integration_health"]["params"] == {
        "removed": [],
        "archived": [],
        "unmaintained": ["demo"],
    }

    fake.health["demo"] = health("ok")
    await sched.scan()
    await sched.scan()
    await flush_worker(hass)

    rows = file_rows(hass, ChangeKind.INTEGRATION_HEALTH)
    assert [r.action for r in rows] == [
        ChangeAction.ARCHIVED,
        ChangeAction.UNMAINTAINED,
        ChangeAction.RECOVERED,
    ]
    assert rows[0].details["repository"] == "owner/demo"
    assert our_issues(hass) == set()
    items = await posture_items(hass, setup_entry)
    assert items["integration_health"]["level"] == "ok"


async def test_repairs_switch_off_clears(
    hass: HomeAssistant, fake: Fake, setup_entry: MockConfigEntry
) -> None:
    """Turning repairs off removes them at once; the records and events stay."""
    events = async_capture_events(hass, EVENT_SETTINGS_CHANGED)
    fake.files["demo"] = files("changed", changed=["a.py"])
    fake.health["demo"] = health("archived")
    await integrity(setup_entry).scan()
    await flush_worker(hass)
    assert our_issues(hass) == {
        "integration_files_changed_demo",
        "integration_archived_demo",
    }

    hass.config_entries.async_update_entry(
        setup_entry,
        options=replace(
            Settings.from_options(setup_entry.options), integration_repairs=False
        ).to_options(),
    )
    await hass.async_block_till_done()

    assert our_issues(hass) == set()
    assert {r.action for r in change_rows(hass)} >= {
        ChangeAction.FILES_CHANGED,
        ChangeAction.ARCHIVED,
    }
    assert {e.data["action"] for e in events} >= {"files_changed", "archived"}
    assert await posture_items(hass, setup_entry) == {}

    await integrity(setup_entry).scan()
    assert our_issues(hass) == set()

    hass.config_entries.async_update_entry(
        setup_entry,
        options=replace(
            Settings.from_options(setup_entry.options), integration_repairs=True
        ).to_options(),
    )
    await hass.async_block_till_done()
    assert our_issues(hass) == {
        "integration_files_changed_demo",
        "integration_archived_demo",
    }


async def test_no_repair_for_removed(
    hass: HomeAssistant, fake: Fake, setup_entry: MockConfigEntry
) -> None:
    """A removed repository is critical in posture but raises no repair here."""
    fake.items = [make_item(), make_item("other")]
    fake.health["demo"] = health("removed")
    fake.health["other"] = health("archived", "other")
    await integrity(setup_entry).scan()
    await flush_worker(hass)

    assert our_issues(hass) == {"integration_archived_other"}
    item = (await posture_items(hass, setup_entry))["integration_health"]
    assert item == {
        "id": "integration_health",
        "level": "critical",
        "params": {"removed": ["demo"], "archived": ["other"], "unmaintained": []},
    }
    assert [r.action for r in file_rows(hass, ChangeKind.INTEGRATION_HEALTH)] == [
        ChangeAction.REMOVED_FROM_STORE,
        ChangeAction.ARCHIVED,
    ]


async def test_uninstalled_integration_findings_dropped(
    hass: HomeAssistant, fake: Fake, setup_entry: MockConfigEntry
) -> None:
    """An integration that is gone loses its findings and its repairs."""
    fake.items = [make_item(), make_item("other")]
    fake.files["demo"] = files("changed", changed=["a.py"])
    sched = integrity(setup_entry)
    store = setup_entry.runtime_data.store
    await sched.scan()
    assert await hass.async_add_executor_job(store.finding, "demo", "files")

    fake.items = [make_item("other")]
    await sched.scan()

    assert await hass.async_add_executor_job(store.finding, "demo", "files") is None
    assert await hass.async_add_executor_job(store.finding, "other", "files")
    assert our_issues(hass) == set()
    assert [s.domain for s in sched.results()] == ["other"]


async def test_scan_one_at_a_time(
    hass: HomeAssistant, fake: Fake, setup_entry: MockConfigEntry
) -> None:
    """A second scan while one runs waits for it and gets its result."""
    release = asyncio.Event()
    running = 0
    peak = 0

    async def slow(_hass: HomeAssistant, _rt: Any, it: InstalledIntegration) -> Any:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await release.wait()
        running -= 1
        return files(domain=it.domain)

    fake.items = [make_item(), make_item("other")]
    fake.verify.side_effect = slow
    sched = integrity(setup_entry)
    first = hass.async_create_task(sched.scan())
    second = hass.async_create_task(sched.scan())
    await asyncio.sleep(0)
    release.set()
    one, two = await asyncio.gather(first, second)

    assert one == two
    assert fake.inventory.await_count == 1
    assert fake.verify.await_count == 2
    assert peak == 1


async def test_one_failure_does_not_stop_the_scan(
    hass: HomeAssistant, fake: Fake, setup_entry: MockConfigEntry
) -> None:
    """A check that raises leaves that integration unchecked; the rest go on."""
    fake.items = [make_item(), make_item("other")]
    fake.files["demo"] = RuntimeError("boom")  # type: ignore[assignment]
    fake.files["other"] = files("changed", "other", changed=["a.py"])
    results = await integrity(setup_entry).scan()

    by_domain = {s.domain: s for s in results}
    assert by_domain["demo"].files is not None
    assert by_domain["demo"].files.status == "unchecked"
    assert our_issues(hass) == {"integration_files_changed_other"}


async def test_verify_files_off(
    hass: HomeAssistant, fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With file checks off, no file is checked; health still is."""
    entry = await setup_with(hass, monkeypatch, verify_files=False)
    fake.health["demo"] = health("archived")
    results = await integrity(entry).scan()

    assert fake.verify.await_count == 0
    assert results[0].files is None
    assert results[0].health.status == "archived"
    assert our_issues(hass) == {"integration_archived_demo"}


async def test_contract_failure_skips_all_but_manual(
    hass: HomeAssistant, fake: Fake, setup_entry: MockConfigEntry
) -> None:
    """A Marketplace whose shape changed is not relied on; manual still checks."""
    fake.items = [make_item(), make_item("mine", source="manual", repository=None)]
    fake.health["demo"] = health("archived")
    fake.files["mine"] = files("baseline_recorded", "mine")
    fake.contract = False
    results = await integrity(setup_entry).scan()
    await flush_worker(hass)

    assert [c.args[2].domain for c in fake.verify.await_args_list] == ["mine"]
    assert [(r.action, r.target_id) for r in change_rows(hass)] == [
        (ChangeAction.BASELINE_RECORDED, "mine")
    ]
    assert our_issues(hass) == set()
    by_domain = {s.domain: s for s in results}
    assert by_domain["demo"].files is not None
    assert by_domain["demo"].files.status == "unchecked"


async def test_file_lists_capped(
    hass: HomeAssistant, fake: Fake, setup_entry: MockConfigEntry
) -> None:
    """Each file list keeps at most 500 paths, and the status says so."""
    many = [f"f{i:04}.py" for i in range(600)]
    fake.files["demo"] = files("changed", added=many)
    results = await integrity(setup_entry).scan()
    await flush_worker(hass)

    status = results[0]
    assert status.truncated
    assert status.files is not None
    assert status.files.report is not None
    assert status.files.report.added == many[:500]
    assert len(file_rows(hass)[0].details["added"]) == 500


async def test_stop_cancels_a_running_scan(
    hass: HomeAssistant, fake: Fake, setup_entry: MockConfigEntry
) -> None:
    """Unloading cancels a scan that is still running."""
    started = asyncio.Event()

    async def hang(_hass: HomeAssistant, _rt: Any, _it: Any) -> Any:
        started.set()
        await asyncio.Event().wait()

    fake.verify.side_effect = hang
    fake.health["demo"] = health("archived")
    sched = integrity(setup_entry)
    task = hass.async_create_task(sched.scan())
    await started.wait()

    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    with pytest.raises(asyncio.CancelledError):
        await task
    await hass.async_block_till_done()

    def stored() -> tuple[list[Any], Any, Any]:
        store = FenceStore(Path(hass.config.path(".storage", "electric_fence.db")))
        store.open()
        try:
            return (
                store.query_changes(ChangeFilter(include_system=True), limit=50),
                store.finding("demo", "files"),
                store.finding("demo", "health"),
            )
        finally:
            store.close()

    rows, files_finding, health_finding = await hass.async_add_executor_job(stored)
    assert [
        r for r in rows if r.kind in (ChangeKind.FILES, ChangeKind.INTEGRATION_HEALTH)
    ] == []
    assert files_finding is None
    assert health_finding is None
    assert our_issues(hass) == set()


async def test_unload_clears_repairs(
    hass: HomeAssistant, fake: Fake, setup_entry: MockConfigEntry
) -> None:
    """A disabled entry keeps no repairs nothing would keep current."""
    fake.files["demo"] = files("changed", changed=["a.py"])
    fake.health["demo"] = health("archived")
    await integrity(setup_entry).scan()
    assert our_issues(hass)

    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    assert our_issues(hass) == set()


def _progress(progress: int | bool) -> dict[str, Any]:
    return {"repository": "owner/demo", "progress": progress}


async def test_change_during_install_is_unchecked(
    hass: HomeAssistant, fake: Fake, setup_entry: MockConfigEntry
) -> None:
    """A check that overlaps a running install never reports a change."""

    async def installing(_hass: HomeAssistant, _rt: Any, it: Any) -> Any:
        async_dispatcher_send(hass, scheduler.INSTALL_PROGRESS_SIGNAL, _progress(40))
        return files("changed", changed=["a.py"])

    fake.verify.side_effect = installing
    results = await integrity(setup_entry).scan()
    await flush_worker(hass)

    assert results[0].files is not None
    assert results[0].files.status == "unchecked"
    assert results[0].files.reason == "install_in_progress"
    assert file_rows(hass) == []
    assert our_issues(hass) == set()


async def test_stale_install_stops_counting(
    hass: HomeAssistant,
    fake: Fake,
    freezer: FrozenDateTimeFactory,
    setup_entry: MockConfigEntry,
) -> None:
    """An install that never says it ended stops blocking findings."""
    async_dispatcher_send(hass, scheduler.INSTALL_PROGRESS_SIGNAL, _progress(40))
    fake.files["demo"] = files("changed", changed=["a.py"])
    sched = integrity(setup_entry)
    assert (await sched.scan())[0].files.status == "unchecked"  # type: ignore[union-attr]

    freezer.tick(timedelta(hours=2))
    await sched.scan()
    await flush_worker(hass)
    assert [r.action for r in file_rows(hass)] == [ChangeAction.FILES_CHANGED]


async def test_install_ending_mid_scan_rescans(
    hass: HomeAssistant,
    fake: Fake,
    freezer: FrozenDateTimeFactory,
    setup_entry: MockConfigEntry,
) -> None:
    """An install that ends during a scan gives no change; the rescan decides."""
    await tick(hass, freezer, timedelta(minutes=30))
    assert fake.inventory.await_count == 1

    async def installed(_hass: HomeAssistant, _rt: Any, it: Any) -> Any:
        async_dispatcher_send(hass, scheduler.INSTALL_PROGRESS_SIGNAL, _progress(False))
        return files("changed", changed=["a.py"])

    fake.verify.side_effect = installed
    await integrity(setup_entry).scan()
    await flush_worker(hass)
    assert file_rows(hass) == []

    fake.verify.side_effect = None
    fake.verify.return_value = files("match")
    await tick(hass, freezer, timedelta(minutes=30))
    await flush_worker(hass)
    assert fake.inventory.await_count == 3
    assert file_rows(hass) == []


async def test_version_moved_is_unchecked(
    hass: HomeAssistant,
    fake: Fake,
    monkeypatch: pytest.MonkeyPatch,
    freezer: FrozenDateTimeFactory,
    setup_entry: MockConfigEntry,
) -> None:
    """A new installed version since the listing means the check is unchecked."""
    await tick(hass, freezer, timedelta(minutes=30))
    repo = SimpleNamespace(data=SimpleNamespace(installed_version="1.1.0"))
    monkeypatch.setattr(
        scheduler, "marketplace_repositories", lambda _h: (object(), {"demo": repo})
    )
    fake.files["demo"] = files("changed", changed=["a.py"])
    results = await integrity(setup_entry).scan()
    await flush_worker(hass)

    assert results[0].files is not None
    assert results[0].files.reason == "install_in_progress"
    assert file_rows(hass) == []
    await tick(hass, freezer, timedelta(minutes=30))
    assert fake.inventory.await_count == 3


async def test_rescan_due_during_a_scan_runs_after_it(
    hass: HomeAssistant,
    fake: Fake,
    freezer: FrozenDateTimeFactory,
    setup_entry: MockConfigEntry,
) -> None:
    """A rescan that comes due while a scan runs follows it, not skipped."""
    await tick(hass, freezer, timedelta(minutes=30))
    assert fake.inventory.await_count == 1
    release = asyncio.Event()

    async def slow(_hass: HomeAssistant, _rt: Any, it: Any) -> Any:
        await release.wait()
        return files(domain=it.domain)

    fake.verify.side_effect = slow
    sched = integrity(setup_entry)
    running = asyncio.get_running_loop().create_task(sched.scan())
    await asyncio.sleep(0)
    assert fake.inventory.await_count == 2

    async_dispatcher_send(hass, SIGNAL_REPOSITORY_INSTALLED, object())
    freezer.tick(timedelta(minutes=30))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert fake.inventory.await_count == 2

    release.set()
    await running
    await hass.async_block_till_done()
    task = sched._task  # noqa: SLF001
    assert task is not None
    await asyncio.wait({task})
    assert fake.inventory.await_count == 3


def test_signals_known() -> None:
    """The Marketplace signals the scheduler listens to still exist."""
    assert scheduler.INSTALLED_SIGNAL == SIGNAL_REPOSITORY_INSTALLED
    assert (
        scheduler.INSTALL_PROGRESS_SIGNAL
        == MarketplaceSignal.REPOSITORY_INSTALL_PROGRESS
    )
