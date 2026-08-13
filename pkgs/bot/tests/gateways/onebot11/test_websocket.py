from __future__ import annotations

from asyncio import Event, TaskGroup, timeout
from http import HTTPStatus
from unittest.mock import AsyncMock

import orjson
import pytest
from bot import (
    ApiStatus,
    Bot,
    BotSelf,
    Connection,
    Injected,
    PrivateMessageEvent,
    ReturnAction,
)
from bot.gateways.onebot11 import (
    ForwardWebSocket,
    HttpWebhook,
    OneBot11Gateway,
    ReverseWebSocket,
    WebSocketAction,
)
from bot.protocol.base import Model
from bot.testing import ScriptedWebSocket
from ulid import ULID
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from .support import private_msg_payload


def test_forward_websocket_validates_role_endpoint_and_identity() -> None:
    self_ = BotSelf(platform="qq", user_id="10000")

    with pytest.raises(ValueError, match="/api endpoint"):
        ForwardWebSocket("ws://onebot.example/", role="api", self_=self_)
    with pytest.raises(ValueError, match="requires a bot identity"):
        ForwardWebSocket("ws://onebot.example/api", role="api")
    with pytest.raises(ValueError, match="decimal qq account"):
        ForwardWebSocket(
            "ws://onebot.example/",
            role="universal",
            self_=BotSelf(platform="discord", user_id="bot"),
        )
    with pytest.raises(ValueError, match="must be positive"):
        ForwardWebSocket(
            "ws://onebot.example/event",
            role="event",
            reconnect_interval=0,
        )


@pytest.mark.parametrize(
    "ingress",
    [
        pytest.param(
            [HttpWebhook("/same"), HttpWebhook("/same", quick_response=False)],
            id="http-path",
        ),
        pytest.param(
            [
                ReverseWebSocket(host="127.0.0.1", port=9000, path="/one"),
                ReverseWebSocket(host="127.0.0.1", port=9000, path="/two"),
            ],
            id="reverse-address",
        ),
        pytest.param(
            [
                ForwardWebSocket("ws://onebot.example/event", role="event"),
                ForwardWebSocket(
                    "ws://onebot.example/event",
                    role="event",
                    self_=BotSelf(platform="qq", user_id="10000"),
                ),
            ],
            id="forward-url",
        ),
    ],
)
def test_gateway_rejects_duplicate_ingress(
    ingress: list[HttpWebhook | ReverseWebSocket | ForwardWebSocket],
) -> None:
    with pytest.raises(ValueError, match="must be unique"):
        OneBot11Gateway(Bot(), ingress=ingress)


async def test_handle_ws_enqueues_event_without_response() -> None:
    bot = Bot()
    gateway = OneBot11Gateway(bot)
    bot.add_gateway(gateway)
    received = Event()

    @bot.on_msg()
    def collect(event: Injected[PrivateMessageEvent]) -> None:
        assert event.message.text == "hello"
        received.set()

    async with timeout(1), bot:
        response = await gateway.handle_ws(Model.model_validate(private_msg_payload()))
        await received.wait()

    assert response is None


async def test_handle_ws_rejects_invalid_action_response_shape() -> None:
    gateway = OneBot11Gateway(Bot())

    response = await gateway.handle_ws(
        Model.model_validate({"status": "failed", "retcode": "bad"})
    )

    assert response is not None
    assert response.status == ApiStatus.FAILED
    assert response.retcode == 1400


