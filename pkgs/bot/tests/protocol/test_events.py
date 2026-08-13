from __future__ import annotations

import pytest
from bot import (
    ChannelCreateNoticeEvent,
    ChannelDeleteNoticeEvent,
    ChannelMemberDecreaseNoticeEvent,
    ChannelMemberIncreaseNoticeEvent,
    ChannelMessageDeleteNoticeEvent,
    ChannelMessageEvent,
    ConnectMetaEvent,
    Event,
    EventPayload,
    FriendDecreaseNoticeEvent,
    FriendIncreaseNoticeEvent,
    FriendRequestEvent,
    GroupMemberDecreaseNoticeEvent,
    GroupMemberIncreaseNoticeEvent,
    GroupMessageDeleteNoticeEvent,
    GroupMessageEvent,
    GroupRequestEvent,
    GuildMemberDecreaseNoticeEvent,
    GuildMemberIncreaseNoticeEvent,
    HeartbeatMetaEvent,
    MetaEvent,
    NoticeEvent,
    PrivateMessageDeleteNoticeEvent,
    PrivateMessageEvent,
    RequestEvent,
    StatusUpdateMetaEvent,
)
from pydantic import ValidationError


def _bot_self() -> dict[str, str]:
    return {"platform": "qq", "user_id": "10000"}


def _event(
    event_type: str,
    detail_type: str,
    **fields: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": f"event-{detail_type}",
        "time": 1.25,
        "type": event_type,
        "detail_type": detail_type,
        "sub_type": "",
    }
    if event_type != "meta":
        payload["self"] = _bot_self()
    payload.update(fields)
    return payload


def _message(detail_type: str, **target: object) -> dict[str, object]:
    return _event(
        "message",
        detail_type,
        message_id=f"message-{detail_type}",
        message=[{"type": "text", "data": {"text": "hello"}}],
        alt_message="hello",
        user_id="42",
        **target,
    )


EVENT_CASES: tuple[object, ...] = (
    pytest.param(_message("private"), PrivateMessageEvent, id="message-private"),
    pytest.param(
        _message("group", group_id="20000"),
        GroupMessageEvent,
        id="message-group",
    ),
    pytest.param(
        _message("channel", guild_id="30000", channel_id="40000"),
        ChannelMessageEvent,
        id="message-channel",
    ),
    pytest.param(
        _event("notice", "friend_increase", user_id="42"),
        FriendIncreaseNoticeEvent,
        id="notice-friend-increase",
    ),
    pytest.param(
        _event("notice", "friend_decrease", user_id="42"),
        FriendDecreaseNoticeEvent,
        id="notice-friend-decrease",
    ),
    pytest.param(
        _event(
            "notice",
            "private_message_delete",
            user_id="42",
            message_id="message-1",
        ),
        PrivateMessageDeleteNoticeEvent,
        id="notice-private-message-delete",
    ),
    pytest.param(
        _event(
            "notice",
            "group_member_increase",
            group_id="20000",
            user_id="42",
            operator_id="43",
        ),
        GroupMemberIncreaseNoticeEvent,
        id="notice-group-member-increase",
    ),
    pytest.param(
        _event(
            "notice",
            "group_member_decrease",
            group_id="20000",
            user_id="42",
            operator_id="43",
        ),
        GroupMemberDecreaseNoticeEvent,
        id="notice-group-member-decrease",
    ),
    pytest.param(
        _event(
            "notice",
            "group_message_delete",
            group_id="20000",
            user_id="42",
            message_id="message-1",
            operator_id="43",
        ),
        GroupMessageDeleteNoticeEvent,
        id="notice-group-message-delete",
    ),
    pytest.param(
        _event(
            "notice",
            "guild_member_increase",
            guild_id="30000",
            user_id="42",
            operator_id="43",
        ),
        GuildMemberIncreaseNoticeEvent,
        id="notice-guild-member-increase",
    ),
    pytest.param(
        _event(
            "notice",
            "guild_member_decrease",
            guild_id="30000",
            user_id="42",
            operator_id="43",
        ),
        GuildMemberDecreaseNoticeEvent,
        id="notice-guild-member-decrease",
    ),
    pytest.param(
        _event(
            "notice",
            "channel_member_increase",
            guild_id="30000",
            channel_id="40000",
            user_id="42",
            operator_id="43",
        ),
        ChannelMemberIncreaseNoticeEvent,
        id="notice-channel-member-increase",
    ),
    pytest.param(
        _event(
            "notice",
            "channel_member_decrease",
            guild_id="30000",
            channel_id="40000",
            user_id="42",
            operator_id="43",
        ),
        ChannelMemberDecreaseNoticeEvent,
        id="notice-channel-member-decrease",
    ),
    pytest.param(
        _event(
            "notice",
            "channel_message_delete",
            guild_id="30000",
            channel_id="40000",
            user_id="42",
            message_id="message-1",
            operator_id="43",
        ),
        ChannelMessageDeleteNoticeEvent,
        id="notice-channel-message-delete",
    ),
    pytest.param(
        _event(
            "notice",
            "channel_create",
            guild_id="30000",
            channel_id="40000",
            operator_id="43",
        ),
        ChannelCreateNoticeEvent,
        id="notice-channel-create",
    ),
    pytest.param(
        _event(
            "notice",
            "channel_delete",
            guild_id="30000",
            channel_id="40000",
            operator_id="43",
        ),
        ChannelDeleteNoticeEvent,
        id="notice-channel-delete",
    ),
    pytest.param(
        _event(
            "request",
            "friend",
            user_id="42",
            comment="hello",
            flag="flag-1",
        ),
        FriendRequestEvent,
        id="request-friend",
    ),
    pytest.param(
        _event(
            "request",
            "group",
            group_id="20000",
            user_id="42",
            comment="join",
            flag="flag-2",
        ),
        GroupRequestEvent,
        id="request-group",
    ),
    pytest.param(
        _event(
            "meta",
            "connect",
            version={
                "impl": "test",
                "version": "1.0.0",
                "onebot_version": "12",
            },
        ),
        ConnectMetaEvent,
        id="meta-connect",
    ),
    pytest.param(
        _event("meta", "heartbeat", interval=5000),
        HeartbeatMetaEvent,
        id="meta-heartbeat",
    ),
    pytest.param(
        _event(
            "meta",
            "status_update",
            status={
                "good": True,
                "bots": [{"self": _bot_self(), "online": True}],
            },
        ),
        StatusUpdateMetaEvent,
        id="meta-status-update",
    ),
)


