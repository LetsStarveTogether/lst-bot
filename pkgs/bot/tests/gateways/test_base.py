from asyncio import (
    CancelledError,
    Event,
    create_task,
    gather,
    get_running_loop,
    sleep,
    timeout,
)
from contextlib import suppress
from dataclasses import FrozenInstanceError
from gc import collect
from gzip import compress
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from bot import ActionResponse, Bot, BotSelf, Gateway
from bot._tasks import (  # ruff: ignore[import-private-name] - cleanup regression
    await_cleanup,
)
from bot.gateways import base as base_module
from bot.gateways.base import (
    HttpAction,
    WebSocketAction,
    WebSocketActionManager,
    WebSocketClosedError,
    WebsocketsConnection,
    access_token_value,
    bearer_or_query_token,
    connect_websocket,
    header_value,
    read_http_body,
    request_target_path,
    run_while_open,
    token_matches,
    validate_https_base_url,
)
from bot.json import dumpb, loads
from bot.testing import ScriptedWebSocket
from robyn import Headers
from urllib3_future import AsyncHTTPResponse
from urllib3_future.exceptions import HTTPError
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from websockets.frames import Close


class MultiValueFields:
    def __init__(self, **values: list[str]) -> None:
        self.values = values

    def get_all(self, name: str) -> list[str]:
        return self.values.get(name, [])


def test_https_base_url_is_strict_and_canonical() -> None:
    assert (
        validate_https_base_url("https://api.example/path/", "Test")
        == "https://api.example/path"
    )
    for invalid in (
        "http://api.example",
        " https://api.example",
        "https://user:secret@api.example",
        "https://api.example?query=1",
        "https://api.example#fragment",
        "https://:",
    ):
        with pytest.raises(ValueError, match="absolute HTTPS URL"):
            validate_https_base_url(invalid, "Test")


def test_http_action_base_url_is_strict_and_canonical() -> None:
    assert (
        HttpAction("http://onebot.example/action?source=test").base_url
        == "http://onebot.example/action?source=test"
    )
    for invalid in (
        "",
        "/action",
        "ftp://onebot.example/action",
        " http://onebot.example/action",
        "http://onebot.example/a b",
        "http://user:secret@onebot.example/action",
        "http://onebot.example/action#fragment",
        "http://",
    ):
        with pytest.raises(ValueError, match=r"absolute HTTP\(S\) URL"):
            HttpAction(invalid)


def test_json_codec_is_compact_utf8_and_strict() -> None:
    payload = {"文本": ["é", 1.5]}
    encoded = dumpb(payload)

    assert encoded == '{"文本":["é",1.5]}'.encode()
    assert loads(encoded) == loads(encoded.decode()) == payload
    for invalid in (b"NaN", b'{"v":1e400}', b'"\\ud800"', b"\xef\xbb\xbf{}", b"\xff"):
        with pytest.raises(ValueError, match=r"."):
            loads(invalid)
    for invalid in (float("nan"), "\ud800"):
        with pytest.raises(ValueError, match=r"."):
            dumpb(invalid)


def test_authorization_header_does_not_fall_back_to_query_token() -> None:
    source = SimpleNamespace(
        headers={"Authorization": "Basic invalid"},
        query_params={"access_token": "query-token"},
    )

    assert bearer_or_query_token(source) is None


@pytest.mark.parametrize(
    ("authorization", "token"),
    [("bearer token", "token"), ("BEARER token", "token"), ("Bearer  token", " token")],
)
def test_bearer_scheme_is_case_insensitive_and_preserves_token_whitespace(
    authorization: str,
    token: str,
) -> None:
    source = SimpleNamespace(headers={"Authorization": authorization})

    assert bearer_or_query_token(source) == token


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


