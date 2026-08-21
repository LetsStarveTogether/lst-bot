from __future__ import annotations

from asyncio import Event as AsyncEvent
from asyncio import timeout
from http import HTTPStatus
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from bot import ActionResponse, ApiStatus, Bot
from bot.gateways.onebot12 import HttpAction, OneBot12Gateway
from urllib3_future import AsyncPoolManager

from tests.gateways.support import ActionServer

from .support import SELF

AUTH = "test-value"
ACTION_RESPONSE = ActionResponse.ok().model_dump(mode="json")


async def test_http_action_preserves_wire_envelope_self_and_null() -> None:
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
            response = cast(
                ActionResponse,
                await gateway.connection_for(SELF).action(
                    "vendor.test",
                    optional=None,
                ),
            )

    assert response.status == ApiStatus.OK
    assert response.data is None
    assert len(server.requests) == 1
    request = server.requests[0]
    assert request.path == "/action?source=test"
    assert request.headers["Authorization"] == "Bearer test-value"
    assert request.json == {
        "action": "vendor.test",
        "params": {"optional": None},
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
