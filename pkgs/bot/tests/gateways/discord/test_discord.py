# ruff: file-ignore[private-member-access]
from asyncio import (
    CancelledError,
    Event,
    QueueFull,
    TaskGroup,
    create_task,
    sleep,
    timeout,
)
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast
from unittest.mock import AsyncMock

import pytest
from bot import Bot, BotSelf, MetaEvent, NoticeEvent, PrivateMessageEvent
from bot.gateways import discord as discord_module
from bot.gateways.base import WebSocketClosedError
from bot.gateways.discord import (
    DiscordAPIError,
    DiscordBytes,
    DiscordChannel,
    DiscordChannelList,
    DiscordGateway,
    DiscordGatewayFatalError,
    DiscordGatewayPayload,
    DiscordGuildList,
    DiscordGuildMember,
    DiscordGuildMemberEvent,
    DiscordHelloData,
    DiscordIntent,
    DiscordInteraction,
    DiscordMemberList,
    DiscordMessage,
    DiscordNoContent,
    DiscordPayload,
    DiscordReady,
    DiscordRequest,
    DiscordUser,
)
from bot.json import loads
from bot.protocol.actions import ActionParamModel
from bot.testing import ScriptedWebSocket
from pydantic import ValidationError
from urllib3_future import AsyncHTTPResponse, AsyncPoolManager
from urllib3_future.exceptions import HTTPError

from tests.gateways.support import response

from .support import (
    CREDENTIAL,
    Pool,
    client,
    gateway,
    interaction,
    message,
    ready_payload,
    user,
)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    async def advance(self, delay: float) -> None:
        self.now += delay


def test_strict_boundaries_and_secret_repr() -> None:
    with pytest.raises(ValueError, match="invalid value"):
        DiscordIntent(1 << 19)
    with pytest.raises(ValidationError):
        DiscordGatewayPayload.model_validate({"op": True, "d": None})
    with pytest.raises(ValidationError):
        DiscordMessage.model_validate({**message(message_id="01")})
    marker = f"sensitive-{id(object())}"
    with pytest.raises(ValidationError) as gateway_error:
        DiscordGatewayPayload.model_validate({
            "op": 0,
            "d": {"token": marker},
            "s": 1,
        })
    assert marker not in repr(gateway_error.value)
    assert marker not in str(gateway_error.value)
    with pytest.raises(ValidationError) as hello_error:
        DiscordHelloData.model_validate({"heartbeat_interval": marker})
    assert marker not in str(hello_error.value)
    with pytest.raises(ValidationError):
        DiscordRequest.model_validate({
            "method": "GET",
            "path": "//attacker.example/token",
        })
    request = DiscordRequest.model_validate({
        "method": "POST",
        "path": "/webhooks/1/secret",
        "json": {"token": "secret"},
    })
    assert "secret" not in repr(request)
    assert "secret" not in str(request)
    with pytest.raises(ValidationError) as error:
        DiscordRequest.model_validate({
            "method": "POST",
            "path": "/webhooks/1/secret?invalid=true",
        })
    assert "secret" not in str(error.value)
    payload = DiscordPayload({"token": "secret"})
    binary = DiscordBytes(b"secret")
    for value in (payload, binary):
        assert "secret" not in repr(value)
        assert "secret" not in str(value)

    ready = {
        "v": 10,
        "user": user("1"),
        "guilds": [{"id": "2", "unavailable": True}],
        "session_id": "session",
        "resume_gateway_url": "wss://resume.discord.example",
        "shard": [0, 1],
        "application": {"id": "3", "flags": 0},
    }
    assert DiscordReady.model_validate(ready).guilds[0].id == "2"
    with pytest.raises(ValidationError):
        DiscordReady.model_validate({**ready, "guilds": [{"id": "2"}]})
    with pytest.raises(ValidationError):
        DiscordReady.model_validate({**ready, "shard": [1, 1]})
    with pytest.raises(ValidationError):
        DiscordGuildMemberEvent.model_validate({"guild_id": "1", "roles": []})


@pytest.mark.parametrize(
    ("interaction_type", "data", "valid_data"),
    [
        (2, None, {"id": "12", "name": "query", "type": 1}),
        (
            3,
            {"custom_id": "button"},
            {"custom_id": "button", "component_type": 2},
        ),
        (
            4,
            {"id": "12", "name": "query"},
            {"id": "12", "name": "query", "type": 1},
        ),
        (
            5,
            {"custom_id": "modal"},
            {"custom_id": "modal", "components": []},
        ),
    ],
)
def test_interaction_types_require_and_accept_their_minimum_data(
    interaction_type: int,
    data: object,
    valid_data: object,
) -> None:
    payload = {
        "id": "10",
        "application_id": "11",
        "type": interaction_type,
        "token": CREDENTIAL,
        "version": 1,
    }
    with pytest.raises(ValidationError, match="incomplete data"):
        DiscordInteraction.model_validate(payload | {"data": data})
    assert DiscordInteraction.model_validate(payload | {"data": valid_data}).type == (
        interaction_type
    )


async def test_default_connector_accepts_unbounded_official_gateway_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect = AsyncMock(return_value=ScriptedWebSocket())
    monkeypatch.setattr(discord_module, "connect_websocket", connect)

    instance = gateway()
    await instance._websocket_connector("wss://gateway.example", None)

    connect.assert_awaited_once_with(
        "wss://gateway.example",
        None,
        max_size=None,
    )


async def test_rest_json_rate_limit_errors_and_multipart() -> None:
    pool = Pool(
        response(429, {"retry_after": 0.001, "global": True}),
        response(200, {"id": "1"}),
        response(204),
        response(200, {"id": "2"}),
        response(200, body=b"raw"),
        response(400, {"code": 50035, "message": "Invalid Form Body"}),
    )
    rest = client(pool)

    payload = await rest.request_discord(
        "POST",
        "/channels/1/messages",
        query={"wait": True},
        json={"content": "hello"},
        reason="test reason",
    )
    no_content = await rest.request_discord("DELETE", "/channels/1/messages/2")
    upload = await rest.request_discord(
        "POST",
        "/channels/1/messages",
        files=[{"filename": "a.txt", "data": b"hello"}],
    )
    raw = await rest.request_discord(
        "GET",
        "/invites/code/target-users",
        response_type="bytes",
    )
    with pytest.raises(DiscordAPIError, match="50035"):
        await rest.request_discord("POST", "/channels/1/messages", json={})

    assert isinstance(payload, DiscordPayload)
    assert payload.root == {"id": "1"}
    assert isinstance(no_content, DiscordNoContent)
    assert isinstance(upload, DiscordPayload)
    assert upload.root == {"id": "2"}
    assert isinstance(raw, DiscordBytes)
    assert raw.root == b"raw"
    _, url, kwargs = pool.requests[0]
    assert url == "https://discord.example/api/v10/channels/1/messages?wait=true"
    assert kwargs["headers"] == {
        "User-Agent": (
            "DiscordBot (https://github.com/LetsStarveTogether/lst-bot, 0.0.0)"
        ),
        "Authorization": "Bot token",
        "X-Audit-Log-Reason": "test%20reason",
        "Content-Type": "application/json",
    }
    multipart = cast(bytes, pool.requests[3][2]["body"])
    assert b'name="files[0]"' in multipart
    assert b"payload_json" not in multipart
    assert all(kwargs["retries"] is False for _, _, kwargs in pool.requests)


