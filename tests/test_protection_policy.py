"""Tests for the per-IP brute-force rule."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from freezegun.api import FrozenDateTimeFactory
import pytest

from custom_components.electric_fence.models import (
    LoginAttempt,
    LoginReason,
    LoginResult,
    UserExists,
)
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
    bans.is_blocked = Mock(return_value=False)
    rt.bans = bans
    return rt


@pytest.fixture
def policy(hass: HomeAssistant, runtime: Runtime) -> IpPolicy:
    """Return the policy under test."""
    return IpPolicy(hass, runtime)


def _success(
    ip: str | None = IP,
    *,
    confirmed: bool = True,
    result: LoginResult = LoginResult.SUCCESS,
    user: str = "alice",
) -> LoginAttempt:
    return LoginAttempt(
        ts=dt_util.utcnow(),
        result=result,
        reason=LoginReason.OK,
        username_typed=user,
        user_exists=UserExists.YES,
        user_id="u1",
        provider=None,
        ip=ip,
        source=None,
        url=None,
        user_agent=None,
        client_id=None,
        confirmed=confirmed,
    )


def _fail(
    policy: IpPolicy, times: int, ip: str = IP, user: str | None = "alice"
) -> None:
    for _ in range(times):
        policy.on_attempt(ip, *FAIL, username=user)


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
    """A recent confirmed success protects the address until the window ends."""
    policy.on_attempt(IP, *OK, username="alice")
    policy.on_record(_success())
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
    """A success clears the failures counted so far for its username."""
    _fail(policy, 4)
    policy.on_attempt(IP, *OK, username="Alice")
    freezer.tick(runtime.settings.trusted_window + timedelta(seconds=1))
    _fail(policy, 4)
    await hass.async_block_till_done()
    runtime.bans.async_ban.assert_not_awaited()  # type: ignore[union-attr]


async def test_unconfirmed_success_does_not_trust(
    hass: HomeAssistant, runtime: Runtime, policy: IpPolicy
) -> None:
    """Like the startup seed, only a confirmed success trusts an address."""
    policy.on_attempt(IP, *OK)
    policy.on_record(_success(confirmed=False))
    policy.on_record(_success(result=LoginResult.BLOCKED))
    policy.on_record(_success(ip=None))
    policy.on_record(_success(ip="not an ip"))
    _fail(policy, 5)
    await hass.async_block_till_done()
    runtime.bans.async_ban.assert_awaited_once()  # type: ignore[union-attr]


async def test_record_hook_fails_open(hass: HomeAssistant, policy: IpPolicy) -> None:
    """An error while trusting never raises out of the listener."""
    with patch.object(policy, "_trust", side_effect=RuntimeError):
        policy.on_record(_success())


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
    """Successes seeded from the store trust their address for their username."""
    now = dt_util.utcnow()
    policy.seed_trusted([(f"::ffff:{IP}", "alice", now), ("bad", "alice", now)])
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
    policy.seed_trusted([("127.0.0.1", "alice", dt_util.utcnow())])
    policy.on_record(_success("::1"))
    assert not policy._trusted  # noqa: SLF001


async def test_exempt_ip_never_counted(
    hass: HomeAssistant, runtime: Runtime, policy: IpPolicy
) -> None:
    """Loopback is never counted."""
    _fail(policy, 10, "127.0.0.1")
    await hass.async_block_till_done()
    runtime.bans.async_ban.assert_not_awaited()  # type: ignore[union-attr]


@pytest.mark.parametrize("ip", ["10.0.0.5", "172.17.0.1", "100.101.102.103", "fd7a::1"])
async def test_local_counted(
    hass: HomeAssistant, runtime: Runtime, policy: IpPolicy, ip: str
) -> None:
    """A local address is banned like any other by default."""
    _fail(policy, 5, ip)
    await hass.async_block_till_done()
    runtime.bans.async_ban.assert_awaited_once_with(ip, 5, None, None)  # type: ignore[union-attr]


async def test_local_exempt_when_set(
    hass: HomeAssistant, runtime: Runtime, policy: IpPolicy
) -> None:
    """With exempt_local (read live) local addresses are not counted."""
    runtime.settings = replace(runtime.settings, exempt_local=True)
    _fail(policy, 10, "10.0.0.5")
    await hass.async_block_till_done()
    runtime.bans.async_ban.assert_not_awaited()  # type: ignore[union-attr]


async def test_ipv6_counted_per_prefix(
    hass: HomeAssistant, runtime: Runtime, policy: IpPolicy
) -> None:
    """Rotating addresses inside one /64 still bans, on the fifth failure."""
    for n in range(1, 6):
        _fail(policy, 1, f"2001:db8:1:2::{n}")
    await hass.async_block_till_done()
    runtime.bans.async_ban.assert_awaited_once_with("2001:db8:1:2::5", 5, None, None)  # type: ignore[union-attr]


async def test_ipv6_prefixes_counted_apart(
    hass: HomeAssistant, runtime: Runtime, policy: IpPolicy
) -> None:
    """Different /64s are counted apart; a /128 setting counts addresses."""
    _fail(policy, 4, "2001:db8:1:2::1")
    _fail(policy, 4, "2001:db8:1:3::1")
    await hass.async_block_till_done()
    runtime.bans.async_ban.assert_not_awaited()  # type: ignore[union-attr]
    runtime.settings = replace(runtime.settings, ipv6_prefix=128)
    for n in range(1, 6):
        _fail(policy, 1, f"2001:db8:9:9::{n}")
    await hass.async_block_till_done()
    runtime.bans.async_ban.assert_not_awaited()  # type: ignore[union-attr]


async def test_trust_is_per_username(
    hass: HomeAssistant, runtime: Runtime, policy: IpPolicy
) -> None:
    """A guest's success doesn't stop a ban for failures on another username."""
    policy.on_attempt(IP, *OK, username="guest")
    policy.on_record(_success(user="guest"))
    _fail(policy, 5, user="admin")
    await hass.async_block_till_done()
    runtime.bans.async_ban.assert_awaited_once_with(IP, 5, None, None)  # type: ignore[union-attr]


async def test_trust_needs_every_failure_trusted(
    hass: HomeAssistant, runtime: Runtime, policy: IpPolicy
) -> None:
    """Mixed usernames ban, even when some are the trusted user's typos."""
    policy.on_record(_success(user="alice"))
    _fail(policy, 3, user="ALICE ")
    _fail(policy, 1, user="admin")
    _fail(policy, 1, user=None)
    await hass.async_block_till_done()
    runtime.bans.async_ban.assert_awaited_once()  # type: ignore[union-attr]


