from asyncio import CancelledError, Event, QueueFull, TaskGroup, create_task, timeout
from collections.abc import AsyncIterator, Mapping
from hashlib import sha256
from typing import cast

import pytest
from bot import Bot, BotSelf, Msg, Status
from bot.gateways import telegram as telegram_module
from bot.gateways import telegram_api as telegram_api_module
from bot.gateways.telegram import TelegramGateway
from bot.gateways.telegram_api import (
    TelegramAPIError,
    TelegramChat,
    TelegramDownloadedFile,
    TelegramEnvelope,
    TelegramFileTooLargeError,
    TelegramLocation,
    TelegramPollAnswer,
    TelegramResponseParameters,
    TelegramRestClient,
    TelegramResult,
    TelegramUpdate,
    TelegramUpload,
    TelegramUser,
    TelegramVenue,
)
from bot.json import dumpb
from bot.protocol.enums import Action
from bot.protocol.events import (
    GroupMessageEvent,
    GroupRequestEvent,
    MessageEvent,
    NoticeEvent,
)
from bot.protocol.returns import ReturnAction
from diwire import Injected
from pydantic import JsonValue, ValidationError
from urllib3_future import AsyncHTTPResponse, AsyncPoolManager

CREDENTIAL = "opaque-token"
SUPERGROUP_ID = -1_000_000_000_001


def response(payload: JsonValue, status: int = 200) -> AsyncHTTPResponse:
    return AsyncHTTPResponse(
        body=dumpb(payload),
        status=status,
        headers={"Content-Type": "application/json"},
    )


class Pool:
    def __init__(self, *payloads: JsonValue) -> None:
        self.responses: list[AsyncHTTPResponse] = [
            response(payload) for payload in payloads
        ]
        self.requests: list[tuple[str, str, dict[str, object]]] = []

    async def request(
        self,
        method: str,
        url: str,
        **kwargs: object,
    ) -> AsyncHTTPResponse:
        self.requests.append((method, url, kwargs))
        return self.responses.pop(0)