async def test_payload_json_multipart_supports_named_files_and_nested_json() -> None:
    pool = Pool(response(200, {}))
    rest = client(pool)

    await rest.request_discord(
        "POST",
        "/invites/code/target-users",
        json={"target_user_ids": ["1", "2"]},
        files=[
            {
                "field": "target_users_file",
                "filename": "users.txt",
                "data": b"1\n2",
            }
        ],
    )

    body = cast(bytes, pool.requests[0][2]["body"])
    assert b'name="payload_json"' in body
    assert b'"target_user_ids":["1","2"]' in body
    assert b'name="target_users_file"; filename="users.txt"' in body


async def test_form_fields_multipart_requires_flat_json() -> None:
    pool = Pool(response(200, {}))
    rest = client(pool)

    await rest.request_discord(
        "POST",
        "/guilds/1/stickers",
        json={"name": "wave", "description": None, "tags": "hello"},
        files=[{"field": "file", "filename": "wave.png", "data": b"png"}],
        multipart="form_fields",
    )

    body = cast(bytes, pool.requests[0][2]["body"])
    assert b'name="name"' in body
    assert b'name="description"' in body
    assert b'name="tags"' in body
    assert b'name="file"; filename="wave.png"' in body
    assert b"payload_json" not in body
    with pytest.raises(ValidationError, match="flat JSON object"):
        DiscordRequest.model_validate({
            "method": "POST",
            "path": "/guilds/1/stickers",
            "json": {"nested": {"value": True}},
            "files": [{"field": "file", "filename": "a", "data": b"a"}],
            "multipart": "form_fields",
        })
    with pytest.raises(ValidationError, match="part names must be unique"):
        DiscordRequest.model_validate({
            "method": "POST",
            "path": "/guilds/1/stickers",
            "json": {"name": "wave"},
            "files": [{"field": "name", "filename": "a", "data": b"a"}],
            "multipart": "form_fields",
        })
    with pytest.raises(ValidationError, match="part names must be unique"):
        DiscordRequest.model_validate({
            "method": "POST",
            "path": "/channels/1/messages",
            "files": [{"field": "payload_json", "filename": "a", "data": b"a"}],
        })


async def test_bad_gateway_retries_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mocked_sleep = AsyncMock()
    monkeypatch.setattr(discord_module, "sleep", mocked_sleep)
    recovered_pool = Pool(response(502, body=b"bad gateway"), response(200, {}))

    result = await client(recovered_pool).request_discord("GET", "/gateway/bot")

    assert isinstance(result, DiscordPayload)
    assert len(recovered_pool.requests) == 2
    failed_pool = Pool(*(response(502, body=b"bad gateway") for _ in range(5)))
    with pytest.raises(DiscordAPIError, match="502"):
        await client(failed_pool).request_discord("GET", "/gateway/bot")
    assert len(failed_pool.requests) == 5
    assert [call.args[0] for call in mocked_sleep.await_args_list] == [
        1.0,
        1.0,
        2.0,
        5.0,
        10.0,
    ]


async def test_public_gateway_lifecycle_can_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_info = {
        "url": "wss://gateway.discord.example",
        "shards": 1,
        "session_start_limit": {
            "total": 1000,
            "remaining": 10,
            "reset_after": 60_000,
            "max_concurrency": 1,
        },
    }

    def scripted(session_id: str) -> ScriptedWebSocket:
        return ScriptedWebSocket(
            {"op": 10, "d": {"heartbeat_interval": 60_000}},
            {
                "op": 0,
                "s": 1,
                "t": "READY",
                "d": ready_payload(session_id),
            },
            {"op": 0, "s": 2, "t": "MESSAGE_CREATE", "d": message()},
        )

    pool = Pool(response(200, gateway_info), response(200, gateway_info))
    websockets = (scripted("first"), scripted("second"))
    pending = iter(websockets)
    urls: list[str] = []

    async def connect(  # ruff: ignore[unused-async] - async connector test double
        url: str,
        _: dict[str, str] | None,
    ) -> ScriptedWebSocket:
        urls.append(url)
        return next(pending)

    instance = DiscordGateway(
        Bot(),
        token=CREDENTIAL,
        base_url="https://discord.example/api/v10",
        http_pool=cast(AsyncPoolManager, pool),
        websocket_connector=connect,
    )
    ready = Event()
    events: list[object] = []

    def enqueue(event: object) -> None:
        events.append(event)
        if isinstance(event, PrivateMessageEvent):
            ready.set()

    instance.enqueue_event = enqueue  # ty: ignore[invalid-assignment]
    monkeypatch.setattr(instance.bot, "wait_until_running", AsyncMock())
    clock = Clock()
    monkeypatch.setattr(discord_module, "get_running_loop", lambda: clock)
    monkeypatch.setattr(discord_module, "sleep", AsyncMock(side_effect=clock.advance))

    try:
        for _ in websockets:
            ready.clear()
            async with timeout(1):
                await instance.start()
                await ready.wait()
                await instance.close()
    finally:
        async with timeout(1):
            await instance.close()

    assert [event.detail_type for event in events if isinstance(event, MetaEvent)] == [
        "discord.ready",
        "discord.ready",
    ]
    assert sum(isinstance(event, PrivateMessageEvent) for event in events) == 2
    assert urls == ["wss://gateway.discord.example/?v=10&encoding=json"] * 2
    assert [websocket.close_code for websocket in websockets] == [1000, 1000]
    assert [loads(websocket.sent.get_nowait())["op"] for websocket in websockets] == [
        2,
        2,
    ]
    assert instance._task is None
    assert instance._session_id is None
    assert instance._closed


async def test_gateway_start_reaps_finished_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = gateway()
    old_task = create_task(sleep(0))
    await old_task
    instance._task = old_task
    restarted = Event()
    stop = Event()

    async def run() -> None:
        restarted.set()
        await stop.wait()

    monkeypatch.setattr(instance, "_run_gateway", run)
    await instance.start()
    async with timeout(1):
        await restarted.wait()
    assert instance._task is not old_task
    stop.set()
    await instance.close()