@pytest.mark.parametrize("size", [4, 5])
async def test_http_body_limit_applies_after_decompression(size: int) -> None:
    encoded = compress(b"x" * size)
    response = AsyncHTTPResponse(
        body=BytesIO(encoded),
        headers={
            "Content-Encoding": "gzip",
            "Content-Length": str(len(encoded)),
        },
        preload_content=False,
    )

    with patch.object(response, "read", wraps=response.read) as read:
        if size == 4:
            assert await read_http_body(response, max_bytes=4) == b"xxxx"
        else:
            with pytest.raises(HTTPError, match="4-byte limit"):
                await read_http_body(response, max_bytes=4)
        read.assert_awaited_once_with(5, decode_content=True)
    assert response.closed


def test_token_matches_supports_unicode_credentials() -> None:
    assert token_matches("密钥", "密钥") is True
    assert token_matches("密钥", "别的") is False


@pytest.mark.parametrize("value", [1, True, object()])
def test_access_token_rejects_non_strings(value: object) -> None:
    with pytest.raises(TypeError, match="must be a string"):
        access_token_value(value)  # ty: ignore[invalid-argument-type]


async def test_scripted_websocket_clears_receiving_after_cancellation() -> None:
    websocket = ScriptedWebSocket()
    receive = create_task(websocket.receive_text())
    await websocket.receiving.wait()

    receive.cancel()
    with pytest.raises(CancelledError):
        await receive

    assert not websocket.receiving.is_set()


async def test_scripted_websocket_rejects_send_closed_while_blocked() -> None:
    websocket = ScriptedWebSocket()
    websocket.send_allowed.clear()
    sending = create_task(websocket.send_text("payload"))
    await websocket.close()
    websocket.send_allowed.set()

    with pytest.raises(ConnectionError, match="closed"):
        await sending


async def test_await_cleanup_finishes_after_repeated_cancellation() -> None:
    release = Event()

    async def cleanup() -> None:
        await release.wait()

    cleanup_task = create_task(cleanup())
    closing = create_task(await_cleanup(cleanup_task))
    await sleep(0)
    closing.cancel()
    await sleep(0)
    closing.cancel()
    release.set()

    with pytest.raises(CancelledError):
        await closing
    assert cleanup_task.result() is None


async def test_run_while_open_preserves_cleanup_on_repeated_cancel() -> None:
    started = Event()
    cleaning = Event()
    release = Event()
    cleaned = Event()

    async def operation() -> None:
        started.set()
        try:
            await Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()

    running = create_task(run_while_open(operation(), Event(), lambda _: None))
    await started.wait()
    running.cancel()
    await cleaning.wait()
    running.cancel()
    release.set()

    with pytest.raises(CancelledError):
        await running
    assert cleaned.is_set()


async def test_await_cleanup_preserves_cancellation_and_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = Event()
    loop_errors: list[dict[str, object]] = []
    monkeypatch.setattr(
        get_running_loop(), "call_exception_handler", loop_errors.append
    )

    async def cleanup() -> None:
        await release.wait()
        msg = "cleanup failed"
        raise RuntimeError(msg)

    cleanup_task = create_task(cleanup())
    closing = create_task(await_cleanup(cleanup_task))
    await sleep(0)
    closing.cancel()
    await sleep(0)
    release.set()

    with pytest.raises(BaseExceptionGroup) as error:
        await closing
    await sleep(0)
    assert [type(exc) for exc in error.value.exceptions] == [
        CancelledError,
        RuntimeError,
    ]
    assert loop_errors == []


async def test_await_cleanup_remains_cancelled_when_cleanup_is_cancelled() -> None:
    release = Event()

    async def cleanup() -> None:
        await release.wait()
        raise CancelledError

    cleanup_task = create_task(cleanup())
    closing = create_task(await_cleanup(cleanup_task))
    await sleep(0)
    closing.cancel()
    await sleep(0)
    release.set()

    with pytest.raises(CancelledError) as error:
        await closing
    assert closing.cancelled()
    assert cleanup_task.cancelled()
    assert isinstance(error.value.__cause__, CancelledError)


