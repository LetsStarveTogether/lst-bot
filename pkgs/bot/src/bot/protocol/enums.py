from enum import IntEnum, StrEnum, auto


class ApiStatus(StrEnum):
    OK = auto()
    ASYNC = auto()
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
