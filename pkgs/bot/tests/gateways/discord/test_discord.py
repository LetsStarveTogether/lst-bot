# ruff: file-ignore[private-member-access]
from asyncio import (
    CancelledError,
    Event,
    QueueFull,
    Task,
    TaskGroup,
    create_task,
    gather,
    sleep,
    timeout,
    wait,
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
from bot.protocol.actions import ActionParamInput, ActionParamModel
from bot_test_support import ScriptedWebSocket
from pydantic import BaseModel, ValidationError
from urllib3_future import AsyncHTTPResponse, AsyncPoolManager
from urllib3_future.exceptions import HTTPError

from tests.gateways.support import HangingBodyResponse, response

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
    for token in (1, True, object()):
        with pytest.raises(ValueError, match="Discord bot token"):
            discord_module.DiscordRestClient(
                cast(str, token),
                http_pool=cast(AsyncPoolManager, Pool()),
            )
    with pytest.raises(ValueError, match="invalid value"):
        DiscordIntent(1 << 19)
    with pytest.raises(ValidationError):
        DiscordGatewayPayload.model_validate({"op": True, "d": None})
    with pytest.raises(ValidationError):
        DiscordMessage.model_validate({**message(message_id="01")})
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
        "application": {"id": "3", "flags": 0, "flags_new": "0"},
    }
    assert DiscordReady.model_validate(ready).guilds[0].id == "2"
    with pytest.raises(ValidationError):
        DiscordReady.model_validate({
            **ready,
            "guilds": [{"id": "2", "unavailable": 1}],
        })
    with pytest.raises(ValidationError):
        DiscordReady.model_validate({**ready, "guilds": [{"id": "2"}]})
    with pytest.raises(ValidationError):
        DiscordReady.model_validate({**ready, "shard": [1, 1]})
    with pytest.raises(ValidationError):
        DiscordReady.model_validate({
            **ready,
            "application": {"id": "3", "flags": 0, "flags_new": {}},
        })
    with pytest.raises(ValidationError):
        DiscordGuildMemberEvent.model_validate({"guild_id": "1", "roles": []})


def test_resource_models_follow_official_wire_contract() -> None:
    for field in ("global_name", "avatar"):
        payload = user()
        payload.pop(field)
        with pytest.raises(ValidationError):
            DiscordUser.model_validate(payload)

    role = {
        "id": "1",
        "name": "role",
        "color": 0,
        "colors": {
            "primary_color": 0,
            "secondary_color": None,
            "tertiary_color": None,
        },
        "hoist": False,
        "position": 0,
        "permissions": "0",
        "managed": False,
        "mentionable": False,
        "flags": 0,
    }
    assert discord_module.DiscordRole.model_validate(role).colors.primary_color == 0
    with pytest.raises(ValidationError, match="primary_color"):
        discord_module.DiscordRole.model_validate(role | {"color": 1})
    for field in role:
        payload = role.copy()
        payload.pop(field)
        with pytest.raises(ValidationError):
            discord_module.DiscordRole.model_validate(payload)
    for field in ("primary_color", "secondary_color", "tertiary_color"):
        colors = role["colors"].copy()
        colors.pop(field)
        with pytest.raises(ValidationError):
            discord_module.DiscordRole.model_validate(role | {"colors": colors})


