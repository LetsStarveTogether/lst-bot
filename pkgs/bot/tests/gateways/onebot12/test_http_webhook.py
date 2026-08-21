from __future__ import annotations

from asyncio import Event as AsyncEvent
from asyncio import QueueFull, TaskGroup, timeout
from http import HTTPStatus
from typing import override
from unittest.mock import AsyncMock, patch

import orjson
import pytest
from bot import (
    ActionResponse,
    Bot,
    Connection,
    Event,
    EventPayload,
    Injected,
    PrivateMessageEvent,
    ReturnAction,
)
from bot.gateways import Gateway
from bot.gateways.onebot12 import HttpWebhook, OneBot12Gateway
from bot.routing import DispatchResult
from robyn import Robyn
from robyn.testing import TestClient as RobynTestClient

from .support import ObservableReadinessBot, private_message_payload

IDENTITY_HEADERS = {
    "Content-Type": "application/json",
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
    ) -> list[DispatchResult]:
        _ = connection, gateway
        if self.overloaded:
            raise QueueFull
        self.events.append(event)
        return []

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
    ) -> list[str | ReturnAction]:
        assert event.message.text == "hello"
        assert connection.self_.user_id == "10000"
        return ["pong", ReturnAction.call("vendor.test", {"optional": None})]

    async with timeout(1), bot:
        response = await gateway.handle_http(
            EventPayload.model_validate(private_message_payload())
        )

    assert response.status_code == HTTPStatus.OK
    assert response.headers["Content-Type"] == "application/json"
    assert orjson.loads(response.description) == [
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


def test_http_webhook_dispatches_through_robyn_test_client() -> None:
    bot = ImmediateBot()
    client = mounted_client(bot)

    with client:
        response = client.post(
            "/onebot",
            json_data=private_message_payload(),
            headers=dict(IDENTITY_HEADERS),
        )

    assert response.status_code == HTTPStatus.NO_CONTENT
    assert len(bot.events) == 1
    assert isinstance(bot.events[0], PrivateMessageEvent)


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
    client = mounted_client(ImmediateBot(), token=AUTH)

    with client:
        response = client.post(
            "/onebot",
            json_data=private_message_payload(),
            headers=dict(headers),
            query_params=query,
        )

    assert response.status_code == status


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"X-Impl": "test"}, id="missing-version"),
        pytest.param(
            {"X-OneBot-Version": "11", "X-Impl": "test"},
            id="wrong-version",
        ),
        pytest.param({"X-OneBot-Version": "12"}, id="missing-implementation"),
        pytest.param(
            {"X-OneBot-Version": "12", "X-Impl": "BAD"},
            id="invalid-implementation",
        ),
    ],
)
def test_http_webhook_requires_onebot12_identity(
    headers: dict[str, str],
) -> None:
    client = mounted_client(ImmediateBot())

    with client:
        response = client.post(
            "/onebot",
            json_data=private_message_payload(),
            headers={"Content-Type": "application/json", **headers},
        )

    assert response.status_code == HTTPStatus.BAD_REQUEST


@pytest.mark.parametrize(
    "content_type",
    [
        pytest.param(None, id="missing"),
        pytest.param("text/plain", id="text"),
    ],
)
def test_http_webhook_requires_json_content_type(
    content_type: str | None,
) -> None:
    client = mounted_client(ImmediateBot())
    headers = {"X-OneBot-Version": "12", "X-Impl": "test"}
    if content_type is not None:
        headers["Content-Type"] = content_type

    with client:
        response = client.post(
            "/onebot",
            body=orjson.dumps(private_message_payload()),
            headers=headers,
        )

    assert response.status_code == HTTPStatus.UNSUPPORTED_MEDIA_TYPE


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"{", id="malformed-json"),
        pytest.param(
            orjson.dumps({"action": "get_version", "params": {}}),
            id="action-request",
        ),
        pytest.param(
            orjson.dumps({
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