@pytest.mark.parametrize(
    ("path", "headers", "status"),
    [
        pytest.param(
            "/onebot/ws",
            {
                "Authorization": "Bearer wrong",
                "X-Self-ID": "10000",
                "X-Client-Role": "Universal",
            },
            HTTPStatus.UNAUTHORIZED,
            id="access-token",
        ),
        pytest.param(
            "/onebot/ws",
            {"Authorization": "Bearer secret", "X-Self-ID": "10000"},
            HTTPStatus.BAD_REQUEST,
            id="missing-role",
        ),
        pytest.param(
            "/onebot/ws",
            {
                "Authorization": "Bearer secret",
                "X-Self-ID": "10000",
                "X-Client-Role": "bad",
            },
            HTTPStatus.BAD_REQUEST,
            id="invalid-role",
        ),
        pytest.param(
            "/onebot/ws",
            {
                "Authorization": "Bearer secret",
                "X-Client-Role": "Universal",
            },
            HTTPStatus.BAD_REQUEST,
            id="missing-self",
        ),
        pytest.param(
            "/onebot/ws",
            {
                "Authorization": "Bearer secret",
                "X-Self-ID": "bot",
                "X-Client-Role": "Universal",
            },
            HTTPStatus.BAD_REQUEST,
            id="invalid-self",
        ),
        pytest.param(
            "/wrong",
            {
                "Authorization": "Bearer secret",
                "X-Self-ID": "10000",
                "X-Client-Role": "Universal",
            },
            HTTPStatus.NOT_FOUND,
            id="path",
        ),
    ],
)
async def test_reverse_websocket_handshake_boundaries(
    path: str,
    headers: dict[str, str],
    status: HTTPStatus,
) -> None:
    bot = Bot()
    credential = "secret"
    gateway = OneBot11Gateway(
        bot,
        ingress=[ReverseWebSocket(port=0, path="/onebot/ws")],
        access_token=credential,
    )
    bot.add_gateway(gateway)

    async with timeout(1), bot:
        port = gateway.reverse_websocket_ports[0]
        with pytest.raises(InvalidStatus) as exc_info:
            async with connect(
                f"ws://127.0.0.1:{port}{path}",
                additional_headers=headers,
                proxy=None,
            ):
                pass

    assert exc_info.value.response.status_code == status


async def test_reverse_websocket_dispatches_and_matches_action_response() -> None:
    bot = Bot()
    gateway = OneBot11Gateway(
        bot,
        ingress=[ReverseWebSocket(port=0, path="/onebot/ws")],
        action=WebSocketAction(timeout=1),
    )
    bot.add_gateway(gateway)

    @bot.on_msg(block=True)
    def collect() -> ReturnAction:
        return ReturnAction.call("get_user_info", {"user_id": "42"})

    async with timeout(2), bot:
        port = gateway.reverse_websocket_ports[0]
        async with connect(
            f"ws://127.0.0.1:{port}/onebot/ws",
            additional_headers={
                "X-Self-ID": "10000",
                "X-Client-Role": "Universal",
            },
            proxy=None,
        ) as websocket:
            await websocket.send(orjson.dumps(private_msg_payload()).decode())
            raw_request = await websocket.recv()
            assert isinstance(raw_request, str)
            request = orjson.loads(raw_request)
            await websocket.send(
                orjson.dumps({
                    "status": "ok",
                    "retcode": 0,
                    "data": {"user_id": 42},
                    "echo": request["echo"],
                }).decode()
            )

    assert str(ULID.from_str(request["echo"])) == request["echo"]
    assert request["action"] == "get_stranger_info"
    assert request["params"] == {"user_id": 42}


async def test_forward_websocket_dispatches_event_with_authorization() -> None:
    bot = Bot()
    received = Event()
    credential = "secret"
    websocket = ScriptedWebSocket(private_msg_payload(), StopAsyncIteration())
    connector = AsyncMock(return_value=websocket)

    @bot.on_msg(block=True)
    def collect(event: Injected[PrivateMessageEvent]) -> None:
        assert event.message.text == "hello"
        received.set()

    gateway = OneBot11Gateway(
        bot,
        ingress=[
            ForwardWebSocket(
                "ws://onebot.example/",
                role="universal",
                self_=BotSelf(platform="qq", user_id="10000"),
                reconnect_interval=60,
            )
        ],
        access_token=credential,
        websocket_connector=connector,
    )
    bot.add_gateway(gateway)

    async with timeout(1), bot:
        await received.wait()
        await websocket.closed.wait()

    connector.assert_awaited_once_with(
        "ws://onebot.example/",
        {"Authorization": "Bearer secret"},
    )