async def test_gateway_identify_dispatch_resume_and_raw_fallback() -> None:
    incoming_message = {
        **message(),
        "type": 19,
        "message_reference": {"message_id": "9"},
        "referenced_message": message(message_id="9", content="previous"),
    }
    websocket = ScriptedWebSocket(
        {"op": 10, "d": {"heartbeat_interval": 60_000}},
        {
            "op": 0,
            "s": 1,
            "t": "READY",
            "d": ready_payload(),
        },
        {"op": 0, "s": 2, "t": "MESSAGE_CREATE", "d": incoming_message},
        {"op": 0, "s": 3, "t": "MESSAGE_CREATE", "d": {"id": "broken"}},
        {"op": 7, "d": None},
    )
    instance = gateway()
    events: list[object] = []
    instance.enqueue_event = events.append  # ty: ignore[invalid-assignment]

    async with timeout(1):
        with pytest.raises(ConnectionError, match="reconnect"):
            await instance._read_websocket(websocket)

    identify = loads(websocket.sent.get_nowait())
    assert identify["op"] == 2
    assert identify["d"]["intents"] == 4609
    assert identify["d"]["token"] == CREDENTIAL
    assert isinstance(events[0], MetaEvent)
    assert events[0].detail_type == "discord.ready"
    ready_extra = events[0].model_extra
    assert ready_extra is not None
    assert ready_extra["discord_event_type"] == "READY"
    assert ready_extra["discord_data"]["user"]["global_name"] is None
    assert ready_extra["discord_raw"] is False
    assert isinstance(events[1], PrivateMessageEvent)
    message_extra = events[1].model_extra
    assert message_extra is not None
    assert message_extra["channel_id"] == "20"
    assert message_extra["reply_alt_message"] == "previous"
    assert message_extra["discord_data"]["edited_timestamp"] is None
    assert message_extra["discord_raw"] is False
    assert [segment.type for segment in events[1].message] == [
        "reply",
        "text",
        "mention",
    ]
    assert isinstance(events[2], NoticeEvent)
    notice_extra = events[2].model_extra
    assert notice_extra is not None
    assert notice_extra["discord_data"] == {"id": "broken"}
    assert notice_extra["discord_raw"] is True
    assert instance._seq == 3

    malformed_ready = DiscordGatewayPayload.model_validate({
        "op": 0,
        "s": 4,
        "t": "READY",
        "d": {"v": 10},
    })
    with pytest.raises(ConnectionError, match="READY payload is invalid"):
        await instance._receive_dispatch(malformed_ready)
    assert instance._seq == 3

    resumed = ScriptedWebSocket()
    await instance._authenticate_websocket(resumed)
    assert loads(resumed.sent.get_nowait()) == {
        "op": 6,
        "d": {"token": "token", "session_id": "session", "seq": 3},
    }


def test_message_conversion_distinguishes_forward_and_voice() -> None:
    incoming = DiscordMessage.model_validate({
        **message(),
        "flags": 1 << 13,
        "message_reference": {"type": 1, "message_id": "9"},
        "referenced_message": message(message_id="9", content="forwarded"),
        "attachments": [
            {
                "id": "4",
                "filename": "voice.ogg",
                "content_type": "audio/ogg",
                "size": 4,
                "url": "https://cdn.discord.example/voice.ogg",
                "proxy_url": "https://proxy.discord.example/voice.ogg",
                "duration_secs": 1.0,
                "waveform": "AA==",
            }
        ],
    })

    assert [segment.type for segment in discord_module._discord_message(incoming)] == [
        "text",
        "mention",
        "voice",
    ]