async def test_success_keeps_other_usernames_failures(
    hass: HomeAssistant, runtime: Runtime, policy: IpPolicy
) -> None:
    """A guest login between admin guesses doesn't reset the admin failures."""
    for _ in range(5):
        _fail(policy, 1, user="admin")
        policy.on_attempt(IP, *OK, username="guest")
    await hass.async_block_till_done()
    runtime.bans.async_ban.assert_awaited_once_with(IP, 5, None, None)  # type: ignore[union-attr]


async def test_trust_is_per_prefix(
    hass: HomeAssistant, runtime: Runtime, policy: IpPolicy
) -> None:
    """A success trusts its whole /64: privacy addresses change."""
    policy.on_record(_success(ip="2001:db8:1:2::aaaa"))
    _fail(policy, 5, "2001:db8:1:2::bbbb")
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
        [(f"198.51.100.{n}", "alice", now + timedelta(seconds=n)) for n in (1, 2, 3, 4)]
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


async def test_trusted_users_bounded(
    monkeypatch: pytest.MonkeyPatch, hass: HomeAssistant, runtime: Runtime
) -> None:
    """Per address, the user who logged in longest ago is dropped first."""
    monkeypatch.setattr(policy_module, "MAX_TRUSTED_USERS", 2)
    policy = IpPolicy(hass, runtime)
    now = dt_util.utcnow()
    policy.seed_trusted(
        [(IP, f"user{n}", now - timedelta(minutes=10 - n)) for n in (1, 2, 3)]
    )
    assert policy._trusted_users(IP, now) == {"user2", "user3"}  # noqa: SLF001


async def test_no_ban_request_when_already_blocked(
    hass: HomeAssistant, runtime: Runtime, policy: IpPolicy
) -> None:
    """An address its prefix ban already covers asks for no further ban."""
    runtime.bans.is_blocked = Mock(return_value=True)  # type: ignore[union-attr]
    _fail(policy, 10, "2001:db8::7")
    await hass.async_block_till_done()
    runtime.bans.async_ban.assert_not_awaited()  # type: ignore[union-attr]
    runtime.bans.is_blocked.assert_called_with("2001:db8::7")  # type: ignore[union-attr]