async def test_forward_websocket_waits_until_bot_start_completes() -> None:
    bot = Bot()
    startup_paused = Event()
    continue_startup = Event()
    connected = Event()
    received = Event()
    websocket = ScriptedWebSocket(private_msg_payload(), StopAsyncIteration())

    def connector(
        _url: str,
        _headers: dict[str, str] | None,
    ) -> ScriptedWebSocket:
        connected.set()
        return websocket

    gateway = OneBot11Gateway(
        bot,
        ingress=[
            ForwardWebSocket(
                "ws://onebot.example/",
                role="universal",
                self_=BotSelf(platform="qq", user_id="10000"),
                reconnect_interval=60,
            )
        ],
        websocket_connector=AsyncMock(side_effect=connector),
    )
    bot.add_gateway(gateway)

    @bot.on_start
    async def pause_startup() -> None:
        startup_paused.set()
        await continue_startup.wait()

    @bot.on_msg(block=True)
    def collect() -> None:
        received.set()

    async with timeout(1), TaskGroup() as tasks:
        started = tasks.create_task(bot.start())
        await startup_paused.wait()
        await connected.wait()
        continue_startup.set()
        await started
        await received.wait()
        await bot.close()

    assert websocket.closed.is_set()


async def test_forward_websocket_receives_action_while_waiting_for_events() -> None:
    bot = Bot()
    completed = Event()
    websocket = ScriptedWebSocket(private_msg_payload())
    self_ = BotSelf(platform="qq", user_id="10000")
    gateway = OneBot11Gateway(
        bot,
        ingress=[
            ForwardWebSocket(
                "ws://onebot.example/",
                role="universal",
                self_=self_,
                reconnect_interval=60,
            )
        ],
        action=WebSocketAction(timeout=1),
        websocket_connector=AsyncMock(return_value=websocket),
    )
    bot.add_gateway(gateway)

    @bot.on_msg(block=True)
    async def reply(connection: Injected[Connection]) -> None:
        await connection.send_msg("pong", user_id="42")
        completed.set()

    async with timeout(1), bot:
        request = orjson.loads(await websocket.sent.get())
        websocket.feed({
            "status": "ok",
            "retcode": 0,
            "data": {"message_id": 1},
            "echo": request["echo"],
        })
        await completed.wait()
        websocket.finish()
        await websocket.closed.wait()

    assert request["action"] == "send_private_msg"
    assert request["params"] == {
        "user_id": 42,
        "message": [{"type": "text", "data": {"text": "pong"}}],
    }


async def test_forward_websocket_reconnects_after_connect_and_receive_errors() -> None:
    bot = Bot()
    received = Event()
    calls = 0
    broken = ScriptedWebSocket(ConnectionError("receive failed"))
    working = ScriptedWebSocket(private_msg_payload(), StopAsyncIteration())

    @bot.on_msg(block=True)
    def collect() -> None:
        received.set()

    def connector(
        _url: str,
        _headers: dict[str, str] | None,
    ) -> ScriptedWebSocket:
        nonlocal calls
        calls += 1
        if calls == 1:
            message = "connect failed"
            raise ConnectionError(message)
        return broken if calls == 2 else working

    gateway = OneBot11Gateway(
        bot,
        ingress=[
            ForwardWebSocket(
                "ws://onebot.example/",
                role="universal",
                self_=BotSelf(platform="qq", user_id="10000"),
                reconnect_interval=0.001,
            )
        ],
        websocket_connector=AsyncMock(side_effect=connector),
    )
    bot.add_gateway(gateway)

    async with timeout(1), bot:
        await received.wait()
        await broken.closed.wait()

    assert calls == 3


async def test_websocket_queue_overload_closes_connection() -> None:
    bot = Bot(max_dispatches=1)
    release = Event()
    websocket = ScriptedWebSocket(
        *(private_msg_payload(str(index)) for index in range(66))
    )
    gateway = OneBot11Gateway(
        bot,
        ingress=[
            ForwardWebSocket(
                "ws://onebot.example/",
                role="universal",
                self_=BotSelf(platform="qq", user_id="10000"),
                reconnect_interval=60,
            )
        ],
        websocket_connector=AsyncMock(return_value=websocket),
    )
    bot.add_gateway(gateway)

    @bot.on_msg(block=True)
    async def block() -> None:
        await release.wait()

    async with timeout(1), bot:
        await websocket.closed.wait()
        release.set()

    assert websocket.closed.is_set()
