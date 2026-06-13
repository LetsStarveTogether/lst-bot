from __future__ import annotations

from http import HTTPStatus
from typing import cast

from robyn import Response as RobynResponse
from robyn import Robyn

from lst_bot import (
    Bot,
    Injected,
    Msg,
    PrivateMessageEvent,
    RequestResponse,
    ReturnAction,
)
from lst_bot.gateways.onebot11 import HttpWebhook, OneBot11Gateway
from lst_bot.protocol.base import Model

from .support import (
    CaptureOneBot11Gateway,
    QueuedRequest,
    RobynServer,
    friend_request_payload,
    group_request_payload,
    private_msg_payload,
    response_body,
)


async def test_http_webhook_event_dispatches_and_returns_no_content() -> None:
    bot = Bot()
    gateway = OneBot11Gateway(bot)
    bot.add_gateway(gateway)
    events: list[str] = []

    @bot.on_msg()
    def collect(event: Injected[PrivateMessageEvent]) -> None:
        events.append(event.message.text)

    response = await gateway.handle_http(Model.model_validate(private_msg_payload()))

    assert response.status_code == HTTPStatus.NO_CONTENT
    assert events == ["hello"]


async def test_http_webhook_rejects_invalid_event_shape() -> None:
    gateway = OneBot11Gateway(Bot())
    payload = {**private_msg_payload(), "self_id": True}

    response = await gateway.handle_http(Model.model_validate(payload))
    description = (
        response.description.decode()
        if isinstance(response.description, bytes)
        else response.description
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert "self_id" in description


async def test_http_webhook_quick_reply_falls_back_after_first_reply() -> None:
    bot = Bot()
    gateway = CaptureOneBot11Gateway(bot)
    bot.add_gateway(gateway)

    @bot.on_msg(block=True)
    def collect() -> list[Msg | ReturnAction]:
        return [
            Msg.from_input("one"),
            Msg.from_input("two"),
            ReturnAction.call("get_user_info", {"user_id": "42"}),
        ]

    response = await gateway.handle_http(Model.model_validate(private_msg_payload()))

    assert response.status_code == HTTPStatus.OK
    assert response.headers["Content-Type"] == "application/json"
    assert response_body(response) == {
        "reply": [{"type": "text", "data": {"text": "one"}}],
        "at_sender": False,
    }
    assert gateway.calls == [
        (
            "send_private_msg",
            {
                "user_id": 42,
                "message": [{"type": "text", "data": {"text": "two"}}],
            },
            None,
        ),
        ("get_stranger_info", {"user_id": 42}, None),
    ]


async def test_http_webhook_quick_reply_does_not_need_action_backend() -> None:
    bot = Bot()
    gateway = OneBot11Gateway(bot)
    bot.add_gateway(gateway)

    @bot.on_msg(block=True)
    def collect() -> str:
        return "pong"

    response = await gateway.handle_http(Model.model_validate(private_msg_payload()))

    assert response.status_code == HTTPStatus.OK
    assert response_body(response) == {
        "reply": [{"type": "text", "data": {"text": "pong"}}],
        "at_sender": False,
    }


async def test_http_webhook_quick_friend_request_response() -> None:
    bot = Bot()
    gateway = OneBot11Gateway(bot)
    bot.add_gateway(gateway)

    @bot.on_event(block=True)
    def collect(request: Injected[RequestResponse]) -> ReturnAction:
        return request.approve(remark="tester")

    response = await gateway.handle_http(Model.model_validate(friend_request_payload()))

    assert response.status_code == HTTPStatus.OK
    assert response_body(response) == {
        "approve": True,
        "remark": "tester",
    }


async def test_http_webhook_quick_group_request_response() -> None:
    bot = Bot()
    gateway = OneBot11Gateway(bot)
    bot.add_gateway(gateway)

    @bot.on_event(block=True)
    def collect(request: Injected[RequestResponse]) -> ReturnAction:
        return request.reject("not now")

    response = await gateway.handle_http(Model.model_validate(group_request_payload()))

    assert response.status_code == HTTPStatus.OK
    assert response_body(response) == {
        "approve": False,
        "reason": "not now",
    }


async def test_http_webhook_request_response_falls_back_after_first_operation() -> None:
    bot = Bot()
    gateway = CaptureOneBot11Gateway(bot)
    bot.add_gateway(gateway)

    @bot.on_event(block=True)
    def collect(request: Injected[RequestResponse]) -> list[ReturnAction]:
        return [
            request.reject("first"),
            ReturnAction.request(False, reason="later"),
        ]

    response = await gateway.handle_http(Model.model_validate(group_request_payload()))

    assert response.status_code == HTTPStatus.OK
    assert response_body(response) == {
        "approve": False,
        "reason": "first",
    }
    assert gateway.calls == [
        (
            "set_group_add_request",
            {
                "flag": "group-flag",
                "sub_type": "add",
                "approve": False,
                "reason": "later",
            },
            None,
        ),
    ]


def test_http_webhook_mounts_lifecycle_and_route_idempotently() -> None:
    bot = Bot()
    gateway = OneBot11Gateway(bot, ingress=[HttpWebhook("/onebot")])
    bot.add_gateway(gateway)
    raw_server = RobynServer()
    server = cast(Robyn, raw_server)

    mounted = gateway.mount(server)
    mounted_again = gateway.mount(server)

    assert mounted is server
    assert mounted_again is server
    assert raw_server.startup == bot.start
    assert raw_server.shutdown == bot.close
    assert [(method, path) for method, path, _ in raw_server.routes] == [
        ("POST", "/onebot"),
    ]


async def test_http_webhook_robyn_route_checks_bearer_or_query_token() -> None:
    bot = Bot()
    credential = "secret"
    gateway = OneBot11Gateway(
        bot,
        ingress=[HttpWebhook("/onebot")],
        access_token=credential,
    )
    bot.add_gateway(gateway)
    events: list[str] = []

    @bot.on_msg(block=True)
    def collect(event: Injected[PrivateMessageEvent]) -> None:
        events.append(event.message.text)

    raw_server = RobynServer()
    gateway.mount(cast(Robyn, raw_server))
    post_handler = raw_server.routes[0][2]

    async with bot:
        rejected = cast(
            RobynResponse,
            await post_handler(
                QueuedRequest(
                    private_msg_payload(),
                    headers={"Authorization": "Bearer wrong"},
                )
            ),
        )
        accepted = cast(
            RobynResponse,
            await post_handler(
                QueuedRequest(
                    private_msg_payload(),
                    query_params={"access_token": credential},
                )
            ),
        )

    assert rejected.status_code == HTTPStatus.UNAUTHORIZED
    assert accepted.status_code == HTTPStatus.NO_CONTENT
    assert events == ["hello"]
