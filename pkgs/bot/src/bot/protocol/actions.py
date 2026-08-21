from __future__ import annotations

from base64 import b64decode, b64encode
from binascii import Error as Base64Error
from collections.abc import Mapping
from typing import Annotated, Literal, Self, cast

from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    Discriminator,
    Field,
    JsonValue,
    PlainSerializer,
    RootModel,
    SerializeAsAny,
    StrictBool,
    StrictBytes,
    StrictInt,
    StrictStr,
    StringConstraints,
    Tag,
    field_validator,
    model_validator,
)

from .base import Model
from .common import BotSelf
from .constants import (
    ACTION_CALL_TAGS,
    MAX_RETCODE,
    SHA256_STRING_PATTERN,
)
from .enums import (
    Action,
    ActionCallTag,
    ApiStatus,
    FileStage,
    MsgTargetTag,
    Retcode,
    UploadFileTag,
)
from .msg import MsgValue

type ActionParamInput = (
    BaseModel
    | JsonValue
    | bytes
    | bytearray
    | Mapping[str, ActionParamInput]
    | list[ActionParamInput]
    | tuple[ActionParamInput, ...]
)
type NonNegativeStrictInt = Annotated[StrictInt, Field(ge=0, le=2**63 - 1)]
type Sha256String = Annotated[
    StrictStr,
    StringConstraints(pattern=SHA256_STRING_PATTERN, to_lower=True),
]
type HeaderMap = dict[StrictStr, StrictStr]


def _load_base64_bytes(value: object) -> object:
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, bytes) or not isinstance(value, str):
        return value
    try:
        return b64decode(value, validate=True)
    except Base64Error, ValueError:
        msg = "bytes must be valid Base64"
        raise ValueError(msg) from None


def _dump_base64_bytes(value: bytes) -> str:
    return b64encode(value).decode()


type WireBytes = Annotated[
    StrictBytes,
    BeforeValidator(_load_base64_bytes),
    PlainSerializer(_dump_base64_bytes, return_type=str, when_used="json"),
]

type _ActionParamValue = (
    JsonValue
    | WireBytes
    | SerializeAsAny[BaseModel]
    | dict[str, _ActionParamValue]
    | list[_ActionParamValue]
)


class ActionParamModel(Model):
    __pydantic_extra__: dict[str, _ActionParamValue] = Field(init=False)

    @model_validator(mode="before")
    @classmethod
    def model_input(cls, value: object) -> object:
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json", by_alias=True, serialize_as_any=True)
        return value

    def __str__(self) -> str:
        parts: list[str] = []

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
        if user_id:
            parts.append(f"user:{user_id}")

        message_id = getattr(self, "message_id", None)
        if message_id:
            parts.append(f"msg:{message_id}")
        file_id = getattr(self, "file_id", None)
        if file_id:
            parts.append(f"file:{file_id}")
        stage = getattr(self, "stage", None)
        if stage:
            parts.append(f"stage:{stage}")
        file_type = getattr(self, "type", None)
        if file_type:
            parts.append(f"type:{file_type}")

        if parts:
            return " ".join(parts)

        fields = {
            key
            for key in (*type(self).model_fields, *(self.model_extra or ()))
            if key not in {"data", "headers", "message"}
            and getattr(self, key, None) is not None
        }
        return f"params={len(fields)}" if fields else "-"


class ActionRequest(Model):
    action: StrictStr
    params: SerializeAsAny[ActionParamModel]
    echo: StrictStr | None = None
    self_: BotSelf | None = Field(
        alias="self",
        default=None,
    )

    def __str__(self) -> str:
        params = str(self.params)
        text = self.action if params == "-" else f"{self.action} {params}"
        if self.self_ is None:
            return text
        return f"{text} @ {self.self_}"


