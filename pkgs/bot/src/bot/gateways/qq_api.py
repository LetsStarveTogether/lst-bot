from asyncio import (
    Event,
    Lock,
    get_running_loop,
    timeout,
)
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPMethod, HTTPStatus
from string import Formatter
from typing import Annotated, Literal, Self
from unicodedata import east_asian_width
from urllib.parse import quote, urlencode

from pydantic import (
    AfterValidator,
    AliasChoices,
    AnyHttpUrl,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PositiveInt,
    RootModel,
    SecretStr,
    StrictBool,
    StrictInt,
    StrictStr,
    UrlConstraints,
    WebsocketUrl,
    model_validator,
)
from urllib3_future import AsyncHTTPResponse, AsyncPoolManager

from bot.json import loads
from bot.protocol.base import Model

from .base import header_value, run_while_open, validate_https_base_url

QQ_API_BASE_URL = "https://api.bot.qq.com"
_HTTP_TIMEOUT = 30.0
_RETRYABLE_ERROR_CODES = frozenset({"11242", "11252", "11263", "11281"})
_KEYBOARD_ROLE_PERMISSION = 3
_TEXT_MESSAGE_TYPE = 0
_MEDIA_MESSAGE_TYPE = 7

type QQID = Annotated[StrictStr, Field(min_length=1)]
type QQLimit50 = Annotated[StrictInt, Field(ge=1, le=50)]
type QQLimit100 = Annotated[StrictInt, Field(ge=1, le=100)]
type QQUInt64 = Annotated[StrictInt, Field(ge=0, le=2**64 - 1)]
type QQPositiveInt = Annotated[StrictInt, Field(gt=0)]
type QQByteSize = Annotated[StrictStr, Field(pattern=r"^[0-9]+$")]
type QQHttpsUrl = Annotated[AnyHttpUrl, UrlConstraints(allowed_schemes=["https"])]
type QQWebsocketUrl = Annotated[
    WebsocketUrl,
    UrlConstraints(allowed_schemes=["wss"]),
]


def _qq_weighted_length(max_length: int) -> AfterValidator:
    def validate(value: str) -> str:
        if (
            sum(
                2 if east_asian_width(character) in {"W", "F"} else 1
                for character in value
            )
            > max_length
        ):
            msg = f"must be at most {max_length} characters; Chinese counts as two"
            raise ValueError(msg)
        return value

    return AfterValidator(validate)


type QQText10 = Annotated[StrictStr, _qq_weighted_length(10)]
type QQText14 = Annotated[StrictStr, _qq_weighted_length(14)]
type QQText30 = Annotated[StrictStr, _qq_weighted_length(30)]


class QQRequest(Model):
    model_config = ConfigDict(extra="forbid", frozen=True)


class QQNoContent(QQRequest):
    pass


class QQChannelParams(QQRequest):
    channel_id: QQID


class QQGuildParams(QQRequest):
    guild_id: QQID


class QQGroupParams(QQRequest):
    group_openid: QQID


class QQUserParams(QQRequest):
    user_openid: QQID


class QQUser(Model):
    id: QQID | None = None
    username: StrictStr | None = None
    avatar: StrictStr | None = None
    bot: StrictBool | None = None
    union_openid: StrictStr | None = None
    union_user_account: StrictStr | None = None
    user_openid: StrictStr | None = None
    member_openid: StrictStr | None = None
    share_url: StrictStr | None = None
    welcome_msg: StrictStr | None = None


class QQIdentifiedUser(QQUser):
    id: QQID


class QQGuild(Model):
    id: QQID
    name: StrictStr | None = None
    icon: StrictStr | None = None
    owner_id: StrictStr | None = None
    owner: StrictBool | None = None
    member_count: Annotated[StrictInt, Field(ge=0)] | None = None
    max_members: Annotated[StrictInt, Field(ge=0)] | None = None
    description: StrictStr | None = None
    joined_at: AwareDatetime | None = None


class QQGuildList(RootModel[list[QQGuild]]):
    pass


class QQChannel(Model):
    id: QQID
    guild_id: QQID
    name: StrictStr | None = None
    type: StrictInt | None = None
    sub_type: StrictInt | None = None
    position: Annotated[StrictInt, Field(ge=0)] | None = None
    parent_id: StrictStr | None = None
    owner_id: StrictStr | None = None
    private_type: Literal[0, 1, 2] | None = None
    speak_permission: Literal[0, 1, 2] | None = None
    application_id: StrictStr | None = None


class QQChannelList(RootModel[list[QQChannel]]):
    pass


class QQGatewayInfo(Model):
    url: QQWebsocketUrl


class QQSessionStartLimit(Model):
    total: Annotated[StrictInt, Field(ge=0)]
    remaining: Annotated[StrictInt, Field(ge=0)]
    reset_after: Annotated[StrictInt, Field(ge=0)]
    max_concurrency: QQPositiveInt


class QQGatewayBotInfo(QQGatewayInfo):
    shards: QQPositiveInt
    session_start_limit: QQSessionStartLimit


class QQGuildListParams(QQRequest):
    before: QQID | None = None
    after: QQID | None = None
    limit: QQLimit100 | None = None


class QQShareLinkRequest(QQRequest):
    callback_data: Annotated[StrictStr, Field(max_length=32)] | None = None


class QQShareLink(Model):
    url_link: StrictStr


class QQChannelCreateRequest(QQGuildParams):
    name: Annotated[StrictStr, Field(min_length=1)]
    type: Literal[0, 2, 4, 10005, 10006, 10007]
    sub_type: Literal[0, 1, 2, 3] = 0
    position: StrictInt | None = None
    parent_id: QQID | None = None
    private_type: Literal[0, 1, 2] | None = None
    private_user_ids: list[QQID] | None = None
    speak_permission: Literal[0, 1, 2] | None = None
    application_id: QQID | None = None


class QQChannelUpdateRequest(QQChannelParams):
    name: Annotated[StrictStr, Field(min_length=1)] | None = None
    position: StrictInt | None = None
    parent_id: QQID | None = None
    private_type: Literal[0, 1, 2] | None = None
    speak_permission: Literal[0, 1, 2] | None = None

    @model_validator(mode="after")
    def has_change(self) -> Self:
        if not self.model_dump(exclude_none=True, exclude={"channel_id"}):
            msg = "channel update requires at least one change"
            raise ValueError(msg)
        return self


class QQInteractionAckRequest(QQRequest):
    interaction_id: QQID
    code: Literal[0, 1, 2, 3, 4, 5] = 0


class QQMarkdownParams(QQRequest):
    key: StrictStr
    values: list[StrictStr]


class QQMarkdown(QQRequest):
    content: StrictStr | None = None
    custom_template_id: QQID | None = None
    params: list[QQMarkdownParams] | None = None
    force_verify_image_resource: StrictBool | None = None

    @model_validator(mode="after")
    def content_or_template(self) -> Self:
        if (self.content is None) == (self.custom_template_id is None):
            msg = "markdown requires exactly one of content and custom_template_id"
            raise ValueError(msg)
        if self.params is not None and self.custom_template_id is None:
            msg = "markdown params require custom_template_id"
            raise ValueError(msg)
        return self


class QQKeyboardPermission(QQRequest):
    type: Literal[0, 1, 2, 3]
    specify_user_ids: Annotated[list[QQID], Field(min_length=1)] | None = None
    specify_role_ids: Annotated[list[QQID], Field(min_length=1)] | None = None

    @model_validator(mode="after")
    def subjects_match_type(self) -> Self:
        if (self.specify_user_ids is not None) != (self.type == 0):
            msg = "type 0 requires specify_user_ids; other types forbid it"
            raise ValueError(msg)
        if (self.specify_role_ids is not None) != (
            self.type == _KEYBOARD_ROLE_PERMISSION
        ):
            msg = "type 3 requires specify_role_ids; other types forbid it"
            raise ValueError(msg)
        return self


class QQKeyboardAction(QQRequest):
    type: Literal[0, 1, 2]
    permission: QQKeyboardPermission
    data: StrictStr
    reply: StrictBool | None = None
    enter: StrictBool | None = None
    anchor: StrictInt | None = None
    click_limit: Annotated[StrictInt, Field(ge=0)] | None = None
    unsupport_tips: StrictStr | None = None


class QQKeyboardRenderData(QQRequest):
    label: StrictStr
    visited_label: StrictStr | None = None
    style: Literal[0, 1, 2, 3]


class QQKeyboardButton(QQRequest):
    id: QQID
    render_data: QQKeyboardRenderData
    action: QQKeyboardAction
    group_id: QQID | None = None


class QQKeyboardRow(QQRequest):
    buttons: Annotated[list[QQKeyboardButton], Field(min_length=1, max_length=5)]


class QQKeyboardContent(QQRequest):
    rows: Annotated[list[QQKeyboardRow], Field(min_length=1, max_length=5)]


class QQKeyboard(QQRequest):
    id: QQID | None = None
    content: QQKeyboardContent | None = None

    @model_validator(mode="after")
    def id_or_content(self) -> Self:
        if (self.id is None) == (self.content is None):
            msg = "keyboard requires exactly one of id and content"
            raise ValueError(msg)
        return self


class QQMediaInfo(QQRequest):
    file_info: QQID


class QQMessageReference(QQRequest):
    message_id: QQID
    ignore_get_message_error: StrictBool | None = None


class QQInputNotify(QQRequest):
    input_type: Literal[1]
    input_second: Annotated[StrictInt, Field(ge=1, le=60)]


class QQReplySourceFields(QQRequest):
    msg_id: QQID | None = None
    event_id: QQID | None = None
    msg_seq: Annotated[StrictInt, Field(ge=0, le=65535)] | None = None
    is_wakeup: StrictBool | None = None

    @model_validator(mode="after")
    def one_reply_source(self) -> Self:
        if self.msg_id is not None and self.event_id is not None:
            msg = "msg_id and event_id are mutually exclusive"
            raise ValueError(msg)
        return self


