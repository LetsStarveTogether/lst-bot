from __future__ import annotations

from asyncio import gather, timeout
from unittest.mock import AsyncMock

import pytest
from bot import Bot, BotSelf
from bot.gateways.onebot11 import (
    ForwardWebSocket as OneBot11ForwardWebSocket,
)
from bot.gateways.onebot11 import OneBot11Gateway
from bot.gateways.onebot11 import WebSocketAction as OneBot11WebSocketAction
from bot.gateways.onebot12 import (
    ForwardWebSocket as OneBot12ForwardWebSocket,
)
from bot.gateways.onebot12 import OneBot12Gateway
from bot.gateways.onebot12 import WebSocketAction as OneBot12WebSocketAction
from bot.testing import ScriptedWebSocket

from .onebot12.support import connect_payload, status_payload


async def assert_disconnect_cancels_action(
    bot: Bot,
    gateway: OneBot11Gateway | OneBot12Gateway,
    websocket: ScriptedWebSocket,
    self_: BotSelf,
) -> None:
    bot.add_gateway(gateway)
    connection = gateway.connection_for(self_)

    async def disconnect() -> None:
        await websocket.sent.get()
        websocket.finish()

    async with timeout(1), bot:
        await websocket.receiving.wait()
        with pytest.raises(ConnectionError, match="closed"):
            await gather(connection.action("get_version"), disconnect())

    assert websocket.closed.is_set()


@pytest.mark.parametrize(
    "gateway_type",
    [
        pytest.param(OneBot11Gateway, id="onebot11"),
        pytest.param(OneBot12Gateway, id="onebot12"),
    ],
)
async def test_gateway_lifecycle_is_restartable(
    gateway_type: type[OneBot11Gateway | OneBot12Gateway],
) -> None:
    bot = Bot()
    bot.add_gateway(gateway_type(bot))

    async with bot:
        pass
    async with bot:
        pass


async def test_onebot11_disconnect_cancels_pending_action() -> None:
    bot = Bot()
    self_ = BotSelf(platform="qq", user_id="10000")
    websocket = ScriptedWebSocket()
    gateway = OneBot11Gateway(
        bot,
        ingress=[
            OneBot11ForwardWebSocket(
                "ws://onebot.example/api",
                role="api",
                self_=self_,
                reconnect_interval=60,
            )
        ],
        action=OneBot11WebSocketAction(timeout=60),
        websocket_connector=AsyncMock(return_value=websocket),
    )

    await assert_disconnect_cancels_action(bot, gateway, websocket, self_)


async def test_onebot12_disconnect_cancels_pending_action() -> None:
    bot = Bot()
    self_ = BotSelf(platform="qq", user_id="10000")
    websocket = ScriptedWebSocket(
        connect_payload(),
        status_payload(self_),
    )
    gateway = OneBot12Gateway(
        bot,
        ingress=[
            OneBot12ForwardWebSocket(
                "ws://onebot.example/ws",
                reconnect_interval=60,
            )
        ],
        action=OneBot12WebSocketAction(timeout=60),
        websocket_connector=AsyncMock(return_value=websocket),
    )

    await assert_disconnect_cancels_action(bot, gateway, websocket, self_)
