from asyncio import Event, create_task, get_running_loop, timeout
from http import HTTPStatus
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest
from bot import (
    Action,
    ActionResponse,
    ApiStatus,
    Bot,
    BotSelf,
    EventPayload,
    GroupMessageEvent,
    Msg,
    MsgSegmentInput,
    ReturnAction,
)
from bot.gateways import onebot11 as onebot11_module
from bot.gateways.onebot11 import (
    HttpAction,
    OneBot11ActionRequest,
    OneBot11Gateway,
    OneBot11MessageSegment,
    ReverseWebSocket,
    adapt_action_response,
    decode_action_response,
)
from bot.protocol.actions import ActionParamModel
from pydantic import JsonValue, RootModel, ValidationError
from urllib3_future import AsyncPoolManager
from websockets.asyncio.server import Server

from tests.gateways.support import ActionServer

from .support import action_response_payload


def test_action_request_validates_mapping_params() -> None:
    request = OneBot11ActionRequest.model_validate({
        "action": "vendor_action",
        "params": {"answer": 42},
    })

    assert request.model_dump(mode="json") == {
        "action": "vendor_action",
        "params": {"answer": 42},
    }


async def test_http_action_uses_real_transport_and_onebot11_wire_shape() -> None:
    credential = "token-1"
    async with ActionServer(action_response_payload({"message_id": 99})) as server:
        gateway = OneBot11Gateway(
            Bot(),
            action=HttpAction(f"{server.base_url}/base?trace=1"),
            access_token=credential,
        )
        connection = gateway.connection_for(BotSelf(platform="qq", user_id="10000"))
        async with gateway:
            response = await connection.action(
                "send_message",
                detail_type="private",
                user_id="42",
                message=[
                    {"type": "text", "data": {"text": "hello"}},
                    {"type": "mention_all", "data": {}},
                ],
            )

    assert isinstance(response, ActionResponse)
    assert response.data == {"message_id": "99"}
    (request,) = server.requests
    assert request.path == "/base/send_private_msg?trace=1"
    assert request.headers["Authorization"] == "Bearer token-1"
    assert request.json == {
        "user_id": 42,
        "message": [
            {"type": "text", "data": {"text": "hello"}},
            {"type": "at", "data": {"qq": "all"}},
        ],
    }


async def test_action_rejects_a_connection_from_another_gateway() -> None:
    gateway = OneBot11Gateway(Bot())
    foreign = OneBot11Gateway(Bot()).connection_for(
        BotSelf(platform="qq", user_id="10000")
    )

    with pytest.raises(ValueError, match="another gateway"):
        await gateway.request_action(foreign, "get_status", ActionParamModel())


async def test_raw_actions_preserve_name_null_and_message_array() -> None:
    message: list[JsonValue] = [
        {"type": "image", "data": {"file": "1.jpg"}},
        {"type": "location", "data": {"lat": "1", "lon": "2"}},
        {"type": "reply", "data": {"id": "3"}},
    ]
    async with ActionServer(action_response_payload({})) as server:
        gateway = OneBot11Gateway(Bot(), action=HttpAction(server.base_url))
        connection = gateway.connection_for(BotSelf(platform="qq", user_id="10000"))
        async with gateway:
            await connection.action("vendor/action", optional=None)
            await connection.action(
                "send_group_msg",
                group_id="42",
                message=message,
            )

    assert [(request.path, request.json) for request in server.requests] == [
        ("/vendor%2Faction", {"optional": None}),
        ("/send_group_msg", {"group_id": 42, "message": message}),
    ]


def test_message_media_uses_onebot11_wire_types() -> None:
    for segment_type, wire_type in (
        ("image", "image"),
        ("video", "video"),
        ("voice", "record"),
        ("audio", "record"),
    ):
        message: list[MsgSegmentInput] = [
            {
                "type": segment_type,
                "data": {"file_id": "resource", "vendor.flag": "kept"},
            }
        ]
        converted = onebot11_module._dump_ob11_message(message)  # ruff: ignore[private-member-access]
        assert converted.model_dump(mode="json") == [
            {
                "type": wire_type,
                "data": {"file": "resource", "vendor.flag": "kept"},
            }
        ]

    with pytest.raises(TypeError, match="does not define a file"):
        onebot11_module._dump_ob11_message(  # ruff: ignore[private-member-access]
            [{"type": "file", "data": {"file_id": "resource"}}]
        )


def test_message_segment_requires_data_and_preserves_null() -> None:
    segment = OneBot11MessageSegment.model_validate({
        "type": "vendor",
        "data": None,
    })

    assert segment.model_dump(mode="json") == {"type": "vendor", "data": None}
    with pytest.raises(ValidationError):
        OneBot11MessageSegment.model_validate({"type": "vendor"})