def test_gateway_does_not_cache_connections_from_untrusted_ids() -> None:
    gateway = Gateway(Bot())
    self_ = BotSelf(platform="test", user_id="bot")

    first = gateway.connection_for(self_)
    second = gateway.connection_for(self_)

    assert first is not second
    assert first.gateway is second.gateway is gateway
    assert first.self_ == second.self_ == self_
    with pytest.raises(FrozenInstanceError):
        first.self_ = BotSelf(platform="test", user_id="other")  # ty: ignore[invalid-assignment]


@pytest.mark.parametrize(
    ("action_type", "args"),
    [
        pytest.param(WebSocketAction, (), id="websocket"),
        pytest.param(HttpAction, ("https://onebot.example",), id="http"),
    ],
)
@pytest.mark.parametrize(
    "timeout",
    [
        pytest.param(True, id="boolean"),
        pytest.param("30", id="string"),
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="infinity"),
    ],
)
def test_action_rejects_invalid_timeout(
    action_type: type[HttpAction | WebSocketAction],
    args: tuple[str, ...],
    timeout: object,
) -> None:
    with pytest.raises(ValueError, match="Input should be"):
        action_type(*args, timeout=timeout)  # ty: ignore[invalid-argument-type]


async def test_websockets_connection_requires_text_frames() -> None:
    native = AsyncMock()
    native.recv.side_effect = ["text", b"binary"]
    connection = WebsocketsConnection(native)

    await connection.send_text("sent")
    await connection.close(4000)

    native.send.assert_awaited_once_with("sent")
    native.close.assert_awaited_once_with(code=4000)
    assert await connection.receive_text() == "text"
    with pytest.raises(TypeError, match="text frame"):
        await connection.receive_text()


async def test_cancelled_websockets_send_closes_transport_despite_close_error() -> None:
    started = Event()

    async def send(_payload: str) -> None:
        started.set()
        await Event().wait()

    native = AsyncMock()
    native.send.side_effect = send
    native.close.side_effect = RuntimeError("close failed")
    sending = create_task(WebsocketsConnection(native).send_text("payload"))
    await started.wait()
    sending.cancel()

    with pytest.raises(CancelledError):
        await sending
    native.close.assert_awaited_once_with()


async def test_connect_websocket_passes_explicit_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = AsyncMock()
    mocked_connect = AsyncMock(return_value=native)
    monkeypatch.setattr(base_module, "connect", mocked_connect)

    connection = await connect_websocket(
        "wss://gateway.example",
        None,
        proxy="http://proxy.example",
    )

    assert connection.websocket is native
    mocked_connect.assert_awaited_once_with(
        "wss://gateway.example",
        additional_headers=None,
        proxy="http://proxy.example",
        max_size=2**20,
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(
            ConnectionClosedOK(Close(1000, "done"), None),
            StopAsyncIteration,
            id="clean-close",
        ),
        pytest.param(
            ConnectionClosedError(Close(4008, "rate limited"), None),
            WebSocketClosedError,
            id="error-close",
        ),
    ],
)
async def test_websockets_connection_normalizes_close(
    error: BaseException,
    expected: type[BaseException],
) -> None:
    native = AsyncMock()
    native.recv.side_effect = error
    connection = WebsocketsConnection(native)

    with pytest.raises(expected) as received:
        await connection.receive_text()
    assert received.value.__cause__ is error
    if isinstance(received.value, WebSocketClosedError):
        assert received.value.code == 4008

    native.send.side_effect = error
    with pytest.raises(WebSocketClosedError) as sent:
        await connection.send_text("payload")
    assert sent.value.__cause__ is error
    assert sent.value.code == (
        4008 if isinstance(error, ConnectionClosedError) else 1000
    )


