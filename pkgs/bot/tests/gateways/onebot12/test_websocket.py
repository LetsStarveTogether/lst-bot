from __future__ import annotations

from asyncio import Event as AsyncEvent
from asyncio import TaskGroup, gather, wait_for
from http import HTTPStatus
from typing import cast

import orjson
import pytest
from bot import (
    ActionResponse,
    Bot,
    BotSelf,
    Event,
    Injected,
    PrivateMessageEvent,
    StatusUpdateMetaEvent,
)
from bot.gateways.onebot12 import (
    ForwardWebSocket,
    HttpWebhook,
    OneBot12Gateway,
    ReverseWebSocket,
    WebSocketAction,
)
from bot.testing import ScriptedWebSocket
from pydantic import JsonValue
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.typing import Subprotocol

from .support import SELF, connect_payload, private_message_payload, status_payload

AUTH = "test-value"


def reverse_gateway(
    bot: Bot,
    *,
    token: str | None = None,
    action: bool = False,
) -> OneBot12Gateway:
    gateway = OneBot12Gateway(
        bot,
        ingress=[ReverseWebSocket(host="127.0.0.1", port=0, path="/onebot/ws")],
        action=WebSocketAction(timeout=1) if action else None,
        access_token=token,
    )
    bot.add_gateway(gateway)
    return gateway


def websocket_url(port: int, path: str = "/onebot/ws") -> str:
    return f"ws://127.0.0.1:{port}{path}"


async def open_onebot12(
    port: int,
    *,
    impl: str = "test",
) -> ClientConnection:
    return await connect(
        websocket_url(port),
        subprotocols=[Subprotocol(f"12.{impl}")],
        proxy=None,
    )


async def test_reverse_websocket_dispatches_real_text_frames() -> None:
    bot = Bot()
    gateway = reverse_gateway(bot)
    received = AsyncEvent()

    @bot.on_msg(block=True)
    def collect(event: Injected[PrivateMessageEvent]) -> None:
        assert event.message.text == "hello"
        received.set()

    async with bot:
        port = gateway.reverse_websocket_ports[0]
        async with await open_onebot12(port) as websocket:
            await websocket.send(orjson.dumps(connect_payload()).decode())
            await websocket.send(orjson.dumps(private_message_payload()).decode())
            await wait_for(received.wait(), timeout=1)


@pytest.mark.parametrize(
    ("path", "headers", "subprotocols", "status"),
    [
        pytest.param(
            "/wrong?access_token=test-value",
            None,
            [Subprotocol("12.test")],
            HTTPStatus.NOT_FOUND,
            id="wrong-path",
        ),
        pytest.param(
            "/onebot/ws?access_token=test-value",
            {"Authorization": "Bearer wrong"},
            [Subprotocol("12.test")],
            HTTPStatus.UNAUTHORIZED,
            id="invalid-bearer-precedes-query",
        ),
        pytest.param(
            "/onebot/ws?access_token=test-value",
            None,
            None,
            HTTPStatus.BAD_REQUEST,
            id="missing-subprotocol",
        ),
        pytest.param(
            "/onebot/ws?access_token=test-value",
            None,
            [Subprotocol("12.BAD")],
            HTTPStatus.BAD_REQUEST,
            id="invalid-implementation-name",
        ),
    ],
)
async def test_reverse_websocket_handshake_rejections(
    path: str,
    headers: dict[str, str] | None,
    subprotocols: list[Subprotocol] | None,
    status: HTTPStatus,
) -> None:
    bot = Bot()
    gateway = reverse_gateway(bot, token=AUTH)

    async with bot:
        port = gateway.reverse_websocket_ports[0]
        with pytest.raises(InvalidStatus) as rejected:
            async with connect(
                websocket_url(port, path),
                additional_headers=headers,
                subprotocols=subprotocols,
                proxy=None,
            ):
                pass

    assert rejected.value.response.status_code == status