class ActionResponse(Model):
    status: ApiStatus
    retcode: StrictInt
    data: JsonValue
    message: StrictStr
    echo: StrictStr | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @field_validator("echo", mode="before")
    @classmethod
    def echo_value(cls, value: object) -> object:
        if isinstance(value, str) and not value:
            return None
        return value

    def __str__(self) -> str:
        text = f"{self.status}:{self.retcode}"
        if not self.message:
            return text
        message = " ".join(self.message.split())
        return f"{text} {message}"

    @model_validator(mode="after")
    def match_status_and_retcode(self) -> Self:
        if self.retcode < 0 or self.retcode > MAX_RETCODE:
            msg = "action response retcode must be between 0 and 99999"
            raise ValueError(msg)
        if self.status == ApiStatus.OK:
            if self.retcode != Retcode.OK:
                msg = "ok action response must use retcode 0"
                raise ValueError(msg)
            if self.message:
                msg = "ok action response message must be empty"
                raise ValueError(msg)
            return self
        if self.retcode == Retcode.OK:
            msg = "failed action response must not use retcode 0"
            raise ValueError(msg)
        return self

    @classmethod
    def ok(cls, data: JsonValue = None, *, echo: str | None = None) -> Self:
        return cls(
            status=ApiStatus.OK,
            retcode=Retcode.OK,
            data=data,
            message="",
            echo=echo,
        )

    @classmethod
    def failed(
        cls,
        retcode: Retcode,
        message: str,
        *,
        echo: str | None = None,
    ) -> Self:
        return cls(
            status=ApiStatus.FAILED,
            retcode=retcode,
            data=None,
            message=message,
            echo=echo,
        )


def _field_value(value: object, key: str) -> object:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value).get(key)
    return getattr(value, key, None)


def _send_msg_params_tag(value: object) -> MsgTargetTag:
    detail_type = _field_value(value, "detail_type")
    if detail_type is None:
        if _field_value(value, "guild_id") and _field_value(value, "channel_id"):
            return MsgTargetTag.CHANNEL
        if _field_value(value, "group_id"):
            return MsgTargetTag.GROUP
        if _field_value(value, "user_id"):
            return MsgTargetTag.PRIVATE
    try:
        return MsgTargetTag(detail_type)
    except ValueError:
        return MsgTargetTag.EXTENSION


def _upload_file_params_tag(value: object) -> UploadFileTag:
    file_type = _field_value(value, "type")
    try:
        return UploadFileTag(file_type)
    except ValueError:
        return UploadFileTag.EXTENSION


def _action_call_tag(value: object) -> ActionCallTag:
    action = _field_value(value, "action")
    try:
        action = Action(action)
    except ValueError:
        return ActionCallTag.EXTENSION
    return ACTION_CALL_TAGS.get(action, ActionCallTag.EXTENSION)


class EmptyActionParams(ActionParamModel):
    pass


class LatestEventsParams(ActionParamModel):
    limit: NonNegativeStrictInt | None = None
    timeout: NonNegativeStrictInt | None = None


