"""Tests for the posture checks."""

from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.electric_fence import posture
from custom_components.electric_fence.const import DOMAIN
from custom_components.electric_fence.integrity import scheduler
from custom_components.electric_fence.models import (
    LoginAttempt,
    LoginReason,
    LoginResult,
    Source,
    UserExists,
)
from homeassistant.components.http.ban import KEY_LOGIN_THRESHOLD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from tests.helpers import create_hass_user

PROXY_IP = "172.30.32.1"


def success(user_id: str, ip: str = PROXY_IP, **kw: Any) -> LoginAttempt:
    """Return a successful login of `user_id`."""
    return LoginAttempt(
        ts=kw.pop("ts", dt_util.utcnow() - timedelta(hours=1)),
        result=LoginResult.SUCCESS,
        reason=LoginReason.OK,
        username_typed=user_id,
        user_exists=UserExists.YES,
        user_id=user_id,
        provider="homeassistant",
        ip=ip,
        source=Source.LOCAL,
        url="/auth/login_flow",
        user_agent="test",
        client_id="http://127.0.0.1:8123/",
        **kw,
    )


async def check(hass: HomeAssistant, entry: MockConfigEntry) -> list[dict[str, Any]]:
    """Run all posture checks for the entry."""
    runtime = entry.runtime_data
    return await posture.async_check(hass, runtime, runtime.store)


def by_id(items: list[dict[str, Any]], item_id: str) -> dict[str, Any]:
    """Return the only item with this ID."""
    found = [i for i in items if i["id"] == item_id]
    assert len(found) == 1, found
    return found[0]


async def insert(hass: HomeAssistant, entry: MockConfigEntry, *recs: Any) -> None:
    """Insert records straight into the store."""
    await hass.async_add_executor_job(entry.runtime_data.store.insert, list(recs))


