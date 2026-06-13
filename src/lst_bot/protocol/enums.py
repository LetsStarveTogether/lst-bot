from __future__ import annotations

from enum import IntEnum, StrEnum, auto


class ApiStatus(StrEnum):
    OK = auto()
    FAILED = auto()


class Retcode(IntEnum):
    OK = 0
    BAD_REQUEST = 10001
    UNSUPPORTED_ACTION = 10002
    BAD_PARAM = 10003
    UNSUPPORTED_PARAM = 10004
    UNSUPPORTED_SEGMENT = 10005
    BAD_SEGMENT_DATA = 10006
    UNSUPPORTED_SEGMENT_DATA = 10007
    WHO_AM_I = 10101
    UNKNOWN_SELF = 10102
    BAD_HANDLER = 20001
    INTERNAL_HANDLER_ERROR = 20002


class Action(StrEnum):
    GET_LATEST_EVENTS = auto()
    GET_SUPPORTED_ACTIONS = auto()
    GET_STATUS = auto()
    GET_VERSION = auto()
    GET_SELF_INFO = auto()
    GET_USER_INFO = auto()
    GET_FRIEND_LIST = auto()
    SEND_MESSAGE = auto()
    DELETE_MESSAGE = auto()
    GET_GROUP_INFO = auto()
    GET_GROUP_LIST = auto()
    GET_GROUP_MEMBER_INFO = auto()
    GET_GROUP_MEMBER_LIST = auto()
    SET_GROUP_NAME = auto()
    LEAVE_GROUP = auto()
    GET_GUILD_INFO = auto()
    GET_GUILD_LIST = auto()
    SET_GUILD_NAME = auto()
    GET_GUILD_MEMBER_INFO = auto()
    GET_GUILD_MEMBER_LIST = auto()
    LEAVE_GUILD = auto()
    GET_CHANNEL_INFO = auto()
    GET_CHANNEL_LIST = auto()
    SET_CHANNEL_NAME = auto()
    GET_CHANNEL_MEMBER_INFO = auto()
    GET_CHANNEL_MEMBER_LIST = auto()
    LEAVE_CHANNEL = auto()
    UPLOAD_FILE = auto()
    UPLOAD_FILE_FRAGMENTED = auto()
    GET_FILE = auto()
    GET_FILE_FRAGMENTED = auto()


class ActionCallTag(StrEnum):
    EMPTY = auto()
    LATEST_EVENTS = auto()
    SEND_MESSAGE = auto()
    USER_ID = auto()
    MESSAGE_ID = auto()
    GROUP_ID = auto()
    GROUP_USER_ID = auto()
    GROUP_NAME = auto()
    GUILD_ID = auto()
    GUILD_USER_ID = auto()
    GUILD_NAME = auto()
    CHANNEL_ID = auto()
    CHANNEL_LIST = auto()
    CHANNEL_USER_ID = auto()
    CHANNEL_NAME = auto()
    GET_FILE = auto()
    UPLOAD_FILE = auto()
    UPLOAD_FILE_FRAGMENTED = auto()
    GET_FILE_FRAGMENTED = auto()
    EXTENSION = auto()


class EventKind(StrEnum):
    MESSAGE = auto()
    NOTICE = auto()
    REQUEST = auto()
    META = auto()


class EventDetailType(StrEnum):
    FRIEND = auto()
    PRIVATE = auto()
    GROUP = auto()
    CHANNEL = auto()
    FRIEND_INCREASE = auto()
    FRIEND_DECREASE = auto()
    PRIVATE_MESSAGE_DELETE = auto()
    GROUP_MEMBER_INCREASE = auto()
    GROUP_MEMBER_DECREASE = auto()
    GROUP_MESSAGE_DELETE = auto()
    GUILD_MEMBER_INCREASE = auto()
    GUILD_MEMBER_DECREASE = auto()
    CHANNEL_MEMBER_INCREASE = auto()
    CHANNEL_MEMBER_DECREASE = auto()
    CHANNEL_MESSAGE_DELETE = auto()
    CHANNEL_CREATE = auto()
    CHANNEL_DELETE = auto()
    CONNECT = auto()
    HEARTBEAT = auto()
    STATUS_UPDATE = auto()


class EventTag(StrEnum):
    MESSAGE_PRIVATE = "message:private"
    MESSAGE_GROUP = "message:group"
    MESSAGE_CHANNEL = "message:channel"
    REQUEST_FRIEND = "request:friend"
    REQUEST_GROUP = "request:group"
    NOTICE_FRIEND_INCREASE = "notice:friend_increase"
    NOTICE_FRIEND_DECREASE = "notice:friend_decrease"
    NOTICE_PRIVATE_MESSAGE_DELETE = "notice:private_message_delete"
    NOTICE_GROUP_MEMBER_INCREASE = "notice:group_member_increase"
    NOTICE_GROUP_MEMBER_DECREASE = "notice:group_member_decrease"
    NOTICE_GROUP_MESSAGE_DELETE = "notice:group_message_delete"
    NOTICE_GUILD_MEMBER_INCREASE = "notice:guild_member_increase"
    NOTICE_GUILD_MEMBER_DECREASE = "notice:guild_member_decrease"
    NOTICE_CHANNEL_MEMBER_INCREASE = "notice:channel_member_increase"
    NOTICE_CHANNEL_MEMBER_DECREASE = "notice:channel_member_decrease"
    NOTICE_CHANNEL_MESSAGE_DELETE = "notice:channel_message_delete"
    NOTICE_CHANNEL_CREATE = "notice:channel_create"
    NOTICE_CHANNEL_DELETE = "notice:channel_delete"
    META_CONNECT = "meta:connect"
    META_HEARTBEAT = "meta:heartbeat"
    META_STATUS_UPDATE = "meta:status_update"
    EXTENSION = auto()
    META_EXTENSION = auto()
    REQUEST_EXTENSION = auto()


class FileStage(StrEnum):
    PREPARE = auto()
    TRANSFER = auto()
    FINISH = auto()


class MsgSegmentType(StrEnum):
    TEXT = auto()
    MENTION = auto()
    MENTION_ALL = auto()
    IMAGE = auto()
    VOICE = auto()
    AUDIO = auto()
    VIDEO = auto()
    FILE = auto()
    LOCATION = auto()
    REPLY = auto()
    EXTENSION = auto()


class MsgTargetTag(StrEnum):
    PRIVATE = auto()
    GROUP = auto()
    CHANNEL = auto()
    EXTENSION = auto()


class UploadFileTag(StrEnum):
    URL = auto()
    PATH = auto()
    DATA = auto()
    EXTENSION = auto()


__all__ = [
    "Action",
    "ActionCallTag",
    "ApiStatus",
    "EventDetailType",
    "EventKind",
    "EventTag",
    "FileStage",
    "MsgSegmentType",
    "MsgTargetTag",
    "Retcode",
    "UploadFileTag",
]
