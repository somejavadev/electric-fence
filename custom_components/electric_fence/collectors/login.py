"""Login collector: H1 (login flow) and H8 (refresh-token confirmation).

Safety (spec §4): every wrapper awaits Core's original first and returns its
result, or lets its exception propagate, unchanged. Everything Electric Fence
does runs inside `self.guard` (fail open), reads memory only, and queues one
record. User resolution and storage happen later in the worker.

Privacy: only `username` is read from the submitted step, never the password
or code. Typed usernames are sanitised before they are kept, and never logged.
"""

from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import TYPE_CHECKING, Any

from aiohttp import hdrs

from homeassistant.auth.models import TOKEN_TYPE_NORMAL, Credentials, RefreshToken
from homeassistant.auth.providers.homeassistant import HassAuthProvider
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, UnknownFlow
from homeassistant.helpers.http import current_request
from homeassistant.helpers.network import is_cloud_connection
from homeassistant.util import dt as dt_util
from homeassistant.util.network import is_local

from ..hooks import wrap_async_method
from ..models import LoginAttempt, LoginReason, LoginResult, Source, UserExists
from ..privacy import sanitise_text
from .base import BaseCollector

if TYPE_CHECKING:
    from ..runtime import Runtime

# Flows remembered between steps (username, MFA module); the oldest is dropped.
MAX_FLOWS = 1024

_FAILURE_ERRORS = frozenset({"invalid_auth", "invalid_code"})

type _IP = IPv4Address | IPv6Address
type _Handler = tuple[str, str | None]
type _AsyncFn = Callable[..., Awaitable[Any]]


@dataclass(slots=True, frozen=True)
class _FlowInfo:
    """What the flow told us before the step ran."""

    handler: _Handler | None
    ip: _IP | None


@dataclass(slots=True)
class _FlowMemo:
    """What earlier steps of a flow told us (sanitised)."""

    username: str | None
    mfa_module: str | None = None


def _arg(args: tuple[Any, ...], kwargs: dict[str, Any], i: int, name: str) -> Any:
    """Return a call argument by position or keyword, else None."""
    return args[i] if len(args) > i else kwargs.get(name)


def _as_handler(value: Any) -> _Handler | None:
    """Return a `(provider_type, provider_id)` handler, if `value` is one."""
    if (
        isinstance(value, (tuple, list))
        and len(value) == 2
        and isinstance(value[0], str)
        and (value[1] is None or isinstance(value[1], str))
    ):
        return (value[0], value[1])
    return None


def _as_ip(value: str | None) -> _IP | None:
    """Return an IP address object from a request's remote address."""
    if value is None:
        return None
    try:
        return ip_address(value)
    except ValueError:
        return None


def _is_attempt(result: Any) -> bool:
    """Return True for results that are recorded."""
    if not isinstance(result, Mapping):
        return False
    rtype = result.get("type")
    if rtype == FlowResultType.CREATE_ENTRY:
        return True
    errors = result.get("errors")
    return (
        rtype == FlowResultType.FORM
        and isinstance(errors, Mapping)
        and errors.get("base") in _FAILURE_ERRORS
    )


def _mfa_module(result: Mapping[str, Any]) -> str | None:
    """Return the MFA module ID an `mfa` step form names, sanitised."""
    placeholders = result.get("description_placeholders")
    if result.get("step_id") != "mfa" or not isinstance(placeholders, Mapping):
        return None
    value = placeholders.get("mfa_module_id")
    return sanitise_text(value) if isinstance(value, str) else None


def _reraise(err: Exception) -> None:
    """Re-raise `err` inside a FailOpen guard so it is counted and logged."""
    raise err