async def test_dispatch_models_commit_only_valid_session_and_rate_state(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG", logger="bot")
    instance = gateway()
    events: list[object] = []
    instance.enqueue_event = events.append  # ty: ignore[invalid-assignment]
    clock = Clock()
    clock.now = 10.0
    monkeypatch.setattr(discord_module, "get_running_loop", lambda: clock)

    for sequence, data in enumerate((None, 1, "invalid", []), start=1):
        await instance._receive_dispatch(
            DiscordGatewayPayload.model_validate({
                "op": 0,
                "s": sequence,
                "t": "RESUMED",
                "d": data,
            })
        )
        assert instance._online is False
        extra = cast(NoticeEvent, events[-1]).model_extra
        assert extra is not None
        assert extra["discord_raw"] is True

    await instance._receive_dispatch(
        DiscordGatewayPayload.model_validate({
            "op": 0,
            "s": 5,
            "t": "RESUMED",
            "d": {},
        })
    )
    assert instance._online is True
    assert cast(MetaEvent, events[-1]).detail_type == "discord.resumed"

    await instance._receive_dispatch(
        DiscordGatewayPayload.model_validate({
            "op": 0,
            "s": 6,
            "t": "RATE_LIMITED",
            "d": {
                "opcode": 8,
                "retry_after": 2.5,
                "meta": {"guild_id": "42", "nonce": "members"},
            },
        })
    )
    assert instance._full_member_ready_at["42"] == pytest.approx(12.5)
    extra = cast(NoticeEvent, events[-1]).model_extra
    assert extra is not None
    assert extra["discord_raw"] is False

    marker = f"sensitive-{id(instance)}"
    await instance._receive_dispatch(
        DiscordGatewayPayload.model_validate({
            "op": 0,
            "s": 7,
            "t": "INTERACTION_CREATE",
            "d": {
                "id": marker,
                "application_id": "11",
                "type": 2,
                "token": "secret",
                "version": 1,
            },
        })
    )
    assert marker not in caplog.text


async def test_gateway_discovery_refetches_and_throttles_identify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def info(remaining: int, *, reset_after: int, max_concurrency: int) -> dict:
        return {
            "url": "wss://gateway.discord.example?compress=zlib-stream&route=stable",
            "shards": 4,
            "session_start_limit": {
                "total": 1000,
                "remaining": remaining,
                "reset_after": reset_after,
                "max_concurrency": max_concurrency,
            },
        }

    clock = Clock()
    mocked_sleep = AsyncMock(side_effect=clock.advance)
    monkeypatch.setattr(discord_module, "get_running_loop", lambda: clock)
    monkeypatch.setattr(discord_module, "sleep", mocked_sleep)
    instance = gateway(
        Pool(
            response(200, info(0, reset_after=1500, max_concurrency=1)),
            response(200, info(10, reset_after=60_000, max_concurrency=2)),
        )
    )

    assert await instance._gateway_url() == (
        "wss://gateway.discord.example/?route=stable&v=10&encoding=json"
    )
    websocket = ScriptedWebSocket()
    await instance._authenticate_websocket(websocket)
    await instance._wait_to_identify()
    await instance._authenticate_websocket(websocket)
    assert instance._identify_ready_at == pytest.approx(11.5)
    assert [call.args[0] for call in mocked_sleep.await_args_list] == [1.5, 5.0]

    instance._session_id = "session"
    instance._seq = 1
    await instance._authenticate_websocket(websocket)
    assert [call.args[0] for call in mocked_sleep.await_args_list] == [1.5, 5.0]
    assert [loads(websocket.sent.get_nowait())["op"] for _ in range(3)] == [
        2,
        2,
        6,
    ]

    discovery_pool = Pool(
        response(200, info(10, reset_after=60_000, max_concurrency=1))
    )
    resuming = gateway(discovery_pool)
    resuming._session_id = "session"
    resuming._seq = 1
    resuming._resume_gateway_url = "wss://resume.discord.example"
    attempts: list[str] = []
    connections: list[ScriptedWebSocket] = []

    async def reconnecting(  # ruff: ignore[unused-async] - async connector test double
        url: str,
        _: dict[str, str] | None,
    ) -> ScriptedWebSocket:
        attempts.append(url)
        if url.startswith("wss://resume.discord.example"):
            if len(attempts) > discord_module._MAX_RESUME_ATTEMPTS + 1:
                raise DiscordGatewayFatalError
            websocket = ScriptedWebSocket(
                {"op": 10, "d": {"heartbeat_interval": 60_000}},
                {"op": 11, "d": None},
                {"op": 9, "d": True},
            )
        else:
            resuming._closing = True
            websocket = ScriptedWebSocket(
                {"op": 10, "d": {"heartbeat_interval": 60_000}},
                {"op": 7, "d": None},
            )
        connections.append(websocket)
        return websocket

    monkeypatch.setattr(resuming.bot, "wait_until_running", AsyncMock())
    monkeypatch.setattr(resuming, "_websocket_connector", reconnecting)
    async with timeout(1):
        await resuming._run_gateway()

    resume_url = "wss://resume.discord.example/?v=10&encoding=json"
    assert attempts == [resume_url] * (discord_module._MAX_RESUME_ATTEMPTS + 1) + [
        "wss://gateway.discord.example/?route=stable&v=10&encoding=json"
    ]
    assert discovery_pool.requests[0][1].endswith("/gateway/bot")
    assert [loads(item.sent.get_nowait())["op"] for item in connections] == [
        *([6] * (discord_module._MAX_RESUME_ATTEMPTS + 1)),
        2,
    ]
    assert resuming._session_id is None


async def test_gateway_native_limits_and_intent_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    mocked_sleep = AsyncMock(side_effect=clock.advance)
    monkeypatch.setattr(discord_module, "get_running_loop", lambda: clock)
    monkeypatch.setattr(discord_module, "sleep", mocked_sleep)
    instance = gateway()
    websocket = ScriptedWebSocket()
    instance._websocket = websocket

    send = instance._send_gateway
    at_limit = {"op": 1, "d": "x" + "é" * 2040}
    await send(websocket, at_limit, system=True)
    assert len(websocket.sent.get_nowait().encode()) == 4096
    with pytest.raises(ValueError, match="exceeds 4096 bytes"):
        await send(websocket, {"op": 1, "d": "xx" + "é" * 2040}, system=True)
    instance._gateway_send_times.clear()

    for _ in range(6):
        await instance._send_gateway(websocket, {"op": 3, "d": {}})
    assert [call.args[0] for call in mocked_sleep.await_args_list] == [20.0]

    full_members = {
        "op": 8,
        "d": {"guild_id": "1", "query": "", "limit": 0},
    }
    await instance._send_gateway(websocket, full_members)
    # A new connection resets only per-connection limits.
    instance._gateway_send_times.clear()
    instance._presence_send_times.clear()
    await instance._send_gateway(websocket, full_members)
    await instance._send_gateway(
        websocket,
        {"op": 8, "d": {"guild_id": "2", "query": "", "limit": 0}},
    )
    await instance._send_gateway(
        websocket,
        {"op": 8, "d": {"guild_id": "1", "query": "a", "limit": 1}},
    )
    assert [call.args[0] for call in mocked_sleep.await_args_list] == [20.0, 30.0]

    connection = instance.connection_for(instance._self)
    with pytest.raises(ValueError, match="GUILD_MEMBERS"):
        await instance.request_action(
            connection,
            "discord.gateway",
            ActionParamModel.model_validate({
                "opcode": 8,
                "data": {"guild_id": "1", "query": "", "limit": 0},
            }),
        )
    with pytest.raises(ValueError, match="GUILD_PRESENCES"):
        await instance.request_action(
            connection,
            "discord.gateway",
            ActionParamModel.model_validate({
                "opcode": 8,
                "data": {
                    "guild_id": "1",
                    "query": "a",
                    "limit": 1,
                    "presences": True,
                },
            }),
        )
    with pytest.raises(ValidationError):
        await instance.request_action(
            connection,
            "discord.gateway",
            ActionParamModel.model_validate({
                "opcode": 3,
                "data": {
                    "since": None,
                    "activities": [{"name": "invalid", "type": 6}],
                    "status": "online",
                    "afk": False,
                },
            }),
        )

    commands = ScriptedWebSocket()
    instance._websocket = commands
    instance._online = True
    presence = {"since": None, "activities": [], "status": "online", "afk": False}
    voice = {
        "guild_id": "1",
        "channel_id": None,
        "self_mute": False,
        "self_deaf": False,
    }
    await connection.action(
        "discord.gateway",
        opcode=3,
        data=presence,
    )
    await connection.action(
        "discord.gateway",
        opcode=4,
        data=voice,
    )
    channel_info = {"guild_id": "1", "fields": ["status"] * 3}
    await connection.action(
        "discord.gateway",
        opcode=43,
        data=channel_info,
    )
    assert [loads(commands.sent.get_nowait()) for _ in range(3)] == [
        {"op": 3, "d": presence},
        {"op": 4, "d": voice},
        {"op": 43, "d": channel_info},
    ]


async def test_reconnect_heartbeat_and_shutdown_close_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reconnect = ScriptedWebSocket({"op": 7, "d": None})
    async with timeout(1):
        with pytest.raises(ConnectionError, match="before Hello"):
            await gateway()._serve_websocket(reconnect)
    assert reconnect.close_code == 4000

    malformed = ScriptedWebSocket({"op": 11, "d": None})
    async with timeout(1):
        with pytest.raises(ValueError, match="expected Hello"):
            await gateway()._serve_websocket(malformed)
    assert malformed.close_code == 4000

    monkeypatch.setattr(discord_module, "random", lambda: 0.0)
    acknowledged = ScriptedWebSocket({"op": 10, "d": {"heartbeat_interval": 60_000}})
    instance = gateway()
    instance._retry_count = 4

    async def serve() -> None:
        with pytest.raises(ConnectionError, match="requested reconnect"):
            await instance._serve_websocket(acknowledged)

    async def drive() -> None:
        sent = [loads(await acknowledged.sent.get()) for _ in range(2)]
        acknowledged.feed({"op": 1, "d": None})
        sent.append(loads(await acknowledged.sent.get()))
        acknowledged.feed({"op": 11, "d": None})
        acknowledged.feed({"op": 7, "d": None})
        assert [payload["op"] for payload in sent] == [2, 1, 1]

    async with timeout(1), TaskGroup() as tasks:
        tasks.create_task(serve())
        tasks.create_task(drive())
    assert instance._retry_count == 4
    assert acknowledged.close_code == 4000

    missed = ScriptedWebSocket({"op": 10, "d": {"heartbeat_interval": 1}})
    with pytest.raises(ConnectionError) as error:
        async with timeout(1):
            await gateway()._serve_websocket(missed)
    assert error.value.__cause__ is not None
    assert "not acknowledged" in str(error.value.__cause__)
    assert [loads(missed.sent.get_nowait())["op"] for _ in range(2)] == [2, 1]
    assert missed.close_code == 4000

    shutdown = ScriptedWebSocket({"op": 10, "d": {"heartbeat_interval": 60_000}})
    task = create_task(gateway()._serve_websocket(shutdown))
    try:
        async with timeout(1):
            assert loads(await shutdown.sent.get())["op"] == 2
            await shutdown.receiving.wait()
    finally:
        task.cancel()
        async with timeout(1):
            with pytest.raises(CancelledError):
                await task
    assert shutdown.close_code == 1000


async def test_server_heartbeat_request_preserves_the_periodic_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    waits: list[float] = []

    class AdvancingWebSocket(ScriptedWebSocket):
        calls = 0

        async def receive_text(self) -> str:
            self.calls += 1
            if self.calls == 2:
                clock.now = 8.0
            return await super().receive_text()

    @asynccontextmanager
    async def capture_timeout(delay: float) -> AsyncIterator[None]:
        waits.append(delay)
        yield

    websocket = AdvancingWebSocket(
        {"op": 10, "d": {"heartbeat_interval": 10_000}},
        {"op": 1, "d": None},
        {"op": 7, "d": None},
    )
    monkeypatch.setattr(discord_module, "get_running_loop", lambda: clock)
    monkeypatch.setattr(discord_module, "random", lambda: 0.9)
    monkeypatch.setattr(discord_module, "timeout", capture_timeout)

    async with timeout(1):
        with pytest.raises(ConnectionError, match="requested reconnect"):
            await gateway()._read_websocket(websocket)

    assert waits == [30.0, 9.0, 1.0]


@pytest.mark.parametrize(
    ("can_resume", "close_code"),
    [(True, 4000), (False, 1000)],
)
async def test_invalid_session_resume_policy(
    can_resume: bool,
    close_code: int,
) -> None:
    websocket = ScriptedWebSocket(
        {"op": 10, "d": {"heartbeat_interval": 60_000}},
        {"op": 9, "d": can_resume},
    )
    async with timeout(1):
        with pytest.raises(ConnectionError, match="session is invalid") as error:
            await gateway()._serve_websocket(websocket)
    reconnect = cast(discord_module._ReconnectError, error.value)
    assert reconnect.reset_session is not can_resume
    assert websocket.close_code == close_code


def test_gateway_server_close_code_policy() -> None:
    def disconnect(code: int) -> ConnectionError:
        return gateway()._disconnect_error(WebSocketClosedError(code))

    for code in (4004, 4010, 4011, 4012, 4013, 4014):
        assert isinstance(disconnect(code), DiscordGatewayFatalError)
    for code in (4003, 4007, 4009):
        reconnect = cast(discord_module._ReconnectError, disconnect(code))
        assert reconnect.reset_session is True
    rate_limited = cast(discord_module._ReconnectError, disconnect(4008))
    assert rate_limited.reset_session is False
    assert rate_limited.delay is not None
    assert rate_limited.delay > 0
    reconnect = cast(discord_module._ReconnectError, disconnect(1006))
    assert reconnect.reset_session is False
    unknown = gateway()._disconnect_error(ConnectionError())
    assert "close code None" in str(unknown)


async def test_sequence_commit_and_public_message_actions() -> None:
    pool = Pool(response(200, message(message_id="99", content="reply")), response(204))
    instance = gateway(pool)
    instance._seq = 7

    def full(_: object) -> None:
        raise QueueFull

    instance.enqueue_event = full  # ty: ignore[invalid-assignment]
    payload = DiscordGatewayPayload.model_validate({
        "op": 0,
        "s": 8,
        "t": "MESSAGE_CREATE",
        "d": message(guild_id="30"),
    })
    with pytest.raises(ConnectionError, match="queue is full"):
        await instance._receive_dispatch(payload)
    assert instance._seq == 7

    connection = instance.connection_for(instance._self)
    sent = await connection.send_msg(
        "reply",
        user_id="2",
        channel_id="4",
        message_id="3",
    )
    deleted = await connection.action(
        "delete_message",
        channel_id="4",
        message_id="99",
    )
    assert isinstance(sent, DiscordMessage)
    assert isinstance(deleted, DiscordNoContent)
    assert pool.requests[0][0:2] == (
        "POST",
        "https://discord.example/api/v10/channels/4/messages",
    )
    assert pool.requests[0][2]["json"] == {
        "content": "reply",
        "allowed_mentions": {"parse": [], "users": [], "replied_user": False},
        "message_reference": {"message_id": "3", "fail_if_not_exists": False},
    }
    assert pool.requests[1][0:2] == (
        "DELETE",
        "https://discord.example/api/v10/channels/4/messages/99",
    )

    wrong = instance.connection_for(BotSelf(platform="discord", user_id="wrong"))
    with pytest.raises(ValueError, match="wrong BotSelf"):
        await instance.request_action(
            wrong,
            "get_supported_actions",
            ActionParamModel(),
        )


async def test_public_lookup_actions_map_endpoints_and_models() -> None:
    guild = {"id": "10", "name": "guild", "icon": None, "features": []}
    member = {"user": user(), "roles": []}
    channel = {"id": "20", "type": 0}
    cases = (
        (
            "get_self_info",
            {},
            response(200, user("1")),
            DiscordUser,
            "GET",
            "/users/@me",
        ),
        (
            "get_user_info",
            {"user_id": "2"},
            response(200, user()),
            DiscordUser,
            "GET",
            "/users/2",
        ),
        (
            "get_guild_list",
            {},
            response(200, [guild]),
            DiscordGuildList,
            "GET",
            "/users/@me/guilds?limit=200",
        ),
        (
            "get_guild_member_info",
            {"guild_id": "10", "user_id": "2"},
            response(200, member),
            DiscordGuildMember,
            "GET",
            "/guilds/10/members/2",
        ),
        (
            "get_guild_member_list",
            {"guild_id": "10"},
            response(200, [member]),
            DiscordMemberList,
            "GET",
            "/guilds/10/members?limit=1000",
        ),
        (
            "leave_guild",
            {"guild_id": "10"},
            response(204),
            DiscordNoContent,
            "DELETE",
            "/users/@me/guilds/10",
        ),
        (
            "get_channel_info",
            {"guild_id": "10", "channel_id": "20"},
            response(200, channel),
            DiscordChannel,
            "GET",
            "/channels/20",
        ),
        (
            "get_channel_list",
            {"guild_id": "10"},
            response(200, [channel]),
            DiscordChannelList,
            "GET",
            "/guilds/10/channels",
        ),
    )
    pool = Pool(*(reply for _, _, reply, _, _, _ in cases))
    instance = gateway(pool)
    connection = instance.connection_for(instance._self)

    results = [
        await connection.action(action, **params)
        for action, params, _, _, _, _ in cases
    ]

    assert [type(result) for result in results] == [
        expected for _, _, _, expected, _, _ in cases
    ]
    assert [
        (method, url.removeprefix("https://discord.example/api/v10"))
        for method, url, _ in pool.requests
    ] == [(method, path) for _, _, _, _, method, path in cases]


@pytest.mark.parametrize(
    ("action", "data"),
    [
        ("set_guild_name", {"guild_id": "1", "guild_name": "x"}),
        ("set_guild_name", {"guild_id": "1", "guild_name": " guild"}),
        ("set_channel_name", {"channel_id": "1", "channel_name": ""}),
        ("set_channel_name", {"channel_id": "1", "channel_name": "x" * 101}),
    ],
)
async def test_common_name_actions_validate_discord_boundaries(
    action: str,
    data: dict[str, object],
) -> None:
    instance = gateway()

    with pytest.raises(ValidationError):
        await instance._common_action(action, data)


def test_message_model_is_strict_but_accepts_new_fields() -> None:
    parsed = DiscordMessage.model_validate({**message(), "future_field": True})
    assert parsed.model_extra == {"future_field": True}
    with pytest.raises(ValidationError):
        DiscordMessage.model_validate({**message(), "tts": 0})


async def test_interaction_fallback_survives_a_full_event_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = Pool(response(204))
    instance = gateway(pool)

    def full(_: object) -> None:
        raise QueueFull

    instance.enqueue_event = full  # ty: ignore[invalid-assignment]
    monkeypatch.setattr(discord_module, "_INTERACTION_AUTO_ACK_DELAY", 0.0)
    try:
        with pytest.raises(ConnectionError, match="queue is full"):
            await instance._receive_dispatch(interaction())

        pending = next(iter(instance._interaction_callbacks.values()))
        assert pending.task is not None
        async with timeout(1):
            await pending.task
        assert instance._seq is None
        _, url, kwargs = pool.requests[0]
        assert url.endswith(f"/interactions/10/{CREDENTIAL}/callback")
        assert kwargs["json"] == {"type": 5}
        assert "Authorization" not in cast(dict[str, str], kwargs["headers"])
        assert len(pool.requests) == 1
    finally:
        await instance.close()


async def test_interaction_fallback_io_obeys_the_absolute_deadline() -> None:
    class BlockingPool(Pool):
        async def request(
            self,
            method: str,
            url: str,
            **kwargs: object,
        ) -> AsyncHTTPResponse:
            self.requests.append((method, url, kwargs))
            await Event().wait()
            return response(204)

    pool = BlockingPool()
    instance = gateway(pool)
    path = "/interactions/1/secret/callback"
    deadline = discord_module.get_running_loop().time() + 0.01
    pending = discord_module._DiscordInteractionCallback({"type": 5}, deadline)
    instance._interaction_callbacks[path] = pending
    pending.task = create_task(instance._run_interaction_callback(path, pending))
    async with timeout(1):
        await pending.task
    assert len(pool.requests) == 1
    assert instance._interaction_callbacks == {}


async def test_interaction_callbacks_do_not_start_after_deadlines() -> None:
    pool = Pool(response(204))
    instance = gateway(pool)
    now = discord_module.get_running_loop().time()
    path = "/interactions/1/secret/callback"
    pending = discord_module._DiscordInteractionCallback(
        {"type": 5},
        now + 0.5,
        request=DiscordRequest(method="POST", path=path, json={"type": 4}),
        outcome=discord_module.Future(),
    )
    pending.ready.set()

    await instance._run_interaction_callback(path, pending)

    assert pending.outcome is not None
    with pytest.raises(TimeoutError):
        pending.outcome.result()
    assert [request[2]["json"] for request in pool.requests] == [{"type": 5}]

    expired = discord_module._DiscordInteractionCallback({"type": 5}, now - 1)
    await instance._run_interaction_callback(path, expired)
    assert len(pool.requests) == 1


async def test_dynamic_buckets_coordinate_lanes_per_major_resource() -> None:
    interaction = DiscordRequest(method="POST", path="/interactions/1/token/callback")
    assert DiscordGateway._rate_route(interaction)[1] == ""

    class BucketPool(Pool):
        def __init__(self) -> None:
            super().__init__()
            self.blocking = False
            self.active = 0
            self.max_active = 0
            self.first_started = Event()
            self.both_started = Event()
            self.release = Event()

        async def request(
            self,
            method: str,
            url: str,
            **kwargs: object,
        ) -> AsyncHTTPResponse:
            self.requests.append((method, url, kwargs))
            headers = {
                "X-RateLimit-Bucket": "shared-bucket",
                "X-RateLimit-Remaining": "1",
            }
            if not self.blocking:
                return response(200, {}, headers=headers)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.first_started.set()
            if self.active == 2:
                self.both_started.set()
            try:
                await self.release.wait()
            finally:
                self.active -= 1
            return response(200, {}, headers=headers)

    pool = BucketPool()
    rest = client(pool)
    await rest.request_discord("GET", "/channels/1/messages/10")
    await rest.request_discord("GET", "/channels/1/pins/10", auth=False)
    second_entered = Event()

    async def second_request() -> None:
        second_entered.set()
        await rest.request_discord("GET", "/channels/1/pins/11", auth=False)

    pool.blocking = True
    async with timeout(1), TaskGroup() as tasks:
        tasks.create_task(rest.request_discord("GET", "/channels/1/messages/11"))
        await pool.first_started.wait()
        tasks.create_task(second_request())
        await second_entered.wait()
        assert pool.max_active == 1
        pool.release.set()

    pool.active = 0
    pool.max_active = 0
    pool.first_started.clear()
    pool.both_started.clear()
    pool.release.clear()
    async with timeout(1), TaskGroup() as tasks:
        tasks.create_task(rest.request_discord("GET", "/channels/1/messages/12"))
        tasks.create_task(rest.request_discord("GET", "/channels/2/pins/12"))
        await pool.both_started.wait()
        assert pool.max_active == 2
        pool.release.set()


async def test_rest_caches_are_bounded_and_programming_errors_stay_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BucketPool(Pool):
        async def request(
            self,
            method: str,
            url: str,
            **kwargs: object,
        ) -> AsyncHTTPResponse:
            self.requests.append((method, url, kwargs))
            return response(
                200,
                {},
                headers={"X-RateLimit-Bucket": "invites"},
            )

    monkeypatch.setattr(discord_module, "_RATE_BUCKET_PRUNE_THRESHOLD", 2)
    rest = client(BucketPool())
    for index in range(discord_module._RATE_BUCKET_PRUNE_THRESHOLD + 5):
        await rest.request_discord("GET", f"/invites/code-{index}")
    assert len(rest._route_buckets) == discord_module._RATE_BUCKET_PRUNE_THRESHOLD
    assert len(rest._rate_buckets) == 1

    class FailingPool(Pool):
        def __init__(self, error: Exception) -> None:
            super().__init__()
            self.error = error

        async def request(
            self,
            method: str,
            url: str,
            **kwargs: object,
        ) -> AsyncHTTPResponse:
            self.requests.append((method, url, kwargs))
            raise self.error

    marker = f"token-{id(rest)}"
    with pytest.raises(ValueError, match="programming error"):
        await client(FailingPool(ValueError("programming error"))).request_discord(
            "GET", "/gateway/bot"
        )
    with pytest.raises(ConnectionError) as error:
        await client(FailingPool(HTTPError(marker))).request_discord(
            "GET", "/gateway/bot"
        )
    assert marker not in str(error.value)
    assert error.value.__cause__ is None


async def test_bot_global_limit_does_not_block_interactions_or_timeout_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class GlobalPool(Pool):
        def __init__(self) -> None:
            super().__init__()
            self.bot_attempts = 0
            self.authless_attempts = 0
            self.bot_rate_limited = Event()
            self.authless_rate_limited = Event()
            self.bot_interaction = Event()
            self.authless_interaction = Event()

        async def request(
            self,
            method: str,
            url: str,
            **kwargs: object,
        ) -> AsyncHTTPResponse:
            self.requests.append((method, url, kwargs))
            if url.endswith("/gateway/bot"):
                self.bot_attempts += 1
                if self.bot_attempts == 1:
                    self.bot_rate_limited.set()
                    return response(
                        429,
                        {"retry_after": 0.05, "global": True},
                    )
                return response(200, {})
            if url.endswith("/webhooks/1/secret"):
                self.authless_attempts += 1
                if self.authless_attempts == 1:
                    self.authless_rate_limited.set()
                    return response(
                        429,
                        {"retry_after": 0.05, "global": True},
                    )
                return response(200, {})
            if "/interactions/" in url:
                if "/interactions/1/" in url:
                    assert self.bot_attempts == 1
                    self.bot_interaction.set()
                else:
                    assert self.authless_attempts == 1
                    self.authless_interaction.set()
                return response(204)
            return response(200, {})

    monkeypatch.setattr(discord_module, "_API_TIMEOUT", 0.01)
    pool = GlobalPool()
    rest = client(pool)
    async with timeout(1), TaskGroup() as tasks:
        authenticated = tasks.create_task(rest.request_discord("GET", "/gateway/bot"))
        await pool.bot_rate_limited.wait()
        interaction = tasks.create_task(
            rest.request_discord(
                "POST",
                "/interactions/1/secret/callback",
                json={"type": 5},
            )
        )
        await pool.bot_interaction.wait()
        interaction_response = await interaction
        authenticated_response = await authenticated
    assert isinstance(interaction_response, DiscordNoContent)
    assert isinstance(authenticated_response, DiscordPayload)

    async with timeout(1), TaskGroup() as tasks:
        authless = tasks.create_task(rest.request_discord("GET", "/webhooks/1/secret"))
        await pool.authless_rate_limited.wait()
        interaction = tasks.create_task(
            rest.request_discord(
                "POST",
                "/interactions/2/secret/callback",
                json={"type": 5},
            )
        )
        await pool.authless_interaction.wait()
        second_interaction = await interaction
        authless_response = await authless
    assert isinstance(second_interaction, DiscordNoContent)
    assert isinstance(authless_response, DiscordPayload)

    await rest.request_discord("GET", "/webhooks/1")
    token_headers = cast(
        dict[str, str],
        next(
            kwargs["headers"]
            for _, url, kwargs in pool.requests
            if url.endswith("/webhooks/1/secret")
        ),
    )
    bot_headers = cast(dict[str, str], pool.requests[-1][2]["headers"])
    assert "Authorization" not in token_headers
    assert bot_headers["Authorization"] == "Bot token"


async def test_proactive_global_limit_exempts_interactions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()

    class ExpiringTimeout:
        def __init__(self, delay: float) -> None:
            self.delay = delay

        async def __aenter__(self) -> None:
            clock.now += self.delay
            raise TimeoutError

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: object,
        ) -> None:
            pass

    monkeypatch.setattr(discord_module, "get_running_loop", lambda: clock)
    monkeypatch.setattr(discord_module, "timeout", ExpiringTimeout)
    rest = client(Pool())

    for _ in range(discord_module._MAX_GLOBAL_REST_REQUESTS + 1):
        await rest._wait_for_global_limit("bot")
    assert clock.now == pytest.approx(1.0)
    for _ in range(100):
        await rest._wait_for_global_limit("interaction")
    assert clock.now == pytest.approx(1.0)