def test_resource_models_enforce_official_constraints() -> None:
    for name in ("x", " guild", "guild "):
        with pytest.raises(ValidationError):
            DiscordGuildList.model_validate([
                {"id": "1", "name": name, "icon": None, "features": []}
            ])
    for name in ("", "x" * 101):
        with pytest.raises(ValidationError):
            DiscordChannel.model_validate({"id": "1", "type": 0, "name": name})
    with pytest.raises(ValidationError):
        DiscordChannel.model_validate({
            "id": "1",
            "type": 0,
            "rate_limit_per_user": 21601,
        })
    with pytest.raises(ValidationError):
        discord_module.DiscordAttachment.model_validate({
            "id": "1",
            "filename": "file",
            "description": "x" * 1025,
            "size": 0,
            "url": "https://cdn.example/file",
            "proxy_url": "https://proxy.example/file",
        })

    over_limits: tuple[tuple[type[BaseModel], object, tuple[str, ...]], ...] = (
        (
            DiscordMessage,
            message() | {"attachments": [{}] * 11},
            ("attachments",),
        ),
        (DiscordMessage, message() | {"embeds": [{}] * 11}, ("embeds",)),
        (DiscordMessage, message() | {"reactions": [{}] * 21}, ("reactions",)),
        (
            discord_module.DiscordPartialMessage,
            {"id": "1", "channel_id": "2", "attachments": [{}] * 11},
            ("attachments",),
        ),
        (discord_module.DiscordMember, {"roles": ["1"] * 251}, ("roles",)),
        (discord_module.DiscordGuild, {"roles": [{}] * 251}, ("roles",)),
        (
            DiscordChannel,
            {"id": "1", "type": 0, "permission_overwrites": [{}] * 1001},
            ("permission_overwrites",),
        ),
        (
            DiscordChannelList,
            [{"id": str(index), "type": 0} for index in range(1, 502)],
            (),
        ),
    )
    for model, payload, location in over_limits:
        with pytest.raises(ValidationError) as error:
            model.model_validate(payload)
        assert any(
            detail["type"] == "too_long" and detail["loc"] == location
            for detail in error.value.errors()
        )


def test_discord_payload_rejects_non_finite_numbers() -> None:
    with pytest.raises(ValidationError):
        DiscordPayload.model_validate({"nested": [float("inf")]})


def test_wire_timestamps_require_rfc3339_strings() -> None:
    member = {"roles": [], "joined_at": "2026-08-19T00:00:00.123456+00:00"}
    assert discord_module.DiscordMember.model_validate(member).joined_at is not None
    for timestamp in (0, "0", "2026-08-19 00:00:00Z"):
        with pytest.raises(ValidationError):
            discord_module.DiscordMember.model_validate(
                member | {"joined_at": timestamp}
            )


def test_interaction_rate_routes_use_canonical_webhook_majors() -> None:
    rate_route = discord_module.DiscordRestClient._rate_route
    plain = DiscordRequest(method="POST", path="/interactions/1/token/callback")
    encoded = DiscordRequest(
        method="POST",
        path="/interactions/%31/%74oken/callback",
    )

    assert rate_route(plain) == rate_route(encoded)
    assert rate_route(plain)[1] == "webhooks:1:token"
    assert (
        rate_route(
            DiscordRequest(method="POST", path="/interactions/2/other/callback")
        )[1]
        == "webhooks:2:other"
    )


