"""Tests for the per-IP brute-force rule."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from freezegun.api import FrozenDateTimeFactory
import pytest

from custom_components.electric_fence.models import LoginReason, LoginResult
from custom_components.electric_fence.protection import policy as policy_module
from custom_components.electric_fence.protection.policy import IpPolicy
from custom_components.electric_fence.runtime import Runtime
from custom_components.electric_fence.storage import FenceStore
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from tests.helpers import make_runtime

IP = "203.0.113.9"
FAIL = (LoginResult.FAILED, LoginReason.WRONG_PASSWORD)
OK = (LoginResult.SUCCESS, LoginReason.OK)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[FenceStore]:
    """Return an open store."""
    s = FenceStore(tmp_path / "fence.db")
    s.open()
    yield s
    s.close()


@pytest.fixture
def runtime(hass: HomeAssistant, store: FenceStore) -> Runtime:
    """Return a runtime with a mocked, available ban manager."""
    rt = make_runtime(hass, store)
    bans = MagicMock()
    bans.available = True
    bans.async_ban = AsyncMock(return_value=True)
    rt.bans = bans
    return rt


@pytest.fixture
def policy(hass: HomeAssistant, runtime: Runtime) -> IpPolicy:
    """Return the policy under test."""
    return IpPolicy(hass, runtime)


def _fail(policy: IpPolicy, times: int, ip: str = IP) -> None:
    for _ in range(times):
        policy.on_attempt(ip, *FAIL)


async def test_bans_at_threshold(
    hass: HomeAssistant, runtime: Runtime, policy: IpPolicy
) -> None:
    """The fifth counted failure bans, with the count."""
    _fail(policy, 4)
    await hass.async_block_till_done()
    runtime.bans.async_ban.assert_not_awaited()  # type: ignore[union-attr]
    _fail(policy, 1)
    await hass.async_block_till_done()
    runtime.bans.async_ban.assert_awaited_once_with(IP, 5, None, None)  # type: ignore[union-attr]


async def test_mapped_ipv4_counts_with_plain(
    hass: HomeAssistant, runtime: Runtime, policy: IpPolicy
) -> None:
    """An IPv4-mapped address shares its count with the IPv4 form."""
    _fail(policy, 3)
    _fail(policy, 2, f"::ffff:{IP}")
    await hass.async_block_till_done()
    runtime.bans.async_ban.assert_awaited_once_with(IP, 5, None, None)  # type: ignore[union-attr]


async def test_count_restarts_after_ban(
    hass: HomeAssistant, runtime: Runtime, policy: IpPolicy
) -> None:
    """The count is cleared when a ban is requested."""
    _fail(policy, 9)
    await hass.async_block_till_done()
    assert runtime.bans.async_ban.await_count == 1  # type: ignore[union-attr]


async def test_window(
    hass: HomeAssistant,
    runtime: Runtime,
    policy: IpPolicy,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Failures older than the window don't count."""
    _fail(policy, 4)
    freezer.tick(timedelta(minutes=16))
    _fail(policy, 1)
    await hass.async_block_till_done()
    runtime.bans.async_ban.assert_not_awaited()  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "reason",
    [
        LoginReason.INVALID_TOKEN,
        LoginReason.BLOCKED_INACTIVE,
        LoginReason.BLOCKED_LOCAL_ONLY,
    ],
)
async def test_ignored_reasons(
    hass: HomeAssistant, runtime: Runtime, policy: IpPolicy, reason: LoginReason
) -> None:
    """Reasons that don't count never ban."""
    for _ in range(10):
        policy.on_attempt(IP, LoginResult.BLOCKED, reason)
    await hass.async_block_till_done()
    runtime.bans.async_ban.assert_not_awaited()  # type: ignore[union-attr]