async def test_explicit_interaction_response_has_one_owner() -> None:
    pool = Pool(response(204))
    instance = gateway(pool)
    instance.enqueue_event = lambda _: None  # ty: ignore[invalid-assignment]
    path = f"/interactions/10/{CREDENTIAL}/callback"

    try:
        await instance._receive_dispatch(interaction())
        pending = instance._interaction_callbacks[path]
        assert pending.task is not None
        connection = instance.connection_for(instance._self)
        result = await connection.action(
            "discord.request",
            method="POST",
            path=path,
            json={"type": 4},
        )
        await pending.task
        assert isinstance(result, DiscordNoContent)
        assert pool.requests[0][2]["json"] == {"type": 4}
        assert len(pool.requests) == 1
        assert instance._interaction_callbacks == {}
    finally:
        await instance.close()


@pytest.mark.parametrize("cancel_caller", [False, True])
async def test_failed_or_cancelled_interaction_response_falls_back(
    cancel_caller: bool,
) -> None:
    started = Event()
    release = Event()

    class FailingOncePool(Pool):
        async def request(
            self,
            method: str,
            url: str,
            **kwargs: object,
        ) -> AsyncHTTPResponse:
            self.requests.append((method, url, kwargs))
            if len(self.requests) == 1:
                started.set()
                await release.wait()
                msg = "failed"
                raise HTTPError(msg)
            return response(204)

    pool = FailingOncePool()
    instance = gateway(pool)
    instance.enqueue_event = lambda _: None  # ty: ignore[invalid-assignment]
    path = f"/interactions/10/{CREDENTIAL}/callback"

    try:
        await instance._receive_dispatch(interaction())
        pending = instance._interaction_callbacks[path]
        assert pending.task is not None
        connection = instance.connection_for(instance._self)
        caller = create_task(
            connection.action(
                "discord.request",
                method="POST",
                path=path,
                json={"type": 4},
            )
        )
        async with timeout(1):
            await started.wait()
        if cancel_caller:
            caller.cancel()
            with pytest.raises(CancelledError):
                await caller
        release.set()
        if not cancel_caller:
            with pytest.raises(ConnectionError, match="transport failed"):
                await caller
        async with timeout(1):
            await pending.task
        assert [request[2]["json"] for request in pool.requests] == [
            {"type": 4},
            {"type": 5},
        ]
        assert instance._interaction_callbacks == {}
    finally:
        await instance.close()


