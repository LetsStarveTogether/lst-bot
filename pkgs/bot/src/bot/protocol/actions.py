from base64 import b64decode, b64encode
from binascii import Error as Base64Error
from collections.abc import Mapping
from typing import Annotated, Literal, Self

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    BeforeValidator,
    Discriminator,
    Field,
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
from pydantic.experimental.missing_sentinel import MISSING

from .base import Model, _field_value
from .common import BotSelf
from .enums import (
    Action,
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
type StrictInt64 = Annotated[StrictInt, Field(ge=-(2**63), le=2**63 - 1)]
type NonNegativeStrictInt = Annotated[StrictInt64, Field(ge=0)]
type Sha256String = Annotated[
    StrictStr,
    StringConstraints(pattern=r"^[a-f0-9]{64}$"),
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
    dict[str, _ActionParamValue]
    | list[_ActionParamValue]
    | JsonValue
    | WireBytes
    | SerializeAsAny[BaseModel]
)


class ActionParamModel(Model):
    __pydantic_extra__: dict[str, _ActionParamValue] = Field(init=False)

    @model_validator(mode="before")
    @classmethod
    def model_input(cls, value: object) -> object:
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json", by_alias=True, serialize_as_any=True)
        return value


class ActionResponse(Model):
    status: Literal[ApiStatus.OK, ApiStatus.ASYNC, ApiStatus.FAILED]
    retcode: StrictInt64
    data: JsonValue
    message: StrictStr
    echo: StrictStr | MISSING = MISSING

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
        if self.status == ApiStatus.ASYNC:
            if self.retcode != 1:
                msg = "async action response must use retcode 1"
                raise ValueError(msg)
            return self
        if self.retcode in {Retcode.OK, 1}:
            msg = "failed action response must not use retcode 0 or 1"
            raise ValueError(msg)
        return self

    @classmethod
    def ok(cls, data: JsonValue = None, *, echo: str | None = None) -> Self:
        return cls(
            status=ApiStatus.OK,
            retcode=Retcode.OK,
            data=data,
            message="",
            echo=MISSING
            if echo is None or (isinstance(echo, str) and not echo)
            else echo,
        )

    @classmethod
    def failed(
        cls,
        retcode: int,
        message: str,
        *,
        echo: str | None = None,
    ) -> Self:
        return cls(
            status=ApiStatus.FAILED,
            retcode=retcode,
            data=None,
            message=message,
            echo=MISSING
            if echo is None or (isinstance(echo, str) and not echo)
            else echo,
        )


def _send_msg_params_tag(value: object) -> MsgTargetTag:
    try:
        return MsgTargetTag(_field_value(value, "detail_type"))
    except ValueError:
        return MsgTargetTag.EXTENSION


def _upload_file_params_tag(value: object) -> UploadFileTag:
    file_type = _field_value(value, "type")
    try:
        return UploadFileTag(file_type)
    except ValueError:
        return UploadFileTag.EXTENSION


class LatestEventsParams(ActionParamModel):
    limit: NonNegativeStrictInt = 0
    timeout: NonNegativeStrictInt = 0


class SendMsgBaseParams(ActionParamModel):
    detail_type: StrictStr
    message: MsgValue


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


type SendMsgParams = Annotated[
    Annotated[SendPrivateMsgParams, Tag(MsgTargetTag.PRIVATE)]
    | Annotated[SendGroupMsgParams, Tag(MsgTargetTag.GROUP)]
    | Annotated[SendChannelMsgParams, Tag(MsgTargetTag.CHANNEL)]
    | Annotated[SendMsgBaseParams, Tag(MsgTargetTag.EXTENSION)],
    Discriminator(_send_msg_params_tag),
]


class UploadFileBaseParams(ActionParamModel):
    type: StrictStr
    name: StrictStr
    sha256: Sha256String | MISSING = MISSING


class UploadFileUrlParams(UploadFileBaseParams):
    type: Literal[UploadFileTag.URL] = UploadFileTag.URL
    url: AnyHttpUrl
    headers: HeaderMap | MISSING = MISSING


class UploadFilePathParams(UploadFileBaseParams):
    type: Literal[UploadFileTag.PATH] = UploadFileTag.PATH
    path: StrictStr


class UploadFileDataParams(UploadFileBaseParams):
    type: Literal[UploadFileTag.DATA] = UploadFileTag.DATA
    data: WireBytes


type UploadFileParams = Annotated[
    Annotated[UploadFileUrlParams, Tag(UploadFileTag.URL)]
    | Annotated[UploadFilePathParams, Tag(UploadFileTag.PATH)]
    | Annotated[UploadFileDataParams, Tag(UploadFileTag.DATA)]
    | Annotated[UploadFileBaseParams, Tag(UploadFileTag.EXTENSION)],
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
    joined_only: StrictBool = False


class ChannelUserIdParams(ChannelIdParams):
    user_id: StrictStr


class ChannelNameParams(ChannelIdParams):
    channel_name: StrictStr


class GetFileParams(ActionParamModel):
    file_id: StrictStr
    type: StrictStr


_DEFAULT_ACTION_PARAMS = TypeAdapter(ActionParamModel)
_ACTION_PARAM_ADAPTERS = {
    Action.GET_LATEST_EVENTS.value: TypeAdapter(LatestEventsParams),
    Action.SEND_MESSAGE.value: TypeAdapter(SendMsgParams),
    Action.GET_USER_INFO.value: TypeAdapter(UserIdParams),
    Action.DELETE_MESSAGE.value: TypeAdapter(MsgIdParams),
    Action.GET_GROUP_MEMBER_INFO.value: TypeAdapter(GroupUserIdParams),
    Action.SET_GROUP_NAME.value: TypeAdapter(GroupNameParams),
    Action.GET_GUILD_MEMBER_INFO.value: TypeAdapter(GuildUserIdParams),
    Action.SET_GUILD_NAME.value: TypeAdapter(GuildNameParams),
    Action.GET_CHANNEL_LIST.value: TypeAdapter(ChannelListParams),
    Action.GET_CHANNEL_MEMBER_INFO.value: TypeAdapter(ChannelUserIdParams),
    Action.SET_CHANNEL_NAME.value: TypeAdapter(ChannelNameParams),
    Action.GET_FILE.value: TypeAdapter(GetFileParams),
    Action.UPLOAD_FILE.value: TypeAdapter(UploadFileParams),
    Action.UPLOAD_FILE_FRAGMENTED.value: TypeAdapter(FragmentedUploadParams),
    Action.GET_FILE_FRAGMENTED.value: TypeAdapter(FragmentedGetParams),
    **dict.fromkeys(
        (
            Action.GET_GROUP_INFO.value,
            Action.GET_GROUP_MEMBER_LIST.value,
            Action.LEAVE_GROUP.value,
        ),
        TypeAdapter(GroupIdParams),
    ),
    **dict.fromkeys(
        (
            Action.GET_GUILD_INFO.value,
            Action.GET_GUILD_MEMBER_LIST.value,
            Action.LEAVE_GUILD.value,
        ),
        TypeAdapter(GuildIdParams),
    ),
    **dict.fromkeys(
        (
            Action.GET_CHANNEL_INFO.value,
            Action.GET_CHANNEL_MEMBER_LIST.value,
            Action.LEAVE_CHANNEL.value,
        ),
        TypeAdapter(ChannelIdParams),
    ),
}


class ActionCall(Model):
    action: StrictStr
    params: SerializeAsAny[ActionParamModel]

    @field_validator("params", mode="before")
    @classmethod
    def params_for_action(
        cls,
        value: object,
        info: ValidationInfo,
    ) -> ActionParamModel:
        return _ACTION_PARAM_ADAPTERS.get(
            info.data.get("action"),
            _DEFAULT_ACTION_PARAMS,
        ).validate_python(value)


class ActionRequest(ActionCall):
    echo: StrictStr | MISSING = MISSING
    self_: BotSelf | MISSING = Field(alias="self", default=MISSING)
