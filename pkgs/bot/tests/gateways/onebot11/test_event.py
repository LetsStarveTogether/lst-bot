from typing import cast
from uuid import UUID

import pytest
from bot import (
    Event,
    FriendRequestEvent,
    GroupMemberDecreaseNoticeEvent,
    GroupMemberIncreaseNoticeEvent,
    GroupMessageEvent,
    GroupRequestEvent,
    HeartbeatMetaEvent,
    NoticeEvent,
    PrivateMessageEvent,
)
from bot.gateways.onebot11 import decode_event
from pydantic import JsonValue

from .support import private_msg_payload


def event(payload: dict[str, JsonValue]) -> Event:
    return decode_event(payload)


def extra(value: Event) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], value.model_extra or {})


def notice_payload(notice_type: str, **values: JsonValue) -> dict[str, JsonValue]:
    return {
        "time": 1,
        "self_id": 10000,
        "post_type": "notice",
        "notice_type": notice_type,
        "sub_type": "",
        **values,
    }


@pytest.mark.parametrize(
    ("payload", "detail_type"),
    [
        (
            notice_payload(
                "group_upload",
                group_id=20000,
                user_id=42,
                file={"id": "f", "name": "x", "size": 1, "busid": 2},
            ),
            "qq.group_upload",
        ),
        (
            notice_payload(
                "group_admin",
                sub_type="set",
                group_id=20000,
                user_id=42,
            ),
            "qq.group_admin",
        ),
        (
            notice_payload(
                "group_admin",
                sub_type="unset",
                group_id=20000,
                user_id=42,
            ),
            "qq.group_admin",
        ),
        (
            notice_payload(
                "group_ban",
                sub_type="ban",
                group_id=20000,
                operator_id=7,
                user_id=42,
                duration=60,
            ),
            "qq.group_ban",
        ),
        (
            notice_payload(
                "group_ban",
                sub_type="lift_ban",
                group_id=20000,
                operator_id=7,
                user_id=42,
                duration=0,
            ),
            "qq.group_ban",
        ),
        (
            notice_payload(
                "notify",
                sub_type="poke",
                group_id=20000,
                user_id=42,
                target_id=7,
            ),
            "qq.notify",
        ),
        (
            notice_payload(
                "notify",
                sub_type="lucky_king",
                group_id=20000,
                user_id=42,
                target_id=7,
            ),
            "qq.notify",
        ),
        (
            notice_payload(
                "notify",
                sub_type="honor",
                group_id=20000,
                user_id=42,
                honor_type="talkative",
            ),
            "qq.notify",
        ),
    ],
    ids=[
        "group-upload",
        "group-admin-set",
        "group-admin-unset",
        "group-ban",
        "group-unban",
        "notify-poke",
        "notify-lucky-king",
        "notify-honor",
    ],
)
def test_official_extension_notices_are_validated_and_preserved(
    payload: dict[str, JsonValue],
    detail_type: str,
) -> None:
    converted = event(payload)

    assert type(converted) is NoticeEvent
    assert converted.detail_type == detail_type
    assert extra(converted)["group_id"] == "20000"
    assert extra(converted)["user_id"] == "42"