async def test_slow_interaction_response_is_cancelled_then_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = Event()

    class BlockingPool(Pool):
        async def request(
            self,
            method: str,
            url: str,
            **kwargs: object,
        ) -> AsyncHTTPResponse:
            self.requests.append((method, url, kwargs))
            if len(self.requests) == 1:
                started.set()
                await Event().wait()
            return response(204)

    pool = BlockingPool()
    instance = gateway(pool)
    instance.enqueue_event = lambda _: None  # ty: ignore[invalid-assignment]
    monkeypatch.setattr(discord_module, "_INTERACTION_AUTO_ACK_DELAY", 0.01)
    path = f"/interactions/10/{CREDENTIAL}/callback"

    try:
        await instance._receive_dispatch(interaction())
        pending = instance._interaction_callbacks[path]
        connection = instance.connection_for(instance._self)
        async with timeout(1):
            with pytest.raises(TimeoutError):
                await connection.action(
                    "discord.request",
                    method="POST",
                    path=path,
                    json={"type": 4},
                )
        assert pending.task is not None
        await pending.task
        assert [request[2]["json"] for request in pool.requests] == [
            {"type": 4},
            {"type": 5},
        ]
        assert started.is_set()
    finally:
        await instance.close()


async def test_close_cancels_owner_and_waiting_interaction_response() -> None:
    started = Event()
    finished = Event()

    class BlockingPool(Pool):
        async def request(
            self,
            method: str,
            url: str,
            **kwargs: object,
        ) -> AsyncHTTPResponse:
            self.requests.append((method, url, kwargs))
            started.set()
            try:
                await Event().wait()
            finally:
                finished.set()
            return response(204)

    pool = BlockingPool()
    instance = gateway(pool)
    instance.enqueue_event = lambda _: None  # ty: ignore[invalid-assignment]
    path = f"/interactions/10/{CREDENTIAL}/callback"

    try:
        await instance._receive_dispatch(interaction())
        pending = instance._interaction_callbacks[path]
        assert pending.task is not None
        response_task = create_task(
            instance.connection_for(instance._self).action(
                "discord.request",
                method="POST",
                path=path,
                json={"type": 4},
            )
        )
        async with timeout(1):
            await started.wait()
            await instance.close()
        with pytest.raises(CancelledError):
            await response_task
        assert pending.task.cancelled()
        assert finished.is_set()
        assert instance._interaction_callbacks == {}
        assert len(pool.requests) == 1
    finally:
        await instance.close()