class StreamResponse:
    def __init__(
        self,
        *chunks: bytes,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers = dict(headers or {})
        self.chunks = chunks
        self.closed = False

    async def stream(
        self,
        _: int,
        *,
        decode_content: bool,
    ) -> AsyncIterator[bytes]:
        assert decode_content is False
        for chunk in self.chunks:
            yield chunk

    async def close(self) -> None:
        self.closed = True


def client(pool: object) -> TelegramRestClient:
    return TelegramRestClient(
        CREDENTIAL,
        base_url="https://telegram.example",
        http_pool=cast(AsyncPoolManager, pool),
    )


def make_gateway(pool: object | None = None) -> TelegramGateway:
    return TelegramGateway(
        Bot(),
        token=CREDENTIAL,
        base_url="https://telegram.example",
        http_pool=cast(AsyncPoolManager, pool if pool is not None else Pool()),
    )


def message_update(update_id: int, text: str = "hello") -> TelegramUpdate:
    return TelegramUpdate.model_validate({
        "update_id": update_id,
        "message": {
            "message_id": 0,
            "date": 1,
            "from": {"id": 42, "is_bot": False, "first_name": "User"},
            "chat": {
                "id": SUPERGROUP_ID,
                "type": "supergroup",
                "title": "Group",
            },
            "text": text,
        },
    })


async def ready() -> None:  # ruff: ignore[unused-async] - awaitable test double
    return None


def test_strict_models() -> None:
    payload = message_update(1).payload
    assert payload is not None
    assert payload[0] == "message"
    payload = TelegramUpdate.model_validate({
        "update_id": 2,
        "poll_answer": {
            "poll_id": "poll",
            "user": {"id": 42, "is_bot": False, "first_name": "User"},
            "option_ids": [0, 2],
            "option_persistent_ids": ["a", "c"],
        },
    }).payload
    assert payload is not None
    poll_answer = payload[1]
    assert isinstance(poll_answer, TelegramPollAnswer)
    assert poll_answer.option_ids == [0, 2]
    assert poll_answer.option_persistent_ids == ["a", "c"]
    with pytest.raises(ValidationError):
        TelegramUpdate.model_validate({
            "update_id": 5,
            "message": message_update(1).message,
            "poll": {},
        })
    with pytest.raises(ValidationError):
        TelegramLocation(latitude=91, longitude=0)
    with pytest.raises(ValidationError):
        TelegramLocation(latitude=0, longitude=0, heading=0)
    with pytest.raises(ValidationError):
        TelegramLocation(latitude=0, longitude=0, heading=361)
    with pytest.raises(ValidationError):
        TelegramLocation(
            latitude=0, longitude=0, live_period=60, proximity_alert_radius=0
        )
    with pytest.raises(ValidationError, match="live_period"):
        TelegramLocation(latitude=0, longitude=0, heading=1)
    with pytest.raises(ValidationError, match="cannot be live"):
        TelegramVenue.model_validate({
            "location": {"latitude": 0, "longitude": 0, "live_period": 60},
            "title": "Place",
            "address": "Address",
        })
    with pytest.raises(ValidationError):
        TelegramUser(id=-1, is_bot=False, first_name="User")
    for chat in (
        TelegramChat(id=0xFF_FFFF_FFFF, type="private"),
        TelegramChat(id=-999_999_999_999, type="group"),
        TelegramChat(id=SUPERGROUP_ID, type="supergroup"),
        TelegramChat(id=-4_000_000_000_000, type="channel"),
    ):
        assert chat.id
    for chat_id, chat_type in (
        (0, "private"),
        (0x1_00_0000_0000, "private"),
        (-1, "private"),
        (1, "group"),
        (-100, "supergroup"),
        (-2_000_000_000_000, "channel"),
    ):
        with pytest.raises(ValidationError):
            TelegramChat.model_validate({"id": chat_id, "type": chat_type})
    with pytest.raises(ValidationError, match="must be supergroups"):
        TelegramChat(id=1, type="private", is_direct_messages=True)
    assert TelegramUpdate(update_id=2**31 - 1).update_id == 2**31 - 1
    for update_id in (0, 2**31):
        with pytest.raises(ValidationError):
            TelegramUpdate(update_id=update_id)
    with pytest.raises(ValidationError):
        TelegramUpdate.model_validate({
            "update_id": 7,
            "poll_answer": {"poll_id": "poll", "option_ids": [0]},
        })
    with pytest.raises(ValidationError):
        TelegramEnvelope.model_validate({
            "ok": True,
            "result": {},
            "error_code": None,
        })
    with pytest.raises(ValidationError):
        TelegramEnvelope.model_validate({
            "ok": False,
            "result": None,
            "error_code": 400,
            "description": "bad",
        })
    assert telegram_module._payload_time({"date": 0}) > 0  # ruff: ignore[private-member-access]


@pytest.mark.parametrize(
    ("length", "methods"),
    [(1024, ["sendPhoto"]), (1025, ["sendMessage", "sendPhoto"])],
)
def test_media_caption_limit(length: int, methods: list[str]) -> None:
    text = "x" * length
    message = Msg.from_input([
        {"type": "text", "data": {"text": text}},
        {"type": "image", "data": {"file_id": "photo"}},
    ])
    calls = telegram_module._message_calls("42", message, {})  # ruff: ignore[private-member-access]
    assert [method for method, _ in calls] == methods
    assert calls[-1][1].get("caption") == (text if length == 1024 else None)


def test_message_text_limit() -> None:
    message = Msg.from_input("x" * 4096)
    assert telegram_module._message_calls("42", message, {})  # ruff: ignore[private-member-access]
    with pytest.raises(ValueError, match="4096"):
        telegram_module._message_calls(  # ruff: ignore[private-member-access]
            "42", Msg.from_input("x" * 4097), {}
        )
    assert telegram_module._message_calls(  # ruff: ignore[private-member-access]
        "42",
        Msg.from_input(f"<b>{'x' * 4094}</b>"),
        {"parse_mode": "HTML"},
    )


def test_unmapped_message_content_is_preserved() -> None:
    original = message_update(1).message
    assert original is not None
    raw = original.model_dump(exclude_none=True)
    raw.pop("text")
    raw["poll"] = {"id": "poll"}
    message = TelegramUpdate.model_validate({"update_id": 2, "message": raw}).message
    assert message is not None
    converted = telegram_module._telegram_message(message)  # ruff: ignore[private-member-access]
    assert converted[0].type == "telegram.message"
    assert converted[0].data.model_extra == {"raw": message.model_dump(mode="json")}


def test_location_and_venue_conversion() -> None:
    update = TelegramUpdate.model_validate({
        "update_id": 1,
        "message": {
            "message_id": 1,
            "date": 1,
            "from": {"id": 42, "is_bot": False, "first_name": "User"},
            "chat": {"id": 42, "type": "private", "first_name": "User"},
            "venue": {
                "location": {"latitude": 1.25, "longitude": 2.5},
                "title": "Place",
                "address": "Address",
            },
        },
    })
    assert update.message is not None
    venue = telegram_module._telegram_message(update.message)  # ruff: ignore[private-member-access]
    assert venue.model_dump(mode="json") == [
        {
            "type": "location",
            "data": {
                "latitude": 1.25,
                "longitude": 2.5,
                "title": "Place",
                "content": "Address",
            },
        }
    ]
    assert telegram_module._message_calls("42", venue, {}) == [  # ruff: ignore[private-member-access]
        (
            "sendVenue",
            {
                "chat_id": "42",
                "latitude": 1.25,
                "longitude": 2.5,
                "title": "Place",
                "address": "Address",
            },
        )
    ]

    location = Msg.from_input([
        {
            "type": "location",
            "data": {
                "latitude": 1.25,
                "longitude": 2.5,
                "title": "",
                "content": "",
            },
        }
    ])
    assert telegram_module._message_calls("42", location, {}) == [  # ruff: ignore[private-member-access]
        (
            "sendLocation",
            {"chat_id": "42", "latitude": 1.25, "longitude": 2.5},
        )
    ]


@pytest.mark.parametrize(
    "token",
    ["", "a b", "a/b", "a?b", "a%b", "a#b", "a\\b", "a\0b"],
)
def test_token_is_validated_without_leaking(token: str) -> None:
    with pytest.raises(ValueError, match="invalid Telegram bot token") as error:
        TelegramRestClient(token)
    if token:
        assert token not in str(error.value)


def test_secrets_are_not_represented() -> None:
    assert "token" not in repr(client(Pool()))
    secret_result = TelegramResult("managed-secret")
    assert "managed-secret" not in repr(secret_result)
    assert "managed-secret" not in str(secret_result)


async def test_rest_boundaries_and_get_updates_parameters() -> None:
    pool = Pool(
        {
            "ok": True,
            "result": [
                message_update(7).raw,
                {"update_id": 8},
                {"update_id": 9, "future_update": {"value": 1}},
            ],
        },
        {"ok": True, "result": []},
        {"ok": True, "result": []},
        {"ok": True, "result": []},
    )
    rest = client(pool)
    updates = await rest.get_updates(offset=7, poll_timeout=30)
    assert isinstance(updates[0], TelegramUpdate)
    assert updates[1].payload is None
    assert updates[2].payload == ("future_update", {"value": 1})
    params = cast(dict[str, object], pool.requests[0][2]["json"])
    assert (params["offset"], params["timeout"], params["limit"]) == (7, 30, 100)
    assert {
        "chat_member",
        "message_reaction",
        "message_reaction_count",
    } <= set(cast(list[str], params["allowed_updates"]))
    assert pool.requests[0][2]["retries"] is False
    assert cast(float, pool.requests[0][2]["timeout"]) > 30
    assert await rest.get_updates(offset=-1, poll_timeout=0) == []
    assert cast(dict[str, object], pool.requests[1][2]["json"])["offset"] == -1
    for offset in (-(2**31), 2**31 - 1):
        assert await rest.get_updates(offset=offset, poll_timeout=0) == []
        assert cast(dict[str, object], pool.requests[-1][2]["json"])["offset"] == offset
    for offset in (-(2**31) - 1, 2**31):
        with pytest.raises(ValidationError):
            await rest.get_updates(offset=offset, poll_timeout=0)

    with pytest.raises(ValueError, match="invalid Telegram method parameters"):
        await rest.call_json("getMe", cast(Mapping[str, object], []))
    with pytest.raises(ValueError, match="invalid Telegram method parameters"):
        await rest.call_json("getMe", {"bad": [float("inf")]})
    with pytest.raises(ValueError, match="invalid Telegram method parameters"):
        await rest.call_json("getMe", request_timeout=0)
    with pytest.raises(ValidationError):
        await rest.get_updates(offset=True, poll_timeout=30)
    with pytest.raises(ValidationError):
        TelegramRestClient(CREDENTIAL, request_timeout=float("nan"))

    invalid = client(
        Pool({
            "ok": True,
            "result": [{"update_id": 10, "poll": True}],
        })
    )
    with pytest.raises(ValidationError):
        await invalid.get_updates(offset=None, poll_timeout=0)

    class BrokenPool:
        async def request(self, *_: object, **__: object) -> AsyncHTTPResponse:
            msg = "local bug"
            raise ValueError(msg)

    with pytest.raises(ValueError, match="local bug"):
        await client(BrokenPool()).call_json("getMe")


async def test_multipart_preserves_file_metadata() -> None:
    pool = Pool({"ok": True, "result": True})
    await client(pool).call_json(
        "sendDocument",
        {"chat_id": 42},
        {
            "document": TelegramUpload(
                data=b"content",
                filename="report.txt",
                content_type="text/plain",
            )
        },
    )
    body = pool.requests[0][2]["body"]
    assert isinstance(body, bytes)
    assert pool.requests[0][2]["retries"] is False
    assert b'filename="report.txt"' in body
    assert b"Content-Type: text/plain" in body


async def test_file_download_is_streamed_bounded_and_token_safe() -> None:
    file = {"file_id": "file", "file_unique_id": "unique"}
    stream = StreamResponse(b"abc", b"def")
    pool = Pool({
        "ok": True,
        "result": file | {"file_path": "documents/a b.txt"},
    })
    pool.responses.append(cast(AsyncHTTPResponse, stream))
    gateway = make_gateway(pool)
    gateway._self = BotSelf(  # ruff: ignore[private-member-access] - isolate action routing
        platform="telegram", user_id="123"
    )
    connection = gateway.connection_for(gateway._self)  # ruff: ignore[private-member-access] - paired test self

    downloaded = await connection.action(
        Action.GET_FILE,
        file_id="file",
        type="data",
    )
    assert isinstance(downloaded, TelegramDownloadedFile)
    assert downloaded.data == b"abcdef"
    assert downloaded.model_dump(mode="json")["data"] == "YWJjZGVm"
    assert downloaded.sha256 == sha256(b"abcdef").hexdigest()
    assert stream.closed
    assert pool.requests[1] == (
        "GET",
        f"https://telegram.example/file/bot{CREDENTIAL}/documents/a%20b.txt",
        {
            "headers": {"Accept-Encoding": "identity"},
            "retries": False,
            "timeout": 30.0,
            "preload_content": False,
        },
    )
    with pytest.raises(TypeError, match="data result type"):
        await connection.action(Action.GET_FILE, file_id="file", type="url")

    absolute_pool = Pool({
        "ok": True,
        "result": file | {"file_path": "/srv/telegram/file"},
    })
    with pytest.raises(ValueError, match="relative path") as error:
        await client(absolute_pool).download_file("file")
    assert CREDENTIAL not in str(error.value)
    assert len(absolute_pool.requests) == 1

    metadata_pool = Pool({
        "ok": True,
        "result": file | {"file_path": "documents/file.bin", "file_size": 5},
    })
    with pytest.raises(TelegramFileTooLargeError):
        await client(metadata_pool).download_file("file", max_bytes=4)
    assert len(metadata_pool.requests) == 1

    oversized_stream = StreamResponse(b"123", b"45")
    oversized_pool = Pool({
        "ok": True,
        "result": file | {"file_path": "documents/file.bin"},
    })
    oversized_pool.responses.append(cast(AsyncHTTPResponse, oversized_stream))
    with pytest.raises(TelegramFileTooLargeError) as error:
        await client(oversized_pool).download_file("file", max_bytes=4)
    assert CREDENTIAL not in str(error.value)
    assert oversized_stream.closed


async def test_rate_limit_retry_and_error_parameters() -> None:
    limited = {
        "ok": False,
        "error_code": 400,
        "description": "retry later",
        "parameters": {"retry_after": 0},
    }
    pool = Pool()
    pool.responses = [response(limited, 429), response({"ok": True, "result": 1})]
    assert await client(pool).call_json("getMe") == 1
    assert len(pool.requests) == 2

    pool = Pool()
    pool.responses = [
        response(
            {**limited, "parameters": {"retry_after": 31}},
            429,
        )
    ]
    with pytest.raises(TelegramAPIError) as error:
        await client(pool).call_json("getMe")
    assert error.value.parameters is not None
    assert error.value.parameters.retry_after == 31


async def test_close_stops_rate_limit_retry() -> None:
    class SignallingPool(Pool):
        def __init__(self) -> None:
            super().__init__()
            self.requested = Event()

        async def request(
            self,
            method: str,
            url: str,
            **kwargs: object,
        ) -> AsyncHTTPResponse:
            result = await super().request(method, url, **kwargs)
            self.requested.set()
            return result

    pool = SignallingPool()
    pool.responses = [
        response(
            {
                "ok": False,
                "error_code": 429,
                "description": "retry later",
                "parameters": {"retry_after": 1},
            },
            429,
        ),
        response({"ok": True, "result": True}),
    ]
    rest = client(pool)

    async def request_once() -> None:
        with pytest.raises(RuntimeError, match="closed"):
            await rest.call_json("sendMessage", {"chat_id": 1, "text": "x"})

    async with timeout(1), TaskGroup() as tasks:
        tasks.create_task(request_once())
        await pool.requested.wait()
        await rest.close()
        await rest.start()
    assert len(pool.requests) == 1
    assert await rest.call_json("getMe") is True


async def test_close_cancels_in_flight_request_across_restart() -> None:
    class BlockingPool:
        def __init__(self) -> None:
            self.requested = Event()
            self.cancelled = Event()

        async def request(self, *_: object, **__: object) -> AsyncHTTPResponse:
            self.requested.set()
            try:
                await Event().wait()
            except CancelledError:
                self.cancelled.set()
                raise
            msg = "unreachable"
            raise AssertionError(msg)

    pool = BlockingPool()
    rest = client(pool)

    async def request_once() -> None:
        with pytest.raises(RuntimeError, match="closed"):
            await rest.call_json("getMe")

    async with timeout(1), TaskGroup() as tasks:
        tasks.create_task(request_once())
        await pool.requested.wait()
        await rest.close()
        await rest.start()
        await pool.cancelled.wait()


async def test_cancellation_is_not_wrapped() -> None:
    class CancellingPool:
        async def request(self, *_: object, **__: object) -> AsyncHTTPResponse:
            raise CancelledError

    with pytest.raises(CancelledError):
        await client(CancellingPool()).call_json("getMe")

    started = Event()

    class FailingCleanupPool:
        async def request(self, *_: object, **__: object) -> AsyncHTTPResponse:
            started.set()
            try:
                await Event().wait()
            except CancelledError:
                msg = "cleanup failed"
                raise RuntimeError(msg) from None
            msg = "unreachable"
            raise AssertionError(msg)

    async with timeout(1):
        task = create_task(client(FailingCleanupPool()).call_json("getMe"))
        await started.wait()
        task.cancel()
        with pytest.raises(CancelledError):
            await task


async def test_owned_pool_cleanup_can_be_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingClearPool(Pool):
        def __init__(self) -> None:
            super().__init__()
            self.clear_calls = 0

        async def clear(self) -> None:
            self.clear_calls += 1
            if self.clear_calls == 1:
                msg = "clear failed"
                raise RuntimeError(msg)

    pool = FailingClearPool()
    monkeypatch.setattr(telegram_api_module, "AsyncPoolManager", lambda: pool)
    rest = TelegramRestClient(CREDENTIAL)

    with pytest.raises(RuntimeError, match="clear failed"):
        await rest.close()
    await rest.close()

    assert pool.clear_calls == 2


async def test_invalid_server_error_is_retryable_transport_failure() -> None:
    invalid = AsyncHTTPResponse(body=b"bad gateway", status=502)
    pool = Pool()
    pool.responses = [invalid]
    with pytest.raises(ConnectionError, match="invalid response"):
        await client(pool).call_json("getMe")


async def test_gateway_start_actions_and_get_updates_exclusivity() -> None:
    pool = Pool(
        {
            "ok": True,
            "result": {
                "id": 123,
                "is_bot": True,
                "first_name": "Bot",
                "username": "example_bot",
            },
        },
        {
            "ok": True,
            "result": {
                "url": "",
                "has_custom_certificate": False,
                "pending_update_count": 0,
            },
        },
        {"ok": True, "result": {"message_id": 1}},
        {"ok": True, "result": {"message_id": 2}},
        {"ok": True, "result": {"message_id": 3}},
        {"ok": True, "result": []},
        {
            "ok": True,
            "result": {
                "status": "member",
                "user": {"id": 42, "is_bot": False, "first_name": "User"},
            },
        },
    )
    gateway = make_gateway(pool)
    await gateway.start()
    self_ = BotSelf(platform="telegram", user_id="123")
    connection = gateway.connection_for(self_)
    try:
        with pytest.raises(RuntimeError, match="reserved"):
            await gateway.call_json("getUpdates")
        with pytest.raises(RuntimeError, match="unavailable while polling"):
            await gateway.call_json("setWebhook", {"url": "https://example.test"})
        supported = await connection.action(Action.GET_SUPPORTED_ACTIONS)
        assert "getUpdates" not in supported.root  # ty: ignore[unresolved-attribute]
        assert "setWebhook" not in supported.root  # ty: ignore[unresolved-attribute]
        assert "getFile" in supported.root  # ty: ignore[unresolved-attribute]
        result = await connection.send_msg("hello", user_id="42")
        assert isinstance(result, TelegramResult)
        assert result.root == {"message_id": 1}
        assert pool.requests[-1][2]["json"] == {
            "chat_id": "42",
            "text": "hello",
        }
        await connection.send_msg(
            [
                {"type": "text", "data": {"text": "caption"}},
                {"type": "telegram.sticker", "data": {"file_id": "sticker"}},
            ],
            user_id="42",
        )
        assert [request[2]["json"] for request in pool.requests[-2:]] == [
            {"chat_id": "42", "text": "caption"},
            {"chat_id": "42", "sticker": "sticker"},
        ]
        native = await connection.action("getmycommands")
        assert isinstance(native, TelegramResult)
        assert native.root == []
        await connection.action(
            Action.GET_GROUP_MEMBER_INFO,
            group_id=str(SUPERGROUP_ID),
            user_id="42",
        )
        assert [
            (url.rsplit("/", 1)[-1], kwargs["json"])
            for _, url, kwargs in pool.requests[-2:]
        ] == [
            ("getMyCommands", {}),
            (
                "getChatMember",
                {"chat_id": str(SUPERGROUP_ID), "user_id": 42},
            ),
        ]
        with pytest.raises(LookupError, match="unknown bot self"):
            await gateway.connection_for(
                BotSelf(platform="telegram", user_id="other")
            ).send_msg("hello", user_id="42")
    finally:
        await gateway.close()


async def test_start_reserves_polling_before_identification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = make_gateway()
    identifying = Event()
    continue_identification = Event()

    async def identify() -> TelegramUser:
        identifying.set()
        await continue_identification.wait()
        return TelegramUser(id=123, is_bot=True, first_name="Bot")

    monkeypatch.setattr(gateway, "_identify", identify)
    startup = create_task(gateway.start())
    try:
        async with timeout(1):
            await identifying.wait()
            with pytest.raises(RuntimeError, match="unavailable while polling"):
                await gateway.call_json(
                    "setWebhook",
                    {"url": "https://example.test/hook"},
                )
            continue_identification.set()
            await startup
    finally:
        continue_identification.set()
        await gateway.close()


async def test_cancelling_one_start_waiter_keeps_shared_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = make_gateway()
    identifying = Event()
    continue_identification = Event()

    async def identify() -> TelegramUser:
        identifying.set()
        await continue_identification.wait()
        return TelegramUser(id=123, is_bot=True, first_name="Bot")

    monkeypatch.setattr(gateway, "_identify", identify)
    async with timeout(1), TaskGroup() as tasks:
        cancelled = tasks.create_task(gateway.start())
        surviving = tasks.create_task(gateway.start())
        await identifying.wait()
        cancelled.cancel()
        with pytest.raises(CancelledError):
            await cancelled
        continue_identification.set()
        await surviving
        assert gateway._task is not None  # ruff: ignore[private-member-access]
        await gateway.close()


async def test_cancelling_only_start_waiter_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = make_gateway()
    identifying = Event()

    async def identify() -> TelegramUser:
        identifying.set()
        await Event().wait()
        raise AssertionError

    monkeypatch.setattr(gateway, "_identify", identify)
    startup = create_task(gateway.start())
    async with timeout(1):
        await identifying.wait()
        startup.cancel()
        with pytest.raises(CancelledError):
            await startup

    assert gateway._closed  # ruff: ignore[private-member-access]
    assert gateway._startup_task is None  # ruff: ignore[private-member-access]
    assert not gateway._polling_reserved  # ruff: ignore[private-member-access]


async def test_real_bot_polling_lifecycle_dispatches_after_restart() -> None:
    class LifecyclePool:
        def __init__(self) -> None:
            self.generation = 0
            self.delivered: set[int] = set()
            self.requests: list[tuple[str, dict[str, object]]] = []

        async def request(
            self,
            _: str,
            url: str,
            **kwargs: object,
        ) -> AsyncHTTPResponse:
            method = url.rsplit("/", 1)[-1]
            params = cast(dict[str, object], kwargs["json"])
            self.requests.append((method, params))
            if method == "getMe":
                self.generation += 1
                return response({
                    "ok": True,
                    "result": {"id": 123, "is_bot": True, "first_name": "Bot"},
                })
            if method == "getWebhookInfo":
                return response({
                    "ok": True,
                    "result": {
                        "url": "",
                        "has_custom_certificate": False,
                        "pending_update_count": 0,
                    },
                })
            if method == "getUpdates" and self.generation not in self.delivered:
                self.delivered.add(self.generation)
                return response({
                    "ok": True,
                    "result": [message_update(self.generation).raw],
                })
            await Event().wait()
            msg = "unreachable"
            raise AssertionError(msg)

    bot = Bot()
    pool = LifecyclePool()
    gateway = TelegramGateway(
        bot,
        token=CREDENTIAL,
        base_url="https://telegram.example",
        http_pool=cast(AsyncPoolManager, pool),
        poll_timeout=0,
    )
    bot.add_gateway(gateway)
    received: list[int] = []
    dispatched = Event()

    @bot.on_msg()
    def record(event: Injected[GroupMessageEvent]) -> None:
        extra = event.model_extra or {}
        received.append(cast(int, extra["telegram_update_id"]))
        dispatched.set()

    async with timeout(2):
        try:
            await bot.start()
            await dispatched.wait()
            dispatched.clear()
            await gateway.close()
            assert gateway._closed  # ruff: ignore[private-member-access]
            assert gateway._task is None  # ruff: ignore[private-member-access]
            assert not gateway._polling_reserved  # ruff: ignore[private-member-access]
            await gateway.start()
            await dispatched.wait()
        finally:
            await bot.close()

    assert received == [1, 2]
    polls = [params for method, params in pool.requests if method == "getUpdates"]
    assert polls[0].get("offset") is None
    assert any(params.get("offset") == 2 for params in polls)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (
            "new_chat_members",
            [{"id": 43, "is_bot": False, "first_name": "New member"}],
        ),
        (
            "left_chat_member",
            {"id": 43, "is_bot": False, "first_name": "Former member"},
        ),
        ("new_chat_title", "Renamed"),
        ("community_chat_removed", {}),
    ],
)
def test_service_messages_are_not_empty_messages(
    field: str,
    value: JsonValue,
) -> None:
    gateway = make_gateway()
    gateway._self = BotSelf(  # ruff: ignore[private-member-access] - event conversion boundary
        platform="telegram", user_id="123"
    )
    event = gateway._event_from_update(  # ruff: ignore[private-member-access] - event conversion boundary
        TelegramUpdate.model_validate({
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 123,
                "from": {"id": 42, "is_bot": False, "first_name": "User"},
                "chat": {
                    "id": SUPERGROUP_ID,
                    "type": "supergroup",
                    "title": "Group",
                },
                field: value,
            },
        })
    )
    assert isinstance(event, NoticeEvent)
    assert event.detail_type == f"telegram.{field}"
    assert event.time == 123
    raw = (event.model_extra or {})["telegram_raw"]
    assert isinstance(raw, dict)
    assert raw["message"][field] == value


