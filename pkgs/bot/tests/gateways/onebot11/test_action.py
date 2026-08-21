from asyncio import get_running_loop, timeout
from http import HTTPStatus
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

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
    ReturnAction,
)
from bot.gateways.onebot11 import (
    HttpAction,
    OneBot11Gateway,
    adapt_action_response,
    decode_action_response,
)
from pydantic import JsonValue, RootModel
from urllib3_future import AsyncPoolManager

from .support import ActionServer, action_response_payload


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
                user_id="42",
                message=[
                    {"type": "text", "data": {"text": "hello"}},
                    {"type": "mention_all", "data": {}},
                ],
            )

    assert isinstance(response, ActionResponse)
    assert response.data == {"message_id": "99"}
    assert len(server.requests) == 1
    request = server.requests[0]
    assert request.path == "/base/send_private_msg?trace=1"
    assert request.headers["Authorization"] == "Bearer token-1"
    assert request.json == {
        "user_id": 42,
        "message": [
            {"type": "text", "data": {"text": "hello"}},
            {"type": "at", "data": {"qq": "all"}},
        ],
    }


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


async def test_http_action_checks_status_before_decoding_body() -> None:
    async with ActionServer("not-json", status=HTTPStatus.UNAUTHORIZED) as server:
        gateway = OneBot11Gateway(Bot(), action=HttpAction(server.base_url))
        connection = gateway.connection_for(BotSelf(platform="qq", user_id="10000"))
        async with gateway:
            with pytest.raises(RuntimeError, match="HTTP 401"):
                await connection.action("get_status")


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


async def test_gateway_does_not_close_borrowed_http_pool() -> None:
    async with (
        ActionServer(action_response_payload({})) as server,
        AsyncPoolManager() as pool,
    ):
        gateway = OneBot11Gateway(
            Bot(),
            action=HttpAction(server.base_url, http_pool=pool),
        )

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

    assert success == ActionResponse.ok({"message_id": 1})
    assert failed.status == ApiStatus.FAILED
    assert failed.retcode == 1404
    assert failed.message == "missing"


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
            {"status": "async", "retcode": 1, "data": None},
            id="unrepresentable-async",
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
    ],
)
def test_onebot11_action_response_rejects_invalid_status_retcode_pairs(
    payload: dict[str, JsonValue],
) -> None:
    with pytest.raises(ValueError, match=r"status|retcode|represented"):
        decode_action_response(payload)


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
            await connection.execute_return_action(
                event,
                ReturnAction.message(
                    Msg.reply(
                        "msg-1",
                        Msg.mention("42", " hello"),
                        user_id="42",
                    )
                ),
            )

    assert server.requests[0].path == "/send_group_msg"
    assert server.requests[0].json == {
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
                guild_id="g",
                channel_id="c",
                message="hello",
            )
