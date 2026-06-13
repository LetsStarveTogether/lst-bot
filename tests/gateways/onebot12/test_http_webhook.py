from __future__ import annotations

from http import HTTPStatus
from typing import cast

from pydantic import JsonValue
from robyn import Response as RobynResponse
from robyn import Robyn

from lst_bot import (
    Bot,
    BotSelf,
    Connection,
    EventKind,
    HeartbeatMetaEvent,
    Injected,
    InjectionContext,
    PrivateMessageEvent,
    ReturnAction,
)
from lst_bot.gateways.onebot12 import (
    HttpAction,
    HttpWebhook,
    OneBot12Gateway,
)
from lst_bot.protocol.base import Model
from tests.protocol.support import private_msg_payload

from .support import (
    QueuedRequest,
    RobynServer,
    response_body,
)


def meta_heartbeat_payload() -> dict[str, JsonValue]:
    return {
        "id": "evt-heartbeat",
        "time": 1.0,
        "type": "meta",
        "detail_type": "heartbeat",
        "sub_type": "",
        "interval": 5000,
    }


async def test_http_webhook_event_dispatches_and_returns_no_content() -> None:
    bot = Bot()
    gateway = OneBot12Gateway(bot, action=HttpAction())
    bot.add_gateway(gateway)
    events: list[str] = []

    @bot.on_msg()
    def collect(event: Injected[PrivateMessageEvent]) -> None:
        events.append(event.message.text)

    response = await gateway.handle_http(Model.model_validate(private_msg_payload()))

    assert response.status_code == HTTPStatus.NO_CONTENT
    assert events == ["hello"]


async def test_http_webhook_meta_event_dispatches_without_connection() -> None:
    bot = Bot()
    gateway = OneBot12Gateway(bot, action=HttpAction())
    bot.add_gateway(gateway)
    seen: list[tuple[int, bool]] = []

    @bot.on_event(EventKind.META, block=True)
    def collect(
        event: Injected[HeartbeatMetaEvent],
        context: Injected[InjectionContext],
        active_gateway: Injected[OneBot12Gateway],
    ) -> None:
        seen.append((event.interval, context.connection is None))
        assert active_gateway is gateway

    response = await gateway.handle_http(Model.model_validate(meta_heartbeat_payload()))

    assert response.status_code == HTTPStatus.NO_CONTENT
    assert seen == [(5000, True)]


async def test_http_webhook_meta_event_action_return_uses_explicit_self() -> None:
    bot = Bot()
    gateway = OneBot12Gateway(bot, action=HttpAction())
    bot.add_gateway(gateway)
    self_ = BotSelf(platform="qq", user_id="10000")

    @bot.on_event(EventKind.META, block=True)
    def collect() -> ReturnAction:
        return ReturnAction.call("get_user_info", {"user_id": "42"}, self_=self_)

    response = await gateway.handle_http(Model.model_validate(meta_heartbeat_payload()))

    assert response.status_code == HTTPStatus.OK
    assert response_body(response) == [
        {
            "action": "get_user_info",
            "params": {"user_id": "42"},
            "self": {"platform": "qq", "user_id": "10000"},
        },
    ]


async def test_http_webhook_event_returns_action_request_list() -> None:
    bot = Bot()
    gateway = OneBot12Gateway(bot, action=HttpAction())
    bot.add_gateway(gateway)

    @bot.on_msg(block=True)
    def collect(connection: Injected[Connection]) -> list[str | ReturnAction]:
        assert connection.self_ == BotSelf(platform="qq", user_id="10000")
        return [
            "pong",
            ReturnAction.call("get_user_info", {"user_id": "42"}),
        ]

    response = await gateway.handle_http(Model.model_validate(private_msg_payload()))

    assert response.status_code == HTTPStatus.OK
    assert response.headers["Content-Type"] == "application/json"
    assert response_body(response) == [
        {
            "action": "send_message",
            "params": {
                "detail_type": "private",
                "message": [{"type": "text", "data": {"text": "pong"}}],
                "user_id": "42",
            },
            "self": {"platform": "qq", "user_id": "10000"},
        },
        {
            "action": "get_user_info",
            "params": {"user_id": "42"},
            "self": {"platform": "qq", "user_id": "10000"},
        },
    ]


async def test_http_webhook_quick_action_does_not_need_action_backend() -> None:
    bot = Bot()
    gateway = OneBot12Gateway(bot)
    bot.add_gateway(gateway)

    @bot.on_msg(block=True)
    def collect() -> str:
        return "pong"

    response = await gateway.handle_http(Model.model_validate(private_msg_payload()))

    assert response.status_code == HTTPStatus.OK
    assert response_body(response) == [
        {
            "action": "send_message",
            "params": {
                "detail_type": "private",
                "message": [{"type": "text", "data": {"text": "pong"}}],
                "user_id": "42",
            },
            "self": {"platform": "qq", "user_id": "10000"},
        },
    ]


def test_http_webhook_mounts_lifecycle_and_route_idempotently() -> None:
    bot = Bot()
    gateway = OneBot12Gateway(bot, ingress=[HttpWebhook("/onebot")])
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
    gateway = OneBot12Gateway(
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


async def test_http_webhook_rejects_inbound_action_request() -> None:
    gateway = OneBot12Gateway(Bot(), action=HttpAction())

    response = await gateway.handle_http(
        Model.model_validate({"action": "get_version", "params": {}})
    )
    body = cast(dict[str, JsonValue], response_body(response))

    assert response.status_code == HTTPStatus.OK
    assert body["status"] == "failed"
    assert body["retcode"]
