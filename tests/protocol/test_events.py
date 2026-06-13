from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import JsonValue, ValidationError

from lst_bot import (
    ChannelMessageEvent,
    ConnectMetaEvent,
    Event,
    EventDetailType,
    EventKind,
    EventPayload,
    FriendIncreaseNoticeEvent,
    FriendRequestEvent,
    GroupMemberIncreaseNoticeEvent,
    GroupMessageEvent,
    GroupRequestEvent,
    HeartbeatMetaEvent,
    PrivateMessageEvent,
    StatusUpdateMetaEvent,
    UserEvent,
)

from .support import bot_self, private_msg_payload


def test_private_msg_event_model_follows_protocol() -> None:
    event = EventPayload.model_validate(private_msg_payload()).root

    assert isinstance(event, PrivateMessageEvent)
    assert event.type == EventKind.MESSAGE
    assert event.user_id == "42"
    assert event.detail_type == "private"
    assert event.self_ is not None
    assert event.self_.platform == "qq"


def test_event_literal_fields_default_to_protocol_tags() -> None:
    payload = private_msg_payload()
    payload.pop("type")
    payload.pop("detail_type")

    event = PrivateMessageEvent.model_validate(payload)

    assert event.type == EventKind.MESSAGE
    assert event.detail_type == EventDetailType.PRIVATE


def test_event_time_parses_to_datetime_and_dumps_protocol_number() -> None:
    time = datetime.fromtimestamp(1632847927.599013, UTC)
    event = EventPayload.model_validate({**private_msg_payload(), "time": time}).root

    assert event.time == time
    assert event.model_dump(mode="json", by_alias=True)["time"] == pytest.approx(
        1632847927.599013,
    )

    string_event = EventPayload.model_validate({
        **private_msg_payload(),
        "time": "1632847927.599013",
    }).root

    assert string_event.time == time


@pytest.mark.parametrize(
    ("payload", "event_cls"),
    [
        (
            {
                **private_msg_payload(),
                "detail_type": "group",
                "message_id": "g-1",
                "group_id": "20000",
            },
            GroupMessageEvent,
        ),
        (
            {
                **private_msg_payload(),
                "detail_type": "channel",
                "message_id": "c-1",
                "guild_id": "30000",
                "channel_id": "40000",
            },
            ChannelMessageEvent,
        ),
        (
            {
                "id": "evt-friend",
                "self": bot_self(),
                "time": 1,
                "type": "notice",
                "detail_type": "friend_increase",
                "sub_type": "",
                "user_id": "42",
            },
            FriendIncreaseNoticeEvent,
        ),
        (
            {
                "id": "evt-member",
                "self": bot_self(),
                "time": 1.0,
                "type": "notice",
                "detail_type": "group_member_increase",
                "sub_type": "join",
                "group_id": "20000",
                "user_id": "42",
                "operator_id": "43",
            },
            GroupMemberIncreaseNoticeEvent,
        ),
        (
            {
                "id": "evt-friend-request",
                "self": bot_self(),
                "time": 1.0,
                "type": "request",
                "detail_type": "friend",
                "sub_type": "",
                "user_id": "42",
                "comment": "hello",
                "flag": "flag-1",
            },
            FriendRequestEvent,
        ),
        (
            {
                "id": "evt-group-request",
                "self": bot_self(),
                "time": 1.0,
                "type": "request",
                "detail_type": "group",
                "sub_type": "add",
                "group_id": "20000",
                "user_id": "42",
                "comment": "join",
                "flag": "flag-2",
            },
            GroupRequestEvent,
        ),
        (
            {
                "id": "evt-connect",
                "time": 1.0,
                "type": "meta",
                "detail_type": "connect",
                "sub_type": "",
                "version": {
                    "impl": "test",
                    "version": "1.0.0",
                    "onebot_version": "12",
                },
            },
            ConnectMetaEvent,
        ),
        (
            {
                "id": "evt-heartbeat",
                "time": 1.0,
                "type": "meta",
                "detail_type": "heartbeat",
                "sub_type": "",
                "interval": 5000,
            },
            HeartbeatMetaEvent,
        ),
        (
            {
                "id": "evt-status",
                "time": 1.0,
                "type": "meta",
                "detail_type": "status_update",
                "sub_type": "",
                "status": {
                    "good": True,
                    "bots": [{"self": bot_self(), "online": True}],
                },
            },
            StatusUpdateMetaEvent,
        ),
    ],
)
def test_event_matrix_maps_standard_detail_types(
    payload: dict[str, JsonValue],
    event_cls: type[Event],
) -> None:
    event = EventPayload.model_validate(payload).root

    assert isinstance(event, event_cls)
    assert event.type == payload["type"]
    assert event.detail_type == payload["detail_type"]
    assert event.sub_type == payload.get("sub_type", "")
    if "user_id" in payload:
        assert isinstance(event, UserEvent)


def test_unknown_detail_type_is_kept_as_extension_event() -> None:
    payload = {
        "id": "evt-ext",
        "self": bot_self(),
        "time": 1.0,
        "type": "notice",
        "detail_type": "qq.group_file_upload",
        "sub_type": "",
        "group_id": "20000",
        "qq.file_id": "file-1",
    }

    event = EventPayload.model_validate(payload).root

    assert isinstance(event, Event)
    assert event.model_extra is not None
    assert event.model_extra["qq.file_id"] == "file-1"
    assert event.type == EventKind.NOTICE
    assert event.detail_type == "qq.group_file_upload"


def test_unknown_meta_detail_type_allows_missing_self() -> None:
    event = EventPayload.model_validate({
        "id": "evt-meta-ext",
        "time": 1.0,
        "type": "meta",
        "detail_type": "impl.ready",
        "sub_type": "",
    }).root

    assert isinstance(event, Event)
    assert event.self_ is None
    assert event.type == EventKind.META
    assert event.detail_type == "impl.ready"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {**private_msg_payload(), "id": 1},
        {**private_msg_payload(), "time": "not-a-time"},
        {**private_msg_payload(), "type": "startup"},
        {**private_msg_payload(), "type": "system"},
        {**private_msg_payload(), "detail_type": 1},
        {**private_msg_payload(), "sub_type": None},
        {**private_msg_payload(), "self": {"platform": "qq"}},
        {**private_msg_payload(), "message_id": 6283},
        {**private_msg_payload(), "alt_message": None},
        {**private_msg_payload(), "user_id": 42},
        {key: value for key, value in private_msg_payload().items() if key != "self"},
        {
            "id": "evt-ext",
            "time": 1.0,
            "type": "notice",
            "detail_type": "qq.group_file_upload",
            "sub_type": "",
        },
    ],
)
def test_event_rejects_invalid_protocol_shape(payload: object) -> None:
    with pytest.raises((TypeError, ValidationError)):
        EventPayload.model_validate(payload)


@pytest.mark.parametrize(
    "msg",
    [
        "hello",
        [{"type": "text", "data": {"text": "hello"}}],
        [{"type": 1, "data": {}}],
        [{"type": "text"}],
        [{"type": "text", "data": []}],
        [{"type": "text", "data": {}}],
        [{"type": "qq.face", "data": {"type": "reserved"}}],
    ],
)
def test_event_msg_must_be_segment_list(msg: object) -> None:
    payload = {**private_msg_payload(), "message": msg}

    if isinstance(msg, list) and msg == [{"type": "text", "data": {"text": "hello"}}]:
        event = EventPayload.model_validate(payload).root
        assert isinstance(event, PrivateMessageEvent)
        assert event.message.text == "hello"
    else:
        with pytest.raises((TypeError, ValidationError)):
            EventPayload.model_validate(payload)
