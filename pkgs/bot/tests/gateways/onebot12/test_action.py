from asyncio import Event as AsyncEvent
from asyncio import create_task, timeout
from http import HTTPStatus
from typing import cast, override
from unittest.mock import AsyncMock, patch

import pytest
from bot import Action, ActionResponse, ApiStatus, Bot, Msg
from bot.gateways.onebot12 import HttpAction, OneBot12Gateway, ReverseWebSocket
from bot.protocol.actions import ActionParamModel
from httpx2 import AsyncClient, Request, Response
from pydantic import ValidationError

from tests.gateways.support import ActionServer, HangingBodyStream, HttpMock

from .support import SELF

AUTH = "test-value"
ACTION_RESPONSE = ActionResponse.ok().model_dump(mode="json")


async def test_action_rejects_a_connection_from_another_gateway() -> None:
    gateway = OneBot12Gateway(Bot())
    foreign = OneBot12Gateway(Bot()).connection_for(SELF)

    with pytest.raises(ValueError, match="another gateway"):
        await gateway.request_action(foreign, "get_status", ActionParamModel())


async def test_http_action_preserves_wire_envelope_defaults_and_null() -> None:
    async with ActionServer(ACTION_RESPONSE) as server:
        url = f"{server.base_url}/action?source=test"
        bot = Bot()
        gateway = OneBot12Gateway(
            bot,
            action=HttpAction(url, http_client=server.http_client),
            access_token=AUTH,
        )
        bot.add_gateway(gateway)

        async with bot:
            connection = gateway.connection_for(SELF)
            response = cast(
                ActionResponse,
                await connection.action(
                    "vendor.test",
                    optional=None,
                ),
            )
            await connection.action(
                Action.SEND_MESSAGE,
                detail_type="private",
                user_id="42",
                message=Msg.reply("message-1"),
            )

    assert response.status == ApiStatus.OK
    assert response.data is None
    assert len(server.requests) == 2
    request, message_request = server.requests
    assert request.path == "/action?source=test"
    assert request.headers["Authorization"] == "Bearer test-value"
    assert request.json == {
        "action": "vendor.test",
        "params": {"optional": None},
        "self": {"platform": "qq", "user_id": "10000"},
    }
    assert message_request.json == {
        "action": "send_message",
        "params": {
            "detail_type": "private",
            "message": [{"type": "reply", "data": {"message_id": "message-1"}}],
            "user_id": "42",
        },
        "self": {"platform": "qq", "user_id": "10000"},
    }


@pytest.mark.parametrize(
    ("status", "content_type", "message"),
    [
        pytest.param(
            HTTPStatus.UNAUTHORIZED,
            "application/json",
            "HTTP 401",
            id="non-200-status",
        ),
        pytest.param(
            HTTPStatus.OK,
            "text/plain",
            "unsupported Content-Type",
            id="non-json-content-type",
        ),
    ],
)
async def test_http_action_rejects_transport_contract_violations(
    status: HTTPStatus,
    content_type: str,
    message: str,
) -> None:
    async with ActionServer(
        ACTION_RESPONSE,
        status=status,
        content_type=content_type,
    ) as server:
        bot = Bot()
        gateway = OneBot12Gateway(
            bot,
            action=HttpAction(
                f"{server.base_url}/action?source=test",
                http_client=server.http_client,
            ),
        )
        bot.add_gateway(gateway)

        async with bot:
            with pytest.raises(RuntimeError, match=message):
                await gateway.connection_for(SELF).action("get_version")


@pytest.mark.parametrize(
    ("status", "retcode"),
    [("async", 1), ("failed", 10_008), ("failed", 40_000), ("failed", 50_000)],
)
async def test_http_action_rejects_invalid_response(
    status: str,
    retcode: int,
) -> None:
    payload = {
        "status": status,
        "retcode": retcode,
        "data": None,
        "message": "invalid",
    }
    async with ActionServer(payload) as server:
        gateway = OneBot12Gateway(
            Bot(),
            action=HttpAction(
                f"{server.base_url}/action",
                http_client=server.http_client,
            ),
        )
        async with gateway:
            with pytest.raises(ValidationError):
                await gateway.connection_for(SELF).action("get_version")


