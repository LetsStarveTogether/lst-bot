from asyncio import Event as AsyncEvent
from asyncio import QueueFull, TaskGroup, timeout
from http import HTTPStatus
from typing import override
from unittest.mock import AsyncMock, patch

import pytest
from bot import (
    ActionCall,
    ActionResponse,
    Bot,
    Connection,
    Event,
    EventPayload,
    Injected,
    PrivateMessageEvent,
)
from bot.gateways import Gateway
from bot.gateways.onebot12 import HttpWebhook, OneBot12Gateway
from bot.json import dumpb, loads
from robyn import Robyn
from robyn.testing import TestClient as RobynTestClient

from .support import ObservableReadinessBot, private_message_payload

IDENTITY_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "OneBot/12",
    "X-OneBot-Version": "12",
    "X-Impl": "test",
}
AUTH = "test-value"


class ImmediateBot(Bot):
    def __init__(self, *, overloaded: bool = False) -> None:
        super().__init__()
        self.events: list[Event] = []
        self.overloaded = overloaded

    @override
    async def dispatch(
        self,
        connection: Connection | None,
        event: Event,
        *,
        gateway: Gateway | None = None,
    ) -> None:
        _ = connection, gateway
        if self.overloaded:
            raise QueueFull
        self.events.append(event)

    @override
    async def wait_until_running(self) -> None:
        pass


def mounted_client(
    bot: Bot,
    *,
    token: str | None = None,
) -> RobynTestClient:
    app = Robyn(__file__)
    gateway = OneBot12Gateway(
        bot,
        ingress=[HttpWebhook("/onebot")],
        access_token=token,
    )
    bot.add_gateway(gateway)
    gateway.mount(app)
    return RobynTestClient(app)


async def test_http_dispatch_returns_quick_actions_with_explicit_null() -> None:
    bot = Bot()
    gateway = OneBot12Gateway(bot)
    bot.add_gateway(gateway)

    @bot.on_msg(block=True)
    def reply(
        event: Injected[PrivateMessageEvent],
        connection: Injected[Connection],
    ) -> list[str | ActionCall]:
        assert event.message.text == "hello"
        assert connection.self_.user_id == "10000"
        return [
            "pong",
            ActionCall.model_validate({
                "action": "vendor.test",
                "params": {"optional": None},
            }),
        ]

    async with timeout(1), bot:
        response = await gateway.handle_http(
            EventPayload.model_validate(private_message_payload())
        )

    assert response.status_code == HTTPStatus.OK
    assert response.headers["Content-Type"] == "application/json"
    assert loads(response.description) == [
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
            "action": "vendor.test",
            "params": {"optional": None},
            "self": {"platform": "qq", "user_id": "10000"},
        },
    ]


async def test_http_can_disable_quick_actions() -> None:
    bot = Bot()
    gateway = OneBot12Gateway(bot)
    bot.add_gateway(gateway)

    @bot.on_msg(block=True)
    def reply() -> str:
        return "pong"

    request_action = AsyncMock(return_value=ActionResponse.ok())
    with patch.object(gateway, "request_action", request_action):
        async with timeout(1), bot:
            response = await gateway.handle_http(
                EventPayload.model_validate(private_message_payload()),
                quick_response=False,
            )

    assert response.status_code == HTTPStatus.NO_CONTENT
    request_action.assert_awaited_once()
    action_call = request_action.await_args
    assert action_call is not None
    _, action, params = action_call.args
    assert action == "send_message"
    assert params.model_dump(mode="json") == {
        "detail_type": "private",
        "message": [{"type": "text", "data": {"text": "pong"}}],
        "user_id": "42",
    }


async def test_http_event_waits_for_bot_startup() -> None:
    bot = ObservableReadinessBot()
    gateway = OneBot12Gateway(bot)
    bot.add_gateway(gateway)
    received = AsyncEvent()

    @bot.on_msg(block=True)
    def collect() -> None:
        received.set()

    async with timeout(1), TaskGroup() as tasks:
        request = tasks.create_task(
            gateway.handle_http(EventPayload.model_validate(private_message_payload()))
        )
        await bot.waiting.wait()
        assert not request.done()
        await bot.start()
        try:
            response = await request
            await received.wait()
        finally:
            await bot.close()

    assert response.status_code == HTTPStatus.NO_CONTENT