async def test_message_reply_contexts_use_their_official_routes() -> None:
    pool = Pool(
        *({"ok": True, "result": True} for _ in range(6)),
    )
    gateway = make_gateway(pool)
    self_ = BotSelf(platform="telegram", user_id="123")
    gateway._self = self_  # ruff: ignore[private-member-access] - event conversion boundary
    connection = gateway.connection_for(self_)

    business_event = gateway._event_from_update(  # ruff: ignore[private-member-access] - event conversion boundary
        TelegramUpdate.model_validate({
            "update_id": 1,
            "business_message": {
                "message_id": 10,
                "date": 1,
                "from": {"id": 42, "is_bot": False, "first_name": "User"},
                "chat": {"id": 99, "type": "private", "first_name": "User"},
                "business_connection_id": "business",
                "text": "incoming",
            },
        })
    )
    assert isinstance(business_event, MessageEvent)
    await connection.execute_message_action(business_event, "reply")
    assert pool.requests[-1][2]["json"] == {
        "chat_id": 99,
        "business_connection_id": "business",
        "text": "reply",
    }

    direct_event = gateway._event_from_update(  # ruff: ignore[private-member-access] - event conversion boundary
        TelegramUpdate.model_validate({
            "update_id": 2,
            "message": {
                "message_id": 11,
                "message_thread_id": 50,
                "direct_messages_topic": {"topic_id": 60},
                "date": 1,
                "from": {"id": 42, "is_bot": False, "first_name": "User"},
                "chat": {
                    "id": SUPERGROUP_ID,
                    "type": "supergroup",
                    "title": "Direct messages",
                    "is_direct_messages": True,
                },
                "text": "incoming",
            },
        })
    )
    assert isinstance(direct_event, GroupMessageEvent)
    await connection.execute_message_action(direct_event, "reply")
    direct_params = pool.requests[-1][2]["json"]
    assert direct_params == {
        "chat_id": SUPERGROUP_ID,
        "direct_messages_topic_id": 60,
        "text": "reply",
    }
    missing_topic = direct_event.model_copy(
        update={"telegram_direct_messages_topic_id": None}
    )
    with pytest.raises(ValueError, match="direct_messages_topic_id"):
        await connection.execute_message_action(missing_topic, "reply")

    ephemeral_event = gateway._event_from_update(  # ruff: ignore[private-member-access] - event conversion boundary
        TelegramUpdate.model_validate({
            "update_id": 3,
            "message": {
                "message_id": 0,
                "ephemeral_message_id": 70,
                "date": 1,
                "from": {"id": 42, "is_bot": False, "first_name": "User"},
                "receiver_user": {"id": 123, "is_bot": True, "first_name": "Bot"},
                "chat": {
                    "id": SUPERGROUP_ID,
                    "type": "supergroup",
                    "title": "Group",
                },
                "text": "incoming",
            },
        })
    )
    assert isinstance(ephemeral_event, MessageEvent)
    assert (ephemeral_event.model_extra or {})["telegram_receiver_user_id"] == 123
    await connection.execute_message_action(
        ephemeral_event,
        [
            {"type": "text", "data": {"text": "reply"}},
            {"type": "telegram.sticker", "data": {"file_id": "sticker"}},
        ],
    )
    for request in pool.requests[-2:]:
        params = cast(dict[str, object], request[2]["json"])
        assert params["chat_id"] == SUPERGROUP_ID
        assert params["receiver_user_id"] == 42
        assert params["reply_parameters"] == {"ephemeral_message_id": 70}

    guest_event = gateway._event_from_update(  # ruff: ignore[private-member-access] - event conversion boundary
        TelegramUpdate.model_validate({
            "update_id": 4,
            "guest_message": {
                "message_id": 0,
                "guest_query_id": "guest",
                "date": 1,
                "from": {"id": 42, "is_bot": False, "first_name": "User"},
                "chat": {
                    "id": SUPERGROUP_ID,
                    "type": "supergroup",
                    "title": "Group",
                },
                "text": "incoming",
            },
        })
    )
    assert isinstance(guest_event, MessageEvent)
    await connection.execute_message_action(guest_event, "reply")
    assert pool.requests[-1][2]["json"] == {
        "guest_query_id": "guest",
        "result": {
            "type": "article",
            "id": "reply",
            "title": "回复",
            "input_message_content": {"message_text": "reply"},
        },
    }
    with pytest.raises(TypeError, match="only text"):
        await connection.execute_message_action(
            guest_event,
            [{"type": "image", "data": {"file_id": "photo"}}],
        )

    await connection.action(
        Action.DELETE_MESSAGE,
        message_id="0",
        group_id=str(SUPERGROUP_ID),
        ephemeral_message_id=70,
        receiver_user_id=42,
    )
    assert pool.requests[-1][1].endswith("/deleteEphemeralMessage")
    assert pool.requests[-1][2]["json"] == {
        "chat_id": str(SUPERGROUP_ID),
        "receiver_user_id": 42,
        "ephemeral_message_id": 70,
    }
    with pytest.raises(ValueError, match="requires both"):
        await connection.action(
            Action.DELETE_MESSAGE,
            message_id="1",
            group_id=str(SUPERGROUP_ID),
            receiver_user_id=42,
        )
    with pytest.raises(ValueError, match="cannot be deleted"):
        await connection.action(
            Action.DELETE_MESSAGE,
            message_id="0",
            group_id=str(SUPERGROUP_ID),
        )