class QQMessageRequestBase(QQReplySourceFields):
    content: StrictStr | None = None
    markdown: QQMarkdown | None = None
    keyboard: QQKeyboard | None = None
    media: QQMediaInfo | None = None
    message_reference: QQMessageReference | None = None

    @model_validator(mode="after")
    def wakeup_or_reply_source(self) -> Self:
        if self.is_wakeup is True and (
            self.msg_id is not None or self.event_id is not None
        ):
            msg = "is_wakeup=true and a reply source are mutually exclusive"
            raise ValueError(msg)
        return self


def _validate_message_payload(
    msg_type: int,
    payloads: Mapping[int, object],
) -> None:
    provided = {kind for kind, value in payloads.items() if value is not None}
    allowed = (
        {msg_type, _TEXT_MESSAGE_TYPE}
        if msg_type == _MEDIA_MESSAGE_TYPE
        else {msg_type}
    )
    if msg_type not in provided or not provided <= allowed:
        msg = (
            f"msg_type {msg_type} requires its matching payload "
            "and forbids incompatible payloads"
        )
        raise ValueError(msg)


class QQSendGroupMessageRequest(QQGroupParams, QQMessageRequestBase):
    msg_type: Literal[0, 2, 7]

    @model_validator(mode="after")
    def payload_matches_type(self) -> Self:
        _validate_message_payload(
            self.msg_type,
            {
                _TEXT_MESSAGE_TYPE: self.content,
                2: self.markdown,
                _MEDIA_MESSAGE_TYPE: self.media,
            },
        )
        return self


class QQSendC2CMessageRequest(QQUserParams, QQMessageRequestBase):
    msg_type: Literal[0, 2, 6, 7]
    input_notify: QQInputNotify | None = None

    @model_validator(mode="after")
    def payload_matches_type(self) -> Self:
        _validate_message_payload(
            self.msg_type,
            {
                _TEXT_MESSAGE_TYPE: self.content,
                2: self.markdown,
                6: self.input_notify,
                _MEDIA_MESSAGE_TYPE: self.media,
            },
        )
        return self


class QQArkKv(QQRequest):
    key: StrictStr
    value: StrictStr | None = None
    obj: list[dict[StrictStr, JsonValue]] | None = None


class QQArk(QQRequest):
    template_id: StrictInt
    kv: list[QQArkKv]


class QQEmbedField(QQRequest):
    name: StrictStr


class QQEmbed(QQRequest):
    title: StrictStr | None = None
    prompt: StrictStr | None = None
    thumbnail: dict[StrictStr, JsonValue] | None = None
    fields: list[QQEmbedField] | None = None


class QQSendChannelMessageRequest(QQChannelParams):
    content: StrictStr | None = None
    embed: QQEmbed | None = None
    ark: QQArk | None = None
    message_reference: QQMessageReference | None = None
    image: StrictStr | None = None
    msg_id: QQID | None = None
    event_id: QQID | None = None
    markdown: QQMarkdown | None = None
    keyboard: QQKeyboard | None = None

    @model_validator(mode="after")
    def validate_message(self) -> Self:
        if self.msg_id is not None and self.event_id is not None:
            msg = "msg_id and event_id are mutually exclusive"
            raise ValueError(msg)
        if not any((self.content, self.embed, self.ark, self.image, self.markdown)):
            msg = "channel message content is required"
            raise ValueError(msg)
        return self


class QQSendDMMessageRequest(QQSendChannelMessageRequest):
    channel_id: None = Field(default=None, exclude=True)
    guild_id: QQID


class QQRecallMessageRequest(QQRequest):
    message_id: QQID
    hidetip: StrictBool | None = None


class QQRecallChannelMessageRequest(QQRecallMessageRequest):
    channel_id: QQID


class QQRecallDMMessageRequest(QQRecallMessageRequest):
    guild_id: QQID


class QQRecallGroupMessageRequest(QQRecallMessageRequest):
    group_openid: QQID
    hidetip: None = Field(default=None, exclude=True)


class QQRecallC2CMessageRequest(QQRecallMessageRequest):
    user_openid: QQID
    hidetip: None = Field(default=None, exclude=True)


class QQMessage(Model):
    id: QQID
    channel_id: StrictStr | None = None
    guild_id: StrictStr | None = None
    group_openid: StrictStr | None = None
    content: StrictStr | None = None
    timestamp: AwareDatetime
    author: QQUser | None = None


class QQMessageExtInfo(Model):
    ref_idx: StrictStr | None = None


class QQSentMessage(Model):
    id: QQID
    timestamp: AwareDatetime
    ext_info: QQMessageExtInfo | None = None
    remain_msg_len: Annotated[StrictInt, Field(ge=0)] | None = None


class QQCreateDMRequest(QQRequest):
    recipient_id: QQID
    source_guild_id: QQID


class QQDirectMessage(Model):
    guild_id: QQID
    channel_id: QQID
    create_time: StrictStr | None = None


class QQStreamMessageRequest(QQUserParams, QQReplySourceFields):
    input_mode: Literal["append", "replace"] = "append"
    input_state: Literal[1, 10]
    index: Annotated[StrictInt, Field(ge=0)]
    content_type: Literal["text", "markdown"]
    content_raw: StrictStr
    stream_msg_id: QQID | None = None

    @model_validator(mode="after")
    def valid_stream_state(self) -> Self:
        if (self.index == 0) == (self.stream_msg_id is not None):
            msg = "stream_msg_id is forbidden at index 0 and required afterwards"
            raise ValueError(msg)
        if self.index == 0 and self.input_state != 1:
            msg = "the first stream chunk must have input_state=1"
            raise ValueError(msg)
        return self


class QQFileUploadFields(QQRequest):
    file_type: Literal[1, 2, 3, 4]
    srv_send_msg: StrictBool
    url: AnyHttpUrl | None = None
    file_name: StrictStr | None = None
    upload_id: QQID | None = None

    @model_validator(mode="after")
    def upload_source(self) -> Self:
        if (self.url is None) == (self.upload_id is None):
            msg = "exactly one of url and upload_id is required"
            raise ValueError(msg)
        return self


class QQUploadGroupFileRequest(QQGroupParams, QQFileUploadFields):
    pass


class QQUploadC2CFileRequest(QQUserParams, QQFileUploadFields):
    pass


class QQFileInfo(Model):
    file_uuid: StrictStr
    file_info: StrictStr
    ttl: Annotated[StrictInt, Field(ge=0)]
    id: StrictStr | None = None
    raw_url: StrictStr | None = None


class QQFilePrepareFields(QQRequest):
    file_type: Literal[1, 2, 3, 4]
    file_size: QQByteSize
    file_name: Annotated[StrictStr, Field(min_length=1)]
    md5: Annotated[StrictStr, Field(pattern=r"^[0-9a-fA-F]{32}$")]
    sha1: Annotated[StrictStr, Field(pattern=r"^[0-9a-fA-F]{40}$")]
    md5_10m: Annotated[StrictStr, Field(pattern=r"^[0-9a-fA-F]{32}$")]


class QQPrepareGroupFileRequest(QQFilePrepareFields):
    group_id: QQID


class QQPrepareC2CFileRequest(QQFilePrepareFields):
    user_id: QQID


class QQUploadPart(Model):
    index: Annotated[StrictInt, Field(ge=0)]
    presigned_url: StrictStr
    block_size: QQByteSize


class QQUploadConfig(Model):
    concurrency: QQPositiveInt
    retry_timeout: QQPositiveInt
    retry_delay: QQPositiveInt


class QQFilePrepareResult(Model):
    upload_id: QQID
    block_size: QQByteSize
    parts: list[QQUploadPart]
    upload_config: QQUploadConfig


class QQFinishFileFields(QQRequest):
    upload_id: QQID
    part_index: Annotated[StrictInt, Field(ge=0)]
    block_size: QQByteSize
    md5: Annotated[StrictStr, Field(pattern=r"^[0-9a-fA-F]{32}$")]


class QQFinishGroupFileRequest(QQFinishFileFields):
    group_id: QQID


class QQFinishC2CFileRequest(QQFinishFileFields):
    user_id: QQID


class QQGroupInfo(Model):
    group_openid: QQID
    group_name: StrictStr
    group_finger_memo: StrictStr
    group_class_text: StrictStr
    group_tags: list[StrictStr]
    group_member_num: Annotated[StrictInt, Field(ge=0)]


class QQGroupBotState(Model):
    member_openid: QQID
    joined_at: AwareDatetime
    allow_proactive_msg: StrictBool
    recv_msg_setting: Literal["all", "only_mention", "mention_and_context"]
    member_role: Literal["member", "owner", "admin"]


class QQJoinRequestListParams(QQGroupParams):
    cursor: StrictStr | None = None
    limit: QQLimit100 | None = None


class QQReviewQA(Model):
    question: StrictStr
    answer: StrictStr


class QQVerifyInfo(Model):
    method: Literal["verify_message", "admin_review_qa"]
    verify_message: StrictStr | None = None
    review_qa_list: list[QQReviewQA] | None = None

    @model_validator(mode="after")
    def payload_matches_method(self) -> Self:
        if self.method == "verify_message" and self.review_qa_list:
            msg = "verify_message method cannot include review questions"
            raise ValueError(msg)
        if self.method == "admin_review_qa" and self.verify_message:
            msg = "admin_review_qa method cannot include a verification message"
            raise ValueError(msg)
        return self


class QQAutoApproved(Model):
    strategy_id: QQID


