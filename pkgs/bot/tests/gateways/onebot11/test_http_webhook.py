from asyncio import Event, QueueFull, TaskGroup, timeout
from hashlib import sha1
from hmac import new
from http import HTTPStatus
from unittest.mock import AsyncMock, patch

import pytest
from bot import (
    ActionCall,
    Bot,
    BotSelf,
    Connection,
    Injected,
    Msg,
    PrivateMessageEvent,
    ReturnAction,
)
from bot.gateways.onebot11 import HttpAction, HttpWebhook, OneBot11Gateway, decode_event
from bot.json import dumpb, loads
from bot.protocol.base import Model
from pydantic import JsonValue
from robyn import Response, Robyn
from robyn.testing import TestClient as RobynTestClient

from tests.gateways.support import ActionServer, ObservableReadinessBot

from .support import (
    friend_request_payload,
    group_request_payload,
    private_msg_payload,
)


def response_json(response: Response) -> JsonValue:
    return loads(response.description)


async def test_http_webhook_dispatches_event_and_returns_no_content() -> None:
    bot = Bot()
    gateway = OneBot11Gateway(bot)
    bot.add_gateway(gateway)
    messages: list[str] = []

    @bot.on_msg()
    def collect(event: Injected[PrivateMessageEvent]) -> None:
        messages.append(event.message.text)

    async with bot:
        response = await gateway.handle_http(
            Model.model_validate(private_msg_payload())
        )

    assert response.status_code == HTTPStatus.NO_CONTENT
    assert messages == ["hello"]


async def test_http_webhook_rejects_invalid_event_shape() -> None:
    gateway = OneBot11Gateway(Bot())
    payload = {**private_msg_payload(), "self_id": True}

    async with timeout(1):
        response = await gateway.handle_http(Model.model_validate(payload))

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert "self_id" in str(response.description)


async def test_http_event_waits_for_bot_startup() -> None:
    bot = ObservableReadinessBot()
    gateway = OneBot11Gateway(bot)
    bot.add_gateway(gateway)

    async with timeout(1), TaskGroup() as tasks:
        request = tasks.create_task(
            gateway.handle_http(Model.model_validate(private_msg_payload()))
        )
        await bot.waiting.wait()
        assert not request.done()
        await bot.start()
        try:
            response = await request
        finally:
            await bot.close()

    assert response.status_code == HTTPStatus.NO_CONTENT


async def test_http_dispatch_errors_are_not_bad_requests() -> None:
    gateway = OneBot11Gateway(Bot())
    with (
        patch.object(gateway.bot, "wait_until_running", AsyncMock()),
        patch.object(
            gateway,
            "dispatch_event",
            AsyncMock(side_effect=ValueError("dispatch failed")),
        ),
        pytest.raises(ValueError, match="dispatch failed"),
    ):
        await gateway.handle_http(Model.model_validate(private_msg_payload()))


async def test_http_quick_reply_uses_first_operation_and_sends_the_rest() -> None:
    async with ActionServer() as server:
        bot = Bot()
        gateway = OneBot11Gateway(
            bot,
            action=HttpAction(server.base_url, http_client=server.http_client),
        )
        bot.add_gateway(gateway)

        @bot.on_msg(block=True)
        def collect() -> list[Msg | ReturnAction | ActionCall]:
            return [
                Msg.from_input("one"),
                Msg.from_input("two"),
                ActionCall.model_validate({
                    "action": "send_like",
                    "params": {"user_id": "42"},
                }),
            ]

        async with bot:
            response = await gateway.handle_http(
                Model.model_validate(private_msg_payload())
            )

    assert response.status_code == HTTPStatus.OK
    assert response_json(response) == {
        "reply": [{"type": "text", "data": {"text": "one"}}],
    }
    assert [(request.path, request.json) for request in server.requests] == [
        (
            "/send_private_msg",
            {
                "user_id": 42,
                "message": [{"type": "text", "data": {"text": "two"}}],
            },
        ),
        ("/send_like", {"user_id": 42}),
    ]


async def test_group_http_quick_reply_does_not_need_action_backend() -> None:
    bot = Bot()
    gateway = OneBot11Gateway(bot)
    bot.add_gateway(gateway)

    @bot.on_msg(block=True)
    def collect() -> str:
        return "pong"

    async with bot:
        response = await gateway.handle_http(
            Model.model_validate({
                **private_msg_payload(),
                "message_type": "group",
                "sub_type": "normal",
                "group_id": 20000,
            })
        )

    assert response.status_code == HTTPStatus.OK
    assert response_json(response) == {
        "reply": [{"type": "text", "data": {"text": "pong"}}],
        "at_sender": False,
    }