@pytest.mark.parametrize(
    ("subprotocol", "frames"),
    [
        pytest.param(
            "12.test",
            [orjson.dumps(connect_payload())],
            id="binary-connect-frame",
        ),
        pytest.param(
            "12.test",
            [orjson.dumps(private_message_payload()).decode()],
            id="event-before-connect",
        ),
        pytest.param(
            "12.test",
            [
                orjson.dumps({
                    **connect_payload(),
                    "version": {
                        "impl": "test",
                        "version": "1.0.0",
                        "onebot_version": "11",
                    },
                }).decode()
            ],
            id="wrong-onebot-version",
        ),
        pytest.param(
            "12.other",
            [orjson.dumps(connect_payload()).decode()],
            id="implementation-subprotocol-mismatch",
        ),
        pytest.param(
            "12.test",
            [
                orjson.dumps(connect_payload()).decode(),
                orjson.dumps(connect_payload()).decode(),
            ],
            id="repeated-connect",
        ),
    ],
)
async def test_websocket_protocol_violations_close_connection(
    subprotocol: str,
    frames: list[str | bytes],
) -> None:
    bot = Bot()
    gateway = reverse_gateway(bot)

    async with bot:
        port = gateway.reverse_websocket_ports[0]
        async with connect(
            websocket_url(port),
            subprotocols=[Subprotocol(subprotocol)],
            proxy=None,
        ) as websocket:
            for frame in frames:
                await websocket.send(frame)
            with pytest.raises(ConnectionClosed):
                await websocket.recv()


async def test_action_response_is_read_while_dispatch_is_blocked() -> None:
    bot = Bot(max_dispatches=1)
    gateway = reverse_gateway(bot, action=True)
    status_seen = AsyncEvent()
    dispatch_blocked = AsyncEvent()
    release_dispatch = AsyncEvent()

    @bot.on_event(block=True)
    async def observe(event: Injected[Event]) -> None:
        if isinstance(event, StatusUpdateMetaEvent):
            status_seen.set()
        if isinstance(event, PrivateMessageEvent):
            dispatch_blocked.set()
            await release_dispatch.wait()

    async with bot:
        port = gateway.reverse_websocket_ports[0]
        async with await open_onebot12(port) as websocket:
            await websocket.send(orjson.dumps(connect_payload()).decode())
            await websocket.send(orjson.dumps(status_payload(SELF)).decode())
            await wait_for(status_seen.wait(), timeout=1)
            await websocket.send(orjson.dumps(private_message_payload()).decode())
            await wait_for(dispatch_blocked.wait(), timeout=1)

            try:
                async with TaskGroup() as tasks:
                    action = tasks.create_task(
                        gateway.connection_for(SELF).action(
                            "vendor.test",
                            optional=None,
                        )
                    )
                    request_frame = await wait_for(websocket.recv(), timeout=1)
                    assert isinstance(request_frame, str)
                    request = cast(dict[str, JsonValue], orjson.loads(request_frame))
                    assert request == {
                        "action": "vendor.test",
                        "params": {"optional": None},
                        "echo": request["echo"],
                        "self": {"platform": "qq", "user_id": "10000"},
                    }
                    await websocket.send(
                        orjson.dumps({
                            "status": "ok",
                            "retcode": 0,
                            "data": None,
                            "message": "",
                            "echo": request["echo"],
                        }).decode()
                    )
                    response = cast(ActionResponse, await wait_for(action, timeout=1))
            finally:
                release_dispatch.set()

    assert response.data is None


