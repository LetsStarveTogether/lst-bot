from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal, cast

from pydantic import (
    Discriminator,
    Field,
    RootModel,
    StrictInt,
    StrictStr,
    Tag,
)

from .base import Model
from .common import BotSelf, Status, Version
from .enums import EventDetailType, EventKind, EventTag
from .field import UnixSecondsFloat
from .msg import Msg


class Event(Model):
    id: StrictStr
    time: UnixSecondsFloat
    type: EventKind
    detail_type: StrictStr
    sub_type: StrictStr
    self_: BotSelf = Field(alias="self")

    def __str__(self) -> str:
        parts = [f"{self.type}/{self.detail_type}#{self.id}"]

        guild_id = getattr(self, "guild_id", None)
        channel_id = getattr(self, "channel_id", None)
        group_id = getattr(self, "group_id", None)
        user_id = getattr(self, "user_id", None)
        if guild_id and channel_id:
            parts.append(f"channel:{guild_id}/{channel_id}")
        elif group_id:
            parts.append(f"group:{group_id}")
        elif guild_id:
            parts.append(f"guild:{guild_id}")
        elif user_id:
            parts.append(f"user:{user_id}")

        alt_message = getattr(self, "alt_message", "")
        if isinstance(alt_message, str) and alt_message:
            text = " ".join(alt_message.split())
            if text:
                parts.append(f'"{text}"')

        return " ".join(parts)


class UserEvent(Event):
    user_id: StrictStr


class MessageEvent(UserEvent):
    type: Literal[EventKind.MESSAGE] = EventKind.MESSAGE
    message_id: StrictStr
    message: Msg
    alt_message: StrictStr


class PrivateMessageEvent(MessageEvent):
    detail_type: Literal[EventDetailType.PRIVATE] = EventDetailType.PRIVATE


class GroupMessageEvent(MessageEvent):
    detail_type: Literal[EventDetailType.GROUP] = EventDetailType.GROUP
    group_id: StrictStr


class ChannelMessageEvent(MessageEvent):
    detail_type: Literal[EventDetailType.CHANNEL] = EventDetailType.CHANNEL
    guild_id: StrictStr
    channel_id: StrictStr


class NoticeEvent(Event):
    type: Literal[EventKind.NOTICE] = EventKind.NOTICE


class FriendIncreaseNoticeEvent(NoticeEvent, UserEvent):
    detail_type: Literal[EventDetailType.FRIEND_INCREASE] = (
        EventDetailType.FRIEND_INCREASE
    )


class FriendDecreaseNoticeEvent(NoticeEvent, UserEvent):
    detail_type: Literal[EventDetailType.FRIEND_DECREASE] = (
        EventDetailType.FRIEND_DECREASE
    )


class PrivateMessageDeleteNoticeEvent(NoticeEvent, UserEvent):
    detail_type: Literal[EventDetailType.PRIVATE_MESSAGE_DELETE] = (
        EventDetailType.PRIVATE_MESSAGE_DELETE
    )
    message_id: StrictStr


class GroupMemberIncreaseNoticeEvent(NoticeEvent, UserEvent):
    detail_type: Literal[EventDetailType.GROUP_MEMBER_INCREASE] = (
        EventDetailType.GROUP_MEMBER_INCREASE
    )
    group_id: StrictStr
    operator_id: StrictStr


class GroupMemberDecreaseNoticeEvent(NoticeEvent, UserEvent):
    detail_type: Literal[EventDetailType.GROUP_MEMBER_DECREASE] = (
        EventDetailType.GROUP_MEMBER_DECREASE
    )
    group_id: StrictStr
    operator_id: StrictStr


class GroupMessageDeleteNoticeEvent(NoticeEvent, UserEvent):
    detail_type: Literal[EventDetailType.GROUP_MESSAGE_DELETE] = (
        EventDetailType.GROUP_MESSAGE_DELETE
    )
    group_id: StrictStr
    message_id: StrictStr
    operator_id: StrictStr


class GuildMemberIncreaseNoticeEvent(NoticeEvent, UserEvent):
    detail_type: Literal[EventDetailType.GUILD_MEMBER_INCREASE] = (
        EventDetailType.GUILD_MEMBER_INCREASE
    )
    guild_id: StrictStr
    operator_id: StrictStr


class GuildMemberDecreaseNoticeEvent(NoticeEvent, UserEvent):
    detail_type: Literal[EventDetailType.GUILD_MEMBER_DECREASE] = (
        EventDetailType.GUILD_MEMBER_DECREASE
    )
    guild_id: StrictStr
    operator_id: StrictStr