async def test_join_request_query_uses_query_response_endpoint() -> None:
    pool = Pool({"ok": True, "result": True})
    gateway = make_gateway(pool)
    self_ = BotSelf(platform="telegram", user_id="123")
    gateway._self = self_  # ruff: ignore[private-member-access] - event conversion boundary
    connection = gateway.connection_for(self_)
    event = gateway._event_from_update(  # ruff: ignore[private-member-access] - event conversion boundary
        TelegramUpdate.model_validate({
            "update_id": 5,
            "chat_join_request": {
                "chat": {
                    "id": SUPERGROUP_ID,
                    "type": "supergroup",
                    "title": "Group",
                },
                "from": {"id": 42, "is_bot": False, "first_name": "User"},
                "user_chat_id": 42,
                "date": 1,
                "query_id": "query",
            },
        })
    )
    assert isinstance(event, GroupRequestEvent)

    await gateway.execute_return_action(
        connection,
        event,
        ReturnAction.request(True),
    )
    assert pool.requests[-1][2]["json"] == {
        "chat_join_request_query_id": "query",
        "result": "approve",
    }


async def test_webhook_conflict_fails_before_polling() -> None:
    pool = Pool(
        {
            "ok": True,
            "result": {"id": 123, "is_bot": True, "first_name": "Bot"},
        },
        {
            "ok": True,
            "result": {
                "url": "https://secret.example/hook",
                "has_custom_certificate": False,
                "pending_update_count": 0,
            },
        },
    )
    gateway = make_gateway(pool)
    with pytest.raises(RuntimeError, match="webhook") as error:
        await gateway.start()
    assert "secret.example" not in str(error.value)
    assert gateway._closed  # ruff: ignore[private-member-access]
    assert not gateway._polling_reserved  # ruff: ignore[private-member-access]