@pytest.mark.parametrize(("payload", "event_class"), EVENT_CASES)
def test_each_standard_event_variant_round_trips_json(
    payload: dict[str, object],
    event_class: type[Event],
) -> None:
    event = EventPayload.model_validate(payload).root

    assert isinstance(event, event_class)
    assert EventPayload.model_validate_json(event.model_dump_json()).root == event


@pytest.mark.parametrize(
    ("payload", "event_class"),
    [
        pytest.param(
            _event(
                "message",
                "vendor.message",
                **{"vendor.payload": {"nested": [True, None]}},
            ),
            Event,
            id="message-extension",
        ),
        pytest.param(
            _event(
                "notice",
                "vendor.notice",
                **{"vendor.payload": {"nested": [True, None]}},
            ),
            NoticeEvent,
            id="notice-extension",
        ),
        pytest.param(
            _event(
                "request",
                "vendor.request",
                **{"vendor.payload": {"nested": [True, None]}},
            ),
            RequestEvent,
            id="request-extension",
        ),
        pytest.param(
            {
                **_event(
                    "meta",
                    "vendor.meta",
                    **{"vendor.payload": {"nested": [True, None]}},
                ),
                "self": None,
            },
            MetaEvent,
            id="meta-extension-explicit-null-self",
        ),
    ],
)
def test_event_extension_variants_preserve_json_fields(
    payload: dict[str, object],
    event_class: type[Event],
) -> None:
    event = EventPayload.model_validate(payload).root

    assert type(event) is event_class
    assert event.model_dump(mode="json", by_alias=True) == payload
    assert EventPayload.model_validate_json(event.model_dump_json()).root == event


def test_event_normalization_is_idempotent() -> None:
    payload = _message("private")

    normalized = EventPayload.model_validate(payload).model_dump(
        mode="json",
        by_alias=True,
    )

    assert (
        EventPayload.model_validate(normalized).model_dump(
            mode="json",
            by_alias=True,
        )
        == normalized
    )


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(True, id="boolean"),
        pytest.param(None, id="null"),
        pytest.param([], id="array"),
        pytest.param("1", id="string"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="positive-infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
    ],
)
def test_event_time_rejects_non_float_or_non_finite_value(value: object) -> None:
    with pytest.raises(ValidationError):
        EventPayload.model_validate({**_message("private"), "time": value})


@pytest.mark.parametrize(
    "interval",
    [
        pytest.param(1, id="minimum"),
        pytest.param(2**63 - 1, id="int64-maximum"),
    ],
)
def test_heartbeat_accepts_positive_int64_boundaries(interval: int) -> None:
    event = EventPayload.model_validate(
        _event("meta", "heartbeat", interval=interval),
    ).root

    assert isinstance(event, HeartbeatMetaEvent)
    assert event.interval == interval


@pytest.mark.parametrize(
    "interval",
    [
        pytest.param(True, id="boolean"),
        pytest.param(1.0, id="float"),
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param(2**63, id="above-int64-maximum"),
    ],
)
def test_heartbeat_rejects_non_positive_or_non_int64_interval(
    interval: object,
) -> None:
    with pytest.raises(ValidationError):
        EventPayload.model_validate(
            _event("meta", "heartbeat", interval=interval),
        )


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({}, id="empty"),
        pytest.param({**_message("private"), "id": 1}, id="non-string-id"),
        pytest.param(
            {**_message("private"), "detail_type": 1},
            id="non-string-detail-type",
        ),
        pytest.param(
            {**_message("private"), "sub_type": None},
            id="null-sub-type",
        ),
        pytest.param(
            {**_message("private"), "self": {"platform": "qq"}},
            id="incomplete-self",
        ),
        pytest.param(
            {**_message("private"), "message": "hello"},
            id="message-not-segment-list",
        ),
        pytest.param(
            {key: value for key, value in _message("private").items() if key != "self"},
            id="non-meta-missing-self",
        ),
    ],
)
def test_event_rejects_invalid_protocol_shape(payload: object) -> None:
    with pytest.raises((TypeError, ValidationError)):
        EventPayload.model_validate(payload)


def test_event_extension_rejects_nested_non_finite_number() -> None:
    with pytest.raises(ValidationError):
        EventPayload.model_validate(
            _event(
                "notice",
                "vendor.notice",
                **{"vendor.payload": [float("nan")]},
            ),
        )