async def test_action_response_must_come_from_selected_session() -> None:
    bot = Bot()
    gateway = reverse_gateway(bot, action=True)
    self_a = BotSelf(platform="qq", user_id="a")
    self_b = BotSelf(platform="qq", user_id="b")
    seen_statuses: set[str] = set()
    both_bound = AsyncEvent()
    wrong_source_processed = AsyncEvent()

    @bot.on_event(block=True)
    def observe(event: Injected[Event]) -> None:
        if not isinstance(event, StatusUpdateMetaEvent):
            return
        seen_statuses.update(status.self_.user_id for status in event.status.bots)
        if {"a", "b"} <= seen_statuses:
            both_bound.set()
        if event.id == "evt-after-wrong-source":
            wrong_source_processed.set()

    async with bot:
        port = gateway.reverse_websocket_ports[0]
        async with (
            await open_onebot12(port) as websocket_a,
            await open_onebot12(port) as websocket_b,
        ):
            for websocket, self_ in ((websocket_a, self_a), (websocket_b, self_b)):
                await websocket.send(orjson.dumps(connect_payload()).decode())
                await websocket.send(orjson.dumps(status_payload(self_)).decode())
            await wait_for(both_bound.wait(), timeout=1)

            async with TaskGroup() as tasks:
                action = tasks.create_task(
                    gateway.connection_for(self_b).action("get_version")
                )
                request_frame = await wait_for(websocket_b.recv(), timeout=1)
                assert isinstance(request_frame, str)
                request = cast(dict[str, JsonValue], orjson.loads(request_frame))
                response = {
                    "status": "ok",
                    "retcode": 0,
                    "data": None,
                    "message": "",
                    "echo": request["echo"],
                }
                await websocket_a.send(orjson.dumps(response).decode())
                marker = {**status_payload(self_a), "id": "evt-after-wrong-source"}
                await websocket_a.send(orjson.dumps(marker).decode())
                await wait_for(wrong_source_processed.wait(), timeout=1)
                assert not action.done()

                await websocket_b.send(orjson.dumps(response).decode())
                result = cast(ActionResponse, await wait_for(action, timeout=1))

    assert result.data is None


async def test_pending_action_fails_when_session_disconnects() -> None:
    bot = Bot()
    gateway = reverse_gateway(bot, action=True)
    status_seen = AsyncEvent()

    @bot.on_event(block=True)
    def observe(event: Injected[Event]) -> None:
        if isinstance(event, StatusUpdateMetaEvent):
            status_seen.set()

    async with bot:
        port = gateway.reverse_websocket_ports[0]
        websocket = await open_onebot12(port)
        await websocket.send(orjson.dumps(connect_payload()).decode())
        await websocket.send(orjson.dumps(status_payload(SELF)).decode())
        await wait_for(status_seen.wait(), timeout=1)

        async with TaskGroup() as tasks:

            async def expect_disconnect() -> None:
                with pytest.raises(ConnectionError, match="connection closed"):
                    await gateway.connection_for(SELF).action("get_version")

            action = tasks.create_task(expect_disconnect())
            await wait_for(websocket.recv(), timeout=1)
            await websocket.close()
            await action


async def test_websocket_closes_when_global_event_queue_is_full() -> None:
    bot = Bot(max_dispatches=1)
    gateway = reverse_gateway(bot)
    dispatch_started = AsyncEvent()
    release_dispatch = AsyncEvent()

    @bot.on_event(block=True)
    async def block() -> None:
        dispatch_started.set()
        await release_dispatch.wait()

    async with bot:
        port = gateway.reverse_websocket_ports[0]
        async with await open_onebot12(port) as websocket:
            await websocket.send(orjson.dumps(connect_payload()).decode())
            await wait_for(dispatch_started.wait(), timeout=1)
            try:
                for index in range(65):
                    payload = {
                        **private_message_payload(str(index)),
                        "id": f"evt-{index}",
                    }
                    await websocket.send(orjson.dumps(payload).decode())
                with pytest.raises(ConnectionClosed):
                    await websocket.recv()
            except ConnectionClosed:
                pass
            finally:
                release_dispatch.set()


