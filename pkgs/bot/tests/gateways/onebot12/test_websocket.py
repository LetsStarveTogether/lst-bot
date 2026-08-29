from asyncio import Event as AsyncEvent
from asyncio import QueueFull, TaskGroup, timeout
from http import HTTPStatus
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, call, patch

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
from bot.json import dumpb, loads
from bot.testing import ScriptedWebSocket
from pydantic import JsonValue
from websockets.asyncio.client import ClientConnection, connect
from websockets.asyncio.server import Server
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.typing import Origin, Subprotocol

from .support import (
    SELF,
    ObservableReadinessBot,
    connect_payload,
    private_message_payload,
    status_payload,
)

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

    async with timeout(3), bot:
        port = gateway.reverse_websocket_ports[0]
        async with await open_onebot12(port) as websocket:
            await websocket.send(dumpb(connect_payload()).decode())
            await websocket.send(dumpb(private_message_payload()).decode())
            await received.wait()


def test_reverse_websocket_reports_every_actual_port() -> None:
    bot = Bot()
    gateway = OneBot12Gateway(bot)
    server = cast(
        Server,
        SimpleNamespace(
            sockets=[
                SimpleNamespace(getsockname=lambda: ("::1", 10001, 0, 0)),
                SimpleNamespace(getsockname=lambda: ("127.0.0.1", 10002)),
                SimpleNamespace(getsockname=lambda: ("127.0.0.1", 10001)),
            ]
        ),
    )
    gateway._reverse_servers.append(server)  # ruff: ignore[private-member-access]

    assert gateway.reverse_websocket_ports == (10001, 10002)


async def test_reverse_websocket_close_cancels_prestartup_handler() -> None:
    bot = ObservableReadinessBot()
    gateway = reverse_gateway(bot)
    async with timeout(1):
        await gateway.start()
        try:
            websocket = await open_onebot12(gateway.reverse_websocket_ports[0])
            await bot.waiting.wait()
            await gateway.close()
            with pytest.raises(ConnectionClosed):
                await websocket.recv()
        finally:
            await gateway.close()


@pytest.mark.parametrize(
    ("path", "headers", "subprotocols", "client_headers", "status"),
    [
        pytest.param(
            "/wrong?access_token=test-value",
            None,
            [Subprotocol("12.test")],
            ("test", None),
            HTTPStatus.NOT_FOUND,
            id="wrong-path",
        ),
        pytest.param(
            "/onebot/ws?access_token=test-value",
            {"Authorization": "Bearer wrong"},
            [Subprotocol("12.test")],
            ("test", None),
            HTTPStatus.UNAUTHORIZED,
            id="invalid-bearer-precedes-query",
        ),
        pytest.param(
            "/onebot/ws?access_token=test-value",
            None,
            None,
            ("test", None),
            HTTPStatus.BAD_REQUEST,
            id="missing-subprotocol",
        ),
        pytest.param(
            "/onebot/ws?access_token=test-value",
            None,
            [Subprotocol("12.BAD")],
            ("test", None),
            HTTPStatus.BAD_REQUEST,
            id="invalid-implementation-name",
        ),
        pytest.param(
            "/onebot/ws?access_token=test-value",
            None,
            [Subprotocol("12.test")],
            (None, None),
            HTTPStatus.BAD_REQUEST,
            id="missing-user-agent",
        ),
        pytest.param(
            "/onebot/ws?access_token=test-value",
            None,
            [Subprotocol("12.test")],
            ("test", Origin("https://evil.example")),
            HTTPStatus.FORBIDDEN,
            id="browser-origin",
        ),
    ],
)
async def test_reverse_websocket_handshake_rejections(
    path: str,
    headers: dict[str, str] | None,
    subprotocols: list[Subprotocol] | None,
    client_headers: tuple[str | None, Origin | None],
    status: HTTPStatus,
) -> None:
    bot = Bot()
    gateway = reverse_gateway(bot, token=AUTH)
    user_agent, origin = client_headers

    async with timeout(3), bot:
        port = gateway.reverse_websocket_ports[0]
        with pytest.raises(InvalidStatus) as rejected:
            async with connect(
                websocket_url(port, path),
                additional_headers=headers,
                subprotocols=subprotocols,
                user_agent_header=user_agent,
                origin=origin,
                proxy=None,
            ):
                pass

    assert rejected.value.response.status_code == status