@pytest.mark.parametrize(
    "status",
    [HTTPStatus.UNAUTHORIZED, HTTPStatus.CREATED, HTTPStatus.NO_CONTENT],
)
async def test_http_action_checks_status_before_decoding_body(
    status: HTTPStatus,
) -> None:
    async with ActionServer("not-json", status=status) as server:
        gateway = OneBot11Gateway(Bot(), action=HttpAction(server.base_url))
        connection = gateway.connection_for(BotSelf(platform="qq", user_id="10000"))
        async with gateway:
            with pytest.raises(RuntimeError, match=f"HTTP {status}"):
                await connection.action("get_status")


async def test_start_and_cleanup_failures_close_owned_http_pool() -> None:
    gateway = OneBot11Gateway(
        Bot(),
        ingress=[ReverseWebSocket(port=0)],
        action=HttpAction("http://onebot.example"),
    )
    pool = gateway.http_pool
    assert pool is not None
    clear = AsyncMock()
    cleanup = AsyncMock(side_effect=RuntimeError("cleanup failed"))
    with (
        patch.object(pool, "clear", clear),
        patch.object(
            gateway,
            "_start_reverse_websocket",
            AsyncMock(side_effect=RuntimeError("start failed")),
        ),
        patch.object(gateway, "_close_transports", cleanup),
        pytest.raises(BaseExceptionGroup, match="startup and cleanup") as error,
    ):
        await gateway.start()

    assert [str(exc) for exc in error.value.exceptions] == [
        "start failed",
        "cleanup failed",
    ]
    clear.assert_awaited_once()
    assert gateway.http_pool is None
    assert gateway._started is False  # ruff: ignore[private-member-access]


async def test_restart_finishes_cleanup_before_opening_transports() -> None:
    gateway = OneBot11Gateway(
        Bot(),
        ingress=[ReverseWebSocket(port=0)],
        action=HttpAction("http://onebot.example"),
    )
    start_reverse_websocket = gateway._start_reverse_websocket  # ruff: ignore[private-member-access]
    cleanup = AsyncMock(side_effect=[RuntimeError("cleanup failed"), None])

    async def observe_restart(ingress: ReverseWebSocket) -> Server:
        assert cleanup.await_count == 2
        assert gateway.http_pool is not None
        return await start_reverse_websocket(ingress)

    with (
        patch.object(gateway, "_close_transports", cleanup),
        patch.object(gateway, "_start_reverse_websocket", observe_restart),
    ):
        with pytest.raises(RuntimeError, match="cleanup failed"):
            await gateway.close()
        await gateway.start()

    assert gateway.reverse_websocket_ports
    await gateway.close()


async def test_http_action_timeout_covers_response_body() -> None:
    body = get_running_loop().create_future()
    pool = AsyncMock(spec=AsyncPoolManager)
    pool.request.return_value = SimpleNamespace(
        status=HTTPStatus.OK,
        data=body,
    )
    gateway = OneBot11Gateway(
        Bot(),
        action=HttpAction(
            "http://onebot.example",
            timeout=0.01,
            http_pool=cast(AsyncPoolManager, pool),
        ),
    )
    connection = gateway.connection_for(BotSelf(platform="qq", user_id="10000"))

    async with timeout(1):
        async with gateway:
            with pytest.raises(TimeoutError):
                await connection.action("vendor_action")

    assert body.cancelled()
    pool.request.assert_awaited_once()
    assert pool.request.await_args.kwargs["retries"] is False


async def test_closed_gateway_rejects_new_http_actions() -> None:
    gateway = OneBot11Gateway(
        Bot(),
        action=HttpAction("http://127.0.0.1:1"),
    )
    connection = gateway.connection_for(BotSelf(platform="qq", user_id="10000"))
    await gateway.close()

    with pytest.raises(RuntimeError, match="gateway is closed"):
        await connection.action("get_status")

    assert gateway.http_pool is None


