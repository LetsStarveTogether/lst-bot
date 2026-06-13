from __future__ import annotations

from typing import cast

import pytest
from ulid import ULID
from urllib3_future import AsyncPoolManager

from lst_bot import (
    ActionResponse,
    Bot,
    BotSelf,
    EventPayload,
    GroupMessageEvent,
    Injected,
    Msg,
    PrivateMessageEvent,
    ReturnAction,
)
from lst_bot.gateways.onebot11 import HttpAction, OneBot11Gateway
from lst_bot.protocol.base import Model

from .support import (
    CaptureOneBot11Gateway,
    FakePool,
    action_response_payload,
    group_msg_payload,
    private_msg_payload,
)


async def test_http_action_uses_onebot11_action_path_and_params() -> None:
    pool = FakePool(action_response_payload({"message_id": 99}))
    gateway = OneBot11Gateway(
        Bot(),
        action=HttpAction(
            "https://api.example.test/",
            http_pool=cast(AsyncPoolManager, pool),
        ),
    )
    connection = gateway.connection_for(BotSelf(platform="qq", user_id="10000"))

    response = cast(
        ActionResponse,
        await connection.action(
            "send_message",
            user_id="42",
            message=[
                {"type": "text", "data": {"text": "hello"}},
                {"type": "mention_all", "data": {}},
            ],
        ),
    )
    await gateway.close()

    assert response.data == {"message_id": 99}
    assert pool.cleared is True
    assert pool.calls == [
        {
            "method": "POST",
            "url": "https://api.example.test/send_private_msg",
            "body": None,
            "headers": None,
            "json": {
                "user_id": 42,
                "message": [
                    {"type": "text", "data": {"text": "hello"}},
                    {"type": "at", "data": {"qq": "all"}},
                ],
            },
            "multiplexed": False,
            "kwargs": {},
        },
    ]


async def test_http_action_sends_access_token_authorization() -> None:
    credential = "token-1"
    pool = FakePool(action_response_payload({"message_id": 99}))
    gateway = OneBot11Gateway(
        Bot(),
        action=HttpAction(
            "https://api.example.test",
            http_pool=cast(AsyncPoolManager, pool),
        ),
        access_token=credential,
    )
    connection = gateway.connection_for(BotSelf(platform="qq", user_id="10000"))

    await connection.action(
        "send_message",
        user_id="42",
        message="hello",
    )

    assert pool.calls[0]["headers"] == {"Authorization": "Bearer token-1"}


async def test_raw_onebot11_action_is_forwarded() -> None:
    pool = FakePool(action_response_payload({}))
    gateway = OneBot11Gateway(
        Bot(),
        action=HttpAction(
            "https://api.example.test",
            http_pool=cast(AsyncPoolManager, pool),
        ),
    )
    connection = gateway.connection_for(BotSelf(platform="qq", user_id="10000"))

    await connection.action("send_like", user_id="42", times=3)

    assert pool.calls[0]["url"] == "https://api.example.test/send_like"
    assert pool.calls[0]["json"] == {"user_id": 42, "times": 3}


def test_onebot11_message_helpers_dump_to_cq_segments() -> None:
    gateway = OneBot11Gateway(Bot())
    message = Msg.reply("msg-1", Msg.mention("42", " hello"), user_id="42")

    assert gateway.quick_reply_message(message).model_dump(
        mode="json",
        by_alias=True,
    ) == [
        {"type": "reply", "data": {"id": "msg-1"}},
        {"type": "at", "data": {"qq": "42"}},
        {"type": "text", "data": {"text": " hello"}},
    ]


async def test_onebot11_private_message_event_dispatches_internal_event() -> None:
    bot = Bot()
    gateway = OneBot11Gateway(bot)
    bot.add_gateway(gateway)
    events: list[str] = []
    event_ids: list[str] = []

    @bot.on_msg()
    def collect(event: Injected[PrivateMessageEvent]) -> None:
        events.append(str(event.message))
        event_ids.append(event.id)

    await gateway.handle_payload(
        Model.model_validate(
            private_msg_payload(
                "hi&#91;x&#93;[CQ:at,qq=100][CQ:image,file=1.jpg,url=http://x]",
            ),
        ),
    )

    connection = gateway.connection_for(BotSelf(platform="qq", user_id="10000"))
    assert connection.self_ == BotSelf(platform="qq", user_id="10000")
    assert events == ["hi[x]"]
    assert str(ULID.from_str(event_ids[0])) == event_ids[0]


async def test_onebot11_message_return_uses_group_api() -> None:
    gateway = CaptureOneBot11Gateway(Bot())
    event = EventPayload.model_validate(
        {
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
        },
    ).root
    assert isinstance(event, GroupMessageEvent)
    assert event.self_ is not None

    connection = gateway.connection_for(event.self_)
    await connection.execute_return_action(event, ReturnAction.message("reply"))

    assert gateway.calls == [
        (
            "send_group_msg",
            {
                "group_id": 20000,
                "message": [{"type": "text", "data": {"text": "reply"}}],
            },
            None,
        ),
    ]


async def test_onebot11_does_not_support_internal_channel_send() -> None:
    gateway = OneBot11Gateway(
        Bot(),
        action=HttpAction("https://api.example.test"),
    )
    connection = gateway.connection_for(BotSelf(platform="qq", user_id="10000"))

    with pytest.raises(LookupError):
        await connection.action(
            "send_message",
            guild_id="g",
            channel_id="c",
            message="hello",
        )


def test_group_msg_payload_uses_onebot11_shape() -> None:
    payload = group_msg_payload()

    assert payload["post_type"] == "message"
    assert payload["message_type"] == "group"
