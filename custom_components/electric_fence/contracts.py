"""Registry of runtime contract checks for Home Assistant internals."""

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.core import HomeAssistant


@dataclass(frozen=True)
class Contract:
    """A check that Home Assistant behaves the way a collector expects."""

    name: str
    collector: str
    check: Callable[[HomeAssistant], str | None]


CONTRACTS: dict[str, Contract] = {}


def register_contract(contract: Contract) -> None:
    """Register (or replace) a contract by name."""
    CONTRACTS[contract.name] = contract


def check_contracts(hass: HomeAssistant, collector: str) -> list[str]:
    """Return failure reasons for a collector's contracts; [] means OK."""
    reasons: list[str] = []
    for contract in list(CONTRACTS.values()):
        if contract.collector != collector:
            continue
        try:
            reason = contract.check(hass)
        except Exception as err:  # noqa: BLE001
            reason = f"{contract.name} check raised {type(err).__name__}"
        if reason is not None:
            reasons.append(reason)
    return reasons
