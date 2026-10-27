"""Tests for the database size limit."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.electric_fence import SIZE_CHECK_INTERVAL, async_enforce_size
from custom_components.electric_fence.const import DOMAIN
from custom_components.electric_fence.models import (
    ChangeAction,
    ChangeKind,
    LoginAttempt,
    LoginReason,
    LoginResult,
    SettingsChange,
    UserExists,
)
from custom_components.electric_fence.storage import (
    ChangeFilter,
    FenceStore,
    LoginFilter,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

NOW = dt_util.utcnow()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[FenceStore]:
    """Return an open store."""
    s = FenceStore(tmp_path / "fence.db")
    s.open()
    yield s
    s.close()


def _login(n: int, result: LoginResult = LoginResult.FAILED) -> LoginAttempt:
    return LoginAttempt(
        ts=NOW + timedelta(seconds=n),
        result=result,
        reason=LoginReason.OK
        if result is LoginResult.SUCCESS
        else LoginReason.NO_SUCH_USER,
        username_typed=f"user{n}",
        user_exists=UserExists.NO,
        user_id=None,
        provider=None,
        ip="203.0.113.9",
        source=None,
        url=None,
        user_agent="x" * 250,
        client_id=None,
    )


def _change(n: int) -> SettingsChange:
    return SettingsChange(
        ts=NOW + timedelta(seconds=n),
        kind=ChangeKind.LOGIN_PROTECTION,
        action=ChangeAction.COUNTRIES_CLEARED,
        target_id=None,
        target_name=None,
        details={"cleared": n},
        actor_user_id=None,
        actor_name=None,
        actor_ip=None,
    )


def _fill(store: FenceStore, failures: int) -> None:
    store.insert([_login(-1, LoginResult.SUCCESS), _change(-1)])
    store.insert([_login(n) for n in range(failures)])


def test_used_bytes_grows(store: FenceStore) -> None:
    """The used size counts pages in use, not free ones."""
    before = store.used_bytes()
    _fill(store, 500)
    assert store.used_bytes() > before


def test_trim_under_limit_does_nothing(store: FenceStore) -> None:
    """Nothing is deleted while the database is under its limit."""
    _fill(store, 50)
    assert store.trim_failed(store.used_bytes() + 1) == 0
    assert len(store.query_logins(LoginFilter(), limit=500)) == 51


def test_trim_removes_oldest_failures_only(store: FenceStore) -> None:
    """Over the limit, the oldest failed logins go; successes and changes stay."""
    _fill(store, 2000)
    full = store.used_bytes()
    limit = full // 2

    deleted = store.trim_failed(limit)

    assert deleted > 0
    assert store.used_bytes() <= limit
    assert len(store.query_logins(LoginFilter(result=LoginResult.SUCCESS))) == 1
    assert len(store.query_changes(ChangeFilter(include_system=True), limit=10)) == 1
    failed = store.query_logins(LoginFilter(result=LoginResult.FAILED), limit=500)
    # The newest failures are kept.
    assert failed[0].username_typed == "user1999"
    assert "user0" not in {r.username_typed for r in failed}


def test_trim_stops_when_only_kept_rows_left(store: FenceStore) -> None:
    """With no failures left to delete, trimming stops instead of looping."""
    _fill(store, 10)
    assert store.trim_failed(1) == 10
    assert store.trim_failed(1) == 0
    assert len(store.query_logins(LoginFilter(), limit=10)) == 1


async def test_enforce_size_trims_and_raises_repair(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Over the limit the oldest failures go and a repair says so; it clears later."""
    runtime = setup_entry.runtime_data
    store = runtime.store
    await hass.async_add_executor_job(_fill, store, 2000)
    used = await hass.async_add_executor_job(store.used_bytes)
    # Limit in units of 1 KB so the test database is "over" it.
    monkeypatch.setattr("custom_components.electric_fence.MB_BYTES", 1024)
    runtime.settings = replace(runtime.settings, max_db_mb=used // 2048)

    await async_enforce_size(hass, runtime)

    assert await hass.async_add_executor_job(store.used_bytes) <= used // 2
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "storage_limit_reached")
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["limit"] == str(used // 2048)
    assert "removed" in caplog.text

    # Well under the limit again: the repair goes away.
    runtime.settings = replace(runtime.settings, max_db_mb=used // 256)
    await async_enforce_size(hass, runtime)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "storage_limit_reached") is None


async def test_enforce_size_fails_open(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing trim is logged and never raises."""
    runtime = setup_entry.runtime_data
    monkeypatch.setattr(runtime.store, "trim_failed", Mock(side_effect=OSError))
    await async_enforce_size(hass, runtime)
    assert "size check failed: OSError" in caplog.text


async def test_enforce_size_scheduled(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The size is checked every 10 minutes."""
    runtime = setup_entry.runtime_data
    with patch.object(
        runtime.store, "trim_failed", wraps=runtime.store.trim_failed
    ) as trim:
        freezer.tick(SIZE_CHECK_INTERVAL)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert trim.call_count >= 1
