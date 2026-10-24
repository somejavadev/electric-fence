"""Login overhead benchmark (spec §11 criterion 9).

Not part of the default run (`-m "not benchmark"` in pyproject); run it with
`pytest -m benchmark`. Plain Core and Core with Electric Fence are measured
on one instance in alternating rounds: unload restores Core exactly (see
`test_unload_restores_every_hook`), and alternating keeps warm-up and drift
out of the difference.
"""

from statistics import median
import time

from aiohttp import web
from aiohttp.test_utils import TestClient
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.electric_fence.const import DOMAIN, MIN_HA_VERSION
from custom_components.electric_fence.settings import Settings
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from tests.helpers import create_hass_user, flush_worker, login_flow, store_rows

LOGINS = 50
ROUNDS = 5
WARMUP = 5
MAX_OVERHEAD_S = 0.005

type Client = TestClient[web.Request, web.Application]


def _failed_count(hass: HomeAssistant) -> int:
    """Return the number of stored failed attempts (merged rows counted out)."""
    return sum(row.count for row in store_rows(hass) if row.result == "failed")


async def _timed_logins(client: Client, count: int) -> list[float]:
    """Return the duration of `count` wrong-password logins (both HTTP steps)."""
    times: list[float] = []
    for _ in range(count):
        start = time.perf_counter()
        result = await login_flow(client, "alice", "wrong")
        times.append(time.perf_counter() - start)
        assert result["errors"] == {"base": "invalid_auth"}
    return times


@pytest.mark.benchmark
async def test_login_overhead_under_5ms(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The median wrong-password login is < 5 ms slower with Electric Fence."""
    monkeypatch.setattr("custom_components.electric_fence.HA_VERSION", MIN_HA_VERSION)
    await create_hass_user(hass, "alice", "right")
    assert await async_setup_component(hass, "auth", {})
    entry = MockConfigEntry(domain=DOMAIN, options=Settings().to_options())
    entry.add_to_hass(hass)
    client = await hass_client_no_auth()

    # Warm up both paths (imports, first setup of the dependencies).
    await _timed_logins(client, WARMUP)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _timed_logins(client, WARMUP)
    assert await hass.config_entries.async_unload(entry.entry_id)

    plain: list[float] = []
    fenced: list[float] = []
    per_round = LOGINS // ROUNDS
    for _ in range(ROUNDS):
        plain.extend(await _timed_logins(client, per_round))
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        # A disabled login collector would leave the entry loaded but unhooked.
        login = next(c for c in entry.runtime_data.collectors if c.name == "login")
        assert login.is_installed
        stored = _failed_count(hass)
        fenced.extend(await _timed_logins(client, per_round))
        await flush_worker(hass)
        assert _failed_count(hass) == stored + per_round
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    assert len(plain) == len(fenced) == LOGINS
    overhead = median(fenced) - median(plain)
    print(  # noqa: T201
        f"login median: plain {median(plain) * 1000:.2f} ms, "
        f"with Electric Fence {median(fenced) * 1000:.2f} ms, "
        f"overhead {overhead * 1000:.2f} ms"
    )
    assert overhead < MAX_OVERHEAD_S
