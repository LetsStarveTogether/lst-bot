from __future__ import annotations

from typing import cast

import pytest
from pydantic import JsonValue, ValidationError
from urllib3_future import AsyncPoolManager

from lst_bot import (
    ActionParamInput,
    ActionResponse,
    ApiStatus,
    Bot,
    BotSelf,
    EventPayload,
    GroupMessageEvent,
    Msg,
    ReturnAction,
)
from lst_bot.gateways.onebot12 import HttpAction, OneBot12Gateway
from tests.protocol.support import private_msg_payload

from .support import CaptureOneBot12Gateway, FakePool


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        (
            {"detail_type": "private", "user_id": "42", "message": "hello"},
            {
                "detail_type": "private",
                "user_id": "42",
                "message": [{"type": "text", "data": {"text": "hello"}}],
            },
        ),
        (
            {
                "group_id": "20000",
                "message": {"type": "mention_all", "data": {}},
            },
            {
                "detail_type": "group",
                "group_id": "20000",
                "message": [{"type": "mention_all", "data": {}}],
            },
        ),
        (
            {
                "guild_id": "30000",
                "channel_id": "40000",
                "message": Msg.t("hi"),
            },
            {
                "detail_type": "channel",
                "guild_id": "30000",
                "channel_id": "40000",
                "message": [{"type": "text", "data": {"text": "hi"}}],
            },
        ),
        (
            {
                "user_id": "42",
                "message": Msg.reply("msg-1", Msg.mention("42", " hello")),
            },
            {
                "detail_type": "private",
                "user_id": "42",
                "message": [
                    {
                        "type": "reply",
                        "data": {"message_id": "msg-1"},
                    },
                    {"type": "mention", "data": {"user_id": "42"}},
                    {"type": "text", "data": {"text": " hello"}},
                ],
            },
        ),
    ],
)
async def test_action_params_follow_protocol_msg_rules(
    params: dict[str, str | Msg | dict[str, JsonValue]],
    expected: dict[str, JsonValue],
) -> None:
    gateway = CaptureOneBot12Gateway(Bot())
    connection = gateway.connection_for(BotSelf(platform="qq", user_id="10000"))

    response = cast(ActionResponse, await connection.action("send_message", **params))

    assert response.data == {"message_id": "out-1", "time": 1.0}
    assert gateway.calls == [
        ("send_message", expected, BotSelf(platform="qq", user_id="10000")),
    ]


@pytest.mark.parametrize(
    "params",
    [
        {"detail_type": "private", "message": "hello"},
        {
            "detail_type": "private",
            "user_id": "42",
            "message": "hello",
            "session_id": "internal",
        },
        {"detail_type": "group", "user_id": "42", "message": "hello"},
        {"detail_type": "channel", "guild_id": "30000", "message": "hello"},
        {"detail_type": "private", "user_id": "42"},
        {"detail_type": "private", "user_id": 42, "message": "hello"},
        {"detail_type": "private", "user_id": "42", "message": {"type": "text"}},
        {
            "detail_type": "private",
            "user_id": "42",
            "message": {"type": "text", "data": {}},
        },
    ],
)
async def test_send_msg_rejects_bad_standard_params(
    params: dict[str, ActionParamInput],
) -> None:
    gateway = CaptureOneBot12Gateway(Bot())
    connection = gateway.connection_for(BotSelf(platform="qq", user_id="10000"))

    with pytest.raises((TypeError, ValueError, ValidationError)):
        await connection.action("send_message", **params)


async def test_http_action_uses_underlying_async_pool() -> None:
    credential = "token-1"
    pool = FakePool()
    gateway = OneBot12Gateway(
        Bot(),
        action=HttpAction(
            "https://api.example.test/",
            http_pool=cast(AsyncPoolManager, pool),
        ),
        access_token=credential,
    )
    connection = gateway.connection_for(BotSelf(platform="qq", user_id="10000"))

    response = cast(
        ActionResponse,
        await connection.action(
            "send_message",
            user_id="42",
            message="hello",
        ),
    )
    await gateway.close()

    assert response.status == ApiStatus.OK
    assert pool.cleared is True
    assert pool.calls == [
        {
            "method": "POST",
            "url": "https://api.example.test",
            "body": None,
            "headers": {"Authorization": "Bearer token-1"},
            "json": {
                "action": "send_message",
                "params": {
                    "detail_type": "private",
                    "user_id": "42",
                    "message": [{"type": "text", "data": {"text": "hello"}}],
                },
                "self": {"platform": "qq", "user_id": "10000"},
            },
            "multiplexed": False,
            "kwargs": {},
        },
    ]


async def test_message_return_uses_event_target_fields() -> None:
    bot = Bot()
    gateway = CaptureOneBot12Gateway(bot)
    bot.add_gateway(gateway)
    event = EventPayload.model_validate(
        {
            **private_msg_payload(),
            "detail_type": "group",
            "message_id": "g-1",
            "group_id": "20000",
        },
    ).root
    assert isinstance(event, GroupMessageEvent)
    assert event.self_ is not None

    connection = gateway.connection_for(event.self_)
    await connection.execute_return_action(event, ReturnAction.message("reply"))

    assert gateway.calls == [
        (
            "send_message",
            {
                "message": [{"type": "text", "data": {"text": "reply"}}],
                "detail_type": "group",
                "group_id": "20000",
            },
            event.self_,
        ),
    ]