async def test_close_waits_for_all_inflight_requests() -> None:
    class BlockingPool(Pool):
        def __init__(self) -> None:
            super().__init__()
            self.started = 0
            self.both_started = Event()
            self.release = Event()

        async def request(
            self,
            method: str,
            url: str,
            **kwargs: object,
        ) -> AsyncHTTPResponse:
            self.requests.append((method, url, kwargs))
            self.started += 1
            if self.started == 2:
                self.both_started.set()
            await self.release.wait()
            return response(204)

    pool = BlockingPool()
    rest = client(pool)
    closing = Event()

    async def close() -> None:
        closing.set()
        await rest.close()

    async with timeout(1), TaskGroup() as tasks:
        interaction_task = tasks.create_task(
            rest.request_discord(
                "POST",
                "/interactions/1/secret/callback",
            )
        )
        authenticated_task = tasks.create_task(
            rest.request_discord("GET", "/gateway/bot")
        )
        await pool.both_started.wait()
        close_task = tasks.create_task(close())
        await closing.wait()
        assert rest._accepting_requests is False
        assert not close_task.done()

        pool.release.set()
        assert isinstance(await interaction_task, DiscordNoContent)
        assert isinstance(await authenticated_task, DiscordNoContent)
        await close_task
    assert rest._interaction_callbacks == {}
    assert not pool.cleared