async def test_posture_flags_admin_without_mfa(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An active admin without MFA is a warning; with MFA everywhere it's ok."""
    alice = await create_hass_user(hass, "alice", "pw")
    bob = await create_hass_user(hass, "bob", "pw", admin=False)
    inactive = await create_hass_user(hass, "carol", "pw")
    await hass.auth.async_update_user(inactive, is_active=False)

    item = by_id(await check(hass, setup_entry), "admins_without_mfa")

    assert item["level"] == "warning"
    ids = [u["id"] for u in item["params"]["users"]]
    assert alice.id in ids
    assert bob.id not in ids
    assert inactive.id not in ids
    assert {"id": alice.id, "name": "alice"} in item["params"]["users"]

    async def enabled(user: Any) -> dict[str, str]:
        return {"totp": "Authenticator app"}

    monkeypatch.setattr(hass.auth, "async_get_enabled_mfa", enabled)
    item = by_id(await check(hass, setup_entry), "admins_without_mfa")
    assert item == {"id": "admins_without_mfa", "level": "ok", "params": {"users": []}}


async def test_posture_flags_proxy_suspected(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three users all logging in from one private IP suggests a proxy."""
    await insert(hass, setup_entry, success("u1"), success("u2"))
    assert by_id(await check(hass, setup_entry), "proxy_suspected")["level"] == "ok"

    await insert(
        hass,
        setup_entry,
        success("u3"),
        success("u1"),
        # Older than 7 days: not looked at.
        success("u4", ip="192.168.1.20", ts=dt_util.utcnow() - timedelta(days=8)),
        # Failures don't count.
        replace(
            success("u5", ip="192.168.1.21"),
            result=LoginResult.FAILED,
            reason=LoginReason.WRONG_PASSWORD,
        ),
    )
    item = by_id(await check(hass, setup_entry), "proxy_suspected")
    assert item["level"] == "warning"
    assert item["params"]["ip"] == PROXY_IP
    assert item["params"]["users"] == 3

    # Known to be configured for a proxy: nothing to warn about.
    monkeypatch.setitem(
        hass.data,
        "http",
        SimpleNamespace(
            active_config_type="stable", stable={"use_x_forwarded_for": True}
        ),
    )
    assert by_id(await check(hass, setup_entry), "proxy_suspected")["level"] == "ok"


async def test_posture_proxy_pages_through_logins(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The proxy check reads every page of successes, up to its cap."""
    monkeypatch.setattr(posture, "_PAGE", 2)
    await insert(hass, setup_entry, success("u1"), success("u2"), success("u3"))
    assert (
        by_id(await check(hass, setup_entry), "proxy_suspected")["level"] == "warning"
    )

    # Only the newest rows up to the cap are looked at.
    monkeypatch.setattr(posture, "_PROXY_SCAN_ROWS", 2)
    item = by_id(await check(hass, setup_entry), "proxy_suspected")
    assert item["level"] == "ok"
    assert item["params"]["users"] == 2


async def test_posture_proxy_needs_one_private_ip(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Different IPs, or one public IP, are no sign of a proxy."""
    await insert(
        hass,
        setup_entry,
        success("u1", ip="203.0.113.5"),
        success("u2", ip="203.0.113.5"),
        success("u3", ip="203.0.113.5"),
    )
    assert by_id(await check(hass, setup_entry), "proxy_suspected")["level"] == "ok"

    await insert(hass, setup_entry, success("u4", ip="192.168.1.2"))
    assert by_id(await check(hass, setup_entry), "proxy_suspected")["level"] == "ok"


@pytest.mark.parametrize(
    ("store", "expected"),
    [
        (None, None),
        (SimpleNamespace(), None),
        (SimpleNamespace(active_config_type="stable", stable={}), False),
        (
            SimpleNamespace(
                active_config_type="pending", pending={"use_x_forwarded_for": True}
            ),
            True,
        ),
        (SimpleNamespace(active_config_type="pending", pending=None), None),
        (SimpleNamespace(active_config_type="default"), False),
        (SimpleNamespace(active_config_type="default_legacy_port"), False),
        (SimpleNamespace(active_config_type="other"), None),
        (
            SimpleNamespace(
                active_config_type="stable", stable={"use_x_forwarded_for": "yes"}
            ),
            None,
        ),
    ],
)
async def test_use_x_forwarded_for_read_defensively(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    store: Any,
    expected: bool | None,
) -> None:
    """The running HTTP config is read from the HTTP config store, if present."""
    if store is None:
        hass.data.pop("http", None)
    else:
        monkeypatch.setitem(hass.data, "http", store)
    assert posture.use_x_forwarded_for(hass) is expected


async def test_use_x_forwarded_for_from_real_http(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """With Core's HTTP set up from defaults, forwarded headers are off."""
    assert posture.use_x_forwarded_for(hass) is False


async def test_posture_auto_ban_off_default(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without Electric Fence's auto-ban, Core's threshold decides."""
    runtime = setup_entry.runtime_data
    runtime.settings = replace(runtime.settings, auto_ban=False)
    item = by_id(await check(hass, setup_entry), "auto_ban_off")
    assert item == {
        "id": "auto_ban_off",
        "level": "warning",
        "params": {"threshold": -1, "electric_fence": False},
    }

    monkeypatch.setattr(hass, "http", SimpleNamespace(app={KEY_LOGIN_THRESHOLD: 0}))
    item = by_id(await check(hass, setup_entry), "auto_ban_off")
    assert item["level"] == "warning"
    assert item["params"] == {"threshold": 0, "electric_fence": False}

    monkeypatch.setattr(hass, "http", SimpleNamespace(app={KEY_LOGIN_THRESHOLD: 5}))
    item = by_id(await check(hass, setup_entry), "auto_ban_off")
    assert item == {
        "id": "auto_ban_off",
        "level": "ok",
        "params": {"threshold": 5, "electric_fence": False},
    }

    # Ban middleware not set up (ip_ban_enabled off), or no HTTP at all.
    monkeypatch.setattr(hass, "http", SimpleNamespace(app={}))
    item = by_id(await check(hass, setup_entry), "auto_ban_off")
    assert item == {
        "id": "auto_ban_off",
        "level": "warning",
        "params": {"threshold": None, "electric_fence": False},
    }
    monkeypatch.setattr(hass, "http", None)
    assert by_id(await check(hass, setup_entry), "auto_ban_off")["level"] == "warning"


async def test_posture_reports_disabled_collector(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """Each collector disabled by its contract guard is an error."""
    items = await check(hass, setup_entry)
    assert by_id(items, "collector_disabled") == {
        "id": "collector_disabled",
        "level": "ok",
        "params": {},
    }

    collectors = setup_entry.runtime_data.collectors
    collectors[0].disabled_reason = ["login flow signature changed"]
    collectors[1].disabled_reason = ["a", "b"]
    items = [
        i for i in await check(hass, setup_entry) if i["id"] == "collector_disabled"
    ]

    assert items == [
        {
            "id": "collector_disabled",
            "level": "error",
            "params": {
                "collector": collectors[0].name,
                "reasons": ["login flow signature changed"],
            },
        },
        {
            "id": "collector_disabled",
            "level": "error",
            "params": {"collector": collectors[1].name, "reasons": ["a", "b"]},
        },
    ]


async def test_posture_geoip_missing(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """A GeoIP provider without a database is a warning; `none` is ok."""
    status = setup_entry.runtime_data.geo_status
    assert status is not None
    status.last_error = "download failed"
    item = by_id(await check(hass, setup_entry), "geoip_missing")
    assert item == {
        "id": "geoip_missing",
        "level": "warning",
        "params": {"provider": "dbip_lite", "last_error": "download failed"},
    }

    status.db_date = date(2026, 10, 1)
    assert by_id(await check(hass, setup_entry), "geoip_missing")["level"] == "ok"

    status.db_date = None
    status.provider = "none"
    assert by_id(await check(hass, setup_entry), "geoip_missing")["level"] == "ok"

    setup_entry.runtime_data.geo_status = None
    item = by_id(await check(hass, setup_entry), "geoip_missing")
    assert item["level"] == "warning"
    assert item["params"]["provider"] == "dbip_lite"


async def test_posture_storage_error_last_hour(
    hass: HomeAssistant, setup_entry: MockConfigEntry
) -> None:
    """A storage error in the last hour is an error; an older one is not."""
    worker = setup_entry.runtime_data.worker
    assert by_id(await check(hass, setup_entry), "storage_error")["level"] == "ok"

    at = dt_util.utcnow() - timedelta(minutes=10)
    worker.storage_errors = 2
    worker.last_storage_error = "OperationalError: database is locked"
    worker.last_storage_error_at = at
    item = by_id(await check(hass, setup_entry), "storage_error")
    assert item == {
        "id": "storage_error",
        "level": "error",
        "params": {
            "storage_errors": 2,
            "last_error": "OperationalError: database is locked",
            "last_error_at": at.isoformat(),
        },
    }

    worker.last_storage_error_at = dt_util.utcnow() - timedelta(hours=2)
    assert by_id(await check(hass, setup_entry), "storage_error")["level"] == "ok"


async def test_posture_check_failure_is_contained(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A check that raises reports an error item; the others still run."""

    async def boom() -> list[Any]:
        raise RuntimeError("nope")

    monkeypatch.setattr(hass.auth, "async_get_users", boom)

    items = await check(hass, setup_entry)

    assert by_id(items, "admins_without_mfa") == {
        "id": "admins_without_mfa",
        "level": "error",
        "params": {"check_failed": "RuntimeError"},
    }
    assert by_id(items, "storage_error")["level"] == "ok"


async def test_posture_auto_ban_ok_with_electric_fence(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Electric Fence's own auto-ban satisfies the check, Core's threshold or not."""
    runtime = setup_entry.runtime_data
    assert runtime.bans is not None
    monkeypatch.setattr(runtime.bans.backend, "health", lambda: None)
    monkeypatch.setattr(hass, "http", SimpleNamespace(app={KEY_LOGIN_THRESHOLD: -1}))
    item = by_id(await check(hass, setup_entry), "auto_ban_off")
    assert item == {
        "id": "auto_ban_off",
        "level": "ok",
        "params": {"threshold": -1, "electric_fence": True},
    }

    runtime.settings = replace(runtime.settings, auto_ban=False)
    item = by_id(await check(hass, setup_entry), "auto_ban_off")
    assert item["level"] == "warning"
    assert item["params"]["electric_fence"] is False

    runtime.settings = replace(runtime.settings, auto_ban=True)
    monkeypatch.setattr(runtime.bans.backend, "health", lambda: "ip_ban_disabled")
    item = by_id(await check(hass, setup_entry), "auto_ban_off")
    assert item["level"] == "warning"
    assert item["params"]["electric_fence"] is False

    runtime.bans = None
    item = by_id(await check(hass, setup_entry), "auto_ban_off")
    assert item["params"]["electric_fence"] is False


async def test_ip_ban_disabled_posture(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A warning only while Core's IP ban is switched off."""
    runtime = setup_entry.runtime_data
    assert runtime.bans is not None
    monkeypatch.setattr(runtime.bans.backend, "health", lambda: "ip_ban_disabled")
    assert by_id(await check(hass, setup_entry), "ip_ban_disabled") == {
        "id": "ip_ban_disabled",
        "level": "warning",
        "params": {},
    }
    monkeypatch.setattr(runtime.bans.backend, "health", lambda: None)
    assert by_id(await check(hass, setup_entry), "ip_ban_disabled")["level"] == "ok"
    runtime.bans = None
    assert by_id(await check(hass, setup_entry), "ip_ban_disabled")["level"] == "ok"


async def test_ip_ban_disabled_repair(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The repair issue exists while Core's IP ban is off and clears after."""
    registry = ir.async_get(hass)
    runtime = setup_entry.runtime_data
    assert runtime.bans is not None
    monkeypatch.setattr(runtime.bans.backend, "health", lambda: "ip_ban_disabled")
    await runtime.bans.async_start()
    issue = registry.async_get_issue(DOMAIN, "ip_ban_disabled")
    assert issue is not None
    assert issue.is_fixable is False
    assert issue.translation_key == "ip_ban_disabled"

    monkeypatch.setattr(runtime.bans.backend, "health", lambda: None)
    await runtime.bans.async_start()
    assert registry.async_get_issue(DOMAIN, "ip_ban_disabled") is None


async def test_integration_posture_waits_for_first_scan(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No integration items before a scan; ok items after a clean one."""
    ids = {"integration_files_changed", "integration_health"}
    ids.add("integration_checks_unchecked")
    assert not ids & {i["id"] for i in await check(hass, setup_entry)}

    async def inventory(_hass: HomeAssistant) -> list[Any]:
        return []

    monkeypatch.setattr(scheduler, "async_inventory", inventory)
    sched = setup_entry.runtime_data.integrity
    assert sched is not None
    await sched.scan()
    items = await check(hass, setup_entry)

    assert by_id(items, "integration_files_changed") == {
        "id": "integration_files_changed",
        "level": "ok",
        "params": {"integrations": []},
    }
    assert by_id(items, "integration_health")["level"] == "ok"
    assert by_id(items, "integration_checks_unchecked") == {
        "id": "integration_checks_unchecked",
        "level": "ok",
        "params": {"integrations": []},
    }
