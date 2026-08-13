from __future__ import annotations

from asyncio import CancelledError, create_task, gather
from contextlib import suppress
from types import SimpleNamespace

import pytest
from bot import ActionResponse, Bot, BotSelf, Gateway
from bot.gateways.base import (
    WebSocketActionManager,
    WebsocketsConnection,
    bearer_or_query_token,
    header_value,
    request_target_path,
    token_matches,
)
from bot.testing import ScriptedWebSocket
from robyn import Headers
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from websockets.typing import Data


class NativeWebSocket:
    def __init__(self, *payloads: str | bytes | BaseException) -> None:
        self.payloads = list(payloads)
        self.sent: list[str] = []

    async def recv(self) -> Data:
        payload = self.payloads.pop(0)
        if isinstance(payload, BaseException):
            raise payload
        return payload

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        pass


class MultiValueFields:
    def __init__(self, **values: list[str]) -> None:
        self.values = values

    def get_all(self, name: str) -> list[str]:
        return self.values.get(name, [])


def test_authorization_header_does_not_fall_back_to_query_token() -> None:
    source = SimpleNamespace(
        headers={"Authorization": "Basic invalid"},
        query_params={"access_token": "query-token"},
    )

    assert bearer_or_query_token(source) is None


def test_websocket_request_reads_query_token_from_target() -> None:
    source = SimpleNamespace(headers={}, path="/onebot/ws?access_token=query-token")

    assert bearer_or_query_token(source) == "query-token"


@pytest.mark.parametrize(
    "target",
    [
        pytest.param("//*[?access_token=query-token", id="malformed"),
        pytest.param("//evil/x?access_token=query-token", id="network-path"),
        pytest.param("/x#fragment?access_token=query-token", id="fragment"),
    ],
)
def test_invalid_request_target_is_rejected_without_raising(target: str) -> None:
    source = SimpleNamespace(headers={}, path=target)

    assert request_target_path(source) is None
    assert bearer_or_query_token(source) is None


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            SimpleNamespace(
                headers=MultiValueFields(
                    Authorization=["Bearer first", "Bearer second"]
                ),
                query_params={"access_token": "fallback"},
            ),
            id="authorization-header",
        ),
        pytest.param(
            SimpleNamespace(
                headers={},
                query_params=MultiValueFields(access_token=["first", "second"]),
            ),
            id="query-fields",
        ),
        pytest.param(
            SimpleNamespace(
                headers={},
                path="/onebot/ws?access_token=first&access_token=second",
            ),
            id="request-target",
        ),
    ],
)
def test_bearer_or_query_token_rejects_repeated_credentials(
    source: object,
) -> None:
    assert bearer_or_query_token(source) is None


def test_header_value_rejects_repeated_headers() -> None:
    headers = Headers({})
    headers.append("X-Self-ID", "first")
    headers.append("X-Self-ID", "second")

    assert header_value(headers, "X-Self-ID") is None


def test_token_matches_supports_unicode_credentials() -> None:
    assert token_matches("密钥", "密钥") is True
    assert token_matches("密钥", "别的") is False


def test_gateway_does_not_cache_connections_from_untrusted_ids() -> None:
    gateway = Gateway(Bot())
    self_ = BotSelf(platform="test", user_id="bot")

    first = gateway.connection_for(self_)
    second = gateway.connection_for(self_)

    assert first is not second
    assert first.gateway is second.gateway is gateway
    assert first.self_ == second.self_ == self_


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="infinity"),
    ],
)
def test_websocket_action_manager_rejects_invalid_timeout(value: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        WebSocketActionManager(value)


async def test_websockets_connection_requires_text_frames() -> None:
    native = NativeWebSocket("text", b"binary")
    connection = WebsocketsConnection(native)

    await connection.send_text("sent")

    assert native.sent == ["sent"]
    assert await connection.receive_text() == "text"
    with pytest.raises(TypeError, match="text frame"):
        await connection.receive_text()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(
            ConnectionClosedOK(None, None),
            StopAsyncIteration,
            id="clean-close",
        ),
        pytest.param(
            ConnectionClosedError(None, None),
            ConnectionError,
            id="error-close",
        ),
    ],
)
async def test_websockets_connection_normalizes_close(
    error: BaseException,
    expected: type[BaseException],
) -> None:
    connection = WebsocketsConnection(NativeWebSocket(error))

    with pytest.raises(expected):
        await connection.receive_text()


