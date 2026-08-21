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
    InstanceOf,
    JsonValue,
    PlainSerializer,
    SerializeAsAny,
    StrictBool,
    StrictBytes,
    StrictInt,
    StrictStr,
    StringConstraints,
    Tag,
    TypeAdapter,
    ValidationInfo,
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
    retcode: Annotated[StrictInt, Field(ge=0, le=MAX_RETCODE)]
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


def _action_call_tag(action: str) -> ActionCallTag:
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


_ACTION_PARAM_ADAPTERS = {
    ActionCallTag.EMPTY: TypeAdapter(EmptyActionParams),
    ActionCallTag.LATEST_EVENTS: TypeAdapter(LatestEventsParams),
    ActionCallTag.SEND_MESSAGE: TypeAdapter(SendMsgParams),
    ActionCallTag.USER_ID: TypeAdapter(UserIdParams),
    ActionCallTag.MESSAGE_ID: TypeAdapter(MsgIdParams),
    ActionCallTag.GROUP_ID: TypeAdapter(GroupIdParams),
    ActionCallTag.GROUP_USER_ID: TypeAdapter(GroupUserIdParams),
    ActionCallTag.GROUP_NAME: TypeAdapter(GroupNameParams),
    ActionCallTag.GUILD_ID: TypeAdapter(GuildIdParams),
    ActionCallTag.GUILD_USER_ID: TypeAdapter(GuildUserIdParams),
    ActionCallTag.GUILD_NAME: TypeAdapter(GuildNameParams),
    ActionCallTag.CHANNEL_ID: TypeAdapter(ChannelIdParams),
    ActionCallTag.CHANNEL_LIST: TypeAdapter(ChannelListParams),
    ActionCallTag.CHANNEL_USER_ID: TypeAdapter(ChannelUserIdParams),
    ActionCallTag.CHANNEL_NAME: TypeAdapter(ChannelNameParams),
    ActionCallTag.GET_FILE: TypeAdapter(GetFileParams),
    ActionCallTag.UPLOAD_FILE: TypeAdapter(UploadFileParams),
    ActionCallTag.UPLOAD_FILE_FRAGMENTED: TypeAdapter(FragmentedUploadParams),
    ActionCallTag.GET_FILE_FRAGMENTED: TypeAdapter(FragmentedGetParams),
    ActionCallTag.EXTENSION: TypeAdapter(ExtensionActionParams),
}


class ActionCall(Model):
    action: StrictStr
    params: SerializeAsAny[InstanceOf[ActionParamModel]]

    @field_validator("params", mode="before")
    @classmethod
    def params_for_action(
        cls,
        value: object,
        info: ValidationInfo,
    ) -> ActionParamModel:
        action = info.data.get("action")
        tag = (
            _action_call_tag(action)
            if isinstance(action, str)
            else ActionCallTag.EXTENSION
        )
        return _ACTION_PARAM_ADAPTERS[tag].validate_python(value)

    def __str__(self) -> str:
        params = str(self.params)
        return self.action if params == "-" else f"{self.action} {params}"
