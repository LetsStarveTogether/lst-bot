from asyncio import Event, QueueFull, TaskGroup, gather, timeout
from http import HTTPStatus
from math import inf, nan
from types import SimpleNamespace
from typing import Any, cast, override
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from bot import (
    ActionResponse,
    Bot,
    BotSelf,
    Connection,
    Gateway,
    Injected,
    PrivateMessageEvent,
)
from bot.gateways.onebot11 import (
    ForwardWebSocket,
    HttpWebhook,
    OneBot11Gateway,
    ReverseWebSocket,
    WebSocketAction,
)
from bot.json import dumpb, loads
from bot.testing import ScriptedWebSocket
from websockets.asyncio.client import connect
from websockets.asyncio.server import Server
from websockets.exceptions import InvalidStatus
from websockets.typing import Origin

from .support import private_msg_payload


def test_forward_websocket_validates_role_endpoint_and_identity() -> None:
    self_ = BotSelf(platform="qq", user_id="10000")

    with pytest.raises(ValueError, match="role"):
        ForwardWebSocket(
            "ws://onebot.example/event",
            role=cast(Any, "invalid"),
        )
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


def test_gateway_preserves_falsey_websocket_connector() -> None:
    connector = AsyncMock()
    connector.__bool__.return_value = False
    gateway = OneBot11Gateway(Bot(), websocket_connector=connector)
    assert gateway._websocket_connector is connector  # ruff: ignore[private-member-access]


@pytest.mark.parametrize(
    "url",
    [
        "",
        "//onebot.example/event",
        "http://onebot.example/event",
        "ws://one bot.example/event",
        "ws://onebot.example/event//",
        "ws://onebot.example/event#fragment",
        "ws://onebot.example:invalid/event",
    ],
)
def test_forward_websocket_rejects_invalid_url(url: str) -> None:
    with pytest.raises(ValueError, match=r"URL|endpoint"):
        ForwardWebSocket(url, role="event")


@pytest.mark.parametrize(
    "interval",
    [True, "1", 0, -1, nan, inf],
    ids=["boolean", "string", "zero", "negative", "nan", "infinity"],
)
def test_forward_websocket_rejects_invalid_reconnect_interval(
    interval: Any,
) -> None:
    with pytest.raises(ValueError, match="reconnect_interval"):
        ForwardWebSocket(
            "ws://onebot.example/event",
            role="event",
            reconnect_interval=interval,
        )


@pytest.mark.parametrize(
    "port",
    [True, 1.5, -1, 65536],
    ids=["boolean", "float", "negative", "too-large"],
)
def test_reverse_websocket_rejects_invalid_port(port: Any) -> None:
    with pytest.raises(ValueError, match="port"):
        ReverseWebSocket(port=port)


@pytest.mark.parametrize(
    "path",
    [
        "onebot",
        "//host/path",
        "/path?query=1",
        "/path#fragment",
        "/path\n",
        "/two words",
    ],
)
def test_ingress_rejects_non_path_targets(path: str) -> None:
    with pytest.raises(ValueError, match="path"):
        HttpWebhook(path=path)


def test_http_webhook_repr_hides_secret() -> None:
    credential = "secret"

    assert credential not in repr(HttpWebhook(secret=credential))


def test_reverse_websocket_reports_every_actual_port() -> None:
    gateway = OneBot11Gateway(Bot())
    sockets = (
        SimpleNamespace(getsockname=lambda port=port: ("localhost", port))
        for port in (10001, 10002, 10001)
    )
    gateway._reverse_servers.append(  # ruff: ignore[private-member-access]
        cast(Server, SimpleNamespace(sockets=list(sockets)))
    )

    assert gateway.reverse_websocket_ports == (10001, 10002)


def test_universal_websocket_identifies_events_before_extension_fields() -> None:
    gateway = OneBot11Gateway(Bot())
    payload = {
        **private_msg_payload(),
        "status": "ok",
        "retcode": 0,
    }

    with patch.object(gateway, "enqueue_event") as enqueue:
        self_ = gateway._queue_ws_payload(  # ruff: ignore[private-member-access]
            payload,
            "universal",
            None,
            None,
        )

    assert self_ == BotSelf(platform="qq", user_id="10000")
    assert enqueue.call_args.args[0].model_extra["status"] == "ok"


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


@pytest.mark.parametrize(
    ("path", "headers", "origin", "status"),
    [
        pytest.param(
            "/onebot/ws",
            {
                "Authorization": "Bearer wrong",
                "X-Self-ID": "10000",
                "X-Client-Role": "Universal",
            },
            None,
            HTTPStatus.UNAUTHORIZED,
            id="access-token",
        ),
        pytest.param(
            "/onebot/ws",
            {"Authorization": "Bearer secret", "X-Self-ID": "10000"},
            None,
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
            None,
            HTTPStatus.BAD_REQUEST,
            id="invalid-role",
        ),
        pytest.param(
            "/onebot/ws",
            {
                "Authorization": "Bearer secret",
                "X-Client-Role": "Universal",
            },
            None,
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
            None,
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
            None,
            HTTPStatus.NOT_FOUND,
            id="path",
        ),
        pytest.param(
            "/onebot/ws",
            {
                "Authorization": "Bearer secret",
                "X-Self-ID": "10000",
                "X-Client-Role": "Universal",
            },
            Origin("https://evil.example"),
            HTTPStatus.FORBIDDEN,
            id="browser-origin",
        ),
    ],
)
async def test_reverse_websocket_handshake_boundaries(
    path: str,
    headers: dict[str, str],
    origin: Origin | None,
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
                origin=origin,
                proxy=None,
            ):
                pass

    assert exc_info.value.response.status_code == status


