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
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from tests.helpers import make_runtime


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
        self.fail_install = False
        self.unsubbed = 0
        self.handle = _Handle()
        self.uninstalled = 0

    def _install(self) -> None:
        self.installed += 1
        if self.fail_install:
            raise ValueError("secret-user")
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
    c = DummyCollector(hass, make_runtime(hass))
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
    c = DummyCollector(hass, make_runtime(hass))
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


async def test_install_failure_rolls_back_and_raises_issue(
    hass: HomeAssistant,
) -> None:
    """An exception in _install is contained, rolled back and reported."""
    c = DummyCollector(hass, make_runtime(hass))
    c.fail_install = True
    assert c.install() is False
    assert c.disabled_reason == ["install raised ValueError"]
    assert c.uninstalled == 1
    assert _issue(hass) is not None
    assert c.handle.restored == 0


async def test_install_is_noop_when_installed_and_uninstall_runs_once(
    hass: HomeAssistant,
) -> None:
    """Double install does nothing; repeated uninstall tears down once."""
    c = DummyCollector(hass, make_runtime(hass))
    assert c.install() is True
    assert c.install() is True
    assert c.installed == 1
    c.uninstall()
    c.uninstall()
    assert c.uninstalled == 1
    c.uninstall()
    assert c.uninstalled == 1


async def test_uninstall_survives_failing_restore_and_unsub(
    hass: HomeAssistant,
) -> None:
    """One raising restore/unsub/_uninstall does not stop the rest."""

    class Bad:
        def restore(self) -> None:
            raise RuntimeError("x")

    class Boom(DummyCollector):
        def _uninstall(self) -> None:
            raise RuntimeError("x")

    c = Boom(hass, make_runtime(hass))
    assert c.install() is True
    c._handles.insert(0, Bad())  # type: ignore[arg-type]  # noqa: SLF001
    c._unsubs.insert(0, Bad().restore)  # noqa: SLF001
    c.uninstall()
    assert c.handle.restored == 1
    assert c.unsubbed == 1