async def test_poller_respects_flood_wait_and_stops_on_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = make_gateway()
    self_ = BotSelf(platform="telegram", user_id="123")
    gateway._self = self_  # ruff: ignore[private-member-access] - public status requires identified self
    connection = gateway.connection_for(self_)
    outcomes = iter((
        [],
        TelegramAPIError(
            429,
            429,
            "rate limited",
            TelegramResponseParameters(retry_after=45),
        ),
        [],
        TelegramAPIError(401, 401, "unauthorized"),
    ))
    delays: list[float] = []
    online_states: list[bool] = []

    async def online() -> bool:
        status = await connection.action(Action.GET_STATUS)
        assert isinstance(status, Status)
        return status.bots[0].online

    async def get_updates(
        *, offset: int | None, poll_timeout: int
    ) -> list[TelegramUpdate]:
        assert offset is None
        assert poll_timeout == 30
        outcome = next(outcomes)
        if isinstance(outcome, TelegramAPIError):
            online_states.append(await online())
            raise outcome
        return outcome

    async def record_sleep(delay: float) -> None:
        online_states.append(await online())
        delays.append(delay)

    monkeypatch.setattr(gateway.bot, "wait_until_running", ready)
    monkeypatch.setattr(gateway, "get_updates", get_updates)
    monkeypatch.setattr("bot.gateways.telegram.sleep", record_sleep)

    await gateway._run_poller()  # ruff: ignore[private-member-access] - isolated retry loop
    online_states.append(await online())
    assert delays == [45]
    assert online_states == [True, False, True, False]