async def test_http_quick_action_context_expires_with_response() -> None:
    bot = Bot()
    gateway = OneBot12Gateway(bot)
    bot.add_gateway(gateway)
    release = AsyncEvent()
    completed = AsyncEvent()
    errors: list[type[Exception]] = []

    async with timeout(1), TaskGroup() as tasks:

        @bot.on_msg(block=True)
        def reply_later(connection: Injected[Connection]) -> None:
            async def call() -> None:
                await release.wait()
                try:
                    await connection.action("get_version")
                except Exception as exc:
                    errors.append(type(exc))
                finally:
                    completed.set()

            tasks.create_task(call())

        async with bot:
            response = await gateway.handle_http(
                EventPayload.model_validate(private_message_payload())
            )
            release.set()
            await completed.wait()

    assert response.status_code == HTTPStatus.NO_CONTENT
    assert errors == [LookupError]


@pytest.mark.parametrize(
    ("headers", "query", "status"),
    [
        pytest.param(
            {**IDENTITY_HEADERS, "Authorization": "Bearer wrong"},
            {"access_token": "test-value"},
            HTTPStatus.UNAUTHORIZED,
            id="invalid-bearer-precedes-query",
        ),
        pytest.param(
            IDENTITY_HEADERS,
            {"access_token": "test-value"},
            HTTPStatus.NO_CONTENT,
            id="valid-query-token",
        ),
        pytest.param(
            {**IDENTITY_HEADERS, "Authorization": "Bearer test-value"},
            {},
            HTTPStatus.NO_CONTENT,
            id="valid-bearer-token",
        ),
    ],
)
def test_http_webhook_authentication(
    headers: dict[str, str],
    query: dict[str, str],
    status: HTTPStatus,
) -> None:
    bot = ImmediateBot()
    client = mounted_client(bot, token=AUTH)

    with client:
        response = client.post(
            "/onebot",
            json_data=private_message_payload(),
            headers=dict(headers),
            query_params=query,
        )

    assert response.status_code == status
    assert len(bot.events) == (status == HTTPStatus.NO_CONTENT)


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        (
            {
                "Content-Type": "application/json",
                "User-Agent": "OneBot/12",
                "X-Impl": "test",
            },
            HTTPStatus.BAD_REQUEST,
        ),
        (IDENTITY_HEADERS | {"X-OneBot-Version": "11"}, HTTPStatus.BAD_REQUEST),
        (
            {
                "Content-Type": "application/json",
                "User-Agent": "OneBot/12",
                "X-OneBot-Version": "12",
            },
            HTTPStatus.BAD_REQUEST,
        ),
        (IDENTITY_HEADERS | {"X-Impl": "BAD"}, HTTPStatus.BAD_REQUEST),
        (IDENTITY_HEADERS | {"User-Agent": ""}, HTTPStatus.BAD_REQUEST),
        (
            {
                "User-Agent": "OneBot/12",
                "X-OneBot-Version": "12",
                "X-Impl": "test",
            },
            HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
        ),
        (
            IDENTITY_HEADERS | {"Content-Type": "text/plain"},
            HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
        ),
    ],
    ids=(
        "missing-version",
        "wrong-version",
        "missing-implementation",
        "invalid-implementation",
        "empty-user-agent",
        "missing-content-type",
        "wrong-content-type",
    ),
)
def test_http_webhook_rejects_invalid_headers(
    headers: dict[str, str],
    status: HTTPStatus,
) -> None:
    bot = ImmediateBot()
    client = mounted_client(bot)

    with client:
        response = client.post(
            "/onebot",
            body=dumpb(private_message_payload()),
            headers=headers,
        )

    assert response.status_code == status
    assert bot.events == []


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"{", id="malformed-json"),
        pytest.param(
            dumpb({"action": "get_version", "params": {}}),
            id="action-request",
        ),
        pytest.param(
            dumpb({
                "status": "ok",
                "retcode": 0,
                "data": None,
                "message": "",
                "echo": "request-1",
            }),
            id="action-response",
        ),
    ],
)
def test_http_webhook_rejects_non_event_bodies(body: bytes) -> None:
    client = mounted_client(ImmediateBot())

    with client:
        response = client.post(
            "/onebot",
            body=body,
            headers=dict(IDENTITY_HEADERS),
        )

    assert response.status_code == HTTPStatus.BAD_REQUEST


def test_http_webhook_maps_dispatch_overload_to_503() -> None:
    client = mounted_client(ImmediateBot(overloaded=True))

    with client:
        response = client.post(
            "/onebot",
            json_data=private_message_payload(),
            headers=dict(IDENTITY_HEADERS),
        )

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