async def test_forward_websocket_reconnects_and_keeps_received_event() -> None:
    first = ScriptedWebSocket(ConnectionError("receive failed"))
    second = ScriptedWebSocket(
        connect_payload(),
        private_message_payload(),
    )
    attempts: list[tuple[str, dict[str, str] | None]] = []
    second_connected = AsyncEvent()

    async def connect_ws(  # ruff: ignore[unused-async] - protocol is asynchronous
        url: str,
        headers: dict[str, str] | None,
    ) -> ScriptedWebSocket:
        attempts.append((url, headers))
        if len(attempts) == 1:
            return first
        second_connected.set()
        return second

    bot = Bot()
    gateway = OneBot12Gateway(
        bot,
        ingress=[
            ForwardWebSocket(
                "ws://onebot.example/ws",
                reconnect_interval=0.001,
            )
        ],
        access_token=AUTH,
        websocket_connector=connect_ws,
    )
    bot.add_gateway(gateway)
    received = AsyncEvent()

    @bot.on_msg(block=True)
    def collect() -> None:
        received.set()

    async with bot:
        await wait_for(second_connected.wait(), timeout=1)
        await wait_for(received.wait(), timeout=1)
        assert first.closed.is_set()

    assert second.closed.is_set()
    assert attempts == [
        ("ws://onebot.example/ws", {"Authorization": "Bearer test-value"}),
        ("ws://onebot.example/ws", {"Authorization": "Bearer test-value"}),
    ]


async def test_lifecycle_is_concurrently_idempotent() -> None:
    websocket = ScriptedWebSocket(connect_payload(), private_message_payload())
    connected = AsyncEvent()
    received = AsyncEvent()
    calls = 0

    async def connect_ws(  # ruff: ignore[unused-async] - protocol is asynchronous
        _url: str,
        _headers: dict[str, str] | None,
    ) -> ScriptedWebSocket:
        nonlocal calls
        calls += 1
        connected.set()
        return websocket

    bot = Bot()
    gateway = OneBot12Gateway(
        bot,
        ingress=[ForwardWebSocket("ws://onebot.example/ws", reconnect_interval=60)],
        websocket_connector=connect_ws,
    )
    bot.add_gateway(gateway)

    @bot.on_msg(block=True)
    def collect() -> None:
        received.set()

    await gather(bot.start(), bot.start())
    await wait_for(connected.wait(), timeout=1)
    await wait_for(received.wait(), timeout=1)
    await gather(bot.close(), bot.close())

    assert calls == 1
    assert websocket.closed.is_set()


@pytest.mark.parametrize(
    "ingress",
    [
        pytest.param(
            [
                ForwardWebSocket("ws://onebot.example/ws"),
                ForwardWebSocket("ws://onebot.example/ws"),
            ],
            id="duplicate-forward-url",
        ),
        pytest.param(
            [HttpWebhook("/events"), HttpWebhook("/events", False)],
            id="duplicate-http-path",
        ),
        pytest.param(
            [
                ReverseWebSocket(host="127.0.0.1", port=8082, path="/a"),
                ReverseWebSocket(host="127.0.0.1", port=8082, path="/b"),
            ],
            id="duplicate-reverse-listener",
        ),
    ],
)
def test_ingress_endpoints_must_be_unique(
    ingress: list[ForwardWebSocket | HttpWebhook | ReverseWebSocket],
) -> None:
    with pytest.raises(ValueError, match="unique"):
        OneBot12Gateway(Bot(), ingress=ingress)


@pytest.mark.parametrize(
    "interval",
    [
        pytest.param(0, id="zero"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="infinity"),
    ],
)
def test_forward_reconnect_interval_must_be_positive(interval: float) -> None:
    with pytest.raises(ValueError, match="positive"):
        ForwardWebSocket("ws://onebot.example/ws", reconnect_interval=interval)


@pytest.mark.parametrize(
    "port",
    [
        pytest.param(True, id="boolean"),
        pytest.param(-1, id="negative"),
        pytest.param(65536, id="above-uint16"),
    ],
)
def test_reverse_port_must_be_valid(port: int) -> None:
    with pytest.raises(ValueError, match="port"):
        ReverseWebSocket(port=port)


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("relative", id="relative"),
        pytest.param("//authority/path", id="authority-form"),
        pytest.param("/path?query", id="query"),
        pytest.param("/path#fragment", id="fragment"),
    ],
)
def test_ingress_path_must_be_origin_form(path: str) -> None:
    with pytest.raises(ValueError, match="origin-form"):
        HttpWebhook(path)
