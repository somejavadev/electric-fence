"""Tests for hook wrapping."""

from collections.abc import Awaitable, Callable
from typing import Any

from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.electric_fence.hooks import (
    wrap_async_method,
    wrap_sync_method,
    wrap_ws_command,
)
from homeassistant.components import websocket_api
from homeassistant.components.websocket_api.const import DOMAIN as WS_DOMAIN
from homeassistant.core import HomeAssistant


class T:
    """Target with async and sync methods."""

    async def f(self, a: int) -> int:
        """Return a."""
        return a

    def g(self, a: int) -> int:
        """Return a."""
        return a


async def test_wrap_and_restore_async_method() -> None:
    """Wrapping records calls; restore removes the wrapper."""
    t = T()
    calls: list[int] = []

    def mk(orig: Callable[..., Awaitable[int]]) -> Callable[..., Awaitable[int]]:
        async def w(a: int) -> int:
            calls.append(a)
            return await orig(a)

        return w

    h = wrap_async_method(t, "f", mk)
    assert await t.f(1) == 1
    assert calls == [1]
    h.restore()
    h.restore()
    assert await t.f(2) == 2
    assert calls == [1]
    assert "f" not in vars(t)


async def test_restore_when_someone_wrapped_after_us_becomes_passthrough() -> None:
    """A later third-party wrapper stays; ours stops recording."""
    t = T()
    calls: list[int] = []

    def mk(orig: Callable[..., Awaitable[int]]) -> Callable[..., Awaitable[int]]:
        async def w(a: int) -> int:
            calls.append(a)
            return await orig(a)

        return w

    h = wrap_async_method(t, "f", mk)
    ours = t.f
    third_calls: list[int] = []

    async def third(a: int) -> int:
        third_calls.append(a)
        return await ours(a)

    t.f = third  # type: ignore[method-assign]
    h.restore()
    assert t.f is third
    assert await t.f(5) == 5
    assert third_calls == [5]
    assert calls == []


def test_wrap_and_restore_sync_method_with_own_attribute() -> None:
    """A pre-existing instance attribute is put back."""
    t = T()
    t.g = lambda a: a * 10  # type: ignore[method-assign]
    own = t.g
    calls: list[int] = []

    def mk(orig: Callable[..., Any]) -> Callable[..., Any]:
        def w(a: int) -> Any:
            calls.append(a)
            return orig(a)

        return w

    h = wrap_sync_method(t, "g", mk)
    assert t.g(2) == 20
    assert calls == [2]
    h.restore()
    assert t.g is own


async def test_wrap_ws_command_restores_original(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """The handler is wrapped with its schema kept, then restored."""
    seen: list[int] = []

    @websocket_api.websocket_command({"type": "ef_dummy", "n": int})
    @websocket_api.async_response
    async def handler(
        hass: HomeAssistant, connection: Any, msg: dict[str, Any]
    ) -> None:
        connection.send_result(msg["id"], msg["n"])

    websocket_api.async_register_command(hass, handler)
    original = hass.data[WS_DOMAIN]["ef_dummy"]
    client = await hass_ws_client(hass)

    def mk(orig: Any) -> Any:
        def w(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
            seen.append(msg["n"])
            orig(hass, connection, msg)

        return w

    h = wrap_ws_command(hass, "ef_dummy", mk)
    assert hass.data[WS_DOMAIN]["ef_dummy"][1] is original[1]
    await client.send_json_auto_id({"type": "ef_dummy", "n": 7})
    assert (await client.receive_json())["result"] == 7
    assert seen == [7]
    h.restore()
    assert hass.data[WS_DOMAIN]["ef_dummy"] is original
    await client.send_json_auto_id({"type": "ef_dummy", "n": 8})
    assert (await client.receive_json())["result"] == 8
    assert seen == [7]


async def test_ws_restore_when_replaced_becomes_passthrough(
    hass: HomeAssistant,
) -> None:
    """If another party replaced the entry, restore leaves it alone."""
    _, seen = _ws_dummy(hass, "ef_dummy2")
    h, ours = _wrap_recording(hass, "ef_dummy2", seen)
    other = (lambda *a: ours(*a), None)
    hass.data[WS_DOMAIN]["ef_dummy2"] = other
    h.restore()
    assert hass.data[WS_DOMAIN]["ef_dummy2"] is other
    ours(hass, None, {"id": 1, "n": 3})  # type: ignore[arg-type]
    assert seen == []


def _ws_dummy(hass: HomeAssistant, name: str) -> tuple[Any, list[int]]:
    @websocket_api.websocket_command({"type": name})
    @websocket_api.callback
    def handler(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
        pass

    websocket_api.async_register_command(hass, handler)
    return handler, []


def _wrap_recording(hass: HomeAssistant, name: str, seen: list[int]) -> tuple[Any, Any]:
    def mk(orig: Any) -> Any:
        def w(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
            seen.append(msg["n"])
            orig(hass, connection, msg)

        return w

    h = wrap_ws_command(hass, name, mk)
    return h, hass.data[WS_DOMAIN][name][0]


async def test_ws_rewrap_after_passthrough_records_again(
    hass: HomeAssistant,
) -> None:
    """A fresh wrapper is live even if an earlier one was passthrough."""
    _, seen = _ws_dummy(hass, "ef_dummy3")
    h, ours = _wrap_recording(hass, "ef_dummy3", seen)
    third = (lambda *a: ours(*a), None)
    original = hass.data[WS_DOMAIN]["ef_dummy3"]
    hass.data[WS_DOMAIN]["ef_dummy3"] = third
    h.restore()  # passthrough
    hass.data[WS_DOMAIN]["ef_dummy3"] = original  # third party unwraps
    _wrap_recording(hass, "ef_dummy3", seen)
    new = hass.data[WS_DOMAIN]["ef_dummy3"][0]
    new(hass, None, {"id": 1, "n": 4})
    assert seen == [4]


async def test_rewrap_method_after_passthrough_records_again() -> None:
    """Regression: functools.wraps must not copy a passthrough flag."""
    t = T()
    calls: list[int] = []

    def mk(orig: Callable[..., Awaitable[int]]) -> Callable[..., Awaitable[int]]:
        async def w(a: int) -> int:
            calls.append(a)
            return await orig(a)

        return w

    h = wrap_async_method(t, "f", mk)
    ours = t.f

    async def third(a: int) -> int:
        return await ours(a)

    t.f = third  # type: ignore[method-assign]
    h.restore()  # passthrough
    t.f = ours  # type: ignore[method-assign]  # third party unwraps
    wrap_async_method(t, "f", mk)
    assert await t.f(9) == 9
    assert calls == [9]


def test_wrap_sync_method_on_class_restores_class_attribute() -> None:
    """Wrapping a class-level method leaves no instance attribute behind."""
    t = T()
    h = wrap_sync_method(T, "g", lambda orig: lambda self, a: orig(self, a) + 1)
    assert t.g(1) == 2
    h.restore()
    assert t.g(1) == 1
    assert "g" not in vars(t)
    assert vars(T)["g"] is T.g