async def test_startup_and_cleanup_failures_are_grouped() -> None:
    gateway = OneBot12Gateway(Bot(), ingress=[ReverseWebSocket(port=0)])
    startup_error = RuntimeError("start failed")
    cleanup_error = RuntimeError("cleanup failed")
    with (
        patch.object(
            gateway,
            "_start_reverse_websocket",
            AsyncMock(side_effect=startup_error),
        ),
        patch.object(
            gateway,
            "_close_transports",
            AsyncMock(side_effect=cleanup_error),
        ),
        pytest.raises(BaseExceptionGroup) as error,
    ):
        await gateway.start()

    assert error.value.exceptions == (startup_error, cleanup_error)
    assert gateway._started is False  # ruff: ignore[private-member-access]


async def test_start_retries_failed_cleanup() -> None:
    gateway = OneBot12Gateway(Bot())
    cleanup = AsyncMock(side_effect=[RuntimeError("cleanup failed"), None, None])
    with patch.object(gateway, "_close_transports", cleanup):
        with pytest.raises(RuntimeError, match="cleanup failed"):
            await gateway.close()
        await gateway.start()
        await gateway.close()

    assert cleanup.await_count == 3


async def test_http_action_timeout_includes_response_body() -> None:
    hanging = HangingBodyStream()
    http = HttpMock(
        Response(200, stream=hanging, headers={"Content-Type": "application/json"})
    )
    bot = Bot()
    gateway = OneBot12Gateway(
        bot,
        action=HttpAction(
            "http://onebot.example/action",
            timeout=0.01,
            http_client=http.http_client,
        ),
    )
    bot.add_gateway(gateway)

    async with timeout(1), http.http_client, bot:
        with pytest.raises(TimeoutError):
            await gateway.connection_for(SELF).action("get_version")

    assert len(http.requests) == 1
    assert http.requests[0].extensions["timeout"] == {
        "connect": 0.01,
        "read": 0.01,
        "write": 0.01,
        "pool": 0.01,
    }
    assert hanging.cancelled.is_set()
    assert hanging.close_called.is_set()


async def test_closed_gateway_rejects_actions() -> None:
    bot = Bot()
    gateway = OneBot12Gateway(
        bot,
        action=HttpAction(
            "http://127.0.0.1:1/action",
            http_client=AsyncMock(spec=AsyncClient),
        ),
    )
    bot.add_gateway(gateway)
    connection = gateway.connection_for(SELF)

    async with bot:
        pass

    with pytest.raises(RuntimeError, match="gateway is closed"):
        await connection.action("get_version")


async def test_http_action_cannot_cross_close_and_restart() -> None:
    class BlockingHTTP(HttpMock):
        def __init__(self) -> None:
            super().__init__(ACTION_RESPONSE)
            self.started = AsyncEvent()
            self.release = AsyncEvent()

        @override
        async def handle(self, request: Request) -> Response:
            self.started.set()
            await self.release.wait()
            return await super().handle(request)

    http = BlockingHTTP()
    gateway = OneBot12Gateway(
        Bot(),
        action=HttpAction(
            "http://onebot.example/action",
            http_client=http.http_client,
        ),
    )
    connection = gateway.connection_for(SELF)
    async with timeout(1), http.http_client:
        await gateway.start()
        action = create_task(connection.action("vendor_action"))
        try:
            await http.started.wait()
            await gateway.close()
            await gateway.start()
            http.release.set()
            with pytest.raises(RuntimeError, match="gateway is closed"):
                await action
        finally:
            http.release.set()
            async with timeout(1):
                await gateway.close()
