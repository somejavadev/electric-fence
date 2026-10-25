"""Swap attributes on live objects, with safe restore."""

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, cast

from homeassistant.components.websocket_api.const import (
    DOMAIN as WEBSOCKET_API_DOMAIN,
    WebSocketCommandHandler,
)
from homeassistant.core import HassJob, HassJobType, HomeAssistant, ServiceCall

_MISSING: Any = object()


class _State:
    """Mutable passthrough flag shared by a wrapper and its handle."""

    passthrough = False


class HookHandle:
    """Handle to an installed hook."""

    def __init__(
        self,
        state: _State,
        is_current: Callable[[], bool],
        put_back: Callable[[], None],
    ) -> None:
        """Initialise the handle."""
        self._state = state
        self._is_current = is_current
        self._put_back = put_back
        self._restored = False

    def restore(self) -> None:
        """Restore the original, or make our wrapper a passthrough.

        Idempotent. If someone wrapped after us, their wrapper stays in
        place and ours calls straight through to the original.
        """
        if self._restored:
            return
        self._restored = True
        if self._is_current():
            self._put_back()
        else:
            self._state.passthrough = True


def _passthrough_async[T](
    orig: Callable[..., Awaitable[T]],
    inner: Callable[..., Awaitable[T]],
    state: _State,
) -> Callable[..., Awaitable[T]]:
    @wraps(orig)
    async def outer(*args: Any, **kwargs: Any) -> T:
        if state.passthrough:
            return await orig(*args, **kwargs)
        return await inner(*args, **kwargs)

    return outer


def _passthrough_sync[R](
    orig: Callable[..., R], inner: Callable[..., R], state: _State
) -> Callable[..., R]:
    @wraps(orig)
    def outer(*args: Any, **kwargs: Any) -> R:
        if state.passthrough:
            return orig(*args, **kwargs)
        return inner(*args, **kwargs)

    return outer


def _install_attr(
    obj: object,
    attr: str,
    build: Callable[[Callable[..., Any], _State], Callable[..., Any]],
) -> HookHandle:
    orig = getattr(obj, attr)
    try:
        own = vars(obj).get(attr, _MISSING)
    except TypeError:
        own = _MISSING
    state = _State()
    wrapper = build(orig, state)
    setattr(obj, attr, wrapper)

    def put_back() -> None:
        if own is _MISSING:
            try:
                delattr(obj, attr)
            except AttributeError:
                setattr(obj, attr, orig)
        else:
            setattr(obj, attr, own)

    def is_current() -> bool:
        return getattr(obj, attr, None) is wrapper

    return HookHandle(state, is_current, put_back)


def wrap_async_method[T](
    obj: object,
    attr: str,
    make_wrapper: Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]],
) -> HookHandle:
    """Wrap the async callable `obj.attr`."""
    return _install_attr(
        obj,
        attr,
        lambda orig, state: _passthrough_async(orig, make_wrapper(orig), state),
    )


def wrap_sync_method(
    obj: object,
    attr: str,
    make_wrapper: Callable[[Callable[..., Any]], Callable[..., Any]],
) -> HookHandle:
    """Wrap the sync callable `obj.attr`."""
    return _install_attr(
        obj,
        attr,
        lambda orig, state: _passthrough_sync(orig, make_wrapper(orig), state),
    )


def wrap_ws_command(
    hass: HomeAssistant,
    command: str,
    make_wrapper: Callable[[WebSocketCommandHandler], WebSocketCommandHandler],
) -> HookHandle:
    """Wrap a registered WebSocket command handler, keeping its schema."""
    handlers = hass.data[WEBSOCKET_API_DOMAIN]
    original = handlers[command]
    handler, schema = original
    state = _State()
    wrapper = cast(
        WebSocketCommandHandler,
        _passthrough_sync(handler, make_wrapper(handler), state),
    )
    handlers[command] = (wrapper, schema)

    def is_current() -> bool:
        current = handlers.get(command)
        return current is not None and current[0] is wrapper

    def put_back() -> None:
        handlers[command] = original

    return HookHandle(state, is_current, put_back)


type ServiceHandler = Callable[[ServiceCall], Awaitable[Any]]


async def run_service_job(
    hass: HomeAssistant, job: HassJob[..., Any], call: ServiceCall
) -> Any:
    """Run a service's job as Core's `ServiceRegistry._execute_service` does."""
    target = job.target
    if job.job_type is HassJobType.Coroutinefunction:
        return await target(call)
    if job.job_type is HassJobType.Callback:
        return target(call)
    return await hass.async_add_executor_job(target, call)


def wrap_service(
    hass: HomeAssistant,
    domain: str,
    service: str,
    make_wrapper: Callable[[ServiceHandler], ServiceHandler],
) -> HookHandle:
    """Wrap a registered service's handler (the `Service` object's job).

    The registry's private `_services[domain][service]` is Core's `Service`
    (pinned by a contract). Its job is replaced by a coroutine job that runs
    our wrapper, which gets a function running Core's original job exactly
    as Core would (same thread, same result, same exception).
    """
    service_obj = hass.services._services[domain][service]  # noqa: SLF001
    original: HassJob[..., Any] = service_obj.job
    state = _State()

    async def run_original(call: ServiceCall) -> Any:
        return await run_service_job(hass, original, call)

    inner = make_wrapper(run_original)

    async def service_wrapper(call: ServiceCall) -> Any:
        if state.passthrough:
            return await run_original(call)
        return await inner(call)

    job = HassJob(
        service_wrapper, original.name, job_type=HassJobType.Coroutinefunction
    )
    service_obj.job = job

    def is_current() -> bool:
        return service_obj.job is job

    def put_back() -> None:
        service_obj.job = original

    return HookHandle(state, is_current, put_back)
