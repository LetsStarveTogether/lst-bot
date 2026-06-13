from __future__ import annotations

from asyncio import sleep, wait_for
from typing import cast

import orjson
from robyn import Robyn
from ulid import ULID

from lst_bot import Bot, Injected, PrivateMessageEvent, ReturnAction
from lst_bot.gateways.onebot12 import (
    ForwardWebSocket,
    OneBot12Gateway,
    ReverseWebSocket,
    WebSocketAction,
)
from lst_bot.protocol.base import Model
from tests.gateways.support import (
    EchoResponseWebSocket,
    FailingReceiveWebSocket,
    QueuedWebSocket,
    RobynServer,
)
from tests.protocol.support import private_msg_payload


async def test_reverse_ws_event_dispatches_without_response() -> None:
    bot = Bot()
    gateway = OneBot12Gateway(bot)
    bot.add_gateway(gateway)
    events: list[str] = []

    @bot.on_msg()
    def collect(event: Injected[PrivateMessageEvent]) -> None:
        events.append(event.message.text)

    response = await gateway.handle_ws(Model.model_validate(private_msg_payload()))

    assert response is None
    assert events == ["hello"]


async def test_reverse_ws_route_rejects_invalid_access_token() -> None:
    bot = Bot()
    credential = "secret"
    gateway = OneBot12Gateway(
        bot,
        ingress=[ReverseWebSocket("/onebot/ws")],
        access_token=credential,
    )
    raw_server = RobynServer()
    gateway.mount(cast(Robyn, raw_server))
    ws_handler = raw_server.routes[0][2]
    websocket = QueuedWebSocket(
        private_msg_payload(),
        headers={"Authorization": "Bearer wrong"},
    )

    await ws_handler(websocket)

    assert websocket.closed is True
    assert websocket.sent == []


async def test_reverse_ws_action_uses_echo_response() -> None:
    bot = Bot()
    gateway = OneBot12Gateway(
        bot,
        ingress=[ReverseWebSocket("/onebot/ws")],
        action=WebSocketAction(timeout=1),
    )
    bot.add_gateway(gateway)

    @bot.on_msg(block=True)
    def collect() -> ReturnAction:
        return ReturnAction.call("get_user_info", {"user_id": "42"})

    raw_server = RobynServer()
    gateway.mount(cast(Robyn, raw_server))
    ws_handler = raw_server.routes[0][2]
    websocket = EchoResponseWebSocket(private_msg_payload())

    async with bot:
        await wait_for(ws_handler(websocket), timeout=2)

    sent = [orjson.loads(payload) for payload in websocket.sent]
    assert str(ULID.from_str(sent[0]["echo"])) == sent[0]["echo"]
    assert sent == [
        {
            "action": "get_user_info",
            "params": {"user_id": "42"},
            "echo": sent[0]["echo"],
            "self": {"platform": "qq", "user_id": "10000"},
        },
    ]


async def test_forward_ws_starts_connection_and_dispatches_event() -> None:
    bot = Bot()
    events: list[str] = []
    calls: list[dict[str, str] | None] = []

    @bot.on_msg(block=True)
    def collect(event: Injected[PrivateMessageEvent]) -> None:
        events.append(event.message.text)

    async def connect(
        _url: str,
        headers: dict[str, str] | None,
    ) -> QueuedWebSocket:
        await sleep(0)
        calls.append(headers)
        return QueuedWebSocket(private_msg_payload())

    credential = "secret"
    gateway = OneBot12Gateway(
        bot,
        ingress=[ForwardWebSocket("ws://onebot.example/ws", reconnect_interval=60)],
        access_token=credential,
        websocket_connector=connect,
    )
    bot.add_gateway(gateway)

    async with bot:
        for _ in range(20):
            if events:
                break
            await sleep(0.01)

    assert calls == [{"Authorization": "Bearer secret"}]
    assert events == ["hello"]


async def test_forward_ws_reconnects_after_connect_and_receive_failures() -> None:
    bot = Bot()
    events: list[str] = []
    calls = 0
    broken = FailingReceiveWebSocket()

    @bot.on_msg(block=True)
    def collect(event: Injected[PrivateMessageEvent]) -> None:
        events.append(event.message.text)

    async def connect(
        _url: str,
        _headers: dict[str, str] | None,
    ) -> QueuedWebSocket | FailingReceiveWebSocket:
        nonlocal calls
        calls += 1
        await sleep(0)
        if calls == 1:
            msg = "connect failed"
            raise ConnectionError(msg)
        if calls == 2:
            return broken
        return QueuedWebSocket(private_msg_payload())

    gateway = OneBot12Gateway(
        bot,
        ingress=[ForwardWebSocket("ws://onebot.example/ws", reconnect_interval=0.01)],
        websocket_connector=connect,
    )
    bot.add_gateway(gateway)

    async with bot:
        for _ in range(50):
            if events:
                break
            await sleep(0.01)

    assert calls >= 3
    assert broken.closed is True
    assert events == ["hello"]