@pytest.mark.parametrize("target", ["gateway", "event", "self"])
async def test_http_quick_reply_is_bound_to_source(target: str) -> None:
    async with ActionServer() as server:
        bot = Bot()
        gateway = OneBot11Gateway(
            bot,
            action=HttpAction(server.base_url, http_client=server.http_client),
        )
        other = OneBot11Gateway(
            bot,
            action=HttpAction(server.base_url, http_client=server.http_client),
        )
        bot.add_gateway(gateway)
        bot.add_gateway(other)

        @bot.on_msg(block=True)
        async def reply(event: Injected[PrivateMessageEvent]) -> str:
            selected_gateway = other if target == "gateway" else gateway
            selected_event = (
                decode_event({**private_msg_payload(), "user_id": 43})
                if target == "event"
                else event
            )
            selected_self = (
                BotSelf(platform="qq", user_id="10001")
                if target == "self"
                else event.self_
            )
            await selected_gateway.execute_return_action(
                Connection(selected_gateway, selected_self),
                selected_event,
                ReturnAction.message("separate"),
            )
            return "source"

        async with bot:
            response = await gateway.handle_http(private_msg_payload())

    assert response_json(response) == {
        "reply": [{"type": "text", "data": {"text": "source"}}],
    }
    assert [(request.path, request.json) for request in server.requests] == [
        (
            "/send_private_msg",
            {
                "user_id": 43 if target == "event" else 42,
                "message": [{"type": "text", "data": {"text": "separate"}}],
            },
        ),
    ]


async def test_http_quick_reply_rejects_foreign_connection_and_closed_gateway() -> None:
    bot = Bot()
    gateway = OneBot11Gateway(bot)
    other = OneBot11Gateway(bot)
    bot.add_gateway(gateway)

    @bot.on_msg(block=True)
    async def reply(
        event: Injected[PrivateMessageEvent],
        connection: Injected[Connection],
    ) -> None:
        with pytest.raises(ValueError, match="another gateway"):
            await gateway.execute_return_action(
                Connection(other, event.self_), event, ReturnAction.message("foreign")
            )
        await gateway.close()
        with pytest.raises(RuntimeError, match="closed"):
            await gateway.execute_return_action(
                connection, event, ReturnAction.message("closed")
            )

    async with bot:
        response = await gateway.handle_http(private_msg_payload())

    assert response.status_code == HTTPStatus.NO_CONTENT


async def test_http_handler_can_disable_quick_response() -> None:
    async with ActionServer() as server:
        bot = Bot()
        gateway = OneBot11Gateway(
            bot,
            action=HttpAction(server.base_url, http_client=server.http_client),
        )
        bot.add_gateway(gateway)

        @bot.on_msg(block=True)
        def collect() -> str:
            return "pong"

        async with bot:
            response = await gateway.handle_http(
                Model.model_validate(private_msg_payload()),
                quick_response=False,
            )

    assert response.status_code == HTTPStatus.NO_CONTENT
    assert server.requests[0].json == {
        "user_id": 42,
        "message": [{"type": "text", "data": {"text": "pong"}}],
    }


async def test_http_quick_operation_context_expires_with_response() -> None:
    release = Event()
    completed = Event()

    async with timeout(1), ActionServer() as server, TaskGroup() as tasks:
        bot = Bot()
        gateway = OneBot11Gateway(
            bot,
            action=HttpAction(server.base_url, http_client=server.http_client),
        )
        bot.add_gateway(gateway)

        @bot.on_msg(block=True)
        def reply_later(
            event: Injected[PrivateMessageEvent],
            connection: Injected[Connection],
        ) -> None:
            async def send() -> None:
                await release.wait()
                try:
                    await gateway.execute_return_action(
                        connection,
                        event,
                        ReturnAction.message("late"),
                    )
                finally:
                    completed.set()

            tasks.create_task(send())

        async with bot:
            response = await gateway.handle_http(
                Model.model_validate(private_msg_payload())
            )
            release.set()
            await completed.wait()

    assert response.status_code == HTTPStatus.NO_CONTENT
    assert [(request.path, request.json) for request in server.requests] == [
        (
            "/send_private_msg",
            {
                "user_id": 42,
                "message": [{"type": "text", "data": {"text": "late"}}],
            },
        )
    ]


