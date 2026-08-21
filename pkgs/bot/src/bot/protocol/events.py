from typing import Annotated, Literal

from pydantic import (
    Field,
    InstanceOf,
    RootModel,
    SerializeAsAny,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
)

from .base import Model, field_value
from .common import BotSelf, Status, Version
from .enums import EventDetailType, EventKind
from .msg import Msg


class Event(Model):
    id: StrictStr
    time: StrictFloat
    type: EventKind
    detail_type: StrictStr
    sub_type: StrictStr
    self_: BotSelf = Field(alias="self")

    def __repr_args__(  # ruff: ignore[bad-dunder-method-name] - Pydantic's repr hook
        self,
    ) -> list[tuple[str | None, object]]:
        return [
            (name, value)
            for name, value in super().__repr_args__()
            if name in {"id", "type", "detail_type", "self_"}
        ]


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
        exclude_if=lambda value: value is None,
    )

    @field_validator("self_", mode="before")
    @classmethod
    def self_value(cls, value: object) -> object:
        if value is None:
            msg = "self must be omitted rather than null"
            raise ValueError(msg)
        return value


class ConnectMetaEvent(MetaEvent):
    detail_type: Literal[EventDetailType.CONNECT] = EventDetailType.CONNECT
    version: Version


class HeartbeatMetaEvent(MetaEvent):
    detail_type: Literal[EventDetailType.HEARTBEAT] = EventDetailType.HEARTBEAT
    interval: Annotated[StrictInt, Field(gt=0, le=2**63 - 1)]


class StatusUpdateMetaEvent(MetaEvent):
    detail_type: Literal[EventDetailType.STATUS_UPDATE] = EventDetailType.STATUS_UPDATE
    status: Status


_EVENT_MODELS: dict[str | tuple[str, str], type[Event]] = {
    (EventKind.MESSAGE, EventDetailType.PRIVATE): PrivateMessageEvent,
    (EventKind.MESSAGE, EventDetailType.GROUP): GroupMessageEvent,
    (EventKind.MESSAGE, EventDetailType.CHANNEL): ChannelMessageEvent,
    (EventKind.NOTICE, EventDetailType.FRIEND_INCREASE): FriendIncreaseNoticeEvent,
    (EventKind.NOTICE, EventDetailType.FRIEND_DECREASE): FriendDecreaseNoticeEvent,
    (
        EventKind.NOTICE,
        EventDetailType.PRIVATE_MESSAGE_DELETE,
    ): PrivateMessageDeleteNoticeEvent,
    (
        EventKind.NOTICE,
        EventDetailType.GROUP_MEMBER_INCREASE,
    ): GroupMemberIncreaseNoticeEvent,
    (
        EventKind.NOTICE,
        EventDetailType.GROUP_MEMBER_DECREASE,
    ): GroupMemberDecreaseNoticeEvent,
    (
        EventKind.NOTICE,
        EventDetailType.GROUP_MESSAGE_DELETE,
    ): GroupMessageDeleteNoticeEvent,
    (
        EventKind.NOTICE,
        EventDetailType.GUILD_MEMBER_INCREASE,
    ): GuildMemberIncreaseNoticeEvent,
    (
        EventKind.NOTICE,
        EventDetailType.GUILD_MEMBER_DECREASE,
    ): GuildMemberDecreaseNoticeEvent,
    (
        EventKind.NOTICE,
        EventDetailType.CHANNEL_MEMBER_INCREASE,
    ): ChannelMemberIncreaseNoticeEvent,
    (
        EventKind.NOTICE,
        EventDetailType.CHANNEL_MEMBER_DECREASE,
    ): ChannelMemberDecreaseNoticeEvent,
    (
        EventKind.NOTICE,
        EventDetailType.CHANNEL_MESSAGE_DELETE,
    ): ChannelMessageDeleteNoticeEvent,
    (EventKind.NOTICE, EventDetailType.CHANNEL_CREATE): ChannelCreateNoticeEvent,
    (EventKind.NOTICE, EventDetailType.CHANNEL_DELETE): ChannelDeleteNoticeEvent,
    (EventKind.REQUEST, EventDetailType.FRIEND): FriendRequestEvent,
    (EventKind.REQUEST, EventDetailType.GROUP): GroupRequestEvent,
    (EventKind.META, EventDetailType.CONNECT): ConnectMetaEvent,
    (EventKind.META, EventDetailType.HEARTBEAT): HeartbeatMetaEvent,
    (EventKind.META, EventDetailType.STATUS_UPDATE): StatusUpdateMetaEvent,
    EventKind.NOTICE: NoticeEvent,
    EventKind.REQUEST: RequestEvent,
    EventKind.META: MetaEvent,
}


class EventPayload(RootModel[SerializeAsAny[InstanceOf[Event]]]):
    @field_validator("root", mode="before")
    @classmethod
    def parse_event(cls, value: object) -> Event:
        event_type = field_value(value, "type")
        detail_type = field_value(value, "detail_type")
        model = (
            _EVENT_MODELS.get((event_type, detail_type))
            if isinstance(event_type, str) and isinstance(detail_type, str)
            else None
        )
        if model is None and isinstance(event_type, str):
            model = _EVENT_MODELS.get(event_type)
        return (model or Event).model_validate(value)