async def test_poller_does_not_retry_invalid_update_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = make_gateway()

    async def invalid_updates(  # ruff: ignore[unused-async] - awaitable test double
        *, offset: int | None, poll_timeout: int
    ) -> list[TelegramUpdate]:
        _ = offset, poll_timeout
        return [TelegramUpdate.model_validate({"update_id": 1, "poll": True})]

    monkeypatch.setattr(gateway.bot, "wait_until_running", ready)
    monkeypatch.setattr(gateway, "get_updates", invalid_updates)

    async with timeout(1):
        with pytest.raises(ValidationError):
            await gateway._run_poller()  # ruff: ignore[private-member-access] - retry boundary


def test_offset_advances_only_after_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = make_gateway()
    gateway._self = BotSelf(  # ruff: ignore[private-member-access] - isolate the queue invariant
        platform="telegram", user_id="123"
    )
    events: list[GroupMessageEvent] = []

    def enqueue(event: object) -> None:
        if events:
            raise QueueFull
        assert isinstance(event, GroupMessageEvent)
        events.append(event)

    reply_update = message_update(10)
    assert reply_update.message is not None
    reply_update.message.reply_to_message = reply_update.message.model_copy(
        update={"message_id": 9, "text": "previous"}
    )
    monkeypatch.setattr(gateway, "enqueue_event", enqueue)
    with pytest.raises(QueueFull):
        gateway._accept_updates(  # ruff: ignore[private-member-access] - direct invariant check
            [reply_update, message_update(11)]
        )
    assert gateway._offset == 11  # ruff: ignore[private-member-access] - direct invariant check
    assert events[0].group_id == str(SUPERGROUP_ID)
    extra = events[0].model_extra or {}
    assert extra["reply_alt_message"] == "previous"
    raw = extra["telegram_raw"]
    assert isinstance(raw, dict)
    assert raw["message"]["text"] == "hello"

    gateway._offset = None  # ruff: ignore[private-member-access] - reset isolated invariant
    notices: list[object] = []
    monkeypatch.setattr(gateway, "enqueue_event", notices.append)
    gateway._accept_updates([  # ruff: ignore[private-member-access] - unknown update boundary
        TelegramUpdate.model_validate({"update_id": 12})
    ])
    assert isinstance(notices[0], NoticeEvent)
    assert notices[0].detail_type == "telegram.raw_update"
    assert gateway._offset == 13  # ruff: ignore[private-member-access] - zero payload was consumed