class QQJoinRequest(Model):
    join_request_id: QQID
    member_openid: QQID
    username: StrictStr
    apply_at: AwareDatetime
    apply_source: Literal["self_apply", "invited"]
    risk_tips: StrictStr | None = None
    union_openid: StrictStr | None = None
    invited_by: StrictStr | None = None
    bot: StrictBool | None = None
    verify_info: QQVerifyInfo | None = None
    auto_approved: QQAutoApproved | None = None


class QQJoinRequestList(Model):
    list: list[QQJoinRequest]
    next_cursor: StrictStr


class QQApproveJoinRequest(QQGroupParams):
    member_openid: QQID
    op: Literal["approve", "decline"]
    join_request_id: QQID | None = None
    reject_reason: StrictStr | None = None
    add_to_member_blacklist: StrictBool | None = None

    @model_validator(mode="after")
    def decline_fields(self) -> Self:
        if self.op == "approve" and (
            self.reject_reason is not None or self.add_to_member_blacklist is not None
        ):
            msg = "rejection fields require op=decline"
            raise ValueError(msg)
        return self


class QQRestrictionSchedule(Model):
    task_id: QQID
    start_at: AwareDatetime
    end_at: AwareDatetime
    enabled: StrictBool


class QQRecurringRestriction(Model):
    task_id: QQID
    weekdays: list[Annotated[StrictInt, Field(ge=1, le=7)]]
    start_time: StrictStr
    end_time: StrictStr
    enabled: StrictBool


class QQGlobalRestriction(Model):
    mode: Literal["none", "always", "schedule"]
    schedule_rules: list[QQRestrictionSchedule] | None = None
    recurring_rules: list[QQRecurringRestriction] | None = None


class QQRestrictedMember(Model):
    member_openid: QQID
    mute_expire_at: AwareDatetime | None = None
    username: StrictStr | None = None
    union_openid: StrictStr | None = None


class QQGroupRestrictions(Model):
    global_rule: QQGlobalRestriction
    members: list[QQRestrictedMember]


class QQRestrictionOperation(QQRequest):
    op: Literal["add", "update", "del"]
    member_openid: QQID
    mute_expire_at: AwareDatetime | Literal[""] | None = None

    @model_validator(mode="after")
    def validate_expiry(self) -> Self:
        if self.op in {"add", "update"} and self.mute_expire_at in {None, ""}:
            msg = "add and update require mute_expire_at"
            raise ValueError(msg)
        if self.op == "del" and self.mute_expire_at not in {None, ""}:
            msg = "del only accepts an omitted or empty mute_expire_at"
            raise ValueError(msg)
        return self


class QQUpdateRestrictionsRequest(QQGroupParams):
    members: Annotated[list[QQRestrictionOperation], Field(min_length=1, max_length=10)]


class QQStrategyListParams(QQRequest):
    cursor: StrictStr | None = None
    limit: QQLimit100 | None = None


class QQStrategyGroups(QQRequest):
    group_openids: Annotated[list[QQID], Field(min_length=1, max_length=100)] | None = (
        None
    )
    group_ids: Annotated[list[QQUInt64], Field(min_length=1, max_length=100)] | None = (
        None
    )

    @model_validator(mode="after")
    def one_group_kind(self) -> Self:
        if (self.group_openids is None) == (self.group_ids is None):
            msg = "exactly one of group_openids and group_ids is required"
            raise ValueError(msg)
        return self


class QQCreateStrategyRequest(QQStrategyGroups):
    is_enable: Literal["on", "off"] = "on"
    expire_at: AwareDatetime | Literal[""] | None = None
    remark: Annotated[StrictStr, Field(max_length=255)] | None = None


class QQStrategyResult(Model):
    strategy_id: QQID
    is_enable: Literal["on", "off"] | None = None
    expire_at: AwareDatetime | None = None


class QQStrategyUpdateResult(Model):
    is_enable: Literal["on", "off"]
    expire_at: AwareDatetime


class QQStrategyGroupAction(QQStrategyGroups):
    op: Literal["add", "del"]


class QQUpdateStrategyRequest(QQRequest):
    strategy_id: QQID
    is_enable: Literal["on", "off"] | None = None
    expire_at: AwareDatetime | Literal[""] | None = None
    remark: Annotated[StrictStr, Field(max_length=255)] | None = None
    group_action: QQStrategyGroupAction | None = None

    @model_validator(mode="after")
    def has_change(self) -> Self:
        if not self.model_dump(exclude_none=True, exclude={"strategy_id"}):
            msg = "strategy update requires at least one change"
            raise ValueError(msg)
        return self


class QQDeleteStrategyRequest(QQRequest):
    strategy_id: QQID


class QQUpdateStrategyWhitelistRequest(QQDeleteStrategyRequest):
    op: Literal["add", "del"]
    whitelist_users: Annotated[list[QQID], Field(min_length=1, max_length=10000)]


class QQStrategyWhitelistResult(Model):
    strategy_id: QQID
    whitelist_user_count: Annotated[StrictInt, Field(ge=0)]
    updated_at: AwareDatetime


class QQStrategyRecord(Model):
    strategy_id: QQID
    is_enable: Literal["on", "off"]
    expire_at: AwareDatetime | None = None
    remark: StrictStr | None = None
    group_openids: list[QQID] | None = None
    group_ids: list[StrictStr] | None = None
    whitelist_user_count: Annotated[StrictInt, Field(ge=0)] | None = None
    created_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None


class QQStrategyList(Model):
    strategies: list[QQStrategyRecord]
    next_cursor: StrictStr | None = None


class QQMenuSwitch(QQRequest):
    switch_id: QQID
    default: StrictBool = False


class QQSubMenuSendMessage(QQRequest):
    name: QQText14
    type: Literal["send_message"]
    send_message: StrictStr


class QQSubMenuLink(QQRequest):
    name: QQText14
    type: Literal["link"]
    link: QQHttpsUrl


type QQSubMenuItem = Annotated[
    QQSubMenuSendMessage | QQSubMenuLink,
    Field(discriminator="type"),
]


class QQMenuSwitchItem(QQRequest):
    name: QQText10
    type: Literal["switch"]
    switch: QQMenuSwitch


class QQMenuSendMessageItem(QQRequest):
    name: QQText10
    type: Literal["send_message"]
    send_message: StrictStr


class QQMenuLinkItem(QQRequest):
    name: QQText10
    type: Literal["link"]
    link: QQHttpsUrl


class QQMenuSubmenuItem(QQRequest):
    name: QQText10
    type: Literal["menu"]
    sub_menu_items: Annotated[list[QQSubMenuItem], Field(min_length=1, max_length=5)]


type QQMenuItem = Annotated[
    QQMenuSwitchItem | QQMenuSendMessageItem | QQMenuLinkItem | QQMenuSubmenuItem,
    Field(discriminator="type"),
]


class QQMenu(QQRequest):
    items: Annotated[list[QQMenuItem], Field(max_length=10)] = Field(
        default_factory=list
    )


class QQPutMenuRequest(QQRequest):
    menu: QQMenu | None = None


class QQMenuResult(Model):
    version: Annotated[StrictInt, Field(ge=0)]
    menu: QQMenu | None = None


class QQVersionResult(Model):
    version: Annotated[StrictInt, Field(ge=0)]


class QQPanelCommandItem(QQRequest):
    name: QQText14
    desc: QQText30 | None = None
    type: Literal["command"]
    only_admin: StrictBool | None = None


class QQPanelLinkItem(QQRequest):
    name: QQText14
    desc: QQText30 | None = None
    type: Literal["link"]
    only_admin: StrictBool | None = None
    link: QQHttpsUrl


type QQPanelItem = Annotated[
    QQPanelCommandItem | QQPanelLinkItem,
    Field(discriminator="type"),
]


class QQPanel(QQRequest):
    items: Annotated[list[QQPanelItem], Field(max_length=20)] = Field(
        default_factory=list
    )
    remark: Annotated[StrictStr, Field(max_length=255)] | None = None
    version: Annotated[StrictInt, Field(ge=0)] | None = None


class QQPanelListParams(QQRequest):
    scope: Literal["c2c", "group", "channel", "dm"]
    cursor: StrictStr | None = None
    limit: QQLimit50 | None = None


class QQCreatePanelRequest(QQRequest):
    scope: Literal["c2c", "group", "channel", "dm"]
    target_type: Literal["all", "specific"] = "all"
    user_openids: Annotated[list[QQID], Field(min_length=1, max_length=20)] | None = (
        None
    )
    group_openids: Annotated[list[QQID], Field(min_length=1, max_length=20)] | None = (
        None
    )
    panel: QQPanel

    @model_validator(mode="after")
    def valid_target(self) -> Self:
        if self.scope in {"channel", "dm"} and self.target_type != "all":
            msg = "channel and dm panels require target_type=all"
            raise ValueError(msg)
        expected = self.user_openids if self.scope == "c2c" else self.group_openids
        other = self.group_openids if self.scope == "c2c" else self.user_openids
        if self.target_type == "specific" and expected is None:
            msg = "specific panel requires matching target openids"
            raise ValueError(msg)
        if self.target_type == "all" and (self.user_openids or self.group_openids):
            msg = "all panel does not accept target openids"
            raise ValueError(msg)
        if other is not None:
            msg = "target openids do not match panel scope"
            raise ValueError(msg)
        return self


class QQPanelIDResult(Model):
    panel_id: QQID


class QQPanelParams(QQRequest):
    panel_id: QQID


class QQUpdatePanelRequest(QQPanelParams):
    panel: QQPanel


class QQUpdatePanelTargetsRequest(QQPanelParams):
    op: Literal["add", "del"]
    user_openids: Annotated[list[QQID], Field(min_length=1, max_length=20)] | None = (
        None
    )
    group_openids: Annotated[list[QQID], Field(min_length=1, max_length=20)] | None = (
        None
    )

    @model_validator(mode="after")
    def one_target_kind(self) -> Self:
        if (self.user_openids is None) == (self.group_openids is None):
            msg = "exactly one target openid list is required"
            raise ValueError(msg)
        return self


