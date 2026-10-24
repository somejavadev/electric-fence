"""Registry of runtime contract checks for Home Assistant internals."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import inspect
from typing import Any

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


_EMPTY = inspect.Parameter.empty
_POS = inspect.Parameter.POSITIONAL_OR_KEYWORD.name
_KW = inspect.Parameter.KEYWORD_ONLY.name

# (name, kind name, default); default `_EMPTY` means required.
type _Params = Sequence[tuple[str, str, Any]]


def _params_reason(label: str, fn: Any, expected: _Params, *, coro: bool) -> str | None:
    """Check `fn` is callable with `expected`, and is a coroutine iff `coro`."""
    if not callable(fn):
        return f"{label} is missing"
    is_coro = inspect.iscoroutinefunction(fn)
    if coro and not is_coro:
        return f"{label} is not a coroutine function"
    if not coro and is_coro:
        # Called synchronously by the hook.
        return f"{label} is a coroutine function"
    params = [
        (p.name, p.kind.name, p.default)
        for p in inspect.signature(fn).parameters.values()
    ]
    if params != list(expected):
        return f"{label} has an unexpected signature"
    return None


def _check_login_flow_manager(hass: HomeAssistant) -> str | None:
    """`hass.auth.login_flow` has the methods the login collector wraps."""
    mgr = getattr(hass.auth, "login_flow", None)
    if mgr is None:
        return "hass.auth.login_flow is missing"
    label = "hass.auth.login_flow"
    checks: list[tuple[str, _Params, bool]] = [
        (
            "async_init",
            [("handler", _POS, _EMPTY), ("context", _KW, None), ("data", _KW, None)],
            True,
        ),
        (
            "async_configure",
            [("flow_id", _POS, _EMPTY), ("user_input", _POS, None)],
            True,
        ),
        ("async_get", [("flow_id", _POS, _EMPTY)], False),
    ]
    for attr, expected, coro in checks:
        fn = getattr(mgr, attr, None)
        if reason := _params_reason(f"{label}.{attr}", fn, expected, coro=coro):
            return reason
    return None


def _check_refresh_token_create(hass: HomeAssistant) -> str | None:
    """`hass.auth.async_create_refresh_token(user, client_id, client_name, ...)`."""
    label = "hass.auth.async_create_refresh_token"
    fn = getattr(hass.auth, "async_create_refresh_token", None)
    if not callable(fn):
        return f"{label} is missing"
    if not inspect.iscoroutinefunction(fn):
        return f"{label} is not a coroutine function"
    names = list(inspect.signature(fn).parameters)
    if names[:1] != ["user"] or not {"client_id", "client_name"} <= set(names):
        return f"{label} has an unexpected signature"
    return None


register_contract(Contract("login_flow_manager", "login", _check_login_flow_manager))
register_contract(
    Contract("refresh_token_create", "login", _check_refresh_token_create)
)