@pytest.mark.parametrize(
    "payload",
    [
        notice_payload(
            "group_upload",
            group_id=20000,
            user_id=42,
            file={"id": 1, "name": "x", "size": 1, "busid": 2},
        ),
        notice_payload(
            "group_upload",
            group_id=20000,
            user_id=42,
            file={"id": "f", "name": "x", "size": True, "busid": 2},
        ),
        notice_payload(
            "group_upload",
            group_id=20000,
            user_id=42,
            file={"id": "f", "name": "x", "size": -1, "busid": 2},
        ),
        notice_payload(
            "group_admin",
            sub_type="bad",
            group_id=20000,
            user_id=42,
        ),
        notice_payload(
            "group_ban",
            sub_type="ban",
            group_id=20000,
            user_id=42,
            duration=60,
        ),
        notice_payload(
            "group_ban",
            sub_type="ban",
            group_id=20000,
            operator_id=7,
            user_id=42,
            duration=-1,
        ),
        notice_payload(
            "notify",
            sub_type="poke",
            group_id=20000,
            user_id=42,
        ),
        notice_payload(
            "notify",
            sub_type="honor",
            group_id=20000,
            user_id=42,
        ),
    ],
    ids=[
        "group-upload-file-id",
        "group-upload-file-size",
        "group-upload-negative-size",
        "group-admin-subtype",
        "group-ban-operator",
        "group-ban-negative-duration",
        "notify-poke-target",
        "notify-honor-type",
    ],
)
def test_official_extension_notices_reject_invalid_fields(
    payload: dict[str, JsonValue],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        event(payload)


def test_unknown_notice_remains_a_notice_event() -> None:
    converted = event(notice_payload("vendor_notice", vendor_value=1))

    assert type(converted) is NoticeEvent
    assert converted.detail_type == "qq.vendor_notice"
    assert extra(converted)["vendor_value"] == 1


def test_heartbeat_status_is_strict_and_maps_online_bot() -> None:
    converted = event({
        "time": 1,
        "self_id": 10000,
        "post_type": "meta_event",
        "meta_event_type": "heartbeat",
        "status": {"good": False, "online": True},
        "interval": 5000,
    })

    assert isinstance(converted, HeartbeatMetaEvent)
    status = extra(converted)["status"]
    assert status == {
        "good": False,
        "bots": [{"self": {"platform": "qq", "user_id": "10000"}, "online": True}],
    }


def test_heartbeat_unknown_online_state_has_no_bot_status() -> None:
    converted = event({
        "time": 1,
        "self_id": 10000,
        "post_type": "meta_event",
        "meta_event_type": "heartbeat",
        "status": {"good": False, "online": None},
        "interval": 5000,
    })

    assert isinstance(converted, HeartbeatMetaEvent)
    assert extra(converted)["status"] == {"good": False, "bots": []}


@pytest.mark.parametrize(
    "status",
    [None, "broken", {}, {"good": 1}, {"good": True, "online": 1}],
    ids=["null", "string", "empty", "non-boolean-good", "non-boolean-online"],
)
def test_heartbeat_rejects_invalid_status(status: JsonValue) -> None:
    with pytest.raises((TypeError, ValueError)):
        event({
            "time": 1,
            "self_id": 10000,
            "post_type": "meta_event",
            "meta_event_type": "heartbeat",
            "status": status,
            "interval": 5000,
        })


@pytest.mark.parametrize(
    "interval",
    [True, "5000", 0, -1],
    ids=["boolean", "string", "zero", "negative"],
)
def test_heartbeat_rejects_invalid_interval(interval: JsonValue) -> None:
    with pytest.raises((TypeError, ValueError)):
        event({
            "time": 1,
            "self_id": 10000,
            "post_type": "meta_event",
            "meta_event_type": "heartbeat",
            "status": {"good": True},
            "interval": interval,
        })


@pytest.mark.parametrize("sub_type", ["add", "invite"], ids=["add", "invite"])
def test_group_request_requires_and_preserves_subtype(sub_type: str) -> None:
    converted = event({
        "time": 1,
        "self_id": 10000,
        "post_type": "request",
        "request_type": "group",
        "sub_type": sub_type,
        "group_id": 20000,
        "user_id": 42,
        "comment": "join",
        "flag": "flag",
    })

    assert isinstance(converted, GroupRequestEvent)
    assert converted.sub_type == sub_type
    assert converted.group_id == "20000"


@pytest.mark.parametrize(
    "sub_type",
    [None, "", "bad"],
    ids=["missing", "empty", "unknown"],
)
def test_group_request_rejects_invalid_subtype(sub_type: JsonValue) -> None:
    payload: dict[str, JsonValue] = {
        "time": 1,
        "self_id": 10000,
        "post_type": "request",
        "request_type": "group",
        "group_id": 20000,
        "user_id": 42,
        "comment": "join",
        "flag": "flag",
    }
    if sub_type is not None:
        payload["sub_type"] = sub_type

    with pytest.raises((TypeError, ValueError)):
        event(payload)


def test_friend_request_does_not_require_group_subtype() -> None:
    converted = event({
        "time": 1,
        "self_id": 10000,
        "post_type": "request",
        "request_type": "friend",
        "user_id": 42,
        "comment": "hello",
        "flag": "flag",
    })

    assert isinstance(converted, FriendRequestEvent)
    assert converted.sub_type == ""


@pytest.mark.parametrize(
    ("notice_type", "sub_type", "expected_type", "expected_sub_type"),
    [
        ("group_increase", "approve", GroupMemberIncreaseNoticeEvent, "join"),
        ("group_increase", "invite", GroupMemberIncreaseNoticeEvent, "invite"),
        ("group_increase", "vendor", GroupMemberIncreaseNoticeEvent, "vendor"),
        ("group_decrease", "kick_me", GroupMemberDecreaseNoticeEvent, "kick"),
        ("group_decrease", "leave", GroupMemberDecreaseNoticeEvent, "leave"),
        ("group_decrease", "vendor", GroupMemberDecreaseNoticeEvent, "vendor"),
    ],
    ids=[
        "increase-approve",
        "increase-invite",
        "increase-vendor",
        "decrease-kick-me",
        "decrease-leave",
        "decrease-vendor",
    ],
)
def test_member_notice_subtypes_are_normalized(
    notice_type: str,
    sub_type: str,
    expected_type: type[NoticeEvent],
    expected_sub_type: str,
) -> None:
    converted = event(
        notice_payload(
            notice_type,
            sub_type=sub_type,
            group_id=20000,
            operator_id=7,
            user_id=42,
        )
    )

    assert isinstance(converted, expected_type)
    assert converted.sub_type == expected_sub_type


def test_message_nested_ids_are_normalized() -> None:
    payload = {
        **private_msg_payload(),
        "message_type": "group",
        "group_id": 20000,
        "anonymous": {"id": 8, "name": "anon", "flag": "f"},
    }

    converted = event(cast(dict[str, JsonValue], payload))

    assert isinstance(converted, GroupMessageEvent)
    sender = extra(converted)["sender"]
    anonymous = extra(converted)["anonymous"]
    assert isinstance(sender, dict)
    assert isinstance(anonymous, dict)
    assert sender["user_id"] == "42"
    assert anonymous["id"] == "8"


@pytest.mark.parametrize(
    ("field", "key"),
    [("sender", "user_id"), ("anonymous", "id")],
    ids=["sender", "anonymous"],
)
def test_message_nested_ids_reject_booleans(field: str, key: str) -> None:
    payload = {
        **private_msg_payload(),
        "message_type": "group",
        "group_id": 20000,
        "anonymous": {"id": 8, "name": "anon", "flag": "f"},
    }
    nested = payload[field]
    assert isinstance(nested, dict)
    nested[key] = True

    with pytest.raises(TypeError, match="id fields"):
        event(cast(dict[str, JsonValue], payload))


def test_private_message_decodes_cq_and_generates_event_id() -> None:
    converted = event(
        private_msg_payload(
            "hi&#91;x&#93;[CQ:at,qq=100][CQ:image,file=1.jpg,url=http://x]"
        )
    )

    assert isinstance(converted, PrivateMessageEvent)
    assert converted.message.model_dump(mode="json") == [
        {"type": "text", "data": {"text": "hi[x]"}},
        {"type": "mention", "data": {"user_id": "100"}},
        {
            "type": "image",
            "data": {"file_id": "1.jpg", "url": "http://x"},
        },
    ]
    assert str(UUID(converted.id)) == converted.id


def test_cq_parameters_are_unescaped_once() -> None:
    converted = event(private_msg_payload("[CQ:share,title=&amp;#44;]"))

    assert isinstance(converted, PrivateMessageEvent)
    assert converted.message[0].data.model_extra == {"title": "&#44;"}


@pytest.mark.parametrize(
    "data",
    [[], False, 0, ""],
    ids=["array", "boolean", "integer", "string"],
)
def test_message_segment_rejects_non_object_data(data: JsonValue) -> None:
    with pytest.raises(TypeError, match="data must be an object or null"):
        event(private_msg_payload([{"type": "text", "data": data}]))


@pytest.mark.parametrize(
    "segment",
    [
        pytest.param({"type": "text", "data": None}, id="text-missing"),
        pytest.param({"type": "text", "data": {"text": {}}}, id="text-object"),
        pytest.param({"type": "at", "data": {"qq": True}}, id="mention-boolean"),
        pytest.param({"type": "at", "data": {"qq": []}}, id="mention-array"),
        pytest.param(
            {"type": "image", "data": {"file": False}},
            id="media-boolean",
        ),
        pytest.param(
            {"type": "image", "data": {"file": []}},
            id="media-array",
        ),
        pytest.param({"type": "reply", "data": {"id": {}}}, id="reply-object"),
        pytest.param(
            {"type": "location", "data": {"lat": 1, "lon": 2, "title": []}},
            id="location-title-array",
        ),
    ],
)
def test_message_segment_rejects_invalid_scalar_fields(
    segment: dict[str, JsonValue],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        event(private_msg_payload([segment]))