class QQPanelRecord(Model):
    panel_id: QQID
    scope: Literal["c2c", "group", "channel", "dm"]
    target_type: Literal["all", "specific"]
    panel: QQPanel
    user_openids: list[QQID] | None = None
    group_openids: list[QQID] | None = None
    created_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None
    version: Annotated[StrictInt, Field(ge=0)] | None = None


class QQPanelList(Model):
    records: list[QQPanelRecord]
    next_cursor: StrictStr
    is_end: StrictBool


class QQOnlineNumbers(Model):
    online_nums: Annotated[StrictInt, Field(ge=0)]


class QQMember(Model):
    user: QQIdentifiedUser
    nick: StrictStr
    roles: list[StrictStr] = Field(default_factory=list)
    joined_at: AwareDatetime


class QQMemberList(RootModel[list[QQMember]]):
    pass


class QQMemberListParams(QQGuildParams):
    after: QQID | None = None
    limit: Annotated[StrictInt, Field(ge=1, le=400)] | None = None


class QQMemberParams(QQGuildParams):
    user_id: QQID


class QQRoleMemberListParams(QQGuildParams):
    role_id: QQID
    start_index: QQID | None = None
    limit: Annotated[StrictInt, Field(ge=1, le=400)] | None = None


class QQRoleMemberList(Model):
    data: list[QQMember]
    next: StrictStr | None = None


class QQDeleteMemberRequest(QQMemberParams):
    add_blacklist: StrictBool | None = None
    delete_history_msg_days: Literal[-1, 0, 3, 7, 15, 30] | None = None


class QQRole(Model):
    id: QQID
    name: StrictStr | None = None
    color: Annotated[StrictInt, Field(ge=0)] | None = None
    hoist: StrictInt | None = None
    number: Annotated[StrictInt, Field(ge=0)] | None = None
    member_limit: Annotated[StrictInt, Field(ge=0)] | None = None


class QQGuildRoles(Model):
    guild_id: QQID | None = None
    roles: list[QQRole]
    role_num_limit: StrictStr | None = None


class QQRoleFields(QQRequest):
    name: Annotated[StrictStr, Field(min_length=1)] | None = None
    color: Annotated[StrictInt, Field(ge=0, le=0xFFFFFFFF)] | None = None
    hoist: Literal[0, 1] | None = None

    @model_validator(mode="after")
    def has_role_field(self) -> Self:
        if self.name is None and self.color is None and self.hoist is None:
            msg = "role request requires at least one role field"
            raise ValueError(msg)
        return self


class QQCreateRoleRequest(QQGuildParams, QQRoleFields):
    pass


class QQRoleParams(QQGuildParams):
    role_id: QQID


class QQUpdateRoleRequest(QQRoleParams, QQRoleFields):
    pass


class QQUpdateRoleResult(Model):
    role_id: QQID
    role: QQRole | None = None


class QQMemberRoleRequest(QQMemberParams):
    role_id: QQID
    channel: dict[Literal["id"], QQID] | None = None

    @model_validator(mode="after")
    def channel_for_role_five(self) -> Self:
        if (self.role_id == "5") != (self.channel is not None):
            msg = "role 5 requires channel and other roles do not accept it"
            raise ValueError(msg)
        return self


class QQChannelMemberPermissionParams(QQChannelParams):
    user_id: QQID


class QQChannelRolePermissionParams(QQChannelParams):
    role_id: QQID


class QQPermissionUpdate(QQRequest):
    add: Annotated[StrictStr, Field(pattern=r"^[0-9]+$")]
    remove: Annotated[StrictStr, Field(pattern=r"^[0-9]+$")]


class QQUpdateMemberPermissionRequest(
    QQChannelMemberPermissionParams,
    QQPermissionUpdate,
):
    pass


class QQUpdateRolePermissionRequest(QQChannelRolePermissionParams, QQPermissionUpdate):
    pass


class QQChannelPermissions(Model):
    channel_id: QQID
    user_id: QQID | None = None
    role_id: QQID | None = None
    permissions: Annotated[StrictStr, Field(pattern=r"^[0-9]+$")]


class QQEmojiParams(QQRequest):
    channel_id: QQID
    message_id: QQID
    emoji_type: Literal[1, 2]
    emoji_id: QQID


class QQReactionUsersParams(QQEmojiParams):
    cookie: StrictStr | None = None
    limit: QQLimit50 | None = None


class QQReactionUsers(Model):
    users: list[QQUser]
    cookie: StrictStr | None = None
    is_end: StrictBool


class QQMuteFields(QQRequest):
    mute_end_timestamp: Annotated[StrictStr, Field(pattern=r"^[0-9]+$")] | None = None
    mute_seconds: Annotated[StrictStr, Field(pattern=r"^[0-9]+$")] | None = None

    @model_validator(mode="after")
    def one_duration(self) -> Self:
        if self.mute_end_timestamp is None and self.mute_seconds is None:
            msg = "mute_end_timestamp or mute_seconds is required"
            raise ValueError(msg)
        return self


class QQGuildMuteRequest(QQGuildParams, QQMuteFields):
    pass


class QQMemberMuteRequest(QQMemberParams, QQMuteFields):
    pass


class QQMultiMemberMuteRequest(QQGuildMuteRequest):
    user_ids: Annotated[list[QQID], Field(min_length=1)]


class QQMultiMemberMuteResult(Model):
    user_ids: list[QQID]


class QQPinsParams(QQChannelParams):
    message_id: QQID


class QQPinsMessage(Model):
    guild_id: QQID | None = None
    channel_id: QQID
    message_ids: list[QQID] = Field(default_factory=list)


class QQScheduleParams(QQChannelParams):
    schedule_id: QQID


class QQScheduleListParams(QQChannelParams):
    since: Annotated[StrictInt, Field(ge=0)] | None = None


class QQScheduleFields(QQRequest):
    name: Annotated[StrictStr, Field(min_length=1)]
    description: StrictStr | None = None
    start_timestamp: Annotated[StrictStr, Field(pattern=r"^[0-9]+$")]
    end_timestamp: Annotated[StrictStr, Field(pattern=r"^[0-9]+$")]
    jump_channel_id: QQID | None = None
    remind_type: Literal["0", "1", "2", "3", "4", "5"] | None = None


class QQScheduleRequest(QQChannelParams):
    schedule: QQScheduleFields


class QQSchedulePatch(QQRequest):
    name: Annotated[StrictStr, Field(min_length=1)] | None = None
    description: StrictStr | None = None
    start_timestamp: Annotated[StrictStr, Field(pattern=r"^[0-9]+$")] | None = None
    end_timestamp: Annotated[StrictStr, Field(pattern=r"^[0-9]+$")] | None = None
    jump_channel_id: QQID | None = None
    remind_type: Literal["0", "1", "2", "3", "4", "5"] | None = None

    @model_validator(mode="after")
    def has_change(self) -> Self:
        if not self.model_dump(exclude_none=True):
            msg = "schedule update requires at least one change"
            raise ValueError(msg)
        return self


class QQScheduleUpdateRequest(QQScheduleParams):
    schedule: QQSchedulePatch


class QQSchedule(Model):
    id: QQID
    name: StrictStr
    description: StrictStr | None = None
    start_timestamp: StrictStr
    end_timestamp: StrictStr
    creator: QQMember | None = None
    jump_channel_id: StrictStr | None = None
    remind_type: Literal["0", "1", "2", "3", "4", "5"] | None = None


class QQScheduleList(RootModel[list[QQSchedule]]):
    pass


class QQAudioControlRequest(QQChannelParams):
    audio_url: AnyHttpUrl | None = None
    text: StrictStr | None = None
    status: Literal[0, 1, 2, 3]

    @model_validator(mode="after")
    def audio_for_start(self) -> Self:
        if self.status == 0 and self.audio_url is None:
            msg = "starting audio requires audio_url"
            raise ValueError(msg)
        if self.status != 0 and (self.audio_url is not None or self.text is not None):
            msg = "audio_url and text are only valid when starting audio"
            raise ValueError(msg)
        return self


class QQAPIIdentify(QQRequest):
    path: Annotated[StrictStr, Field(pattern=r"^/")]
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]


class QQAPIPermissionDemandRequest(QQGuildParams):
    channel_id: QQID
    api_identify: QQAPIIdentify
    desc: Annotated[StrictStr, Field(min_length=1)]


class QQAPIPermission(Model):
    path: StrictStr
    method: StrictStr
    desc: StrictStr | None = None
    auth_status: StrictInt | None = None


class QQAPIPermissions(Model):
    apis: list[QQAPIPermission]


class QQAPIPermissionDemand(Model):
    guild_id: QQID
    channel_id: QQID
    api_identify: QQAPIPermission
    title: StrictStr
    desc: StrictStr


class QQForumCreateRequest(QQChannelParams):
    title: StrictStr
    content: StrictStr
    format: Literal[1, 2, 3, 4]


class QQForumThreadParams(QQChannelParams):
    thread_id: QQID


class QQForumThreadInfo(Model):
    thread_id: QQID
    title: StrictStr
    content: StrictStr
    date_time: AwareDatetime


class QQForumThread(Model):
    guild_id: QQID
    channel_id: QQID
    author_id: QQID
    thread_info: QQForumThreadInfo


class QQForumThreadList(Model):
    threads: list[QQForumThread]
    is_finish: Literal[0, 1]


class QQForumThreadDetail(Model):
    thread: QQForumThread


class QQForumCreateResult(Model):
    task_id: QQID
    create_time: Annotated[StrictStr, Field(pattern=r"^[0-9]+$")]


class QQMessageSetting(Model):
    disable_create_dm: StrictBool
    disable_push_msg: StrictBool
    channel_ids: list[QQID]
    channel_push_max_num: Annotated[StrictInt, Field(ge=0)]