async def test_http_action_cannot_cross_close_and_restart() -> None:
    class BlockingPool:
        def __init__(self) -> None:
            self.started = Event()
            self.release = Event()

        async def request(self, *_: object, **__: object) -> object:
            self.started.set()
            await self.release.wait()
            return SimpleNamespace(
                status=HTTPStatus.OK,
                data=AsyncMock(return_value=b'{"status":"ok","retcode":0,"data":{}}')(),
            )

    pool = BlockingPool()
    gateway = OneBot11Gateway(
        Bot(),
        action=HttpAction(
            "http://onebot.example",
            http_pool=cast(AsyncPoolManager, pool),
        ),
    )
    connection = gateway.connection_for(BotSelf(platform="qq", user_id="10000"))
    async with timeout(1):
        await gateway.start()
        action = create_task(connection.action("vendor_action"))
        try:
            await pool.started.wait()
            await gateway.close()
            await gateway.start()
            pool.release.set()
            with pytest.raises(RuntimeError, match="gateway is closed"):
                await action
        finally:
            pool.release.set()
            async with timeout(1):
                await gateway.close()


async def test_gateway_does_not_close_borrowed_http_pool() -> None:
    class FalseyPool(AsyncPoolManager):
        def __bool__(self) -> bool:
            return False

    async with (
        ActionServer(action_response_payload({})) as server,
        FalseyPool() as pool,
    ):
        gateway = OneBot11Gateway(
            Bot(),
            action=HttpAction(server.base_url, http_pool=pool),
        )
        assert gateway.http_pool is pool

        async with gateway:
            pass

        response = await pool.request("POST", server.base_url, json={})
        assert response.status == HTTPStatus.OK
        await response.data


@pytest.mark.parametrize(
    ("action", "data", "expected"),
    [
        pytest.param(
            Action.SEND_MESSAGE,
            {"message_id": 1},
            {"message_id": "1"},
            id="send-message",
        ),
        pytest.param(
            Action.SEND_MESSAGE,
            {"message_id": 1, "time": 1},
            {"message_id": "1", "time": 1.0},
            id="send-message-with-time",
        ),
        pytest.param(
            Action.GET_SELF_INFO,
            {"user_id": 1, "nickname": "bot"},
            {"user_id": "1", "user_name": "bot", "user_displayname": ""},
            id="self-info",
        ),
        pytest.param(
            Action.GET_USER_INFO,
            {"user_id": 2, "nickname": "user", "sex": "unknown", "age": 1},
            {
                "user_id": "2",
                "user_name": "user",
                "user_displayname": "",
                "user_remark": "",
            },
            id="user-info",
        ),
        pytest.param(
            Action.GET_FRIEND_LIST,
            [{"user_id": 2, "nickname": "user", "remark": "friend"}],
            [
                {
                    "user_id": "2",
                    "user_name": "user",
                    "user_displayname": "",
                    "user_remark": "friend",
                }
            ],
            id="friend-list",
        ),
        pytest.param(
            Action.GET_GROUP_INFO,
            {"group_id": 3, "group_name": "group", "member_count": 1},
            {"group_id": "3", "group_name": "group"},
            id="group-info",
        ),
        pytest.param(
            Action.GET_GROUP_LIST,
            [{"group_id": 3, "group_name": "group"}],
            [{"group_id": "3", "group_name": "group"}],
            id="group-list",
        ),
        pytest.param(
            Action.GET_GROUP_MEMBER_INFO,
            {"user_id": 2, "nickname": "user", "card": "card", "group_id": 3},
            {"user_id": "2", "user_name": "user", "user_displayname": "card"},
            id="group-member-info",
        ),
        pytest.param(
            Action.GET_GROUP_MEMBER_LIST,
            [{"user_id": 2, "nickname": "user", "card": ""}],
            [{"user_id": "2", "user_name": "user", "user_displayname": ""}],
            id="group-member-list",
        ),
        pytest.param(
            Action.GET_STATUS,
            {"good": True, "online": False, "vendor": 1},
            {
                "good": True,
                "bots": [
                    {
                        "self": {"platform": "qq", "user_id": "10000"},
                        "online": False,
                    }
                ],
            },
            id="status",
        ),
        pytest.param(
            Action.GET_VERSION,
            {
                "app_name": "ob11",
                "app_version": "1.0",
                "protocol_version": "v11",
            },
            {"impl": "ob11", "version": "1.0", "onebot_version": "12"},
            id="version",
        ),
    ],
)
def test_internal_action_response_data_is_adapted(
    action: Action,
    data: JsonValue,
    expected: JsonValue,
) -> None:
    response = adapt_action_response(
        action,
        ActionResponse.ok(data),
        BotSelf(platform="qq", user_id="10000"),
    )

    assert response.data == expected


@pytest.mark.parametrize(
    "action",
    [Action.DELETE_MESSAGE, Action.SET_GROUP_NAME, Action.LEAVE_GROUP],
)
def test_null_action_response_data_is_validated(action: Action) -> None:
    self_ = BotSelf(platform="qq", user_id="10000")

    assert adapt_action_response(action, ActionResponse.ok(), self_).data is None
    with pytest.raises(TypeError, match="response data must be null"):
        adapt_action_response(action, ActionResponse.ok({}), self_)