class LoginCollector(BaseCollector):
    """Record login attempts from the login flow, confirmed by refresh tokens."""

    name = "login"

    def __init__(self, hass: HomeAssistant, runtime: Runtime) -> None:
        """Initialise the collector."""
        super().__init__(hass, runtime)
        self._flows: OrderedDict[str, _FlowMemo] = OrderedDict()

    def _install(self) -> None:
        login_flow = self.hass.auth.login_flow
        self._handles.append(
            wrap_async_method(login_flow, "async_init", self._wrap_init)
        )
        self._handles.append(
            wrap_async_method(login_flow, "async_configure", self._wrap_configure)
        )
        self._handles.append(
            wrap_async_method(
                self.hass.auth, "async_create_refresh_token", self._wrap_refresh_token
            )
        )

    def _uninstall(self) -> None:
        self._flows.clear()

    # Wrappers. Core's call runs first, untouched; ours never raises.

    def _wrap_init(self, original: _AsyncFn) -> _AsyncFn:
        async def async_init(*args: Any, **kwargs: Any) -> Any:
            result = await original(*args, **kwargs)
            client_id = await self._read_client_id(result)
            self.guard.call(self._observe_init, args, kwargs, result, client_id)
            return result

        return async_init

    def _wrap_configure(self, original: _AsyncFn) -> _AsyncFn:
        async def async_configure(*args: Any, **kwargs: Any) -> Any:
            flow_id = _arg(args, kwargs, 0, "flow_id")
            # Before the call: a finished flow is gone afterwards.
            info = self.guard.call(self._flow_info, flow_id)
            result = await original(*args, **kwargs)
            client_id = await self._read_client_id(result)
            user_input = _arg(args, kwargs, 1, "user_input")
            self.guard.call(self._observe, flow_id, info, user_input, result, client_id)
            return result

        return async_configure

    def _wrap_refresh_token(self, original: _AsyncFn) -> _AsyncFn:
        async def async_create_refresh_token(*args: Any, **kwargs: Any) -> Any:
            token = await original(*args, **kwargs)
            self.guard.call(self._confirm_token, token)
            return token

        return async_create_refresh_token

    # H8

    def _confirm_token(self, token: RefreshToken) -> None:
        """Confirm the held success of the token's user (normal tokens only)."""
        if token.token_type == TOKEN_TYPE_NORMAL:
            self.runtime.worker.confirm_success(
                token.user.id,
                sanitise_text(token.client_id),
                sanitise_text(token.client_name),
            )

    # H1

    def _flow_info(self, flow_id: Any) -> _FlowInfo | None:
        """Return the handler and IP of a flow in progress, if it exists."""
        if not isinstance(flow_id, str):
            return None
        try:
            flow = self.hass.auth.login_flow.async_get(flow_id)
        except UnknownFlow:
            # Core raises it to the caller in a moment; nothing to record.
            return None
        return self._flow_info_from(flow)

    @staticmethod
    def _flow_info_from(flow: Mapping[str, Any]) -> _FlowInfo:
        """Return the handler and IP from a (partial) flow result."""
        context = flow.get("context")
        ip = context.get("ip_address") if isinstance(context, Mapping) else None
        return _FlowInfo(
            handler=_as_handler(flow.get("handler")),
            ip=ip if isinstance(ip, (IPv4Address, IPv6Address)) else None,
        )

    async def _read_client_id(self, result: Any) -> str | None:
        """Return `client_id` from the JSON body Core has already read.

        Only for recorded results, and only once the body is fully read, so
        this never waits for the network. Never raises.
        """
        try:
            if not _is_attempt(result):
                return None
            request = current_request.get()
            if (
                request is None
                or request.can_read_body
                or request.content_type != "application/json"
            ):
                return None
            # Served from aiohttp's cache of the already-read body.
            data = await request.json()
        except Exception as err:  # noqa: BLE001
            self.guard.call(_reraise, err)
            return None
        value = data.get("client_id") if isinstance(data, dict) else None
        return sanitise_text(value) if isinstance(value, str) else None

    def _observe_init(
        self,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        result: Any,
        client_id: str | None,
    ) -> None:
        """Observe `async_init(handler, *, context=None, data=None)`."""
        context = kwargs.get("context")
        info = self._flow_info_from(
            {"handler": _arg(args, kwargs, 0, "handler"), "context": context}
        )
        flow_id = result.get("flow_id") if isinstance(result, Mapping) else None
        self._observe(flow_id, info, None, result, client_id)

    def _observe(
        self,
        flow_id: Any,
        info: _FlowInfo | None,
        user_input: Any,
        result: Any,
        client_id: str | None,
    ) -> None:
        """Turn a step result into a login attempt and queue it."""
        if not isinstance(result, Mapping):
            return
        rtype = result.get("type")
        raw_name = (
            user_input.get("username") if isinstance(user_input, Mapping) else None
        )
        if not isinstance(raw_name, str):
            raw_name = None
        typed = sanitise_text(raw_name)
        errors = result.get("errors")
        base = errors.get("base") if isinstance(errors, Mapping) else None
        module = _mfa_module(result)

        if rtype in (FlowResultType.CREATE_ENTRY, FlowResultType.ABORT):
            memo = self._flows.pop(flow_id, None) if isinstance(flow_id, str) else None
        else:
            memo = self._remember(flow_id, typed, module, failed=base is not None)
        if typed is None and memo is not None:
            typed = memo.username

        handler = (info.handler if info else None) or _as_handler(result.get("handler"))
        credentials: Credentials | None = None
        mfa_module: str | None = None
        if rtype == FlowResultType.FORM and base == "invalid_auth":
            exists = self._user_exists(handler, raw_name)
            outcome, reason = (
                LoginResult.FAILED,
                {
                    UserExists.YES: LoginReason.WRONG_PASSWORD,
                    UserExists.NO: LoginReason.NO_SUCH_USER,
                    UserExists.UNKNOWN: LoginReason.INVALID_CREDENTIALS,
                }[exists],
            )
        elif rtype == FlowResultType.FORM and base == "invalid_code":
            outcome, reason, exists = (
                LoginResult.FAILED,
                LoginReason.WRONG_2FA_CODE,
                UserExists.YES,
            )
            mfa_module = module or (memo.mfa_module if memo else None)
        elif rtype == FlowResultType.CREATE_ENTRY:
            outcome, reason, exists = (
                LoginResult.SUCCESS,
                LoginReason.OK,
                UserExists.YES,
            )
            mfa_module = memo.mfa_module if memo else None
            found = result.get("result")
            credentials = found if isinstance(found, Credentials) else None
        else:
            # Forms without errors, MFA prompts, aborts: not attempts.
            return

        request = current_request.get()
        ip = info.ip if info else None
        if ip is None and request is not None:
            ip = _as_ip(request.remote)
        if is_cloud_connection(self.hass):
            source: Source | None = Source.CLOUD
        elif ip is None:
            source = None
        else:
            source = Source.LOCAL if is_local(ip) else Source.REMOTE

        self.runtime.worker.submit(
            LoginAttempt(
                ts=dt_util.utcnow(),
                result=outcome,
                reason=reason,
                username_typed=typed,
                user_exists=exists,
                user_id=None,
                provider=sanitise_text(handler[0]) if handler else None,
                ip=str(ip) if ip is not None else None,
                source=source,
                url=sanitise_text(request.path) if request is not None else None,
                user_agent=(
                    sanitise_text(request.headers.get(hdrs.USER_AGENT))
                    if request is not None
                    else None
                ),
                client_id=client_id,
                mfa_module=mfa_module,
                # A success waits for its refresh token (H8).
                confirmed=outcome is not LoginResult.SUCCESS,
                flow_id=flow_id if isinstance(flow_id, str) else None,
                credentials=credentials,
            )
        )

    def _remember(
        self, flow_id: Any, typed: str | None, module: str | None, *, failed: bool
    ) -> _FlowMemo | None:
        """Keep the username and MFA module of a flow that goes on to a next step.

        A failed step doesn't start a memo: the next attempt carries its own
        username. Bounded by MAX_FLOWS.
        """
        if not isinstance(flow_id, str):
            return None
        memo = self._flows.get(flow_id)
        if memo is None:
            if failed or (typed is None and module is None):
                return None
            memo = self._flows[flow_id] = _FlowMemo(username=typed)
            while len(self._flows) > MAX_FLOWS:
                self._flows.popitem(last=False)
        else:
            self._flows.move_to_end(flow_id)
            if typed is not None:
                memo.username = typed
        if module is not None:
            memo.mfa_module = module
        return memo

    def _user_exists(
        self, handler: _Handler | None, raw_name: str | None
    ) -> UserExists:
        """Look the typed name up in the `homeassistant` provider's memory."""
        if handler is None or raw_name is None:
            return UserExists.UNKNOWN
        provider = self.hass.auth.get_auth_provider(*handler)
        if not isinstance(provider, HassAuthProvider) or provider.data is None:
            return UserExists.UNKNOWN
        data = provider.data
        name = data.normalize_username(raw_name)
        # Normalised on both sides, as Core's own validate_login does.
        if any(data.normalize_username(u["username"]) == name for u in data.users):
            return UserExists.YES
        return UserExists.NO
