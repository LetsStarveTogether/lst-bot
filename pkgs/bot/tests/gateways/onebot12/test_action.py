from asyncio import Event as AsyncEvent
from asyncio import timeout
from http import HTTPStatus
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest
from bot import Action, ActionResponse, ApiStatus, Bot, Msg
from bot.gateways.onebot12 import HttpAction, OneBot12Gateway, ReverseWebSocket
from urllib3_future import AsyncPoolManager

from tests.gateways.support import ActionServer

from .support import SELF

AUTH = "test-value"
ACTION_RESPONSE = ActionResponse.ok().model_dump(mode="json")


async def test_http_action_preserves_wire_envelope_defaults_and_null() -> None:
    async with ActionServer(ACTION_RESPONSE) as server:
        url = f"{server.base_url}/action?source=test"
        bot = Bot()
        gateway = OneBot12Gateway(
            bot,
            action=HttpAction(url),
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
            action=HttpAction(f"{server.base_url}/action?source=test"),
        )
        bot.add_gateway(gateway)

        async with bot:
            with pytest.raises(RuntimeError, match=message):
                await gateway.connection_for(SELF).action("get_version")


async def test_gateway_does_not_close_borrowed_http_pool() -> None:
    async with (
        ActionServer(ACTION_RESPONSE) as server,
        AsyncPoolManager() as pool,
    ):
        url = f"{server.base_url}/action?source=test"
        bot = Bot()
        gateway = OneBot12Gateway(
            bot,
            action=HttpAction(url, http_pool=pool),
        )
        bot.add_gateway(gateway)
        async with bot:
            await gateway.connection_for(SELF).action("get_version")

        response = await pool.request("POST", url, json={"still": "open"})
        assert response.status == HTTPStatus.OK
        await response.data

    assert len(server.requests) == 2


async def test_start_and_cleanup_failures_close_owned_http_pool() -> None:
    gateway = OneBot12Gateway(
        Bot(),
        ingress=[ReverseWebSocket(port=0)],
        action=HttpAction("http://onebot.example"),
    )
    pool = AsyncMock(spec=AsyncPoolManager)
    gateway.http_pool = cast(AsyncPoolManager, pool)
    cleanup = AsyncMock(side_effect=RuntimeError("cleanup failed"))
    with (
        patch.object(
            gateway,
            "_start_reverse_websocket",
            AsyncMock(side_effect=RuntimeError("start failed")),
        ),
        patch.object(gateway, "_close_transports", cleanup),
        pytest.raises(BaseExceptionGroup, match="startup and cleanup") as error,
    ):
        await gateway.start()

    assert [str(exc) for exc in error.value.exceptions] == [
        "start failed",
        "cleanup failed",
    ]
    cleanup.assert_awaited_once()
    pool.clear.assert_awaited_once()
    assert gateway.http_pool is None
    assert gateway._started is False  # ruff: ignore[private-member-access]


async def test_http_action_timeout_includes_response_body() -> None:
    pool = AsyncMock(spec=AsyncPoolManager)
    pool.request.return_value = SimpleNamespace(
        status=HTTPStatus.OK,
        headers={"Content-Type": "application/json"},
        data=AsyncEvent().wait(),
    )
    bot = Bot()
    gateway = OneBot12Gateway(
        bot,
        action=HttpAction(
            "http://onebot.example/action",
            timeout=0.01,
            http_pool=cast(AsyncPoolManager, pool),
        ),
    )
    bot.add_gateway(gateway)

    async with timeout(1):
        async with bot:
            with pytest.raises(TimeoutError):
                await gateway.connection_for(SELF).action("get_version")


async def test_closed_gateway_rejects_actions() -> None:
    bot = Bot()
    gateway = OneBot12Gateway(
        bot,
        action=HttpAction("http://127.0.0.1:1/action"),
    )
    bot.add_gateway(gateway)
    connection = gateway.connection_for(SELF)

    async with bot:
        pass

    with pytest.raises(RuntimeError, match="gateway is closed"):
        await connection.action("get_version")