class QQRecommendChannel(QQRequest):
    channel_id: QQID
    introduce: StrictStr


class QQRecommendChannelResult(Model):
    channel_id: QQID
    introduce: StrictStr


class QQGuildAnnounceRequest(QQGuildParams):
    channel_id: QQID | None = None
    message_id: StrictStr | None = None
    announces_type: Literal[0, 1] = 0
    recommend_channels: (
        Annotated[list[QQRecommendChannel], Field(min_length=1, max_length=3)] | None
    ) = None

    @model_validator(mode="after")
    def message_or_recommendations(self) -> Self:
        if self.message_id:
            if self.channel_id is None:
                msg = "message announcement requires channel_id"
                raise ValueError(msg)
            if self.announces_type != 0 or self.recommend_channels is not None:
                msg = "message announcement must be a member announcement"
                raise ValueError(msg)
            return self
        if self.announces_type != 1 or not self.recommend_channels:
            msg = "recommended channels announcement requires type 1 and channels"
            raise ValueError(msg)
        return self


class QQGuildAnnounceDeleteRequest(QQGuildParams):
    message_id: QQID


class QQAnnounce(Model):
    guild_id: QQID
    channel_id: QQID
    message_id: StrictStr
    announces_type: Literal[0, 1]
    recommend_channels: list[QQRecommendChannelResult]


class QQAction(StrEnum):
    GET_GATEWAY = "qq.get_gateway"
    GET_GATEWAY_BOT = "qq.get_gateway_bot"
    GET_BOT = "qq.get_bot"
    LIST_BOT_GUILDS = "qq.list_bot_guilds"
    GENERATE_SHARE_LINK = "qq.generate_share_link"
    GET_GUILD = "qq.get_guild"
    LIST_GUILD_CHANNELS = "qq.list_guild_channels"
    CREATE_CHANNEL = "qq.create_channel"
    GET_CHANNEL = "qq.get_channel"
    UPDATE_CHANNEL = "qq.update_channel"
    DELETE_CHANNEL = "qq.delete_channel"
    ACK_INTERACTION = "qq.ack_interaction"
    SEND_C2C_MESSAGE = "qq.send_c2c_message"
    SEND_C2C_STREAM_MESSAGE = "qq.send_c2c_stream_message"
    RECALL_C2C_MESSAGE = "qq.recall_c2c_message"
    UPLOAD_C2C_FILE = "qq.upload_c2c_file"
    PREPARE_C2C_FILE_UPLOAD = "qq.prepare_c2c_file_upload"
    FINISH_C2C_FILE_UPLOAD = "qq.finish_c2c_file_upload"
    SEND_GROUP_MESSAGE = "qq.send_group_message"
    RECALL_GROUP_MESSAGE = "qq.recall_group_message"
    UPLOAD_GROUP_FILE = "qq.upload_group_file"
    PREPARE_GROUP_FILE_UPLOAD = "qq.prepare_group_file_upload"
    FINISH_GROUP_FILE_UPLOAD = "qq.finish_group_file_upload"
    GET_GROUP_INFO = "qq.get_group_info"
    GET_GROUP_BOT_STATE = "qq.get_group_bot_state"
    LIST_GROUP_JOIN_REQUESTS = "qq.list_group_join_requests"
    APPROVE_GROUP_JOIN_REQUEST = "qq.approve_group_join_request"
    GET_GROUP_RESTRICTIONS = "qq.get_group_restrictions"
    UPDATE_GROUP_RESTRICTIONS = "qq.update_group_restrictions"
    LIST_GROUP_APPROVAL_STRATEGIES = "qq.list_group_approval_strategies"
    CREATE_GROUP_APPROVAL_STRATEGY = "qq.create_group_approval_strategy"
    UPDATE_GROUP_APPROVAL_STRATEGY = "qq.update_group_approval_strategy"
    DELETE_GROUP_APPROVAL_STRATEGY = "qq.delete_group_approval_strategy"
    EXECUTE_GROUP_APPROVAL_STRATEGY = "qq.execute_group_approval_strategy"
    UPDATE_GROUP_APPROVAL_WHITELIST = "qq.update_group_approval_whitelist"
    GET_MENU = "qq.get_menu"
    PUT_MENU = "qq.put_menu"
    LIST_PANELS = "qq.list_panels"
    CREATE_PANEL = "qq.create_panel"
    GET_PANEL = "qq.get_panel"
    UPDATE_PANEL = "qq.update_panel"
    DELETE_PANEL = "qq.delete_panel"
    UPDATE_PANEL_TARGETS = "qq.update_panel_targets"
    SEND_CHANNEL_MESSAGE = "qq.send_channel_message"
    RECALL_CHANNEL_MESSAGE = "qq.recall_channel_message"
    CREATE_DM = "qq.create_dm"
    SEND_DM_MESSAGE = "qq.send_dm_message"
    RECALL_DM_MESSAGE = "qq.recall_dm_message"
    GET_CHANNEL_ONLINE_NUMBERS = "qq.get_channel_online_numbers"
    LIST_GUILD_MEMBERS = "qq.list_guild_members"
    LIST_GUILD_ROLE_MEMBERS = "qq.list_guild_role_members"
    GET_GUILD_MEMBER = "qq.get_guild_member"
    DELETE_GUILD_MEMBER = "qq.delete_guild_member"
    LIST_GUILD_ROLES = "qq.list_guild_roles"
    CREATE_GUILD_ROLE = "qq.create_guild_role"
    UPDATE_GUILD_ROLE = "qq.update_guild_role"
    DELETE_GUILD_ROLE = "qq.delete_guild_role"
    ADD_GUILD_MEMBER_ROLE = "qq.add_guild_member_role"
    REMOVE_GUILD_MEMBER_ROLE = "qq.remove_guild_member_role"
    GET_MEMBER_CHANNEL_PERMISSIONS = "qq.get_member_channel_permissions"
    UPDATE_MEMBER_CHANNEL_PERMISSIONS = "qq.update_member_channel_permissions"
    GET_ROLE_CHANNEL_PERMISSIONS = "qq.get_role_channel_permissions"
    UPDATE_ROLE_CHANNEL_PERMISSIONS = "qq.update_role_channel_permissions"
    ADD_MESSAGE_REACTION = "qq.add_message_reaction"
    REMOVE_MESSAGE_REACTION = "qq.remove_message_reaction"
    LIST_MESSAGE_REACTION_USERS = "qq.list_message_reaction_users"
    MUTE_GUILD = "qq.mute_guild"
    MUTE_GUILD_MEMBER = "qq.mute_guild_member"
    MUTE_GUILD_MEMBERS = "qq.mute_guild_members"
    GET_PINS = "qq.get_pins"
    ADD_PIN = "qq.add_pin"
    DELETE_PIN = "qq.delete_pin"
    CLEAN_PINS = "qq.clean_pins"
    LIST_SCHEDULES = "qq.list_schedules"
    GET_SCHEDULE = "qq.get_schedule"
    CREATE_SCHEDULE = "qq.create_schedule"
    UPDATE_SCHEDULE = "qq.update_schedule"
    DELETE_SCHEDULE = "qq.delete_schedule"
    CONTROL_AUDIO = "qq.control_audio"
    PUT_MIC = "qq.put_mic"
    DELETE_MIC = "qq.delete_mic"
    GET_API_PERMISSIONS = "qq.get_api_permissions"
    REQUIRE_API_PERMISSION = "qq.require_api_permission"
    LIST_FORUM_THREADS = "qq.list_forum_threads"
    GET_FORUM_THREAD = "qq.get_forum_thread"
    CREATE_FORUM_THREAD = "qq.create_forum_thread"
    DELETE_FORUM_THREAD = "qq.delete_forum_thread"
    GET_MESSAGE_SETTING = "qq.get_message_setting"
    CREATE_GUILD_ANNOUNCE = "qq.create_guild_announce"
    DELETE_GUILD_ANNOUNCE = "qq.delete_guild_announce"
    CLEAN_GUILD_ANNOUNCES = "qq.clean_guild_announces"


@dataclass(frozen=True, slots=True)
class QQRoute:
    method: HTTPMethod
    path: str
    request: type[QQRequest] = QQRequest
    response: type[BaseModel] = QQNoContent
    query: bool = False
    empty_body: bool = False