@pytest.mark.parametrize(
    ("interaction_type", "data", "valid_data"),
    [
        (2, None, {"id": "12", "name": "query", "type": 1}),
        (
            3,
            {"custom_id": "button"},
            {"id": 2, "custom_id": "button", "component_type": 2},
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
        "app_permissions": "0",
        "entitlements": [],
        "authorizing_integration_owners": {"0": "0"},
        "attachment_size_limit": 10_000_000,
    }
    with pytest.raises(ValidationError, match="incomplete data"):
        DiscordInteraction.model_validate(payload | {"data": data})
    assert DiscordInteraction.model_validate(payload | {"data": valid_data}).type == (
        interaction_type
    )


def test_interaction_requires_official_fields_and_literals() -> None:
    payload = cast(dict[str, object], interaction().d)
    for field in (
        "app_permissions",
        "entitlements",
        "authorizing_integration_owners",
        "attachment_size_limit",
    ):
        invalid = payload.copy()
        invalid.pop(field)
        with pytest.raises(ValidationError):
            DiscordInteraction.model_validate(invalid)
    for invalid in (
        payload | {"context": 3},
        payload | {"authorizing_integration_owners": {"2": "1"}},
        payload | {"data": {"id": 2, "name": "query", "type": 1}},
        payload
        | {
            "type": 3,
            "data": {"id": "12", "custom_id": "button", "component_type": 2},
        },
        payload
        | {
            "type": 3,
            "data": {
                "id": 2**31,
                "custom_id": "button",
                "component_type": 2,
            },
        },
    ):
        with pytest.raises(ValidationError):
            DiscordInteraction.model_validate(invalid)


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
    rest = gateway(pool)

    payload = await rest.request_discord(
        "POST",
        "/channels/1/messages",
        query={"wait": True},
        json={"content": "hello"},
        reason="test reason",
    )
    no_content = await rest.request_discord("DELETE", "/channels/1/messages/2")
    upload = await rest.connection_for(rest._self).action(
        "discord.request",
        method="POST",
        path="/channels/1/messages",
        files=[{"filename": "a.txt", "data": "aGVsbG8="}],
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
    }
    multipart = cast(bytes, pool.requests[3][2]["body"])
    assert b'name="files[0]"' in multipart
    assert b"payload_json" not in multipart
    assert b"\r\n\r\nhello\r\n" in multipart
    assert all(
        kwargs["retries"] is False
        and kwargs["preload_content"] is False
        and kwargs["redirect"] is False
        for _, _, kwargs in pool.requests
    )


async def test_rest_timeout_includes_response_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hanging = HangingBodyResponse()
    monkeypatch.setattr(discord_module, "_API_TIMEOUT", 0.01)
    async with timeout(1):
        with pytest.raises(ConnectionError, match="transport failed"):
            await client(Pool(hanging)).request_discord("GET", "/gateway/bot")
    assert hanging.cancelled.is_set()
    assert hanging.close_called.is_set()
    assert hanging.decode_content is True


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
    assert b'name="payload_json"\r\n\r\n{"target_user_ids":["1","2"]}' in body
    assert (
        b'name="target_users_file"; filename="users.txt"\r\n'
        b"Content-Type: application/octet-stream\r\n\r\n1\n2" in body
    )


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
    assert b'name="name"\r\n\r\nwave' in body
    assert b'name="description"\r\n\r\n\r\n--' in body
    assert b'name="tags"\r\n\r\nhello' in body
    assert (
        b'name="file"; filename="wave.png"\r\n'
        b"Content-Type: application/octet-stream\r\n\r\npng" in body
    )
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
    monkeypatch.setattr(discord_module, "_RECONNECT_DELAYS", (0.0,))
    recovered_pool = Pool(response(502, body=b"bad gateway"), response(200, {}))

    result = await client(recovered_pool).request_discord("GET", "/gateway/bot")

    assert isinstance(result, DiscordPayload)
    assert len(recovered_pool.requests) == 2
    failed_pool = Pool(*(response(502, body=b"bad gateway") for _ in range(5)))
    with pytest.raises(DiscordAPIError, match="502"):
        await client(failed_pool).request_discord("GET", "/gateway/bot")
    assert len(failed_pool.requests) == 5


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
    events: list[MetaEvent | PrivateMessageEvent] = []

    def enqueue(event: MetaEvent | PrivateMessageEvent) -> None:
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

    assert [event.detail_type for event in events] == ["discord.ready", "private"] * 2
    assert urls == ["wss://gateway.discord.example/?v=10&encoding=json"] * 2
    assert [websocket.close_code for websocket in websockets] == [1000, 1000]
    assert [loads(websocket.sent.get_nowait())["op"] for websocket in websockets] == [
        2,
        2,
    ]


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


def test_message_conversion_preserves_special_content() -> None:
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
            },
            {
                "id": "5",
                "filename": "image.png",
                "size": 5,
                "url": "https://cdn.discord.example/image.png",
                "proxy_url": "https://proxy.discord.example/image.png",
            },
        ],
    })

    converted = discord_module._discord_message(incoming)
    assert [segment.type for segment in converted] == [
        "text",
        "mention",
        "voice",
        "image",
    ]
    assert converted[-2].model_dump()["data"]["file_id"] == (
        "https://cdn.discord.example/voice.ogg"
    )
    assert "forwarded" not in converted.text

    empty = DiscordMessage.model_validate({
        **message(content=""),
        "type": 19,
        "message_reference": {"message_id": "9"},
        "embeds": [{"type": "rich"}],
    })
    preserved = discord_module._discord_message(empty)
    assert [segment.type for segment in preserved] == ["reply", "discord.message"]
    assert preserved[1].data.model_extra == {"raw": empty.model_dump(mode="json")}


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
        event = events[-1]
        assert isinstance(event, NoticeEvent)
        extra = event.model_extra
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
    event = events[-1]
    assert isinstance(event, MetaEvent)
    assert event.detail_type == "discord.resumed"

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
    event = events[-1]
    assert isinstance(event, NoticeEvent)
    extra = event.model_extra
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


