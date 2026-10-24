"""Contracts: `http-login` is created inside the failing request (spike 10.3).

These tests characterise Home Assistant Core, not Electric Fence.
"""

from aiohttp import WSMsgType
import pytest
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from homeassistant.components import persistent_notification
from homeassistant.components.persistent_notification import Notification, UpdateType
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.http import current_request
from homeassistant.setup import async_setup_component
from tests.helpers import create_hass_user, login_flow

EXPECTED_PATH_PREFIX = {
    "login_flow": "/auth/login_flow",
    "rest_bearer": "/api/",
    "websocket_token": "/api/websocket",
}


@pytest.mark.parametrize(
    ("kind", "expected_lingering_timers"),
    [
        pytest.param("login_flow", False, id="login_flow"),
        pytest.param("rest_bearer", False, id="rest_bearer"),
        # aiohttp 3.14 re-arms the server heartbeat when the client's close reply
        # arrives after the server closed (`_flush_heartbeat_reset` does not check
        # `_closed`). Core's own websocket_api tests tolerate lingering timers too.
        pytest.param("websocket_token", True, id="websocket_token"),
    ],
)
async def test_http_login_notification_created_inside_request(
    hass: HomeAssistant, hass_client_no_auth: ClientSessionGenerator, kind: str
) -> None:
    """The notification callback runs while `current_request` is set."""
    assert await async_setup_component(hass, "auth", {})
    assert await async_setup_component(hass, "api", {})
    assert await async_setup_component(hass, "websocket_api", {})
    await create_hass_user(hass, "alice", "correct-password")
    client = await hass_client_no_auth()

    seen: list[tuple[UpdateType, str, bool, str | None]] = []

    @callback
    def _listener(
        update_type: UpdateType, notifications: dict[str, Notification]
    ) -> None:
        request = current_request.get()
        seen.extend(
            (
                update_type,
                notification_id,
                request is not None,
                request.path if request is not None else None,
            )
            for notification_id in notifications
        )

    unsub = persistent_notification.async_register_callback(hass, _listener)

    if kind == "login_flow":
        result = await login_flow(client, "alice", "wrong-password")
        assert result["errors"] == {"base": "invalid_auth"}
    elif kind == "rest_bearer":
        resp = await client.get(
            "/api/", headers={"Authorization": "Bearer not-a-valid-token"}
        )
        assert resp.status == 401
    else:
        async with client.ws_connect("/api/websocket") as websocket:
            assert (await websocket.receive_json())["type"] == "auth_required"
            await websocket.send_json({"type": "auth", "access_token": "not-valid"})
            assert (await websocket.receive_json())["type"] == "auth_invalid"
            # Core closes the socket after process_wrong_login has returned.
            assert (await websocket.receive()).type is WSMsgType.CLOSE
    unsub()

    assert ("http-login", True) in [(n, has_req) for _, n, has_req, _ in seen]
    http_login_paths = [
        path for _, n, has_req, path in seen if n == "http-login" and path is not None
    ]
    seen_paths = {kind: http_login_paths[0]}
    assert seen_paths[kind].startswith(EXPECTED_PATH_PREFIX[kind])