@pytest.mark.parametrize(
    ("payload", "approve", "expected"),
    [
        pytest.param(friend_request_payload(), True, {"remark": "tester"}, id="friend"),
        pytest.param(group_request_payload(), False, {"reason": "not now"}, id="group"),
    ],
)
async def test_http_request_quick_response(
    payload: dict[str, JsonValue],
    approve: bool,
    expected: dict[str, str],
) -> None:
    bot = Bot()
    gateway = OneBot11Gateway(bot)
    bot.add_gateway(gateway)

    @bot.on_event(block=True)
    def collect() -> ReturnAction:
        return ReturnAction.request(approve, **expected)

    async with bot:
        response = await gateway.handle_http(Model.model_validate(payload))

    assert response.status_code == HTTPStatus.OK
    assert response_json(response) == {"approve": approve, **expected}


def test_robyn_route_enforces_onebot11_http_headers_and_signature() -> None:
    credential = "secret"
    bot = Bot()
    gateway = OneBot11Gateway(
        bot,
        ingress=[HttpWebhook("/onebot", secret=credential)],
    )
    bot.add_gateway(gateway)
    app = Robyn(__file__)
    gateway.mount(app)
    gateway.mount(app)
    with pytest.raises(ValueError, match="only be mounted on one server"):
        gateway.mount(Robyn(__file__))
    body = dumpb(private_msg_payload())
    signature = "sha1=" + new(credential.encode(), body, sha1).hexdigest()

    with (
        patch.object(bot, "dispatch", AsyncMock(return_value=[])) as dispatch,
        patch.object(bot, "wait_until_running", AsyncMock()),
        RobynTestClient(app) as client,
    ):
        missing_signature = client.post(
            "/onebot",
            body=body,
            headers={"Content-Type": "application/json", "X-Self-ID": "10000"},
        )
        wrong_signature = client.post(
            "/onebot",
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-Self-ID": "10000",
                "X-Signature": "sha1=" + "0" * 40,
            },
        )
        missing_self = client.post(
            "/onebot",
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-Signature": signature,
            },
        )
        wrong_self = client.post(
            "/onebot",
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-Self-ID": "99999",
                "X-Signature": signature,
            },
        )
        accepted = client.post(
            "/onebot",
            body=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "X-Self-ID": "10000",
                "X-Signature": signature,
            },
        )

    assert missing_signature.status_code == HTTPStatus.UNAUTHORIZED
    assert wrong_signature.status_code == HTTPStatus.UNAUTHORIZED
    assert missing_self.status_code == HTTPStatus.BAD_REQUEST
    assert wrong_self.status_code == HTTPStatus.BAD_REQUEST
    assert accepted.status_code == HTTPStatus.NO_CONTENT
    dispatch.assert_awaited_once()


@pytest.mark.parametrize(
    ("body", "headers", "status"),
    [
        pytest.param(
            b"{",
            {"Content-Type": "application/json"},
            HTTPStatus.BAD_REQUEST,
            id="malformed-json",
        ),
        pytest.param(
            dumpb(private_msg_payload()),
            {"Content-Type": "text/plain"},
            HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            id="wrong-content-type",
        ),
    ],
)
def test_robyn_route_rejects_invalid_http_payload(
    body: bytes,
    headers: dict[str, str],
    status: HTTPStatus,
) -> None:
    bot = Bot()
    gateway = OneBot11Gateway(bot, ingress=[HttpWebhook("/onebot")])
    bot.add_gateway(gateway)
    app = Robyn(__file__)
    gateway.mount(app)

    with RobynTestClient(app) as client:
        response = client.post("/onebot", body=body, headers=headers)

    assert response.status_code == status


def test_http_queue_overload_maps_to_service_unavailable() -> None:
    bot = Bot()
    gateway = OneBot11Gateway(bot, ingress=[HttpWebhook("/onebot")])
    bot.add_gateway(gateway)
    app = Robyn(__file__)
    gateway.mount(app)

    with (
        patch.object(bot, "dispatch", AsyncMock(side_effect=QueueFull)),
        patch.object(bot, "wait_until_running", AsyncMock()),
        RobynTestClient(app) as client,
    ):
        response = client.post(
            "/onebot",
            json_data=private_msg_payload(),
            headers={"X-Self-ID": "10000"},
        )

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