QQ_ROUTES: Mapping[QQAction, QQRoute] = {
    QQAction.GET_GATEWAY: QQRoute(HTTPMethod.GET, "/gateway", response=QQGatewayInfo),
    QQAction.GET_GATEWAY_BOT: QQRoute(
        HTTPMethod.GET, "/gateway/bot", response=QQGatewayBotInfo
    ),
    QQAction.GET_BOT: QQRoute(HTTPMethod.GET, "/users/@me", response=QQIdentifiedUser),
    QQAction.LIST_BOT_GUILDS: QQRoute(
        HTTPMethod.GET,
        "/users/@me/guilds",
        QQGuildListParams,
        QQGuildList,
    ),
    QQAction.GENERATE_SHARE_LINK: QQRoute(
        HTTPMethod.POST,
        "/v2/generate_url_link",
        QQShareLinkRequest,
        QQShareLink,
        empty_body=True,
    ),
    QQAction.GET_GUILD: QQRoute(
        HTTPMethod.GET, "/guilds/{guild_id}", QQGuildParams, QQGuild
    ),
    QQAction.LIST_GUILD_CHANNELS: QQRoute(
        HTTPMethod.GET,
        "/guilds/{guild_id}/channels",
        QQGuildParams,
        QQChannelList,
    ),
    QQAction.CREATE_CHANNEL: QQRoute(
        HTTPMethod.POST,
        "/guilds/{guild_id}/channels",
        QQChannelCreateRequest,
        QQChannel,
    ),
    QQAction.GET_CHANNEL: QQRoute(
        HTTPMethod.GET, "/channels/{channel_id}", QQChannelParams, QQChannel
    ),
    QQAction.UPDATE_CHANNEL: QQRoute(
        HTTPMethod.PATCH,
        "/channels/{channel_id}",
        QQChannelUpdateRequest,
        QQChannel,
    ),
    QQAction.DELETE_CHANNEL: QQRoute(
        HTTPMethod.DELETE, "/channels/{channel_id}", QQChannelParams
    ),
    QQAction.ACK_INTERACTION: QQRoute(
        HTTPMethod.PUT,
        "/interactions/{interaction_id}",
        QQInteractionAckRequest,
    ),
    QQAction.SEND_C2C_MESSAGE: QQRoute(
        HTTPMethod.POST,
        "/v2/users/{user_openid}/messages",
        QQSendC2CMessageRequest,
        QQSentMessage,
    ),
    QQAction.SEND_C2C_STREAM_MESSAGE: QQRoute(
        HTTPMethod.POST,
        "/v2/users/{user_openid}/stream_messages",
        QQStreamMessageRequest,
        QQSentMessage,
    ),
    QQAction.RECALL_C2C_MESSAGE: QQRoute(
        HTTPMethod.DELETE,
        "/v2/users/{user_openid}/messages/{message_id}",
        QQRecallC2CMessageRequest,
    ),
    QQAction.UPLOAD_C2C_FILE: QQRoute(
        HTTPMethod.POST,
        "/v2/users/{user_openid}/files",
        QQUploadC2CFileRequest,
        QQFileInfo,
    ),
    QQAction.PREPARE_C2C_FILE_UPLOAD: QQRoute(
        HTTPMethod.POST,
        "/v2/users/{user_id}/upload_prepare",
        QQPrepareC2CFileRequest,
        QQFilePrepareResult,
    ),
    QQAction.FINISH_C2C_FILE_UPLOAD: QQRoute(
        HTTPMethod.POST,
        "/v2/users/{user_id}/upload_part_finish",
        QQFinishC2CFileRequest,
    ),
    QQAction.SEND_GROUP_MESSAGE: QQRoute(
        HTTPMethod.POST,
        "/v2/groups/{group_openid}/messages",
        QQSendGroupMessageRequest,
        QQSentMessage,
    ),
    QQAction.RECALL_GROUP_MESSAGE: QQRoute(
        HTTPMethod.DELETE,
        "/v2/groups/{group_openid}/messages/{message_id}",
        QQRecallGroupMessageRequest,
    ),
    QQAction.UPLOAD_GROUP_FILE: QQRoute(
        HTTPMethod.POST,
        "/v2/groups/{group_openid}/files",
        QQUploadGroupFileRequest,
        QQFileInfo,
    ),
    QQAction.PREPARE_GROUP_FILE_UPLOAD: QQRoute(
        HTTPMethod.POST,
        "/v2/groups/{group_id}/upload_prepare",
        QQPrepareGroupFileRequest,
        QQFilePrepareResult,
    ),
    QQAction.FINISH_GROUP_FILE_UPLOAD: QQRoute(
        HTTPMethod.POST,
        "/v2/groups/{group_id}/upload_part_finish",
        QQFinishGroupFileRequest,
    ),
    QQAction.GET_GROUP_INFO: QQRoute(
        HTTPMethod.GET,
        "/v2/groups/{group_openid}/info",
        QQGroupParams,
        QQGroupInfo,
    ),
    QQAction.GET_GROUP_BOT_STATE: QQRoute(
        HTTPMethod.GET,
        "/v2/groups/{group_openid}/bot_state",
        QQGroupParams,
        QQGroupBotState,
    ),
    QQAction.LIST_GROUP_JOIN_REQUESTS: QQRoute(
        HTTPMethod.GET,
        "/v2/groups/{group_openid}/join_request_list",
        QQJoinRequestListParams,
        QQJoinRequestList,
    ),
    QQAction.APPROVE_GROUP_JOIN_REQUEST: QQRoute(
        HTTPMethod.POST,
        "/v2/groups/{group_openid}/approval_join_request/{member_openid}",
        QQApproveJoinRequest,
    ),
    QQAction.GET_GROUP_RESTRICTIONS: QQRoute(
        HTTPMethod.GET,
        "/v2/groups/{group_openid}/restrict_chat_setting",
        QQGroupParams,
        QQGroupRestrictions,
    ),
    QQAction.UPDATE_GROUP_RESTRICTIONS: QQRoute(
        HTTPMethod.POST,
        "/v2/groups/{group_openid}/restrict_chat_setting",
        QQUpdateRestrictionsRequest,
    ),
    QQAction.LIST_GROUP_APPROVAL_STRATEGIES: QQRoute(
        HTTPMethod.GET,
        "/v2/groups/join_approval_strategy",
        QQStrategyListParams,
        QQStrategyList,
    ),
    QQAction.CREATE_GROUP_APPROVAL_STRATEGY: QQRoute(
        HTTPMethod.POST,
        "/v2/groups/join_approval_strategy",
        QQCreateStrategyRequest,
        QQStrategyResult,
    ),
    QQAction.UPDATE_GROUP_APPROVAL_STRATEGY: QQRoute(
        HTTPMethod.PATCH,
        "/v2/groups/join_approval_strategy/{strategy_id}",
        QQUpdateStrategyRequest,
        QQStrategyUpdateResult,
    ),
    QQAction.DELETE_GROUP_APPROVAL_STRATEGY: QQRoute(
        HTTPMethod.DELETE,
        "/v2/groups/join_approval_strategy/{strategy_id}",
        QQDeleteStrategyRequest,
    ),
    QQAction.EXECUTE_GROUP_APPROVAL_STRATEGY: QQRoute(
        HTTPMethod.POST,
        "/v2/groups/join_approval_strategy/{strategy_id}/execute",
        QQDeleteStrategyRequest,
        empty_body=True,
    ),
    QQAction.UPDATE_GROUP_APPROVAL_WHITELIST: QQRoute(
        HTTPMethod.POST,
        "/v2/groups/join_approval_strategy/{strategy_id}/whitelist_users",
        QQUpdateStrategyWhitelistRequest,
        QQStrategyWhitelistResult,
    ),
    QQAction.GET_MENU: QQRoute(HTTPMethod.GET, "/v2/menu", response=QQMenuResult),
    QQAction.PUT_MENU: QQRoute(
        HTTPMethod.PUT,
        "/v2/menu",
        QQPutMenuRequest,
        QQVersionResult,
        empty_body=True,
    ),
    QQAction.LIST_PANELS: QQRoute(
        HTTPMethod.GET,
        "/v2/panels",
        QQPanelListParams,
        QQPanelList,
    ),
    QQAction.CREATE_PANEL: QQRoute(
        HTTPMethod.POST, "/v2/panels", QQCreatePanelRequest, QQPanelIDResult
    ),
    QQAction.GET_PANEL: QQRoute(
        HTTPMethod.GET,
        "/v2/panels/{panel_id}",
        QQPanelParams,
        QQPanelRecord,
    ),
    QQAction.UPDATE_PANEL: QQRoute(
        HTTPMethod.PUT,
        "/v2/panels/{panel_id}",
        QQUpdatePanelRequest,
        QQVersionResult,
    ),
    QQAction.DELETE_PANEL: QQRoute(
        HTTPMethod.DELETE, "/v2/panels/{panel_id}", QQPanelParams
    ),
    QQAction.UPDATE_PANEL_TARGETS: QQRoute(
        HTTPMethod.PUT,
        "/v2/panels/{panel_id}/target",
        QQUpdatePanelTargetsRequest,
    ),
    QQAction.SEND_CHANNEL_MESSAGE: QQRoute(
        HTTPMethod.POST,
        "/channels/{channel_id}/messages",
        QQSendChannelMessageRequest,
        QQMessage,
    ),
    QQAction.RECALL_CHANNEL_MESSAGE: QQRoute(
        HTTPMethod.DELETE,
        "/channels/{channel_id}/messages/{message_id}",
        QQRecallChannelMessageRequest,
        query=True,
    ),
    QQAction.CREATE_DM: QQRoute(
        HTTPMethod.POST, "/users/@me/dms", QQCreateDMRequest, QQDirectMessage
    ),
    QQAction.SEND_DM_MESSAGE: QQRoute(
        HTTPMethod.POST,
        "/dms/{guild_id}/messages",
        QQSendDMMessageRequest,
        QQMessage,
    ),
    QQAction.RECALL_DM_MESSAGE: QQRoute(
        HTTPMethod.DELETE,
        "/dms/{guild_id}/messages/{message_id}",
        QQRecallDMMessageRequest,
        query=True,
    ),
    QQAction.GET_CHANNEL_ONLINE_NUMBERS: QQRoute(
        HTTPMethod.GET,
        "/channels/{channel_id}/online_nums",
        QQChannelParams,
        QQOnlineNumbers,
    ),
    QQAction.LIST_GUILD_MEMBERS: QQRoute(
        HTTPMethod.GET,
        "/guilds/{guild_id}/members",
        QQMemberListParams,
        QQMemberList,
    ),
    QQAction.LIST_GUILD_ROLE_MEMBERS: QQRoute(
        HTTPMethod.GET,
        "/guilds/{guild_id}/roles/{role_id}/members",
        QQRoleMemberListParams,
        QQRoleMemberList,
    ),
    QQAction.GET_GUILD_MEMBER: QQRoute(
        HTTPMethod.GET,
        "/guilds/{guild_id}/members/{user_id}",
        QQMemberParams,
        QQMember,
    ),
    QQAction.DELETE_GUILD_MEMBER: QQRoute(
        HTTPMethod.DELETE,
        "/guilds/{guild_id}/members/{user_id}",
        QQDeleteMemberRequest,
    ),
    QQAction.LIST_GUILD_ROLES: QQRoute(
        HTTPMethod.GET,
        "/guilds/{guild_id}/roles",
        QQGuildParams,
        QQGuildRoles,
    ),
    QQAction.CREATE_GUILD_ROLE: QQRoute(
        HTTPMethod.POST,
        "/guilds/{guild_id}/roles",
        QQCreateRoleRequest,
        QQUpdateRoleResult,
    ),
    QQAction.UPDATE_GUILD_ROLE: QQRoute(
        HTTPMethod.PATCH,
        "/guilds/{guild_id}/roles/{role_id}",
        QQUpdateRoleRequest,
        QQUpdateRoleResult,
    ),
    QQAction.DELETE_GUILD_ROLE: QQRoute(
        HTTPMethod.DELETE,
        "/guilds/{guild_id}/roles/{role_id}",
        QQRoleParams,
    ),
    QQAction.ADD_GUILD_MEMBER_ROLE: QQRoute(
        HTTPMethod.PUT,
        "/guilds/{guild_id}/members/{user_id}/roles/{role_id}",
        QQMemberRoleRequest,
    ),
    QQAction.REMOVE_GUILD_MEMBER_ROLE: QQRoute(
        HTTPMethod.DELETE,
        "/guilds/{guild_id}/members/{user_id}/roles/{role_id}",
        QQMemberRoleRequest,
    ),
    QQAction.GET_MEMBER_CHANNEL_PERMISSIONS: QQRoute(
        HTTPMethod.GET,
        "/channels/{channel_id}/members/{user_id}/permissions",
        QQChannelMemberPermissionParams,
        QQChannelPermissions,
    ),
    QQAction.UPDATE_MEMBER_CHANNEL_PERMISSIONS: QQRoute(
        HTTPMethod.PUT,
        "/channels/{channel_id}/members/{user_id}/permissions",
        QQUpdateMemberPermissionRequest,
    ),
    QQAction.GET_ROLE_CHANNEL_PERMISSIONS: QQRoute(
        HTTPMethod.GET,
        "/channels/{channel_id}/roles/{role_id}/permissions",
        QQChannelRolePermissionParams,
        QQChannelPermissions,
    ),
    QQAction.UPDATE_ROLE_CHANNEL_PERMISSIONS: QQRoute(
        HTTPMethod.PUT,
        "/channels/{channel_id}/roles/{role_id}/permissions",
        QQUpdateRolePermissionRequest,
    ),
    QQAction.ADD_MESSAGE_REACTION: QQRoute(
        HTTPMethod.PUT,
        "/channels/{channel_id}/messages/{message_id}/reactions/"
        "{emoji_type}/{emoji_id}",
        QQEmojiParams,
    ),
    QQAction.REMOVE_MESSAGE_REACTION: QQRoute(
        HTTPMethod.DELETE,
        "/channels/{channel_id}/messages/{message_id}/reactions/"
        "{emoji_type}/{emoji_id}",
        QQEmojiParams,
    ),
    QQAction.LIST_MESSAGE_REACTION_USERS: QQRoute(
        HTTPMethod.GET,
        "/channels/{channel_id}/messages/{message_id}/reactions/"
        "{emoji_type}/{emoji_id}",
        QQReactionUsersParams,
        QQReactionUsers,
    ),
    QQAction.MUTE_GUILD: QQRoute(
        HTTPMethod.PATCH,
        "/guilds/{guild_id}/mute",
        QQGuildMuteRequest,
    ),
    QQAction.MUTE_GUILD_MEMBER: QQRoute(
        HTTPMethod.PATCH,
        "/guilds/{guild_id}/members/{user_id}/mute",
        QQMemberMuteRequest,
    ),
    QQAction.MUTE_GUILD_MEMBERS: QQRoute(
        HTTPMethod.PATCH,
        "/guilds/{guild_id}/mute",
        QQMultiMemberMuteRequest,
        QQMultiMemberMuteResult,
    ),
    QQAction.GET_PINS: QQRoute(
        HTTPMethod.GET,
        "/channels/{channel_id}/pins",
        QQChannelParams,
        QQPinsMessage,
    ),
    QQAction.ADD_PIN: QQRoute(
        HTTPMethod.PUT,
        "/channels/{channel_id}/pins/{message_id}",
        QQPinsParams,
        QQPinsMessage,
    ),
    QQAction.DELETE_PIN: QQRoute(
        HTTPMethod.DELETE,
        "/channels/{channel_id}/pins/{message_id}",
        QQPinsParams,
    ),
    QQAction.CLEAN_PINS: QQRoute(
        HTTPMethod.DELETE,
        "/channels/{channel_id}/pins/all",
        QQChannelParams,
    ),
    QQAction.LIST_SCHEDULES: QQRoute(
        HTTPMethod.GET,
        "/channels/{channel_id}/schedules",
        QQScheduleListParams,
        QQScheduleList,
    ),
    QQAction.GET_SCHEDULE: QQRoute(
        HTTPMethod.GET,
        "/channels/{channel_id}/schedules/{schedule_id}",
        QQScheduleParams,
        QQSchedule,
    ),
    QQAction.CREATE_SCHEDULE: QQRoute(
        HTTPMethod.POST,
        "/channels/{channel_id}/schedules",
        QQScheduleRequest,
        QQSchedule,
    ),
    QQAction.UPDATE_SCHEDULE: QQRoute(
        HTTPMethod.PATCH,
        "/channels/{channel_id}/schedules/{schedule_id}",
        QQScheduleUpdateRequest,
        QQSchedule,
    ),
    QQAction.DELETE_SCHEDULE: QQRoute(
        HTTPMethod.DELETE,
        "/channels/{channel_id}/schedules/{schedule_id}",
        QQScheduleParams,
    ),
    QQAction.CONTROL_AUDIO: QQRoute(
        HTTPMethod.POST,
        "/channels/{channel_id}/audio",
        QQAudioControlRequest,
    ),
    QQAction.PUT_MIC: QQRoute(
        HTTPMethod.PUT, "/channels/{channel_id}/mic", QQChannelParams
    ),
    QQAction.DELETE_MIC: QQRoute(
        HTTPMethod.DELETE,
        "/channels/{channel_id}/mic",
        QQChannelParams,
    ),
    QQAction.GET_API_PERMISSIONS: QQRoute(
        HTTPMethod.GET,
        "/guilds/{guild_id}/api_permission",
        QQGuildParams,
        QQAPIPermissions,
    ),
    QQAction.REQUIRE_API_PERMISSION: QQRoute(
        HTTPMethod.POST,
        "/guilds/{guild_id}/api_permission/demand",
        QQAPIPermissionDemandRequest,
        QQAPIPermissionDemand,
    ),
    QQAction.LIST_FORUM_THREADS: QQRoute(
        HTTPMethod.GET,
        "/channels/{channel_id}/threads",
        QQChannelParams,
        QQForumThreadList,
    ),
    QQAction.GET_FORUM_THREAD: QQRoute(
        HTTPMethod.GET,
        "/channels/{channel_id}/threads/{thread_id}",
        QQForumThreadParams,
        QQForumThreadDetail,
    ),
    QQAction.CREATE_FORUM_THREAD: QQRoute(
        HTTPMethod.PUT,
        "/channels/{channel_id}/threads",
        QQForumCreateRequest,
        QQForumCreateResult,
    ),
    QQAction.DELETE_FORUM_THREAD: QQRoute(
        HTTPMethod.DELETE,
        "/channels/{channel_id}/threads/{thread_id}",
        QQForumThreadParams,
    ),
    QQAction.GET_MESSAGE_SETTING: QQRoute(
        HTTPMethod.GET,
        "/guilds/{guild_id}/message/setting",
        QQGuildParams,
        QQMessageSetting,
    ),
    QQAction.CREATE_GUILD_ANNOUNCE: QQRoute(
        HTTPMethod.POST,
        "/guilds/{guild_id}/announces",
        QQGuildAnnounceRequest,
        QQAnnounce,
    ),
    QQAction.DELETE_GUILD_ANNOUNCE: QQRoute(
        HTTPMethod.DELETE,
        "/guilds/{guild_id}/announces/{message_id}",
        QQGuildAnnounceDeleteRequest,
    ),
    QQAction.CLEAN_GUILD_ANNOUNCES: QQRoute(
        HTTPMethod.DELETE,
        "/guilds/{guild_id}/announces/all",
        QQGuildParams,
    ),
}


