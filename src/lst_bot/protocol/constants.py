from __future__ import annotations

import re

from .enums import (
    Action,
    ActionCallTag,
    MsgTargetTag,
    UploadFileTag,
)

MAX_RETCODE = 99999
NAME_PATTERN = re.compile(r"[a-z][\-a-z0-9]*(\.[\-a-z0-9]+)*")
SHA256_STRING_PATTERN = r"^[a-fA-F0-9]{64}$"

NO_PARAM_ACTIONS = frozenset({
    Action.GET_SUPPORTED_ACTIONS,
    Action.GET_STATUS,
    Action.GET_VERSION,
    Action.GET_SELF_INFO,
    Action.GET_FRIEND_LIST,
    Action.GET_GROUP_LIST,
    Action.GET_GUILD_LIST,
})

ACTION_CALL_TAGS = {
    Action.SEND_MESSAGE: ActionCallTag.SEND_MESSAGE,
    Action.GET_LATEST_EVENTS: ActionCallTag.LATEST_EVENTS,
    Action.GET_USER_INFO: ActionCallTag.USER_ID,
    Action.DELETE_MESSAGE: ActionCallTag.MESSAGE_ID,
    Action.GET_GROUP_INFO: ActionCallTag.GROUP_ID,
    Action.GET_GROUP_MEMBER_INFO: ActionCallTag.GROUP_USER_ID,
    Action.GET_GROUP_MEMBER_LIST: ActionCallTag.GROUP_ID,
    Action.SET_GROUP_NAME: ActionCallTag.GROUP_NAME,
    Action.LEAVE_GROUP: ActionCallTag.GROUP_ID,
    Action.GET_GUILD_INFO: ActionCallTag.GUILD_ID,
    Action.SET_GUILD_NAME: ActionCallTag.GUILD_NAME,
    Action.GET_GUILD_MEMBER_INFO: ActionCallTag.GUILD_USER_ID,
    Action.GET_GUILD_MEMBER_LIST: ActionCallTag.GUILD_ID,
    Action.LEAVE_GUILD: ActionCallTag.GUILD_ID,
    Action.GET_CHANNEL_INFO: ActionCallTag.CHANNEL_ID,
    Action.GET_CHANNEL_LIST: ActionCallTag.CHANNEL_LIST,
    Action.SET_CHANNEL_NAME: ActionCallTag.CHANNEL_NAME,
    Action.GET_CHANNEL_MEMBER_INFO: ActionCallTag.CHANNEL_USER_ID,
    Action.GET_CHANNEL_MEMBER_LIST: ActionCallTag.CHANNEL_ID,
    Action.LEAVE_CHANNEL: ActionCallTag.CHANNEL_ID,
    Action.UPLOAD_FILE: ActionCallTag.UPLOAD_FILE,
    Action.UPLOAD_FILE_FRAGMENTED: ActionCallTag.UPLOAD_FILE_FRAGMENTED,
    Action.GET_FILE: ActionCallTag.GET_FILE,
    Action.GET_FILE_FRAGMENTED: ActionCallTag.GET_FILE_FRAGMENTED,
    **dict.fromkeys(NO_PARAM_ACTIONS, ActionCallTag.EMPTY),
}

STRING_ACTION_PARAMS = frozenset({
    "channel_id",
    "channel_name",
    "data",
    "detail_type",
    "file_id",
    "group_id",
    "group_name",
    "guild_id",
    "guild_name",
    "message_id",
    "name",
    "operator_id",
    "path",
    "sha256",
    "stage",
    "type",
    "url",
    "user_id",
})

INT_ACTION_PARAMS = frozenset({
    "limit",
    "offset",
    "size",
    "timeout",
    "total_size",
})
BOOL_ACTION_PARAMS = frozenset({"joined_only"})
SEND_MSG_DETAIL_TYPES = frozenset({
    MsgTargetTag.PRIVATE,
    MsgTargetTag.GROUP,
    MsgTargetTag.CHANNEL,
})
UPLOAD_FILE_TYPES = frozenset({
    UploadFileTag.URL,
    UploadFileTag.PATH,
    UploadFileTag.DATA,
})

__all__ = [
    "ACTION_CALL_TAGS",
    "BOOL_ACTION_PARAMS",
    "INT_ACTION_PARAMS",
    "MAX_RETCODE",
    "NAME_PATTERN",
    "NO_PARAM_ACTIONS",
    "SEND_MSG_DETAIL_TYPES",
    "SHA256_STRING_PATTERN",
    "STRING_ACTION_PARAMS",
    "UPLOAD_FILE_TYPES",
]