async def test_reverse_websocket_close_cancels_handler_waiting_for_bot() -> None:
    bot = Bot()
    gateway = OneBot11Gateway(
        bot,
        ingress=[ReverseWebSocket(port=0, path="/onebot/ws")],
    )
    waiting = Event()
    wait_until_running = bot.wait_until_running

    async def mark_waiting() -> None:
        waiting.set()
        await wait_until_running()

    await gateway.start()
    websocket = None
    try:
        with patch.object(bot, "wait_until_running", side_effect=mark_waiting):
            port = gateway.reverse_websocket_ports[0]
            async with timeout(1):
                websocket = await connect(
                    f"ws://127.0.0.1:{port}/onebot/ws",
                    additional_headers={
                        "X-Self-ID": "10000",
                        "X-Client-Role": "Event",
                    },
                    proxy=None,
                )
                await waiting.wait()
                await gateway.close()
                await websocket.wait_closed()
    finally:
        if websocket is not None:
            await websocket.close()
        await gateway.close()


async def test_reverse_websocket_dispatches_and_matches_action_response() -> None:
    bot = Bot()
    completed = Event()
    responses: list[ActionResponse] = []
    gateway = OneBot11Gateway(
        bot,
        ingress=[ReverseWebSocket(port=0, path="/onebot/ws")],
        action=WebSocketAction(timeout=1),
    )
    bot.add_gateway(gateway)

    @bot.on_msg(block=True)
    async def collect(connection: Injected[Connection]) -> None:
        response = await connection.action("get_user_info", user_id="42")
        assert isinstance(response, ActionResponse)
        responses.append(response)
        completed.set()

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
            await websocket.send(dumpb(private_msg_payload()).decode())
            raw_request = await websocket.recv()
            assert isinstance(raw_request, str)
            request = loads(raw_request)
            await websocket.send(
                dumpb({
                    "status": "ok",
                    "retcode": 0,
                    "data": {"user_id": 42, "nickname": "tester"},
                    "echo": request["echo"],
                }).decode()
            )
            await completed.wait()

    assert str(UUID(request["echo"])) == request["echo"]
    assert request["action"] == "get_stranger_info"
    assert request["params"] == {"user_id": 42}
    assert responses[0].data == {
        "user_id": "42",
        "user_name": "tester",
        "user_displayname": "",
        "user_remark": "",
    }


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


async def test_forward_websocket_lifecycle_restarts_real_connections() -> None:
    websockets = [ScriptedWebSocket(), ScriptedWebSocket()]
    connector = AsyncMock(side_effect=websockets)
    bot = Bot()
    gateway = OneBot11Gateway(
        bot,
        ingress=[
            ForwardWebSocket(
                "ws://onebot.example/event",
                role="event",
                reconnect_interval=60,
            )
        ],
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


async def test_disconnect_cancels_pending_action() -> None:
    bot = Bot()
    self_ = BotSelf(platform="qq", user_id="10000")
    websocket = ScriptedWebSocket()
    gateway = OneBot11Gateway(
        bot,
        ingress=[
            ForwardWebSocket(
                "ws://onebot.example/api",
                role="api",
                self_=self_,
                reconnect_interval=60,
            )
        ],
        action=WebSocketAction(timeout=60),
        websocket_connector=AsyncMock(return_value=websocket),
    )
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

    class BlockingGateway(Gateway):
        @override
        async def start(self) -> None:
            startup_paused.set()
            await continue_startup.wait()

    bot.add_gateway(BlockingGateway(bot))

    @bot.on_msg(block=True)
    def collect() -> None:
        received.set()

    async with timeout(1), TaskGroup() as tasks:
        started = tasks.create_task(bot.start())
        await startup_paused.wait()
        await connected.wait()
        assert not received.is_set()
        continue_startup.set()
        await started
        await received.wait()
        await bot.close()

    assert websocket.closed.is_set()


async def test_websocket_closes_when_bot_start_fails() -> None:
    bot = Bot()
    websocket = ScriptedWebSocket()
    gateway = OneBot11Gateway(
        bot,
        ingress=[
            ForwardWebSocket(
                "ws://onebot.example/event",
                role="event",
                reconnect_interval=60,
            )
        ],
        websocket_connector=AsyncMock(return_value=websocket),
    )

    with patch.object(
        bot,
        "wait_until_running",
        AsyncMock(side_effect=RuntimeError("startup failed")),
    ) as wait_until_running:
        await gateway.start()
        try:
            async with timeout(1):
                await websocket.closed.wait()
        finally:
            await gateway.close()

    wait_until_running.assert_awaited_once()


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
        request = loads(await websocket.sent.get())
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


async def test_forward_websocket_reconnects_after_connect_and_receive_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG", logger="bot")
    bot = Bot()
    received = Event()
    marker = f"sensitive-{id(bot)}"
    broken = ScriptedWebSocket(marker)
    working = ScriptedWebSocket(private_msg_payload())
    connector = AsyncMock(side_effect=[ConnectionError(marker), broken, working])

    @bot.on_msg(block=True)
    def collect() -> None:
        received.set()

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
        websocket_connector=connector,
    )
    bot.add_gateway(gateway)

    async with timeout(1), bot:
        await received.wait()
        await broken.closed.wait()

    assert connector.await_count == 3
    assert marker not in caplog.text


async def test_websocket_queue_overload_closes_connection() -> None:
    bot = Bot()
    websocket = ScriptedWebSocket(private_msg_payload())
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

    with patch.object(gateway, "enqueue_event", side_effect=QueueFull) as enqueue:
        async with timeout(1), bot:
            await websocket.closed.wait()

    enqueue.assert_called_once()
