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
from bot.gateways import onebot11 as onebot11_module
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


def group_notice_payload(
    notice_type: str,
    **values: JsonValue,
) -> dict[str, JsonValue]:
    return notice_payload(notice_type, group_id=20000, user_id=42, **values)


@pytest.mark.parametrize(
    "time",
    [
        pytest.param(1.5, id="fractional"),
        pytest.param(-(2**63) - 1, id="below-int64-minimum"),
        pytest.param(2**63, id="above-int64-maximum"),
    ],
)
def test_event_time_is_int64(time: JsonValue) -> None:
    payload = private_msg_payload()
    payload["time"] = time

    with pytest.raises(ValueError, match="time"):
        event(payload)


@pytest.mark.parametrize(
    ("notice_type", "values", "detail_type"),
    [
        pytest.param(
            "group_upload",
            {"file": {"id": "f", "name": "x", "size": 1, "busid": 2}},
            "qq.group_upload",
            id="group-upload",
        ),
        pytest.param(
            "group_admin",
            {"sub_type": "set"},
            "qq.group_admin",
            id="group-admin-set",
        ),
        pytest.param(
            "group_admin",
            {"sub_type": "unset"},
            "qq.group_admin",
            id="group-admin-unset",
        ),
        pytest.param(
            "group_ban",
            {"sub_type": "ban", "operator_id": 7, "duration": 60},
            "qq.group_ban",
            id="group-ban",
        ),
        pytest.param(
            "group_ban",
            {"sub_type": "lift_ban", "operator_id": 7, "duration": 0},
            "qq.group_ban",
            id="group-unban",
        ),
        pytest.param(
            "notify",
            {"sub_type": "poke", "target_id": 7},
            "qq.notify",
            id="notify-poke",
        ),
        pytest.param(
            "notify",
            {"sub_type": "lucky_king", "target_id": 7},
            "qq.notify",
            id="notify-lucky-king",
        ),
        pytest.param(
            "notify",
            {"sub_type": "honor", "honor_type": "talkative"},
            "qq.notify",
            id="notify-honor",
        ),
    ],
)
def test_official_extension_notices_are_validated_and_preserved(
    notice_type: str,
    values: dict[str, JsonValue],
    detail_type: str,
) -> None:
    converted = event(group_notice_payload(notice_type, **values))

    assert type(converted) is NoticeEvent
    assert converted.detail_type == detail_type
    assert extra(converted)["group_id"] == "20000"
    assert extra(converted)["user_id"] == "42"
    dumped = converted.model_dump(mode="json")
    for key, value in values.items():
        assert dumped[key] == (str(value) if key.endswith("_id") else value)


@pytest.mark.parametrize(
    ("notice_type", "values"),
    [
        pytest.param(
            "group_upload",
            {"file": {"id": 1, "name": "x", "size": 1, "busid": 2}},
            id="group-upload-file-id",
        ),
        pytest.param(
            "group_upload",
            {"file": {"id": "f", "name": "x", "size": True, "busid": 2}},
            id="group-upload-file-size",
        ),
        pytest.param(
            "group_upload",
            {"file": {"id": "f", "name": "x", "size": -1, "busid": 2}},
            id="group-upload-negative-size",
        ),
        pytest.param(
            "group_admin",
            {"sub_type": "bad"},
            id="group-admin-subtype",
        ),
        pytest.param(
            "group_ban",
            {"sub_type": "ban", "duration": 60},
            id="group-ban-operator",
        ),
        pytest.param(
            "group_ban",
            {"sub_type": "ban", "operator_id": 7, "duration": -1},
            id="group-ban-negative-duration",
        ),
        pytest.param(
            "notify",
            {"sub_type": "poke"},
            id="notify-poke-target",
        ),
        pytest.param(
            "notify",
            {"sub_type": "honor"},
            id="notify-honor-type",
        ),
    ],
)
def test_official_extension_notices_reject_invalid_fields(
    notice_type: str,
    values: dict[str, JsonValue],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        event(group_notice_payload(notice_type, **values))


def test_unknown_notice_remains_a_notice_event() -> None:
    converted = event(notice_payload("vendor_notice", vendor_value=1))

    assert type(converted) is NoticeEvent
    assert converted.detail_type == "qq.vendor_notice"
    assert extra(converted)["vendor_value"] == 1


@pytest.mark.parametrize(
    "online",
    [pytest.param(True, id="online"), pytest.param(None, id="unknown")],
)
def test_heartbeat_maps_bot_status(online: bool | None) -> None:
    converted = event({
        "time": 1,
        "self_id": 10000,
        "post_type": "meta_event",
        "meta_event_type": "heartbeat",
        "status": {"good": False, "online": online},
        "interval": 5000,
    })

    assert isinstance(converted, HeartbeatMetaEvent)
    bots: list[JsonValue] = []
    if online is not None:
        bots.append({
            "self": {"platform": "qq", "user_id": "10000"},
            "online": online,
        })
    assert extra(converted)["status"] == {"good": False, "bots": bots}


@pytest.mark.parametrize(
    "status",
    [
        None,
        "broken",
        {},
        {"good": True},
        {"good": 1},
        {"good": True, "online": 1},
    ],
    ids=[
        "null",
        "string",
        "empty",
        "missing-online",
        "non-boolean-good",
        "non-boolean-online",
    ],
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
        group_notice_payload(notice_type, sub_type=sub_type, operator_id=7)
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


def test_message_nested_ids_reject_booleans() -> None:
    payload = {
        **private_msg_payload(),
        "message_type": "group",
        "group_id": 20000,
        "anonymous": {"id": 8, "name": "anon", "flag": "f"},
    }
    nested = payload["sender"]
    assert isinstance(nested, dict)
    nested["user_id"] = True

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


def test_message_event_requires_message() -> None:
    payload = private_msg_payload()
    del payload["message"]

    with pytest.raises(ValueError, match="message"):
        event(payload)


def test_message_event_rejects_single_segment_object() -> None:
    with pytest.raises(TypeError, match="string or segment array"):
        event(private_msg_payload({"type": "text", "data": {"text": "hello"}}))


def test_cq_parameters_are_unescaped_once() -> None:
    converted = event(private_msg_payload("[CQ:share,title=&amp;#44;]"))

    assert isinstance(converted, PrivateMessageEvent)
    assert converted.message[0].data.model_extra == {"title": "&#44;"}


def test_standard_message_segments_preserve_extension_data() -> None:
    message: list[JsonValue] = [
        {"type": "text", "data": {"text": "hello", "vendor.flag": "text"}},
        {"type": "at", "data": {"qq": "42", "vendor.flag": "mention"}},
        {"type": "at", "data": {"qq": "all", "vendor.flag": "all"}},
        {
            "type": "location",
            "data": {
                "lat": "1.0",
                "lon": "2.0",
                "title": "",
                "content": "",
                "vendor.flag": "location",
            },
        },
        {"type": "reply", "data": {"id": "3", "vendor.flag": "reply"}},
    ]
    converted = event(private_msg_payload(message))

    assert isinstance(converted, PrivateMessageEvent)
    assert (
        onebot11_module._dump_ob11_message(  # ruff: ignore[private-member-access]
            converted.message
        ).model_dump(mode="json")
        == message
    )


def test_message_segment_rejects_non_object_data() -> None:
    with pytest.raises(TypeError, match="data must be an object or null"):
        event(private_msg_payload([{"type": "text", "data": []}]))


@pytest.mark.parametrize(
    "segment",
    [
        pytest.param({"type": "text", "data": None}, id="text-missing"),
        pytest.param({"type": "at", "data": {"qq": True}}, id="mention-boolean"),
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