async def test_disconnect_fails_action_and_manager_recovers() -> None:
    manager = WebSocketActionManager(timeout=1)
    self_ = BotSelf(platform="test", user_id="bot")
    old_websocket = ScriptedWebSocket()
    old_session = manager.register(old_websocket)
    manager.bind_self(old_session, self_)

    async def disconnect() -> None:
        await old_websocket.sent.get()
        manager.unregister(old_session)

    async with timeout(2):
        with pytest.raises(ConnectionError):
            await gather(manager.request(self_, lambda echo: echo), disconnect())

    websocket = ScriptedWebSocket()
    session = manager.register(websocket)
    manager.bind_self(session, self_)

    async def respond() -> None:
        echo = await websocket.sent.get()
        assert manager.receive(session, ActionResponse.ok({"ok": True}, echo=echo))

    async with timeout(2):
        response, _ = await gather(manager.request(self_, lambda echo: echo), respond())

    assert response.data == {"ok": True}


async def test_websocket_action_manager_prefers_latest_bound_session() -> None:
    manager = WebSocketActionManager(timeout=1)
    self_ = BotSelf(platform="test", user_id="bot")
    request_self = BotSelf.model_validate({
        "platform": "test",
        "user_id": "bot",
        "vendor_session": "new",
    })
    first = ScriptedWebSocket()
    second = ScriptedWebSocket()
    first_session = manager.register(first)
    second_session = manager.register(second)

    with pytest.raises(LookupError):
        await manager.request(request_self, lambda echo: echo)

    manager.bind_self(first_session, self_)
    manager.bind_self(second_session, self_)

    async def respond_latest() -> None:
        echo = await second.sent.get()
        assert manager.receive(second_session, ActionResponse.ok(echo=echo))

    async with timeout(2):
        await gather(
            manager.request(request_self, lambda echo: echo),
            respond_latest(),
        )
    assert first.sent.empty()

    manager.unregister(second_session)

    async def respond_fallback() -> None:
        echo = await first.sent.get()
        assert manager.receive(first_session, ActionResponse.ok(echo=echo))

    async with timeout(2):
        await gather(manager.request(self_, lambda echo: echo), respond_fallback())


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

    async with timeout(2):
        await gather(manager.request(self_a, lambda echo: echo), respond())


async def test_websocket_action_timeout_covers_send_and_recovers() -> None:
    manager = WebSocketActionManager(timeout=0.01)
    self_ = BotSelf(platform="test", user_id="bot")
    websocket = ScriptedWebSocket()
    websocket.send_allowed.clear()
    session = manager.register(websocket)
    manager.bind_self(session, self_)

    async with timeout(1):
        with pytest.raises(TimeoutError):
            await manager.request(self_, lambda echo: echo)

    websocket.send_allowed.set()

    async def respond() -> None:
        echo = await websocket.sent.get()
        assert manager.receive(session, ActionResponse.ok(echo=echo))

    async with timeout(2):
        await gather(manager.request(self_, lambda echo: echo), respond())


async def test_send_failure_consumes_concurrent_disconnect_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = WebSocketActionManager(timeout=1)
    self_ = BotSelf(platform="test", user_id="bot")
    websocket = ScriptedWebSocket()
    session = manager.register(websocket)
    manager.bind_self(session, self_)
    contexts: list[dict[str, object]] = []

    def fail_send(_payload: str) -> None:
        manager.unregister(session)
        raise BrokenPipeError

    monkeypatch.setattr(websocket, "send_text", AsyncMock(side_effect=fail_send))
    loop = get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: contexts.append(context))
    try:
        with pytest.raises(BrokenPipeError):
            await manager.request(self_, lambda echo: echo)
        collect()
    finally:
        loop.set_exception_handler(previous_handler)

    assert not any(
        context.get("message") == "Future exception was never retrieved"
        for context in contexts
    )


async def test_cancelled_websocket_action_rejects_late_response() -> None:
    manager = WebSocketActionManager(timeout=1)
    self_ = BotSelf(platform="test", user_id="bot")
    websocket = ScriptedWebSocket()
    session = manager.register(websocket)
    manager.bind_self(session, self_)
    task = create_task(manager.request(self_, lambda echo: echo))
    try:
        async with timeout(1):
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

    async with timeout(2):
        with pytest.raises(ConnectionError):
            await gather(manager.request(self_, lambda echo: echo), fail())

    with pytest.raises(LookupError):
        await manager.request(self_, lambda echo: echo)