async def test_trusted_after_success(
    hass: HomeAssistant,
    runtime: Runtime,
    policy: IpPolicy,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A recent success protects the address until the trusted window ends."""
    policy.on_attempt(IP, *OK)
    _fail(policy, 5)
    await hass.async_block_till_done()
    runtime.bans.async_ban.assert_not_awaited()  # type: ignore[union-attr]
    freezer.tick(runtime.settings.trusted_window + timedelta(seconds=1))
    _fail(policy, 5)
    await hass.async_block_till_done()
    runtime.bans.async_ban.assert_awaited_once()  # type: ignore[union-attr]


async def test_success_resets_count(
    hass: HomeAssistant,
    runtime: Runtime,
    policy: IpPolicy,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A success clears the failures counted so far."""
    _fail(policy, 4)
    policy.on_attempt(IP, *OK)
    freezer.tick(runtime.settings.trusted_window + timedelta(seconds=1))
    _fail(policy, 4)
    await hass.async_block_till_done()
    runtime.bans.async_ban.assert_not_awaited()  # type: ignore[union-attr]


async def test_blocked_does_not_trust(
    hass: HomeAssistant, runtime: Runtime, policy: IpPolicy
) -> None:
    """A blocked login does not make an address trusted."""
    policy.on_attempt(IP, LoginResult.BLOCKED, LoginReason.BLOCKED_LOCAL_ONLY)
    _fail(policy, 5)
    await hass.async_block_till_done()
    runtime.bans.async_ban.assert_awaited_once()  # type: ignore[union-attr]


async def test_seed_trusted(
    hass: HomeAssistant, runtime: Runtime, policy: IpPolicy
) -> None:
    """Addresses seeded from the store are trusted."""
    policy.seed_trusted({f"::ffff:{IP}": dt_util.utcnow(), "bad": dt_util.utcnow()})
    _fail(policy, 5)
    await hass.async_block_till_done()
    runtime.bans.async_ban.assert_not_awaited()  # type: ignore[union-attr]


async def test_auto_ban_off(
    hass: HomeAssistant, runtime: Runtime, policy: IpPolicy
) -> None:
    """No ban is requested with auto-ban off (read live)."""
    runtime.settings = replace(runtime.settings, auto_ban=False)
    _fail(policy, 5)
    await hass.async_block_till_done()
    runtime.bans.async_ban.assert_not_awaited()  # type: ignore[union-attr]


async def test_bans_unavailable(
    hass: HomeAssistant, runtime: Runtime, policy: IpPolicy
) -> None:
    """No ban is requested without a usable ban manager."""
    bans = runtime.bans
    assert bans is not None
    bans.available = False
    _fail(policy, 5)
    await hass.async_block_till_done()
    bans.async_ban.assert_not_awaited()  # type: ignore[attr-defined]
    runtime.bans = None
    _fail(policy, 5)
    await hass.async_block_till_done()
    bans.async_ban.assert_not_awaited()  # type: ignore[attr-defined]


async def test_exempt_never_trusted(
    hass: HomeAssistant, runtime: Runtime, policy: IpPolicy
) -> None:
    """Exempt addresses take no space in the trusted map."""
    policy.seed_trusted({"10.0.0.5": dt_util.utcnow()})
    policy.on_attempt("10.0.0.6", *OK)
    assert not policy._trusted  # noqa: SLF001


async def test_exempt_ip_never_counted(
    hass: HomeAssistant, runtime: Runtime, policy: IpPolicy
) -> None:
    """Private addresses are never counted."""
    _fail(policy, 10, "10.0.0.5")
    await hass.async_block_till_done()
    runtime.bans.async_ban.assert_not_awaited()  # type: ignore[union-attr]


async def test_missing_ip_ignored(
    hass: HomeAssistant, runtime: Runtime, policy: IpPolicy
) -> None:
    """An attempt without an address is ignored."""
    for _ in range(10):
        policy.on_attempt(None, *FAIL)
    await hass.async_block_till_done()
    runtime.bans.async_ban.assert_not_awaited()  # type: ignore[union-attr]


async def test_memory_bounded(
    monkeypatch: pytest.MonkeyPatch, hass: HomeAssistant, runtime: Runtime
) -> None:
    """The oldest addresses are dropped first."""
    monkeypatch.setattr(policy_module, "MAX_TRACKED", 3)
    policy = IpPolicy(hass, runtime)
    _fail(policy, 4, "203.0.113.1")
    for n in range(2, 5):
        _fail(policy, 1, f"203.0.113.{n}")
    _fail(policy, 1, "203.0.113.1")
    now = dt_util.utcnow()
    policy.seed_trusted(
        {f"198.51.100.{n}": now + timedelta(seconds=n) for n in (1, 2, 3, 4)}
    )
    _fail(policy, 5, "198.51.100.1")
    _fail(policy, 5, "198.51.100.4")
    await hass.async_block_till_done()
    runtime.bans.async_ban.assert_awaited_once_with("198.51.100.1", 5, None, None)  # type: ignore[union-attr]


async def test_never_raises(
    hass: HomeAssistant, runtime: Runtime, policy: IpPolicy
) -> None:
    """A failing ban request never reaches the caller."""
    runtime.bans.async_ban.side_effect = RuntimeError("boom")  # type: ignore[union-attr]
    _fail(policy, 5)
    await hass.async_block_till_done()
    runtime.bans.async_ban.assert_awaited_once()  # type: ignore[union-attr]
    # Broken settings raise inside on_attempt itself, which the guard absorbs.
    runtime.settings = None  # type: ignore[assignment]
    _fail(policy, 1)