class QQAccessTokenRequest(QQRequest):
    app_id: QQID = Field(alias="appId")
    client_secret: QQID = Field(alias="clientSecret")


class QQAccessToken(Model):
    access_token: Annotated[SecretStr, Field(min_length=1)]
    expires_in: PositiveInt


class QQAsyncResult(Model):
    status: Literal[201, 202]
    err_code: StrictInt = Field(validation_alias=AliasChoices("err_code", "code"))
    message: StrictStr
    trace_id: StrictStr | None = None


class QQAPIError(RuntimeError):
    def __init__(
        self,
        status: int,
        *,
        code: JsonValue = None,
        message: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.trace_id = trace_id
        detail = f"QQ API request failed with HTTP {status}"
        if code is not None:
            detail += f", code={code}"
        if message:
            detail += f": {message}"
        if trace_id:
            detail += f" (trace_id={trace_id})"
        super().__init__(detail)


class QQAccessTokenError(QQAPIError):
    pass


class QQRestClient:
    def __init__(
        self,
        app_id: str,
        client_secret: SecretStr | str,
        *,
        base_url: str = QQ_API_BASE_URL,
        http_pool: AsyncPoolManager | None = None,
    ) -> None:
        credential = QQAccessTokenRequest.model_validate({
            "appId": app_id,
            "clientSecret": (
                client_secret.get_secret_value()
                if isinstance(client_secret, SecretStr)
                else client_secret
            ),
        })
        self.app_id = credential.app_id
        self.client_secret = SecretStr(credential.client_secret)
        self.base_url = validate_https_base_url(base_url, "QQ")
        self.http_pool = http_pool if http_pool is not None else AsyncPoolManager()
        self._owns_http_pool = http_pool is None
        self._token: SecretStr | None = None
        self._token_expires_at = 0.0
        self._token_lock = Lock()
        self._rest_lifecycle_lock = Lock()
        self._closed = False
        self._closed_event = Event()

    async def access_token(self) -> str:
        closed_event = self._closed_event
        self._ensure_open(closed_event)
        async with timeout(_HTTP_TIMEOUT):
            return await run_while_open(
                self._access_token(closed_event),
                closed_event,
                self._ensure_open,
            )

    async def _access_token(self, closed_event: Event) -> str:
        self._ensure_open(closed_event)
        now = get_running_loop().time()
        if self._token is not None and now < self._token_expires_at:
            return self._token.get_secret_value()
        async with self._token_lock:
            self._ensure_open(closed_event)
            now = get_running_loop().time()
            if self._token is not None and now < self._token_expires_at:
                return self._token.get_secret_value()
            payload = QQAccessTokenRequest(
                appId=self.app_id,
                clientSecret=self.client_secret.get_secret_value(),
            )
            response, data = await self._request(
                HTTPMethod.POST,
                f"{self.base_url}/app/getAppAccessToken",
                headers={"Content-Type": "application/json"},
                json=payload.model_dump(mode="json"),
            )
            self._ensure_open(closed_event)
            response_payload = self._parse_payload(data, response.status)
            if response.status != HTTPStatus.OK or self._has_business_error(
                response_payload
            ):
                raise self._api_error(
                    response.status,
                    response.headers,
                    response_payload,
                    error_type=QQAccessTokenError,
                )
            try:
                token = QQAccessToken.model_validate(response_payload)
            except ValueError as exc:
                msg = "QQ access-token endpoint returned an invalid response"
                raise RuntimeError(msg) from exc
            self._token = token.access_token
            self._token_expires_at = get_running_loop().time() + max(
                1, token.expires_in - 60
            )
            return token.access_token.get_secret_value()

    def invalidate_token(self, expected: str | None = None) -> None:
        if expected is not None and (
            self._token is None or self._token.get_secret_value() != expected
        ):
            return
        self._token = None
        self._token_expires_at = 0.0

    async def request_qq(
        self,
        action: QQAction | str,
        **params: object,
    ) -> BaseModel:
        closed_event = self._closed_event
        self._ensure_open(closed_event)
        async with timeout(_HTTP_TIMEOUT):
            return await run_while_open(
                self._request_qq(action, params, closed_event),
                closed_event,
                self._ensure_open,
            )

    async def _request_qq(
        self,
        action: QQAction | str,
        params: Mapping[str, object],
        closed_event: Event,
    ) -> BaseModel:
        try:
            qq_action = QQAction(action)
        except ValueError as exc:
            msg = f"unsupported QQ action: {action}"
            raise LookupError(msg) from exc
        route = QQ_ROUTES[qq_action]
        request = route.request.model_validate(params)
        url, body = self._prepare_request(route, request)
        response, response_payload = await self._request_action(
            qq_action,
            route,
            url,
            body,
            closed_event,
        )
        if response.status in {HTTPStatus.CREATED, HTTPStatus.ACCEPTED}:
            return self._async_result(
                response.status,
                response.headers,
                response_payload,
                qq_action,
            )
        if response.status == HTTPStatus.NO_CONTENT:
            return QQNoContent()
        try:
            return route.response.model_validate(response_payload)
        except ValueError as exc:
            msg = f"QQ API returned an invalid response for {qq_action}"
            raise RuntimeError(msg) from exc

    async def _request_action(
        self,
        action: QQAction,
        route: QQRoute,
        url: str,
        body: dict[str, JsonValue] | None,
        closed_event: Event,
    ) -> tuple[AsyncHTTPResponse, JsonValue]:
        token: str | None = None
        retried_token = False
        retried_system_error = False
        while True:
            self._ensure_open(closed_event)
            token = token or await self._access_token(closed_event)
            self._ensure_open(closed_event)
            headers = {
                "Authorization": f"QQBot {token}",
                "Content-Type": "application/json",
                "X-Union-Appid": self.app_id,
            }
            if action is QQAction.ACK_INTERACTION:
                headers["X-Callback-AppID"] = self.app_id
            response, data = await self._request(
                route.method,
                url,
                headers=headers,
                json=body,
            )
            self._ensure_open(closed_event)
            response_payload = self._parse_payload(data, response.status)
            if response.status in {
                HTTPStatus.OK,
                HTTPStatus.CREATED,
                HTTPStatus.ACCEPTED,
                HTTPStatus.NO_CONTENT,
            } and (
                response.status != HTTPStatus.OK
                or not self._has_business_error(response_payload)
            ):
                return response, response_payload
            token_expired = isinstance(response_payload, dict) and any(
                str(response_payload.get(name)) == "11244"
                for name in ("err_code", "code")
            )
            if response.status == HTTPStatus.UNAUTHORIZED or token_expired:
                self.invalidate_token(token)
                if not retried_token:
                    token = None
                    retried_token = True
                    continue
            elif (
                not retried_system_error
                and str(self._error_code(response_payload)) in _RETRYABLE_ERROR_CODES
            ):
                retried_system_error = True
                continue
            raise self._api_error(
                response.status,
                response.headers,
                response_payload,
            )

    async def close(self) -> None:
        async with self._rest_lifecycle_lock:
            if self._closed:
                return
            self._closed_event.set()
            self.invalidate_token()
            if self._owns_http_pool:
                await self.http_pool.clear()
            self._closed = True

    async def start(self) -> None:
        async with self._rest_lifecycle_lock:
            if not self._closed and not self._closed_event.is_set():
                return
            if self._owns_http_pool:
                if not self._closed:
                    await self.http_pool.clear()
                self.http_pool = AsyncPoolManager()
            self._token_lock = Lock()
            self._closed_event = Event()
            self._closed = False

    def _ensure_open(self, closed_event: Event) -> None:
        if (
            self._closed
            or closed_event is not self._closed_event
            or closed_event.is_set()
        ):
            msg = "QQ REST client is closed"
            raise RuntimeError(msg)

    async def _request(
        self,
        method: HTTPMethod,
        url: str,
        *,
        headers: Mapping[str, str],
        json: dict[str, JsonValue] | None,
    ) -> tuple[AsyncHTTPResponse, bytes]:
        response = await self.http_pool.request(
            method,
            url,
            headers=headers,
            json=json,
            retries=False,
            timeout=_HTTP_TIMEOUT,
        )
        return response, await response.data

    def _prepare_request(
        self,
        route: QQRoute,
        request: QQRequest,
    ) -> tuple[str, dict[str, JsonValue] | None]:
        payload = request.model_dump(
            mode="json",
            exclude_none=True,
        )
        path_fields = {
            field_name
            for _, field_name, _, _ in Formatter().parse(route.path)
            if field_name is not None
        }
        path = route.path.format_map({
            name: quote(str(payload.pop(name)), safe="") for name in path_fields
        })
        if route.method is HTTPMethod.GET or route.query:
            query = {
                name: (str(value).lower() if isinstance(value, bool) else str(value))
                for name, value in payload.items()
            }
            payload.clear()
        else:
            query = {}
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        return url, payload or ({} if route.empty_body else None)

    @staticmethod
    def _parse_payload(data: bytes, status: int) -> JsonValue:
        try:
            return loads(data) if data else None
        except ValueError as exc:
            if not HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES:
                return None
            msg = "QQ API returned invalid JSON"
            raise RuntimeError(msg) from exc

    @staticmethod
    def _error_code(payload: JsonValue) -> str | int | float | bool | None:
        if not isinstance(payload, dict):
            return None
        value = payload.get("err_code")
        if value is None:
            value = payload.get("code")
        return value if isinstance(value, str | int | float | bool) else None

    @classmethod
    def _has_business_error(cls, payload: JsonValue) -> bool:
        return cls._error_code(payload) not in {None, 0, "0"}

    @staticmethod
    def _async_result(
        status: int,
        headers: Mapping[str, object],
        payload: JsonValue,
        action: QQAction,
    ) -> QQAsyncResult:
        if not isinstance(payload, dict):
            msg = f"QQ API returned an invalid async response for {action}"
            raise RuntimeError(msg)  # ruff: ignore[type-check-without-type-error] - malformed upstream response
        try:
            data = {**payload, "status": status}
            if not data.get("trace_id"):
                data["trace_id"] = header_value(
                    headers,
                    "X-Tps-Trace-ID",
                ) or header_value(headers, "X-Trace-ID")
            return QQAsyncResult.model_validate(data)
        except ValueError as exc:
            msg = f"QQ API returned an invalid async response for {action}"
            raise RuntimeError(msg) from exc

    @classmethod
    def _api_error(
        cls,
        status: int,
        headers: Mapping[str, object],
        payload: JsonValue,
        *,
        error_type: type[QQAPIError] = QQAPIError,
    ) -> QQAPIError:
        code = cls._error_code(payload)
        message: str | None = None
        body_trace_id: str | None = None
        if isinstance(payload, dict):
            value = payload.get("message") or payload.get("msg")
            if isinstance(value, str):
                message = value
            value = payload.get("trace_id")
            if isinstance(value, str):
                body_trace_id = value
        trace_id = (
            header_value(headers, "X-Tps-Trace-ID")
            or header_value(headers, "X-Trace-ID")
            or body_trace_id
        )
        return error_type(
            status,
            code=code,
            message=message,
            trace_id=trace_id,
        )