class SendMsgBaseParams(ActionParamModel):
    detail_type: StrictStr
    message: MsgValue = Field(
        validation_alias=AliasChoices("message", "msg"),
        serialization_alias="message",
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_input(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        if data.get("detail_type") is None:
            if data.get("guild_id") and data.get("channel_id"):
                data["detail_type"] = MsgTargetTag.CHANNEL
            elif data.get("group_id"):
                data["detail_type"] = MsgTargetTag.GROUP
            elif data.get("user_id"):
                data["detail_type"] = MsgTargetTag.PRIVATE
        return data

    def __str__(self) -> str:
        guild_id = getattr(self, "guild_id", None)
        channel_id = getattr(self, "channel_id", None)
        group_id = getattr(self, "group_id", None)
        user_id = getattr(self, "user_id", None)
        if guild_id and channel_id:
            target = f"channel:{guild_id}/{channel_id}"
        elif group_id:
            target = f"group:{group_id}"
        elif user_id:
            target = f"user:{user_id}"
        else:
            target = str(self.detail_type or "-")

        text = " ".join(self.message.text.split())
        if text:
            message = f'"{text}"'
        else:
            count = len(self.message)
            message = f"{count} segments" if count else "-"
        return f"{target} {message}"


class SendPrivateMsgParams(SendMsgBaseParams):
    detail_type: Literal[MsgTargetTag.PRIVATE] = MsgTargetTag.PRIVATE
    user_id: StrictStr


class SendGroupMsgParams(SendMsgBaseParams):
    detail_type: Literal[MsgTargetTag.GROUP] = MsgTargetTag.GROUP
    group_id: StrictStr


class SendChannelMsgParams(SendMsgBaseParams):
    detail_type: Literal[MsgTargetTag.CHANNEL] = MsgTargetTag.CHANNEL
    guild_id: StrictStr
    channel_id: StrictStr


class SendExtensionMsgParams(SendMsgBaseParams):
    pass


type SendMsgParams = Annotated[
    Annotated[SendPrivateMsgParams, Tag(MsgTargetTag.PRIVATE)]
    | Annotated[SendGroupMsgParams, Tag(MsgTargetTag.GROUP)]
    | Annotated[SendChannelMsgParams, Tag(MsgTargetTag.CHANNEL)]
    | Annotated[SendExtensionMsgParams, Tag(MsgTargetTag.EXTENSION)],
    Discriminator(_send_msg_params_tag),
]


class UploadFileBaseParams(ActionParamModel):
    type: StrictStr
    name: StrictStr
    sha256: Sha256String | None = None


class UploadFileUrlParams(UploadFileBaseParams):
    type: Literal[UploadFileTag.URL] = UploadFileTag.URL
    url: StrictStr
    headers: HeaderMap | None = None


class UploadFilePathParams(UploadFileBaseParams):
    type: Literal[UploadFileTag.PATH] = UploadFileTag.PATH
    path: StrictStr


class UploadFileDataParams(UploadFileBaseParams):
    type: Literal[UploadFileTag.DATA] = UploadFileTag.DATA
    data: WireBytes


class UploadFileExtensionParams(UploadFileBaseParams):
    pass


type UploadFileParams = Annotated[
    Annotated[UploadFileUrlParams, Tag(UploadFileTag.URL)]
    | Annotated[UploadFilePathParams, Tag(UploadFileTag.PATH)]
    | Annotated[UploadFileDataParams, Tag(UploadFileTag.DATA)]
    | Annotated[UploadFileExtensionParams, Tag(UploadFileTag.EXTENSION)],
    Discriminator(_upload_file_params_tag),
]


class FragmentedUploadPrepareParams(ActionParamModel):
    stage: Literal[FileStage.PREPARE] = FileStage.PREPARE
    name: StrictStr
    total_size: NonNegativeStrictInt


class FragmentedUploadTransferParams(ActionParamModel):
    stage: Literal[FileStage.TRANSFER] = FileStage.TRANSFER
    file_id: StrictStr
    offset: NonNegativeStrictInt
    data: WireBytes


class FragmentedUploadFinishParams(ActionParamModel):
    stage: Literal[FileStage.FINISH] = FileStage.FINISH
    file_id: StrictStr
    sha256: Sha256String


type FragmentedUploadParams = Annotated[
    FragmentedUploadPrepareParams
    | FragmentedUploadTransferParams
    | FragmentedUploadFinishParams,
    Field(discriminator="stage"),
]


class FragmentedGetPrepareParams(ActionParamModel):
    stage: Literal[FileStage.PREPARE] = FileStage.PREPARE
    file_id: StrictStr


class FragmentedGetTransferParams(ActionParamModel):
    stage: Literal[FileStage.TRANSFER] = FileStage.TRANSFER
    file_id: StrictStr
    offset: NonNegativeStrictInt
    size: NonNegativeStrictInt


type FragmentedGetParams = Annotated[
    FragmentedGetPrepareParams | FragmentedGetTransferParams,
    Field(discriminator="stage"),
]


class UserIdParams(ActionParamModel):
    user_id: StrictStr


class MsgIdParams(ActionParamModel):
    message_id: StrictStr


class GroupIdParams(ActionParamModel):
    group_id: StrictStr


class GroupUserIdParams(GroupIdParams):
    user_id: StrictStr


class GroupNameParams(GroupIdParams):
    group_name: StrictStr


class GuildIdParams(ActionParamModel):
    guild_id: StrictStr


class GuildUserIdParams(GuildIdParams):
    user_id: StrictStr


class GuildNameParams(GuildIdParams):
    guild_name: StrictStr


class ChannelIdParams(GuildIdParams):
    channel_id: StrictStr


class ChannelListParams(GuildIdParams):
    joined_only: StrictBool | None = None


class ChannelUserIdParams(ChannelIdParams):
    user_id: StrictStr


class ChannelNameParams(ChannelIdParams):
    channel_name: StrictStr


class GetFileParams(ActionParamModel):
    file_id: StrictStr
    type: StrictStr


class ExtensionActionParams(ActionParamModel):
    pass


class ActionCallBase(Model):
    action: StrictStr
    params: ActionParamModel

    def __str__(self) -> str:
        params = str(self.params)
        return self.action if params == "-" else f"{self.action} {params}"


class EmptyActionCall(ActionCallBase):
    action: Literal[
        Action.GET_SUPPORTED_ACTIONS,
        Action.GET_STATUS,
        Action.GET_VERSION,
        Action.GET_SELF_INFO,
        Action.GET_FRIEND_LIST,
        Action.GET_GROUP_LIST,
        Action.GET_GUILD_LIST,
    ]
    params: EmptyActionParams


class LatestEventsActionCall(ActionCallBase):
    action: Literal[Action.GET_LATEST_EVENTS] = Action.GET_LATEST_EVENTS
    params: LatestEventsParams


class SendMsgActionCall(ActionCallBase):
    action: Literal[Action.SEND_MESSAGE] = Action.SEND_MESSAGE
    params: SendMsgParams


class UserIdActionCall(ActionCallBase):
    action: Literal[Action.GET_USER_INFO] = Action.GET_USER_INFO
    params: UserIdParams


class MsgIdActionCall(ActionCallBase):
    action: Literal[Action.DELETE_MESSAGE] = Action.DELETE_MESSAGE
    params: MsgIdParams


class GroupIdActionCall(ActionCallBase):
    action: Literal[
        Action.GET_GROUP_INFO,
        Action.GET_GROUP_MEMBER_LIST,
        Action.LEAVE_GROUP,
    ]
    params: GroupIdParams


class GroupUserIdActionCall(ActionCallBase):
    action: Literal[Action.GET_GROUP_MEMBER_INFO] = Action.GET_GROUP_MEMBER_INFO
    params: GroupUserIdParams


class GroupNameActionCall(ActionCallBase):
    action: Literal[Action.SET_GROUP_NAME] = Action.SET_GROUP_NAME
    params: GroupNameParams


class GuildIdActionCall(ActionCallBase):
    action: Literal[
        Action.GET_GUILD_INFO,
        Action.GET_GUILD_MEMBER_LIST,
        Action.LEAVE_GUILD,
    ]
    params: GuildIdParams


class GuildUserIdActionCall(ActionCallBase):
    action: Literal[Action.GET_GUILD_MEMBER_INFO] = Action.GET_GUILD_MEMBER_INFO
    params: GuildUserIdParams


class GuildNameActionCall(ActionCallBase):
    action: Literal[Action.SET_GUILD_NAME] = Action.SET_GUILD_NAME
    params: GuildNameParams


class ChannelIdActionCall(ActionCallBase):
    action: Literal[
        Action.GET_CHANNEL_INFO,
        Action.GET_CHANNEL_MEMBER_LIST,
        Action.LEAVE_CHANNEL,
    ]
    params: ChannelIdParams


class ChannelListActionCall(ActionCallBase):
    action: Literal[Action.GET_CHANNEL_LIST] = Action.GET_CHANNEL_LIST
    params: ChannelListParams


class ChannelUserIdActionCall(ActionCallBase):
    action: Literal[Action.GET_CHANNEL_MEMBER_INFO] = Action.GET_CHANNEL_MEMBER_INFO
    params: ChannelUserIdParams


class ChannelNameActionCall(ActionCallBase):
    action: Literal[Action.SET_CHANNEL_NAME] = Action.SET_CHANNEL_NAME
    params: ChannelNameParams


class GetFileActionCall(ActionCallBase):
    action: Literal[Action.GET_FILE] = Action.GET_FILE
    params: GetFileParams


class UploadFileActionCall(ActionCallBase):
    action: Literal[Action.UPLOAD_FILE] = Action.UPLOAD_FILE
    params: UploadFileParams


class FragmentedUploadActionCall(ActionCallBase):
    action: Literal[Action.UPLOAD_FILE_FRAGMENTED] = Action.UPLOAD_FILE_FRAGMENTED
    params: FragmentedUploadParams


class FragmentedGetActionCall(ActionCallBase):
    action: Literal[Action.GET_FILE_FRAGMENTED] = Action.GET_FILE_FRAGMENTED
    params: FragmentedGetParams


class ExtensionActionCall(ActionCallBase):
    params: ExtensionActionParams


type ActionCallVariant = Annotated[
    Annotated[EmptyActionCall, Tag(ActionCallTag.EMPTY)]
    | Annotated[LatestEventsActionCall, Tag(ActionCallTag.LATEST_EVENTS)]
    | Annotated[SendMsgActionCall, Tag(ActionCallTag.SEND_MESSAGE)]
    | Annotated[UserIdActionCall, Tag(ActionCallTag.USER_ID)]
    | Annotated[MsgIdActionCall, Tag(ActionCallTag.MESSAGE_ID)]
    | Annotated[GroupIdActionCall, Tag(ActionCallTag.GROUP_ID)]
    | Annotated[GroupUserIdActionCall, Tag(ActionCallTag.GROUP_USER_ID)]
    | Annotated[GroupNameActionCall, Tag(ActionCallTag.GROUP_NAME)]
    | Annotated[GuildIdActionCall, Tag(ActionCallTag.GUILD_ID)]
    | Annotated[GuildUserIdActionCall, Tag(ActionCallTag.GUILD_USER_ID)]
    | Annotated[GuildNameActionCall, Tag(ActionCallTag.GUILD_NAME)]
    | Annotated[ChannelIdActionCall, Tag(ActionCallTag.CHANNEL_ID)]
    | Annotated[ChannelListActionCall, Tag(ActionCallTag.CHANNEL_LIST)]
    | Annotated[ChannelUserIdActionCall, Tag(ActionCallTag.CHANNEL_USER_ID)]
    | Annotated[ChannelNameActionCall, Tag(ActionCallTag.CHANNEL_NAME)]
    | Annotated[GetFileActionCall, Tag(ActionCallTag.GET_FILE)]
    | Annotated[UploadFileActionCall, Tag(ActionCallTag.UPLOAD_FILE)]
    | Annotated[
        FragmentedUploadActionCall,
        Tag(ActionCallTag.UPLOAD_FILE_FRAGMENTED),
    ]
    | Annotated[
        FragmentedGetActionCall,
        Tag(ActionCallTag.GET_FILE_FRAGMENTED),
    ]
    | Annotated[ExtensionActionCall, Tag(ActionCallTag.EXTENSION)],
    Discriminator(_action_call_tag),
]


class ActionCall(RootModel[ActionCallVariant]):
    def __str__(self) -> str:
        return str(self.root)


__all__ = [
    "ActionCall",
    "ActionCallVariant",
    "ActionParamInput",
    "ActionRequest",
    "ActionResponse",
    "ChannelIdParams",
    "ChannelListParams",
    "ChannelNameParams",
    "ChannelUserIdParams",
    "EmptyActionParams",
    "FileStage",
    "FragmentedGetParams",
    "FragmentedUploadParams",
    "GetFileParams",
    "GroupIdParams",
    "GroupNameParams",
    "GroupUserIdParams",
    "GuildIdParams",
    "GuildNameParams",
    "GuildUserIdParams",
    "HeaderMap",
    "LatestEventsParams",
    "MsgIdParams",
    "NonNegativeStrictInt",
    "SendMsgParams",
    "Sha256String",
    "UploadFileParams",
    "UserIdParams",
]