@pytest.mark.parametrize(
    ("subprotocol", "frames"),
    [
        pytest.param(
            "12.test",
            [dumpb(connect_payload())],
            id="binary-connect-frame",
        ),
        pytest.param(
            "12.test",
            [dumpb(private_message_payload()).decode()],
            id="event-before-connect",
        ),
        pytest.param(
            "12.test",
            [
                dumpb({
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
            [dumpb(connect_payload()).decode()],
            id="implementation-subprotocol-mismatch",
        ),
        pytest.param(
            "12.test",
            [
                dumpb(connect_payload()).decode(),
                dumpb(connect_payload()).decode(),
            ],
            id="repeated-connect",
        ),
        pytest.param(
            "12.test",
            [
                dumpb(connect_payload()).decode(),
                dumpb({
                    "status": "failed",
                    "retcode": 40_000,
                    "data": None,
                    "message": "reserved retcode",
                }).decode(),
            ],
            id="invalid-action-response",
        ),
    ],
)
async def test_websocket_protocol_violations_close_connection(
    subprotocol: str,
    frames: list[str | bytes],
) -> None:
    bot = Bot()
    gateway = reverse_gateway(bot)

    async with timeout(3), bot:
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

    async with timeout(3), bot:
        port = gateway.reverse_websocket_ports[0]
        async with await open_onebot12(port) as websocket:
            await websocket.send(dumpb(connect_payload()).decode())
            await websocket.send(dumpb(status_payload(SELF)).decode())
            await status_seen.wait()
            await websocket.send(dumpb(private_message_payload()).decode())
            await dispatch_blocked.wait()

            try:
                async with TaskGroup() as tasks:
                    action = tasks.create_task(
                        gateway.connection_for(SELF).action(
                            "vendor.test",
                            optional=None,
                        )
                    )
                    request_frame = await websocket.recv()
                    assert isinstance(request_frame, str)
                    request = cast(dict[str, JsonValue], loads(request_frame))
                    assert request == {
                        "action": "vendor.test",
                        "params": {"optional": None},
                        "echo": request["echo"],
                        "self": {"platform": "qq", "user_id": "10000"},
                    }
                    await websocket.send(
                        dumpb({
                            "status": "ok",
                            "retcode": 0,
                            "data": None,
                            "message": "",
                            "echo": request["echo"],
                        }).decode()
                    )
                    response = cast(ActionResponse, await action)
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

    async with timeout(3), bot:
        port = gateway.reverse_websocket_ports[0]
        async with (
            await open_onebot12(port) as websocket_a,
            await open_onebot12(port) as websocket_b,
        ):
            for websocket, self_ in ((websocket_a, self_a), (websocket_b, self_b)):
                await websocket.send(dumpb(connect_payload()).decode())
                await websocket.send(dumpb(status_payload(self_)).decode())
            await both_bound.wait()

            async with TaskGroup() as tasks:
                action = tasks.create_task(
                    gateway.connection_for(self_b).action("get_version")
                )
                request_frame = await websocket_b.recv()
                assert isinstance(request_frame, str)
                request = cast(dict[str, JsonValue], loads(request_frame))
                response = {
                    "status": "ok",
                    "retcode": 0,
                    "data": None,
                    "message": "",
                    "echo": request["echo"],
                }
                await websocket_a.send(dumpb(response).decode())
                marker = {**status_payload(self_a), "id": "evt-after-wrong-source"}
                await websocket_a.send(dumpb(marker).decode())
                await wrong_source_processed.wait()
                assert not action.done()

                await websocket_b.send(dumpb(response).decode())
                result = cast(ActionResponse, await action)

    assert result.data is None


async def test_pending_action_fails_when_session_disconnects() -> None:
    bot = Bot()
    gateway = reverse_gateway(bot, action=True)
    status_seen = AsyncEvent()

    @bot.on_event(block=True)
    def observe(event: Injected[Event]) -> None:
        if isinstance(event, StatusUpdateMetaEvent):
            status_seen.set()

    async with timeout(3), bot:
        port = gateway.reverse_websocket_ports[0]
        async with await open_onebot12(port) as websocket:
            await websocket.send(dumpb(connect_payload()).decode())
            await websocket.send(dumpb(status_payload(SELF)).decode())
            await status_seen.wait()

            async with TaskGroup() as tasks:

                async def expect_disconnect() -> None:
                    with pytest.raises(ConnectionError, match="connection closed"):
                        await gateway.connection_for(SELF).action("get_version")

                action = tasks.create_task(expect_disconnect())
                await websocket.recv()
                await websocket.close()
                await action


async def test_websocket_closes_when_global_event_queue_is_full() -> None:
    bot = Bot()
    gateway = OneBot12Gateway(bot)
    websocket = ScriptedWebSocket(connect_payload(), private_message_payload())

    with patch.object(gateway, "enqueue_event", side_effect=[None, QueueFull]):
        async with timeout(1), bot:
            with pytest.raises(ConnectionError, match="event queue is full"):
                await gateway._serve_websocket(websocket)  # ruff: ignore[private-member-access]

    assert websocket.closed.is_set()


async def test_forward_websocket_reconnects_and_keeps_received_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG", logger="bot")
    marker = "sensitive-inbound-value"
    first = ScriptedWebSocket({"secret": marker})
    second = ScriptedWebSocket(
        connect_payload(),
        private_message_payload(),
    )
    connector = AsyncMock(side_effect=[ConnectionError(marker), first, second])

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
        websocket_connector=connector,
    )
    bot.add_gateway(gateway)
    received = AsyncEvent()

    @bot.on_msg(block=True)
    def collect() -> None:
        received.set()

    async with timeout(1), bot:
        await received.wait()
        assert first.closed.is_set()

    assert second.closed.is_set()
    assert marker not in caplog.text
    assert (
        connector.await_args_list
        == [
            call(
                "ws://onebot.example/ws",
                {"Authorization": "Bearer test-value"},
            )
        ]
        * 3
    )


async def test_websocket_event_waits_for_bot_startup() -> None:
    bot = ObservableReadinessBot()
    gateway = OneBot12Gateway(bot)
    bot.add_gateway(gateway)
    received = AsyncEvent()
    websocket = ScriptedWebSocket(
        connect_payload(),
        private_message_payload(),
        StopAsyncIteration(),
    )

    @bot.on_msg(block=True)
    def collect() -> None:
        received.set()

    async with timeout(1), TaskGroup() as tasks:
        serving = tasks.create_task(
            gateway._serve_websocket(websocket)  # ruff: ignore[private-member-access]
        )
        await bot.waiting.wait()
        assert not serving.done()
        assert not received.is_set()
        await bot.start()
        try:
            await received.wait()
            await serving
        finally:
            await bot.close()

    assert websocket.closed.is_set()


async def test_lifecycle_restarts_real_forward_transport() -> None:
    websockets = [
        ScriptedWebSocket(connect_payload()),
        ScriptedWebSocket(connect_payload()),
    ]
    connector = AsyncMock(side_effect=websockets)
    bot = Bot()
    gateway = OneBot12Gateway(
        bot,
        ingress=[ForwardWebSocket("ws://onebot.example/ws", reconnect_interval=60)],
        websocket_connector=connector,
    )
    bot.add_gateway(gateway)

    async with timeout(1):
        for index, websocket in enumerate(websockets, start=1):
            async with bot:
                await websocket.receiving.wait()
                assert connector.await_count == index
            assert websocket.closed.is_set()
            assert not gateway._started  # ruff: ignore[private-member-access]
            assert not gateway._forward_tasks  # ruff: ignore[private-member-access]


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
    "path",
    [
        pytest.param("relative", id="relative"),
        pytest.param("//authority/path", id="authority-form"),
        pytest.param("/path?query", id="query"),
        pytest.param("/path#fragment", id="fragment"),
        pytest.param("/path\n", id="trailing-newline"),
        pytest.param("/has space", id="space"),
        pytest.param("/路径", id="non-ascii"),
    ],
)
def test_ingress_path_must_be_origin_form(path: str) -> None:
    with pytest.raises(ValueError, match="path"):
        HttpWebhook(path=path)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("http://onebot.example/ws", id="http"),
        pytest.param("/onebot/ws", id="relative"),
        pytest.param("ws://", id="missing-host"),
        pytest.param("ws://user:secret@onebot.example/ws", id="credentials"),
        pytest.param("ws://onebot.example/has space", id="space"),
    ],
)
def test_forward_websocket_requires_ws_url(url: str) -> None:
    with pytest.raises(ValueError, match="URL"):
        ForwardWebSocket(url)


@pytest.mark.parametrize(
    "host",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace"),
        pytest.param(" localhost", id="untrimmed"),
    ],
)
def test_reverse_websocket_requires_nonempty_host(host: str) -> None:
    with pytest.raises(ValueError, match="host"):
        ReverseWebSocket(host=host)
