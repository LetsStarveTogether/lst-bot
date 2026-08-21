from asyncio import QueueFull, create_task, sleep, timeout
from unittest.mock import AsyncMock

import pytest
from bot import (
    Action,
    GroupMessageEvent,
    MsgSegmentType,
    NoticeEvent,
    PrivateMessageEvent,
)
from bot.gateways import qq as qq_gateway_module
from bot.gateways.qq import QQDispatch, QQGatewayPayload
from bot.gateways.qq_api import QQGatewayInfo, QQNoContent
from bot.testing import ScriptedWebSocket
from pydantic import ValidationError

from .support import gateway as _gateway


async def test_malformed_known_event_is_preserved_without_blocking_sequence(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG", logger="bot")
    semantically_invalid = {
        "id": "message-without-author",
        "author": {},
        "content": "invalid",
        "timestamp": "2026-08-17T00:00:00Z",
        "message_type": 0,
        "message_scene": {"source": "default", "ext": []},
        "future_nullable_field": None,
    }
    marker = f"sensitive-{id(monkeypatch)}"
    websocket = ScriptedWebSocket(
        {"op": 10, "d": {"heartbeat_interval": 60_000}},
        {
            "op": 0,
            "s": 0,
            "t": "READY",
            "d": {
                "version": 1,
                "session_id": "session",
                "user": {"id": "bot"},
                "shard": [0, 1],
            },
        },
        {
            "id": "malformed",
            "op": 0,
            "s": 1,
            "t": "C2C_MESSAGE_CREATE",
            "d": {"id": "missing-required-message-fields", "content": [marker]},
        },
        {
            "id": "semantic-error",
            "op": 0,
            "s": 2,
            "t": "C2C_MESSAGE_CREATE",
            "d": semantically_invalid,
        },
        {
            "id": "valid",
            "op": 0,
            "s": 3,
            "t": "C2C_MESSAGE_CREATE",
            "d": {
                "id": "message",
                "author": {"user_openid": "user"},
                "content": "next",
                "timestamp": "2026-08-17T00:00:00Z",
            },
        },
        {"op": 7},
    )
    gateway = _gateway()
    events: list[object] = []
    monkeypatch.setattr(gateway, "enqueue_event", events.append)

    with pytest.raises(ConnectionError, match="requested reconnect"):
        async with timeout(1):
            await gateway._read_websocket(  # ruff: ignore[private-member-access] - protocol regression boundary
                websocket, "token"
            )
    assert marker not in caplog.text

    assert gateway._seq == 3  # ruff: ignore[private-member-access]
    assert isinstance(events[1], NoticeEvent)
    assert events[1].model_extra == {
        "qq_event_type": "C2C_MESSAGE_CREATE",
        "qq_data": {"id": "missing-required-message-fields", "content": [marker]},
        "qq_raw": True,
    }
    assert isinstance(events[2], NoticeEvent)
    assert events[2].model_extra == {
        "qq_event_type": "C2C_MESSAGE_CREATE",
        "qq_data": semantically_invalid,
        "qq_raw": True,
    }
    assert isinstance(events[3], PrivateMessageEvent)
    assert events[3].message.text == "next"


async def test_dispatch_envelope_and_control_frames_remain_strict() -> None:
    gateway = _gateway()
    valid_message = {
        "id": "message",
        "author": {"user_openid": "user"},
        "content": "text",
        "timestamp": "2026-08-17T00:00:00Z",
        "message_type": 0,
        "message_scene": {"source": "default"},
    }

    for payload in (
        {"op": 0, "t": "C2C_MESSAGE_CREATE", "d": valid_message},
        {"op": 0, "s": 1, "d": valid_message},
        {"op": 0, "s": 1, "t": "READY", "d": {}},
    ):
        with pytest.raises(ValidationError):
            await gateway._receive_dispatch(  # ruff: ignore[private-member-access] - protocol regression boundary
                QQGatewayPayload.model_validate(payload)
            )


def test_quoted_message_maps_reference_without_copying_quoted_content() -> None:
    event = _gateway()._event_from_dispatch(  # ruff: ignore[private-member-access] - conversion boundary
        QQDispatch.model_validate({
            "id": "event",
            "op": 0,
            "s": 1,
            "t": "C2C_MESSAGE_CREATE",
            "d": {
                "id": "message",
                "author": {"user_openid": "current-user"},
                "content": "current text",
                "timestamp": "2026-08-17T00:00:00Z",
                "message_type": 103,
                "message_scene": {"ext": ["ref_msg_idx=quoted-index"]},
                "msg_elements": [
                    {
                        "msg_idx": "quoted-index",
                        "author": {"user_openid": "quoted-user"},
                        "message_type": 103,
                        "content": "quoted text must not leak",
                    }
                ],
            },
        })
    )

    assert isinstance(event, PrivateMessageEvent)
    assert event.message.text == "current text"
    assert [segment.type for segment in event.message] == [
        MsgSegmentType.REPLY,
        MsgSegmentType.TEXT,
    ]
    assert event.message[0].data.model_dump() == {
        "message_id": "quoted-index",
        "user_id": "quoted-user",
    }
    assert event.model_extra is not None
    assert event.model_extra["reply_alt_message"] == "quoted text must not leak"

    group_event = _gateway()._event_from_dispatch(  # ruff: ignore[private-member-access] - conversion boundary
        QQDispatch.model_validate({
            "id": "group-event",
            "op": 0,
            "s": 2,
            "t": "GROUP_MESSAGE_CREATE",
            "d": {
                "id": "group-message",
                "group_openid": "group",
                "author": {"member_openid": "current-user"},
                "content": "current text",
                "timestamp": "2026-08-17T00:00:00Z",
                "message_type": 103,
                "message_scene": {"ext": ["ref_msg_idx=group-quoted"]},
            },
        })
    )
    assert isinstance(group_event, GroupMessageEvent)
    assert group_event.message[0].data.model_dump().get("message_id") == "group-quoted"
    assert group_event.model_extra is not None
    assert "reply_alt_message" not in group_event.model_extra


async def test_passive_reply_sequence_wraps_and_preserves_explicit_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _gateway(online=True)
    gateway._message_sequence = 65_534  # ruff: ignore[private-member-access]
    request = AsyncMock(return_value=QQNoContent())
    monkeypatch.setattr(gateway, "request_qq", request)
    connection = gateway.connection_for(gateway._self)  # ruff: ignore[private-member-access]

    for _ in range(2):
        await connection.action(
            Action.SEND_MESSAGE,
            detail_type="private",
            user_id="user",
            msg_id="same-message",
            message="reply",
        )
    await connection.action(
        Action.SEND_MESSAGE,
        detail_type="group",
        group_id="group",
        msg_id="same-message",
        msg_seq=42,
        message="explicit",
    )

    sequences = [call.kwargs["msg_seq"] for call in request.await_args_list]
    assert sequences == [65_535, 0, 42]
    assert all(0 <= sequence <= 65_535 for sequence in sequences)


async def test_mentions_use_current_wire_format_and_validate_scene(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _gateway(online=True)
    request = AsyncMock(return_value=QQNoContent())
    monkeypatch.setattr(gateway, "request_qq", request)
    connection = gateway.connection_for(gateway._self)  # ruff: ignore[private-member-access]

    await connection.action(
        Action.SEND_MESSAGE,
        detail_type="group",
        group_id="group",
        message=[{"type": "mention", "data": {"user_id": 'u"&'}}],
    )
    await connection.action(
        Action.SEND_MESSAGE,
        detail_type="channel",
        guild_id="guild",
        channel_id="channel",
        message=[{"type": "mention_all", "data": {}}],
    )

    assert request.await_args_list[0].kwargs["content"] == (
        '<qqbot-at-user id="u&quot;&amp;" />'
    )
    assert request.await_args_list[1].kwargs["content"] == ("<qqbot-at-everyone />")

    invalid = (
        {
            "detail_type": "private",
            "user_id": "user",
            "message": [{"type": "mention", "data": {"user_id": "other"}}],
        },
        {
            "detail_type": "group",
            "group_id": "group",
            "message": [{"type": "mention_all", "data": {}}],
        },
    )
    for params in invalid:
        with pytest.raises(ValueError, match="only supported"):
            await connection.action(Action.SEND_MESSAGE, **params)
    assert request.await_count == 2


async def test_conflicting_message_target_cannot_bypass_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _gateway(online=True)
    request = AsyncMock(return_value=QQNoContent())
    monkeypatch.setattr(gateway, "request_qq", request)
    connection = gateway.connection_for(gateway._self)  # ruff: ignore[private-member-access]

    with pytest.raises(ValueError, match="does not match detail_type"):
        await connection.action(
            Action.SEND_MESSAGE,
            detail_type="group",
            qq_scene="channel",
            user_id="user",
            group_id="group",
            guild_id="guild",
            channel_id="channel",
            message=[{"type": "mention_all", "data": {}}],
        )

    request.assert_not_awaited()


async def test_retry_backoff_resets_only_after_heartbeat_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = ScriptedWebSocket(
        {"op": 10, "d": {"heartbeat_interval": 60_000}},
        {
            "op": 0,
            "s": 1,
            "t": "READY",
            "d": {
                "version": 1,
                "session_id": "session",
                "user": {"id": "bot"},
                "shard": [0, 1],
            },
        },
        {"op": 1},
        {"op": 11},
        {"op": 7},
    )
    connector = AsyncMock(return_value=websocket)
    gateway = _gateway(websocket_connector=connector)
    gateway._retry_count = 4  # ruff: ignore[private-member-access]
    events: list[object] = []
    delays: list[float] = []
    monkeypatch.setattr(gateway, "enqueue_event", events.append)
    monkeypatch.setattr(gateway.bot, "wait_until_running", AsyncMock())
    monkeypatch.setattr(
        gateway,
        "request_qq",
        AsyncMock(return_value=QQGatewayInfo(url="wss://qq.example")),
    )
    monkeypatch.setattr(gateway, "access_token", AsyncMock(return_value="token"))

    async def stop_after_first_retry(delay: float) -> None:
        await sleep(0)
        delays.append(delay)
        gateway._closing = True  # ruff: ignore[private-member-access]

    monkeypatch.setattr(qq_gateway_module, "sleep", stop_after_first_retry)

    async with timeout(1):
        await gateway._run_gateway()  # ruff: ignore[private-member-access] - reconnect regression boundary

    assert delays == [1.0]
    assert gateway._retry_count == 1  # ruff: ignore[private-member-access]
    assert len(events) == 1


async def test_ready_resumed_and_failed_enqueue_preserve_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _gateway()
    gateway._retry_count = 4  # ruff: ignore[private-member-access]
    monkeypatch.setattr(gateway, "enqueue_event", lambda _: None)

    await gateway._receive_dispatch(  # ruff: ignore[private-member-access] - protocol regression boundary
        QQGatewayPayload.model_validate({
            "op": 0,
            "s": 1,
            "t": "READY",
            "d": {
                "version": 1,
                "session_id": "session",
                "user": {"id": "bot"},
                "shard": [0, 1],
            },
        })
    )
    resumed = QQGatewayPayload.model_validate({
        "op": 0,
        "s": 2,
        "t": "RESUMED",
        "d": "",
    })
    await gateway._receive_dispatch(resumed)  # ruff: ignore[private-member-access]

    assert gateway._retry_count == 4  # ruff: ignore[private-member-access]
    gateway._seq = 7  # ruff: ignore[private-member-access]

    def full(_: object) -> None:
        raise QueueFull

    monkeypatch.setattr(gateway, "enqueue_event", full)
    with pytest.raises(ConnectionError, match="queue is full"):
        await gateway._receive_dispatch(  # ruff: ignore[private-member-access] - sequence regression boundary
            resumed.model_copy(update={"s": 8})
        )
    assert gateway._seq == 7  # ruff: ignore[private-member-access]


async def test_start_reaps_a_finished_gateway_task() -> None:
    gateway = _gateway()
    finished = create_task(sleep(0))
    await finished
    gateway._task = finished  # ruff: ignore[private-member-access]
    gateway._session_id = "stale"  # ruff: ignore[private-member-access]

    async with timeout(1):
        await gateway.start()
        assert gateway._task is not finished  # ruff: ignore[private-member-access]
        assert gateway._session_id is None  # ruff: ignore[private-member-access]
        await gateway.close()


async def test_start_retries_unfinished_gateway_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _gateway()
    gateway._closing = True  # ruff: ignore[private-member-access]
    cleanup = AsyncMock(side_effect=[RuntimeError("cleanup failed"), None])

    with monkeypatch.context() as patch:
        patch.setattr(gateway, "_finish_gateway_close", cleanup)
        with pytest.raises(RuntimeError, match="cleanup failed"):
            await gateway.start()
        assert gateway._closing  # ruff: ignore[private-member-access]
        await gateway.start()

    assert not gateway._closing  # ruff: ignore[private-member-access]
    assert cleanup.await_count == 2
    await gateway.close()