class ChannelMemberIncreaseNoticeEvent(NoticeEvent, UserEvent):
    detail_type: Literal[EventDetailType.CHANNEL_MEMBER_INCREASE] = (
        EventDetailType.CHANNEL_MEMBER_INCREASE
    )
    guild_id: StrictStr
    channel_id: StrictStr
    operator_id: StrictStr


class ChannelMemberDecreaseNoticeEvent(NoticeEvent, UserEvent):
    detail_type: Literal[EventDetailType.CHANNEL_MEMBER_DECREASE] = (
        EventDetailType.CHANNEL_MEMBER_DECREASE
    )
    guild_id: StrictStr
    channel_id: StrictStr
    operator_id: StrictStr


class ChannelMessageDeleteNoticeEvent(NoticeEvent, UserEvent):
    detail_type: Literal[EventDetailType.CHANNEL_MESSAGE_DELETE] = (
        EventDetailType.CHANNEL_MESSAGE_DELETE
    )
    guild_id: StrictStr
    channel_id: StrictStr
    message_id: StrictStr
    operator_id: StrictStr


class ChannelCreateNoticeEvent(NoticeEvent):
    detail_type: Literal[EventDetailType.CHANNEL_CREATE] = (
        EventDetailType.CHANNEL_CREATE
    )
    guild_id: StrictStr
    channel_id: StrictStr
    operator_id: StrictStr


class ChannelDeleteNoticeEvent(NoticeEvent):
    detail_type: Literal[EventDetailType.CHANNEL_DELETE] = (
        EventDetailType.CHANNEL_DELETE
    )
    guild_id: StrictStr
    channel_id: StrictStr
    operator_id: StrictStr


class RequestEvent(Event):
    type: Literal[EventKind.REQUEST] = EventKind.REQUEST


class FriendRequestEvent(RequestEvent, UserEvent):
    detail_type: Literal[EventDetailType.FRIEND] = EventDetailType.FRIEND
    comment: StrictStr
    flag: StrictStr


class GroupRequestEvent(RequestEvent, UserEvent):
    detail_type: Literal[EventDetailType.GROUP] = EventDetailType.GROUP
    group_id: StrictStr
    comment: StrictStr
    flag: StrictStr


class MetaEvent(Event):
    type: Literal[EventKind.META] = EventKind.META
    self_: BotSelf | None = Field(
        alias="self",
        default=None,
    )


class ConnectMetaEvent(MetaEvent):
    detail_type: Literal[EventDetailType.CONNECT] = EventDetailType.CONNECT
    version: Version


class HeartbeatMetaEvent(MetaEvent):
    detail_type: Literal[EventDetailType.HEARTBEAT] = EventDetailType.HEARTBEAT
    interval: StrictInt


class StatusUpdateMetaEvent(MetaEvent):
    detail_type: Literal[EventDetailType.STATUS_UPDATE] = EventDetailType.STATUS_UPDATE
    status: Status


type EventVariant = (
    PrivateMessageEvent
    | GroupMessageEvent
    | ChannelMessageEvent
    | FriendIncreaseNoticeEvent
    | FriendDecreaseNoticeEvent
    | PrivateMessageDeleteNoticeEvent
    | GroupMemberIncreaseNoticeEvent
    | GroupMemberDecreaseNoticeEvent
    | GroupMessageDeleteNoticeEvent
    | GuildMemberIncreaseNoticeEvent
    | GuildMemberDecreaseNoticeEvent
    | ChannelMemberIncreaseNoticeEvent
    | ChannelMemberDecreaseNoticeEvent
    | ChannelMessageDeleteNoticeEvent
    | ChannelCreateNoticeEvent
    | ChannelDeleteNoticeEvent
    | FriendRequestEvent
    | GroupRequestEvent
    | RequestEvent
    | ConnectMetaEvent
    | HeartbeatMetaEvent
    | StatusUpdateMetaEvent
    | MetaEvent
    | Event
)


def _field_value(value: object, key: str) -> object:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value).get(key)
    return getattr(value, key, None)


def _event_tag(value: object) -> EventTag:
    event_type = _field_value(value, "type")
    detail_type = _field_value(value, "detail_type")
    if isinstance(event_type, str) and isinstance(detail_type, str):
        try:
            return EventTag(f"{event_type}:{detail_type}")
        except ValueError:
            pass
    if event_type == EventKind.META:
        return EventTag.META_EXTENSION
    if event_type == EventKind.REQUEST:
        return EventTag.REQUEST_EXTENSION
    return EventTag.EXTENSION