async def test_disconnect_fails_action_and_manager_recovers() -> None:
    manager = WebSocketActionManager(timeout=1)
    self_ = BotSelf(platform="test", user_id="bot")
    old_websocket = ScriptedWebSocket()
    old_session = manager.register(old_websocket)
    manager.bind_self(old_session, self_)

    async def disconnect() -> None:
        await old_websocket.sent.get()
        manager.unregister(old_session)

    with pytest.raises(ConnectionError):
        await gather(manager.request(self_, lambda echo: echo), disconnect())

    websocket = ScriptedWebSocket()
    session = manager.register(websocket)
    manager.bind_self(session, self_)

    async def respond() -> None:
        echo = await websocket.sent.get()
        assert manager.receive(session, ActionResponse.ok({"ok": True}, echo=echo))

    response, _ = await gather(manager.request(self_, lambda echo: echo), respond())

    assert response.data == {"ok": True}


async def test_websocket_action_manager_requires_one_bound_session() -> None:
    manager = WebSocketActionManager(timeout=1)
    self_ = BotSelf(platform="test", user_id="bot")
    first = ScriptedWebSocket()
    second = ScriptedWebSocket()
    first_session = manager.register(first)
    second_session = manager.register(second)

    with pytest.raises(LookupError):
        await manager.request(self_, lambda echo: echo)

    manager.bind_self(first_session, self_)
    manager.bind_self(second_session, self_)

    with pytest.raises(LookupError):
        await manager.request(self_, lambda echo: echo)

    assert first.sent.empty()
    assert second.sent.empty()


async def test_websocket_action_response_must_come_from_request_session() -> None:
    manager = WebSocketActionManager(timeout=1)
    self_a = BotSelf(platform="test", user_id="a")
    self_b = BotSelf(platform="test", user_id="b")
    websocket_a = ScriptedWebSocket()
    session_a = manager.register(websocket_a)
    session_b = manager.register(ScriptedWebSocket())
    manager.bind_self(session_a, self_a)
    manager.bind_self(session_b, self_b)

    async def respond() -> None:
        echo = await websocket_a.sent.get()
        response = ActionResponse.ok(echo=echo)
        assert manager.receive(session_b, response) is False
        assert manager.receive(session_a, response) is True
        assert manager.receive(session_a, response) is False

    await gather(manager.request(self_a, lambda echo: echo), respond())


async def test_websocket_action_timeout_covers_send_and_recovers() -> None:
    manager = WebSocketActionManager(timeout=0.01)
    self_ = BotSelf(platform="test", user_id="bot")
    websocket = ScriptedWebSocket()
    websocket.send_allowed.clear()
    session = manager.register(websocket)
    manager.bind_self(session, self_)

    with pytest.raises(TimeoutError):
        await manager.request(self_, lambda echo: echo)

    websocket.send_allowed.set()

    async def respond() -> None:
        echo = await websocket.sent.get()
        assert manager.receive(session, ActionResponse.ok(echo=echo))

    await gather(manager.request(self_, lambda echo: echo), respond())


async def test_cancelled_websocket_action_rejects_late_response() -> None:
    manager = WebSocketActionManager(timeout=1)
    self_ = BotSelf(platform="test", user_id="bot")
    websocket = ScriptedWebSocket()
    session = manager.register(websocket)
    manager.bind_self(session, self_)
    task = create_task(manager.request(self_, lambda echo: echo))
    try:
        echo = await websocket.sent.get()
        task.cancel()
        with pytest.raises(CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
        with suppress(CancelledError):
            await task

    assert manager.receive(session, ActionResponse.ok(echo=echo)) is False


async def test_fail_all_rejects_pending_and_future_requests() -> None:
    manager = WebSocketActionManager(timeout=1)
    self_ = BotSelf(platform="test", user_id="bot")
    websocket = ScriptedWebSocket()
    session = manager.register(websocket)
    manager.bind_self(session, self_)

    async def fail() -> None:
        await websocket.sent.get()
        manager.fail_all()

    with pytest.raises(ConnectionError):
        await gather(manager.request(self_, lambda echo: echo), fail())

    with pytest.raises(LookupError):
        await manager.request(self_, lambda echo: echo)