def test_raw_and_failed_action_responses_are_not_adapted() -> None:
    self_ = BotSelf(platform="qq", user_id="10000")
    raw = ActionResponse.ok({"message_id": 1})
    failed = ActionResponse(
        status=ApiStatus.FAILED,
        retcode=1404,
        data={"message_id": 1},
        message="missing",
    )

    assert adapt_action_response("vendor_action", raw, self_) is raw
    assert adapt_action_response(Action.SEND_MESSAGE, failed, self_) is failed


def test_onebot11_action_response_status_matches_retcode() -> None:
    success = decode_action_response({
        "status": "ok",
        "retcode": 0,
        "data": {"message_id": 1},
    })
    failed = decode_action_response({
        "status": "failed",
        "retcode": 1404,
        "data": None,
        "message": "missing",
    })
    asynchronous = decode_action_response({
        "status": "async",
        "retcode": 1,
        "data": None,
    })

    assert success == ActionResponse.ok({"message_id": 1})
    assert failed.status == ApiStatus.FAILED
    assert failed.retcode == 1404
    assert failed.message == "missing"
    assert asynchronous.status == ApiStatus.ASYNC
    assert asynchronous.retcode == 1


def test_action_response_requires_an_object_model() -> None:
    with pytest.raises(TypeError, match="object"):
        decode_action_response(RootModel[list[int]]([1]))


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {"status": "ok", "retcode": 1, "data": None},
            id="ok-with-async-retcode",
        ),
        pytest.param(
            {"status": "async", "retcode": 0, "data": None},
            id="async-with-ok-retcode",
        ),
        pytest.param(
            {"status": "failed", "retcode": 0, "data": None},
            id="failed-with-ok-retcode",
        ),
        pytest.param(
            {"status": "failed", "retcode": 1, "data": None},
            id="failed-with-async-retcode",
        ),
        pytest.param(
            {"status": "vendor", "retcode": 0, "data": None},
            id="unknown-status",
        ),
        pytest.param({"status": "ok", "data": None}, id="missing-retcode"),
        pytest.param({"status": "ok", "retcode": 0}, id="missing-data"),
    ],
)
def test_onebot11_action_response_rejects_invalid_protocol_shape(
    payload: dict[str, JsonValue],
) -> None:
    with pytest.raises(ValueError, match=r"status|retcode|data|represented"):
        decode_action_response(payload)


def test_onebot11_action_response_rejects_non_string_echo() -> None:
    with pytest.raises(TypeError, match="echo"):
        decode_action_response({
            "status": "ok",
            "retcode": 0,
            "data": None,
            "echo": 1,
        })


async def test_message_return_uses_group_action() -> None:
    event = EventPayload.model_validate({
        "id": "evt-group",
        "self": {"platform": "qq", "user_id": "10000"},
        "time": 1,
        "type": "message",
        "detail_type": "group",
        "sub_type": "normal",
        "message_id": "13",
        "group_id": "20000",
        "user_id": "42",
        "message": [{"type": "text", "data": {"text": "hello"}}],
        "alt_message": "hello",
    }).root
    assert isinstance(event, GroupMessageEvent)
    assert event.self_ is not None

    async with ActionServer() as server:
        gateway = OneBot11Gateway(Bot(), action=HttpAction(server.base_url))
        connection = gateway.connection_for(event.self_)
        async with gateway:
            await gateway.execute_return_action(
                connection,
                event,
                ReturnAction.message(
                    Msg.reply(
                        "msg-1",
                        [
                            {"type": "mention", "data": {"user_id": "42"}},
                            {"type": "text", "data": {"text": " hello"}},
                        ],
                        user_id="42",
                    )
                ),
            )

    (request,) = server.requests
    assert request.path == "/send_group_msg"
    assert request.json == {
        "group_id": 20000,
        "message": [
            {"type": "reply", "data": {"id": "msg-1"}},
            {"type": "at", "data": {"qq": "42"}},
            {"type": "text", "data": {"text": " hello"}},
        ],
    }


async def test_onebot11_does_not_support_internal_channel_send() -> None:
    gateway = OneBot11Gateway(
        Bot(),
        action=HttpAction("http://127.0.0.1:1"),
    )
    connection = gateway.connection_for(BotSelf(platform="qq", user_id="10000"))

    async with gateway:
        with pytest.raises(LookupError):
            await connection.action(
                "send_message",
                detail_type="channel",
                guild_id="g",
                channel_id="c",
                message="hello",
            )