async def test_gateway_identify_limits_and_resume_session_lifetime(
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

    initial_url = "wss://gateway.discord.example/?route=stable&v=10&encoding=json"
    assert await instance._gateway_url() == initial_url
    assert instance._initial_gateway_url == initial_url
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
    resuming._initial_gateway_url = initial_url
    resuming._resume_gateway_url = "wss://resume.discord.example"
    attempts: list[str] = []
    connections: list[ScriptedWebSocket] = []
    resume_attempts = 5

    async def reconnecting(  # ruff: ignore[unused-async] - async connector test double
        url: str,
        _: dict[str, str] | None,
    ) -> ScriptedWebSocket:
        attempts.append(url)
        if len(attempts) == 1:
            raise OSError
        if len(connections) == resume_attempts - 1:
            resuming._closing = True
        reconnect = (
            {"op": 7, "d": None} if len(connections) < 2 else {"op": 9, "d": True}
        )
        websocket = ScriptedWebSocket(
            {"op": 10, "d": {"heartbeat_interval": 60_000}},
            {"op": 11, "d": None},
            reconnect,
        )
        connections.append(websocket)
        return websocket

    monkeypatch.setattr(resuming.bot, "wait_until_running", AsyncMock())
    monkeypatch.setattr(resuming, "_websocket_connector", reconnecting)
    mocked_sleep.reset_mock()
    async with timeout(1):
        await resuming._run_gateway()

    resume_url = "wss://resume.discord.example/?v=10&encoding=json"
    assert attempts == [
        resume_url,
        initial_url,
        *([resume_url] * (resume_attempts - 1)),
    ]
    assert not discovery_pool.requests
    assert [loads(item.sent.get_nowait())["op"] for item in connections] == [
        6
    ] * resume_attempts
    assert resuming._session_id == "session"
    assert [call.args[0] for call in mocked_sleep.await_args_list] == [0, 2, 5, 10, 30]


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

    for _ in range(
        discord_module._MAX_GATEWAY_EVENTS - discord_module._GATEWAY_SYSTEM_RESERVE + 1
    ):
        await send(websocket, {"op": 4, "d": {}})
    assert [call.args[0] for call in mocked_sleep.await_args_list] == [60.0]
    instance._gateway_send_times.clear()
    mocked_sleep.reset_mock()

    for _ in range(121):
        await send(websocket, {"op": 4, "d": {}}, system=True)
    assert [call.args[0] for call in mocked_sleep.await_args_list] == [60.0]
    instance._gateway_send_times.clear()
    mocked_sleep.reset_mock()

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


async def test_full_member_rate_limit_cache_expires_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    monkeypatch.setattr(discord_module, "get_running_loop", lambda: clock)
    instance = gateway()
    websocket = ScriptedWebSocket()
    instance._websocket = websocket
    instance._full_member_ready_at = {"expired": 0.0, "active": 30.0}

    await instance._send_gateway(websocket, {"op": 4, "d": {}})

    assert instance._full_member_ready_at == {"active": 30.0}
    await instance.close()
    assert not instance._full_member_ready_at


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
    instance._self = BotSelf(platform="discord", user_id="1")
    events: list[object] = []
    instance.enqueue_event = events.append  # ty: ignore[invalid-assignment]
    await instance._receive_dispatch(
        DiscordGatewayPayload.model_validate({
            "op": 0,
            "s": 8,
            "t": "MESSAGE_CREATE",
            "d": {**message(), "author": {**user("1"), "bot": True}},
        })
    )
    assert events == []
    assert instance._seq == 8

    instance._seq = 7

    def full(_: object) -> None:
        raise QueueFull

    instance.enqueue_event = full  # ty: ignore[invalid-assignment]
    payload = DiscordGatewayPayload.model_validate({
        "op": 0,
        "s": 8,
        "t": "MESSAGE_CREATE",
        "d": {
            **message(guild_id="30"),
            "author": {**user("2"), "bot": True},
        },
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

    with pytest.raises(ValueError, match=r"unsupported.*tts"):
        await connection.send_msg("ignored", user_id="2", tts=True)
    for segment_type in ("image", "voice", "audio", "video", "file"):
        with pytest.raises(TypeError, match="common messages do not support"):
            await connection.send_msg(
                {"type": segment_type, "data": {"file_id": "opaque-file"}},
                user_id="2",
                channel_id="4",
            )
    assert len(pool.requests) == 2

    for wrong in (
        instance.connection_for(BotSelf(platform="discord", user_id="wrong")),
        gateway().connection_for(instance._self),
    ):
        with pytest.raises(ValueError, match="wrong BotSelf"):
            await instance.request_action(
                wrong,
                "get_supported_actions",
                ActionParamModel(),
            )


async def test_public_common_actions_map_endpoints_and_validate_names() -> None:
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

    supported = await connection.action("get_supported_actions")
    results = [
        await connection.action(action, **params)
        for action, params, _, _, _, _ in cases
    ]

    assert set(supported.model_dump()) == {
        "get_supported_actions",
        "get_status",
        "get_version",
        "send_message",
        "delete_message",
        "get_self_info",
        "get_user_info",
        "get_guild_info",
        "get_guild_list",
        "set_guild_name",
        "get_guild_member_info",
        "get_guild_member_list",
        "leave_guild",
        "get_channel_info",
        "get_channel_list",
        "set_channel_name",
        "discord.request",
        "discord.gateway",
    }
    assert [type(result) for result in results] == [
        expected for _, _, _, expected, _, _ in cases
    ]
    assert [
        (method, url.removeprefix("https://discord.example/api/v10"))
        for method, url, _ in pool.requests
    ] == [(method, path) for _, _, _, _, method, path in cases]
    invalid_names: tuple[tuple[str, dict[str, object]], ...] = (
        ("set_guild_name", {"guild_id": "1", "guild_name": "x"}),
        ("set_guild_name", {"guild_id": "1", "guild_name": " guild"}),
        ("set_channel_name", {"channel_id": "1", "channel_name": ""}),
        ("set_channel_name", {"channel_id": "1", "channel_name": "x" * 101}),
    )
    for action, data in invalid_names:
        with pytest.raises(ValidationError):
            await connection.action(
                action,
                **cast(dict[str, ActionParamInput], data),
            )


async def test_discord_pagination_accumulates_and_rejects_stalled_cursors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = gateway()
    guild_page = DiscordGuildList.model_validate([
        {"id": str(index), "name": "guild", "icon": None, "features": []}
        for index in range(1, discord_module._GUILD_PAGE_SIZE + 1)
    ])
    guild_tail = DiscordGuildList.model_validate([
        {"id": "201", "name": "guild", "icon": None, "features": []}
    ])
    member_page = DiscordMemberList.model_validate([
        {"user": user(str(index)), "roles": []}
        for index in range(1, discord_module._MEMBER_PAGE_SIZE + 1)
    ])
    member_tail = DiscordMemberList.model_validate([
        {"user": user("1001"), "roles": []}
    ])
    request = AsyncMock(
        side_effect=[
            guild_page,
            guild_tail,
            member_page,
            member_tail,
            guild_page,
            guild_page,
            member_page,
            member_page,
        ]
    )
    monkeypatch.setattr(instance, "_request_model", request)

    guilds = await instance._guild_list()
    members = await instance._guild_member_list("10")
    assert [guild.id for guild in guilds.root] == [
        str(index) for index in range(1, 202)
    ]
    assert [member.user.id for member in members.root] == [
        str(index) for index in range(1, 1002)
    ]

    with pytest.raises(RuntimeError, match="guild pagination did not advance"):
        await instance._guild_list()
    with pytest.raises(RuntimeError, match="member pagination did not advance"):
        await instance._guild_member_list("10")

    assert [call.kwargs["query"].get("after") for call in request.await_args_list] == [
        None,
        "200",
        None,
        "1000",
        None,
        "200",
        None,
        "1000",
    ]


def test_message_model_is_strict_but_accepts_new_fields() -> None:
    extra = {"future_field": True, "interaction": {"id": "1"}}
    parsed = DiscordMessage.model_validate(message() | extra)
    assert parsed.model_extra == extra
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
    async with timeout(0.2):
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


async def test_cold_dynamic_routes_share_provisional_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()

    @asynccontextmanager
    async def virtual_timeout(delay: float) -> AsyncIterator[None]:
        if delay != discord_module._API_TIMEOUT:
            clock.now += delay
            raise TimeoutError
        yield

    monkeypatch.setattr(discord_module, "get_running_loop", lambda: clock)
    monkeypatch.setattr(discord_module, "timeout", virtual_timeout)

    class BucketPool(Pool):
        def __init__(self) -> None:
            super().__init__()
            self.started = Event()
            self.release = Event()

        async def request(
            self,
            method: str,
            url: str,
            **kwargs: object,
        ) -> AsyncHTTPResponse:
            index = len(self.requests)
            self.requests.append((method, url, kwargs))
            self.started.set()
            await self.release.wait()
            return response(
                200,
                {},
                headers={
                    "X-RateLimit-Bucket": f"invites-{index}",
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset-After": "2",
                },
            )

    pool = BucketPool()
    rest = client(pool)
    second_started = Event()

    async def second_request() -> None:
        second_started.set()
        await rest.request_discord("GET", "/invites/beta")

    async with timeout(1), TaskGroup() as tasks:
        tasks.create_task(rest.request_discord("GET", "/invites/alpha"))
        await pool.started.wait()
        tasks.create_task(second_request())
        await second_started.wait()
        assert len(pool.requests) == 1
        pool.release.set()

    assert [url.rsplit("/", 1)[-1] for _, url, _ in pool.requests] == ["alpha", "beta"]
    first_bucket = rest._rate_buckets["bucket", "invites-0", ""]
    second_bucket = rest._rate_buckets["bucket", "invites-1", ""]
    assert (first_bucket.ready_at, second_bucket.ready_at) == pytest.approx((2.0, 4.0))


async def test_rest_programming_errors_stay_visible() -> None:
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

    marker = f"token-{id(object())}"
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
    encoded_path = f"/interactions/%31%30/{CREDENTIAL}/callback"

    try:
        await instance._receive_dispatch(interaction())
        pending = instance._interaction_callbacks[path]
        assert pending.task is not None
        connection = instance.connection_for(instance._self)
        result = await connection.action(
            "discord.request",
            method="POST",
            path=encoded_path,
            json={"type": 4},
        )
        await pending.task
        assert isinstance(result, DiscordNoContent)
        assert pool.requests[0][2]["json"] == {"type": 4}
        headers = pool.requests[0][2]["headers"]
        assert isinstance(headers, dict)
        assert "Authorization" not in headers
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
        with pytest.raises(RuntimeError, match="closed"):
            await rest.request_discord("GET", "/gateway/bot")
        assert not close_task.done()

        pool.release.set()
        assert isinstance(await interaction_task, DiscordNoContent)
        assert isinstance(await authenticated_task, DiscordNoContent)
        await close_task
    assert rest._interaction_callbacks == {}


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


async def test_close_interrupts_bad_gateway_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested = Event()

    class BadGatewayPool(Pool):
        async def request(
            self,
            *_: object,
            **__: object,
        ) -> AsyncHTTPResponse:
            requested.set()
            return response(502, {"code": 0, "message": "bad gateway"})

    monkeypatch.setattr(discord_module, "_RECONNECT_DELAYS", (60.0,))
    pool = BadGatewayPool()
    rest = client(pool)
    async with timeout(1):
        request_task = create_task(rest.request_discord("GET", "/gateway/bot"))
        cleanup_tasks: list[Task[object]] = [request_task]
        try:
            await requested.wait()
            close_task = create_task(rest.close())
            cleanup_tasks.append(close_task)
            await wait((close_task,))
            close_task.result()
            with pytest.raises(RuntimeError, match="unavailable"):
                await request_task
        finally:
            for task in cleanup_tasks:
                task.cancel()
            await gather(*cleanup_tasks, return_exceptions=True)


async def test_unauthorized_client_preserves_external_pool() -> None:
    pool = Pool(response(401, {"code": 0, "message": "unauthorized"}))
    rest = client(pool)

    with pytest.raises(DiscordAPIError, match="401"):
        await rest.request_discord("GET", "/users/@me")
    with pytest.raises(RuntimeError, match="authentication failure"):
        await rest.request_discord("GET", "/gateway/bot")
    await rest.close()

    assert len(pool.requests) == 1
