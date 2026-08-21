from asyncio import Event as AsyncEvent
from asyncio import create_task, timeout
from http import HTTPStatus
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest
from bot import Action, ActionResponse, ApiStatus, Bot, Msg
from bot.gateways.onebot12 import HttpAction, OneBot12Gateway, ReverseWebSocket
from bot.protocol.actions import ActionParamModel
from pydantic import ValidationError
from urllib3_future import AsyncPoolManager

from tests.gateways.support import ActionServer

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
            action=HttpAction(f"{server.base_url}/action?source=test"),
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
            action=HttpAction(f"{server.base_url}/action"),
        )
        async with gateway:
            with pytest.raises(ValidationError):
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

    pool.request.assert_awaited_once()
    assert pool.request.await_args.kwargs["retries"] is False


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


async def test_http_action_cannot_cross_close_and_restart() -> None:
    class BlockingPool:
        def __init__(self) -> None:
            self.started = AsyncEvent()
            self.release = AsyncEvent()

        async def request(self, *_: object, **__: object) -> object:
            self.started.set()
            await self.release.wait()
            return SimpleNamespace(
                status=HTTPStatus.OK,
                headers={"Content-Type": "application/json"},
                data=AsyncMock(
                    return_value=ActionResponse.ok().model_dump_json().encode()
                )(),
            )

    pool = BlockingPool()
    gateway = OneBot12Gateway(
        Bot(),
        action=HttpAction(
            "http://onebot.example/action",
            http_pool=cast(AsyncPoolManager, pool),
        ),
    )
    connection = gateway.connection_for(SELF)
    async with timeout(1):
        await gateway.start()
        action = create_task(connection.action("vendor_action"))
        try:
            await pool.started.wait()
            await gateway.close()
            await gateway.start()
            pool.release.set()
            with pytest.raises(RuntimeError, match="gateway is closed"):
                await action
        finally:
            pool.release.set()
            async with timeout(1):
                await gateway.close()