type EventPayloadVariant = Annotated[
    Annotated[PrivateMessageEvent, Tag(EventTag.MESSAGE_PRIVATE)]
    | Annotated[GroupMessageEvent, Tag(EventTag.MESSAGE_GROUP)]
    | Annotated[ChannelMessageEvent, Tag(EventTag.MESSAGE_CHANNEL)]
    | Annotated[
        FriendIncreaseNoticeEvent,
        Tag(EventTag.NOTICE_FRIEND_INCREASE),
    ]
    | Annotated[
        FriendDecreaseNoticeEvent,
        Tag(EventTag.NOTICE_FRIEND_DECREASE),
    ]
    | Annotated[
        PrivateMessageDeleteNoticeEvent,
        Tag(EventTag.NOTICE_PRIVATE_MESSAGE_DELETE),
    ]
    | Annotated[
        GroupMemberIncreaseNoticeEvent,
        Tag(EventTag.NOTICE_GROUP_MEMBER_INCREASE),
    ]
    | Annotated[
        GroupMemberDecreaseNoticeEvent,
        Tag(EventTag.NOTICE_GROUP_MEMBER_DECREASE),
    ]
    | Annotated[
        GroupMessageDeleteNoticeEvent,
        Tag(EventTag.NOTICE_GROUP_MESSAGE_DELETE),
    ]
    | Annotated[
        GuildMemberIncreaseNoticeEvent,
        Tag(EventTag.NOTICE_GUILD_MEMBER_INCREASE),
    ]
    | Annotated[
        GuildMemberDecreaseNoticeEvent,
        Tag(EventTag.NOTICE_GUILD_MEMBER_DECREASE),
    ]
    | Annotated[
        ChannelMemberIncreaseNoticeEvent,
        Tag(EventTag.NOTICE_CHANNEL_MEMBER_INCREASE),
    ]
    | Annotated[
        ChannelMemberDecreaseNoticeEvent,
        Tag(EventTag.NOTICE_CHANNEL_MEMBER_DECREASE),
    ]
    | Annotated[
        ChannelMessageDeleteNoticeEvent,
        Tag(EventTag.NOTICE_CHANNEL_MESSAGE_DELETE),
    ]
    | Annotated[
        ChannelCreateNoticeEvent,
        Tag(EventTag.NOTICE_CHANNEL_CREATE),
    ]
    | Annotated[
        ChannelDeleteNoticeEvent,
        Tag(EventTag.NOTICE_CHANNEL_DELETE),
    ]
    | Annotated[FriendRequestEvent, Tag(EventTag.REQUEST_FRIEND)]
    | Annotated[GroupRequestEvent, Tag(EventTag.REQUEST_GROUP)]
    | Annotated[RequestEvent, Tag(EventTag.REQUEST_EXTENSION)]
    | Annotated[ConnectMetaEvent, Tag(EventTag.META_CONNECT)]
    | Annotated[HeartbeatMetaEvent, Tag(EventTag.META_HEARTBEAT)]
    | Annotated[
        StatusUpdateMetaEvent,
        Tag(EventTag.META_STATUS_UPDATE),
    ]
    | Annotated[MetaEvent, Tag(EventTag.META_EXTENSION)]
    | Annotated[Event, Tag(EventTag.EXTENSION)],
    Discriminator(_event_tag),
]


class EventPayload(RootModel[EventPayloadVariant]):
    pass


__all__ = [
    "ChannelCreateNoticeEvent",
    "ChannelDeleteNoticeEvent",
    "ChannelMemberDecreaseNoticeEvent",
    "ChannelMemberIncreaseNoticeEvent",
    "ChannelMessageDeleteNoticeEvent",
    "ChannelMessageEvent",
    "ConnectMetaEvent",
    "Event",
    "EventPayload",
    "EventPayloadVariant",
    "EventVariant",
    "FriendDecreaseNoticeEvent",
    "FriendIncreaseNoticeEvent",
    "FriendRequestEvent",
    "GroupMemberDecreaseNoticeEvent",
    "GroupMemberIncreaseNoticeEvent",
    "GroupMessageDeleteNoticeEvent",
    "GroupMessageEvent",
    "GroupRequestEvent",
    "GuildMemberDecreaseNoticeEvent",
    "GuildMemberIncreaseNoticeEvent",
    "HeartbeatMetaEvent",
    "MessageEvent",
    "MetaEvent",
    "NoticeEvent",
    "PrivateMessageDeleteNoticeEvent",
    "PrivateMessageEvent",
    "RequestEvent",
    "StatusUpdateMetaEvent",
    "UserEvent",
]
