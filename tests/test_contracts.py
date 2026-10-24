"""Tests for contracts, repairs and the collector base."""

from collections.abc import Iterator

import pytest

from custom_components.electric_fence.collectors.base import BaseCollector
from custom_components.electric_fence.const import DOMAIN
from custom_components.electric_fence.contracts import (
    CONTRACTS,
    Contract,
    check_contracts,
    register_contract,
)
from custom_components.electric_fence.runtime import Runtime
from custom_components.electric_fence.settings import Settings
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir


class _Handle:
    restored = 0

    def restore(self) -> None:
        self.restored += 1


class DummyCollector(BaseCollector):
    """Collector recording its lifecycle."""

    name = "dummy_collector"

    def __init__(self, hass: HomeAssistant, runtime: Runtime) -> None:
        """Initialise."""
        super().__init__(hass, runtime)
        self.installed = 0
        self.unsubbed = 0
        self.handle = _Handle()
        self.uninstalled = 0

    def _install(self) -> None:
        self.installed += 1
        self._handles.append(self.handle)  # type: ignore[arg-type]
        self._unsubs.append(self._unsub)

    def _unsub(self) -> None:
        self.unsubbed += 1

    def _uninstall(self) -> None:
        self.uninstalled += 1


@pytest.fixture(autouse=True)
def _clean_contracts() -> Iterator[None]:
    saved = dict(CONTRACTS)
    yield
    CONTRACTS.clear()
    CONTRACTS.update(saved)


def _issue(hass: HomeAssistant) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, "collector_disabled_dummy_collector"
    )


async def test_install_skipped_and_issue_raised_when_contract_fails(
    hass: HomeAssistant,
) -> None:
    """A failing contract skips install and raises a repair issue."""
    register_contract(
        Contract("dummy", "dummy_collector", lambda hass: "missing thing")
    )
    c = DummyCollector(hass, Runtime(Settings()))
    assert c.install() is False
    assert c.disabled_reason == ["missing thing"]
    assert c.installed == 0
    issue = _issue(hass)
    assert issue is not None
    assert not issue.is_fixable
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == "collector_disabled"
    assert issue.translation_placeholders == {
        "collector": "dummy_collector",
        "reason": "missing thing",
    }


async def test_install_clears_issue_and_uninstall_is_idempotent(
    hass: HomeAssistant,
) -> None:
    """Passing contracts install, clear the issue, and teardown restores."""
    register_contract(Contract("a", "dummy_collector", lambda hass: "bad"))
    c = DummyCollector(hass, Runtime(Settings()))
    assert c.install() is False
    assert _issue(hass) is not None
    CONTRACTS.pop("a")
    assert c.install() is True
    assert c.disabled_reason == []
    assert c.installed == 1
    assert _issue(hass) is None
    c.uninstall()
    c.uninstall()
    assert c.unsubbed == 1
    assert c.handle.restored == 1


async def test_check_contracts_collects_reasons_and_contains_errors(
    hass: HomeAssistant,
) -> None:
    """Reasons are gathered per collector; raising checks count as failures."""

    def boom(hass: HomeAssistant) -> str | None:
        raise RuntimeError("nope")

    register_contract(Contract("ok", "c1", lambda hass: None))
    register_contract(Contract("bad", "c1", lambda hass: "reason"))
    register_contract(Contract("boom", "c1", boom))
    register_contract(Contract("other", "c2", lambda hass: "other reason"))
    assert check_contracts(hass, "c1") == ["reason", "boom check raised RuntimeError"]
    assert check_contracts(hass, "none") == []