async def test_close_waits_for_request_not_its_caller() -> None:
    class BlockingPool(Pool):
        def __init__(self) -> None:
            super().__init__(response(200, {}))
            self.request_started = Event()
            self.release_request = Event()

        async def request(
            self,
            method: str,
            url: str,
            **kwargs: object,
        ) -> AsyncHTTPResponse:
            self.request_started.set()
            await self.release_request.wait()
            return await super().request(method, url, **kwargs)

    pool = BlockingPool()
    rest = client(pool)
    request_done = Event()
    release_caller = Event()

    async def caller() -> None:
        await rest.request_discord("GET", "/gateway/bot")
        request_done.set()
        await release_caller.wait()

    async with timeout(1), TaskGroup() as tasks:
        caller_task = tasks.create_task(caller())
        await pool.request_started.wait()
        close_task = tasks.create_task(rest.close())
        await sleep(0)
        assert rest._accepting_requests is False
        assert not close_task.done()
        pool.release_request.set()
        await request_done.wait()
        await close_task
        assert not caller_task.done()
        release_caller.set()


async def test_close_interrupts_rate_limit_wait() -> None:
    pool = Pool(response(200, {}))
    rest = client(pool)
    request = DiscordRequest.model_validate({
        "method": "GET",
        "path": "/gateway/bot",
    })
    bucket = rest._rate_bucket(request)[2]
    bucket.ready_at = discord_module.get_running_loop().time() + 3600
    started = Event()

    async def blocked_request() -> None:
        started.set()
        with pytest.raises(RuntimeError, match="unavailable"):
            await rest.request_discord("GET", "/gateway/bot")

    async with timeout(1), TaskGroup() as tasks:
        tasks.create_task(blocked_request())
        await started.wait()
        assert bucket.lock.locked()
        await rest.close()
    assert pool.requests == []


async def test_unauthorized_owned_client_still_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = Pool(response(401, {"code": 0, "message": "unauthorized"}))
    monkeypatch.setattr(discord_module, "AsyncPoolManager", lambda: pool)
    rest = discord_module.DiscordRestClient(
        CREDENTIAL,
        base_url="https://discord.example/api/v10",
    )

    with pytest.raises(DiscordAPIError, match="401"):
        await rest.request_discord("GET", "/users/@me")
    with pytest.raises(RuntimeError, match="authentication failure"):
        await rest.request_discord("GET", "/gateway/bot")
    await rest.close()

    assert len(pool.requests) == 1
    assert pool.cleared is True


@pytest.mark.parametrize("restart", [False, True], ids=["close", "start"])
async def test_owned_client_retries_failed_close(
    monkeypatch: pytest.MonkeyPatch,
    *,
    restart: bool,
) -> None:
    class FlakyPool(Pool):
        clear_calls = 0

        async def clear(self) -> None:
            self.clear_calls += 1
            if self.clear_calls == 1:
                msg = "clear failed"
                raise RuntimeError(msg)
            await super().clear()

    old_pool = FlakyPool()
    new_pool = Pool()
    monkeypatch.setattr(
        discord_module,
        "AsyncPoolManager",
        lambda: new_pool if old_pool.cleared else old_pool,
    )
    rest = discord_module.DiscordRestClient(
        CREDENTIAL,
        base_url="https://discord.example/api/v10",
    )

    with pytest.raises(RuntimeError, match="clear failed"):
        await rest.close()
    assert not rest._closed

    await (rest.start() if restart else rest.close())

    assert old_pool.clear_calls == 2
    assert rest._closed is not restart
    if restart:
        assert rest.http_pool is new_pool
        await rest.close()
        assert new_pool.cleared
