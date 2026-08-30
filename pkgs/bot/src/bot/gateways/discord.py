from asyncio import (
    CancelledError,
    Future,
    Lock,
    QueueFull,
    Task,
    create_task,
    gather,
    get_running_loop,
    sleep,
    timeout,
    timeout_at,
)
from asyncio import (
    Event as AsyncEvent,
)
from collections import defaultdict, deque
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from enum import STRICT, IntEnum, IntFlag
from functools import partial
from http import HTTPStatus
from importlib.metadata import version
from logging import getLogger
from math import isfinite
from mimetypes import guess_type
from random import random
from re import compile as compile_regex
from sys import platform as operating_system
from time import time
from typing import Annotated, Literal, Self, cast, override
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    SecretStr,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    TypeAdapter,
    UrlConstraints,
    ValidationError,
    WebsocketUrl,
    model_validator,
)
from urllib3.filepost import encode_multipart_formdata
from urllib3_future import AsyncHTTPResponse, AsyncPoolManager
from urllib3_future.exceptions import HTTPError
from websockets.exceptions import InvalidHandshake

from bot._tasks import await_cleanup
from bot.core import Bot
from bot.json import dumpb, loads
from bot.protocol.actions import ActionParamInput, ActionParamModel, WireBytes
from bot.protocol.base import Model, StrictBoolLiteral, StrictIntLiteral
from bot.protocol.common import BotSelf, BotStatus, Status, Version
from bot.protocol.enums import Action
from bot.protocol.events import (
    ChannelMessageEvent,
    Event,
    MessageEvent,
    MetaEvent,
    NoticeEvent,
    PrivateMessageEvent,
)
from bot.protocol.msg import (
    MentionAllSegment,
    MentionSegment,
    Msg,
    MsgInput,
    ReplySegment,
    TextSegment,
)

from .base import (
    Connection,
    Gateway,
    WebSocketClosedError,
    WebSocketConnection,
    WebSocketConnector,
    connect_websocket,
    header_value,
    read_http_body,
    validate_https_base_url,
)

logger = getLogger(__name__)

DISCORD_API_BASE_URL = "https://discord.com/api/v10"
_API_TIMEOUT = 30.0
_HELLO_TIMEOUT = 30.0
_RECONNECT_DELAYS = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
_FATAL_CLOSE_CODES = frozenset({4004, 4010, 4011, 4012, 4013, 4014})
_NEW_SESSION_CLOSE_CODES = frozenset({4003, 4007, 4009})
_USER_AGENT = "DiscordBot (https://github.com/LetsStarveTogether/lst-bot, 0.0.0)"
_MAX_GATEWAY_PAYLOAD_BYTES = 4096
_MAX_GATEWAY_EVENTS = 120
_GATEWAY_SYSTEM_RESERVE = 10
_GATEWAY_WINDOW_SECONDS = 60.0
_MAX_AUDIT_REASON_LENGTH = 512
_MAX_REST_ATTEMPTS = 5
_MAX_GLOBAL_REST_REQUESTS = 50
_GLOBAL_REST_WINDOW_SECONDS = 1.0
_RATE_BUCKET_PRUNE_THRESHOLD = 256
_MAX_NONCE_BYTES = 32
_MAX_MESSAGE_LENGTH = 2000
_MAX_ALLOWED_MENTIONS = 100
_RATE_LIMITED_CLOSE_CODE = 4008
_INVALID_PERCENT_ESCAPE = compile_regex(r"%(?![0-9A-Fa-f]{2})")
_GUILD_PAGE_SIZE = 200
_MEMBER_PAGE_SIZE = 1000
_ASCII_SPACE = 32
_ASCII_DELETE = 127
_INTERACTION_AUTO_ACK_DELAY = 2.0
_INTERACTION_CALLBACK_PATH = compile_regex(
    r"^/interactions/[1-9][0-9]{0,19}/[^/]+/callback$"
)
_WEBHOOK_TOKEN_PATH = compile_regex(r"^/webhooks/[1-9][0-9]{0,19}/[^/]+(?:/.*)?$")
_INTERACTION_AUTO_RESPONSES: dict[int, JsonValue] = {
    2: {"type": 5},
    3: {"type": 6},
    4: {"type": 8, "data": {"choices": []}},
    5: {"type": 5},
}

type NonNegativeInt = Annotated[StrictInt, Field(ge=0)]
type PositiveInt = Annotated[StrictInt, Field(gt=0)]
type DiscordComponentId = Annotated[StrictInt, Field(ge=0, le=2**31 - 1)]
type DiscordWebsocketUrl = Annotated[
    WebsocketUrl,
    UrlConstraints(allowed_schemes=["wss"]),
]
type DiscordQueryScalar = StrictStr | StrictInt | StrictBool
type DiscordQueryValue = DiscordQueryScalar | list[DiscordQueryScalar]
type MultipartFieldValue = (
    str | bytes | tuple[str, str | bytes] | tuple[str, str | bytes, str]
)


def _snowflake(value: str) -> str:
    if int(value) > 2**64 - 1:
        msg = "Discord snowflake must fit an unsigned 64-bit integer"
        raise ValueError(msg)
    return value


type Snowflake = Annotated[
    StrictStr,
    Field(pattern=r"^[1-9][0-9]{0,19}$"),
    AfterValidator(_snowflake),
]

_SNOWFLAKE_ADAPTER = TypeAdapter(Snowflake)


def _untrimmed_guild_name(value: str) -> str:
    if value != value.strip():
        msg = "Discord guild name cannot have leading or trailing whitespace"
        raise ValueError(msg)
    return value


type DiscordGuildName = Annotated[
    StrictStr,
    Field(min_length=2, max_length=100),
    AfterValidator(_untrimmed_guild_name),
]
type DiscordChannelName = Annotated[StrictStr, Field(min_length=1, max_length=100)]
type _DiscordTimestampString = Annotated[
    StrictStr,
    Field(
        pattern=(
            r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
            r"(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})$"
        )
    ),
]
type DiscordTimestamp = Annotated[
    AwareDatetime,
    BeforeValidator(
        TypeAdapter(_DiscordTimestampString).validate_python,
        json_schema_input_type=_DiscordTimestampString,
    ),
]

_GUILD_NAME_ADAPTER = TypeAdapter(DiscordGuildName)
_CHANNEL_NAME_ADAPTER = TypeAdapter(DiscordChannelName)


class DiscordRequestModel(Model):
    model_config = ConfigDict(extra="forbid")


class DiscordUser(Model):
    id: Snowflake
    username: StrictStr
    discriminator: Annotated[StrictStr, Field(pattern=r"^(?:0|[0-9]{4})$")]
    global_name: StrictStr | None
    avatar: StrictStr | None
    bot: StrictBool | None = None
    system: StrictBool | None = None
    mfa_enabled: StrictBool | None = None
    banner: StrictStr | None = None
    accent_color: StrictInt | None = None
    locale: StrictStr | None = None
    verified: StrictBool | None = None
    email: StrictStr | None = None
    flags: NonNegativeInt | None = None
    premium_type: NonNegativeInt | None = None
    public_flags: NonNegativeInt | None = None


class DiscordRoleColors(Model):
    primary_color: NonNegativeInt
    secondary_color: NonNegativeInt | None
    tertiary_color: NonNegativeInt | None


class DiscordRole(Model):
    id: Snowflake
    name: StrictStr
    color: NonNegativeInt
    colors: DiscordRoleColors
    hoist: StrictBool
    position: StrictInt
    permissions: Annotated[StrictStr, Field(pattern=r"^[0-9]+$")]
    managed: StrictBool
    mentionable: StrictBool
    flags: NonNegativeInt

    @model_validator(mode="after")
    def matching_legacy_color(self) -> Self:
        if self.color != self.colors.primary_color:
            msg = "Discord role color must match colors.primary_color"
            raise ValueError(msg)
        return self


class DiscordMember(Model):
    user: DiscordUser | None = None
    nick: StrictStr | None = None
    avatar: StrictStr | None = None
    banner: StrictStr | None = None
    roles: Annotated[list[Snowflake], Field(max_length=250)]
    joined_at: DiscordTimestamp | None = None
    premium_since: DiscordTimestamp | None = None
    deaf: StrictBool = False
    mute: StrictBool = False
    flags: NonNegativeInt = 0
    pending: StrictBool | None = None
    permissions: Annotated[StrictStr, Field(pattern=r"^[0-9]+$")] | None = None
    communication_disabled_until: DiscordTimestamp | None = None


class DiscordAttachment(Model):
    id: Snowflake
    filename: StrictStr
    title: StrictStr | None = None
    description: Annotated[StrictStr, Field(max_length=1024)] | None = None
    content_type: StrictStr | None = None
    size: NonNegativeInt
    url: StrictStr
    proxy_url: StrictStr
    height: NonNegativeInt | None = None
    width: NonNegativeInt | None = None
    ephemeral: StrictBool | None = None
    duration_secs: Annotated[StrictFloat, Field(ge=0)] | None = None
    waveform: StrictStr | None = None
    flags: NonNegativeInt | None = None


class DiscordMessageReference(Model):
    type: StrictIntLiteral[Literal[0, 1]] | None = None
    message_id: Snowflake | None = None
    channel_id: Snowflake | None = None
    guild_id: Snowflake | None = None
    fail_if_not_exists: StrictBool | None = None


class DiscordMessage(Model):
    id: Snowflake
    channel_id: Snowflake
    author: DiscordUser
    content: StrictStr
    timestamp: DiscordTimestamp
    edited_timestamp: DiscordTimestamp | None
    tts: StrictBool
    mention_everyone: StrictBool
    mentions: list[DiscordUser]
    mention_roles: list[Snowflake]
    attachments: Annotated[list[DiscordAttachment], Field(max_length=10)]
    embeds: Annotated[list[dict[StrictStr, JsonValue]], Field(max_length=10)]
    pinned: StrictBool
    type: NonNegativeInt
    guild_id: Snowflake | None = None
    member: DiscordMember | None = None
    mention_channels: list[dict[StrictStr, JsonValue]] | None = None
    reactions: (
        Annotated[list[dict[StrictStr, JsonValue]], Field(max_length=20)] | None
    ) = None
    nonce: StrictStr | StrictInt | None = None
    webhook_id: Snowflake | None = None
    activity: dict[StrictStr, JsonValue] | None = None
    application: dict[StrictStr, JsonValue] | None = None
    application_id: Snowflake | None = None
    message_reference: DiscordMessageReference | None = None
    flags: NonNegativeInt | None = None
    referenced_message: DiscordMessage | None = None
    thread: DiscordChannel | None = None
    components: list[dict[StrictStr, JsonValue]] | None = None
    sticker_items: list[dict[StrictStr, JsonValue]] | None = None
    position: NonNegativeInt | None = None


class DiscordPartialMessage(Model):
    id: Snowflake
    channel_id: Snowflake
    guild_id: Snowflake | None = None
    author: DiscordUser | None = None
    content: StrictStr | None = None
    timestamp: DiscordTimestamp | None = None
    edited_timestamp: DiscordTimestamp | None = None
    mentions: list[DiscordUser] | None = None
    mention_roles: list[Snowflake] | None = None
    attachments: Annotated[list[DiscordAttachment], Field(max_length=10)] | None = None


class DiscordPermissionOverwrite(Model):
    id: Snowflake
    type: StrictIntLiteral[Literal[0, 1]]
    allow: Annotated[StrictStr, Field(pattern=r"^[0-9]+$")]
    deny: Annotated[StrictStr, Field(pattern=r"^[0-9]+$")]


class DiscordChannel(Model):
    id: Snowflake
    type: NonNegativeInt
    guild_id: Snowflake | None = None
    position: StrictInt | None = None
    permission_overwrites: (
        Annotated[list[DiscordPermissionOverwrite], Field(max_length=1000)] | None
    ) = None
    name: DiscordChannelName | None = None
    topic: StrictStr | None = None
    nsfw: StrictBool | None = None
    last_message_id: Snowflake | None = None
    bitrate: NonNegativeInt | None = None
    user_limit: NonNegativeInt | None = None
    rate_limit_per_user: Annotated[StrictInt, Field(ge=0, le=21600)] | None = None
    recipients: list[DiscordUser] | None = None
    icon: StrictStr | None = None
    owner_id: Snowflake | None = None
    application_id: Snowflake | None = None
    managed: StrictBool | None = None
    parent_id: Snowflake | None = None
    last_pin_timestamp: DiscordTimestamp | None = None
    rtc_region: StrictStr | None = None
    video_quality_mode: NonNegativeInt | None = None
    message_count: NonNegativeInt | None = None
    member_count: NonNegativeInt | None = None
    default_auto_archive_duration: NonNegativeInt | None = None
    permissions: Annotated[StrictStr, Field(pattern=r"^[0-9]+$")] | None = None
    flags: NonNegativeInt | None = None
    total_message_sent: NonNegativeInt | None = None
    applied_tags: list[Snowflake] | None = None


class DiscordGuild(Model):
    id: Snowflake
    name: DiscordGuildName
    icon: StrictStr | None
    owner_id: Snowflake
    afk_channel_id: Snowflake | None
    verification_level: NonNegativeInt
    default_message_notifications: NonNegativeInt
    explicit_content_filter: NonNegativeInt
    roles: Annotated[list[DiscordRole], Field(max_length=250)]
    emojis: list[dict[StrictStr, JsonValue]]
    features: list[StrictStr]
    mfa_level: NonNegativeInt
    application_id: Snowflake | None
    system_channel_id: Snowflake | None
    system_channel_flags: NonNegativeInt
    rules_channel_id: Snowflake | None
    max_presences: NonNegativeInt | None = None
    max_members: NonNegativeInt | None = None
    vanity_url_code: StrictStr | None = None
    description: StrictStr | None = None
    banner: StrictStr | None = None
    premium_tier: NonNegativeInt
    premium_subscription_count: NonNegativeInt | None = None
    preferred_locale: StrictStr
    public_updates_channel_id: Snowflake | None
    max_video_channel_users: NonNegativeInt | None = None
    max_stage_video_channel_users: NonNegativeInt | None = None
    approximate_member_count: NonNegativeInt | None = None
    approximate_presence_count: NonNegativeInt | None = None
    nsfw_level: NonNegativeInt
    stickers: list[dict[StrictStr, JsonValue]] | None = None
    premium_progress_bar_enabled: StrictBool
    safety_alerts_channel_id: Snowflake | None


class DiscordInteractionData(Model):
    id: Snowflake | DiscordComponentId | None = None
    name: StrictStr | None = None
    type: NonNegativeInt | None = None
    resolved: dict[StrictStr, JsonValue] | None = None
    options: list[dict[StrictStr, JsonValue]] | None = None
    guild_id: Snowflake | None = None
    target_id: Snowflake | None = None
    custom_id: StrictStr | None = None
    component_type: NonNegativeInt | None = None
    values: list[StrictStr] | None = None
    components: list[dict[StrictStr, JsonValue]] | None = None


class DiscordInteraction(Model):
    id: Snowflake
    application_id: Snowflake
    type: Annotated[StrictInt, Field(ge=1, le=5)]
    data: DiscordInteractionData | None = None
    guild_id: Snowflake | None = None
    channel: DiscordChannel | None = None
    channel_id: Snowflake | None = None
    member: DiscordMember | None = None
    user: DiscordUser | None = None
    token: Annotated[StrictStr, Field(min_length=1, repr=False)]
    version: StrictIntLiteral[Literal[1]]
    message: DiscordMessage | None = None
    app_permissions: Annotated[StrictStr, Field(pattern=r"^[0-9]+$")]
    locale: StrictStr | None = None
    guild_locale: StrictStr | None = None
    entitlements: list[dict[StrictStr, JsonValue]]
    authorizing_integration_owners: dict[Literal["0", "1"], Snowflake | Literal["0"]]
    context: StrictIntLiteral[Literal[0, 1, 2]] | None = None
    attachment_size_limit: NonNegativeInt

    @model_validator(mode="after")
    def interaction_data_shape(self) -> Self:
        required = {
            2: ("id", "name", "type"),
            3: ("custom_id", "component_type"),
            4: ("id", "name", "type"),
            5: ("custom_id", "components"),
        }.get(self.type)
        if required is None:
            return self
        if self.data is None or any(
            getattr(self.data, field_name) is None for field_name in required
        ):
            msg = f"Discord interaction type {self.type} has incomplete data"
            raise ValueError(msg)
        if self.type in {2, 3, 4} and self.data.id is not None:
            command_id = self.type in {2, 4}
            if command_id != isinstance(self.data.id, str):
                msg = f"Discord interaction type {self.type} has invalid data id"
                raise ValueError(msg)
        return self


class DiscordApplication(Model):
    id: Snowflake
    flags: NonNegativeInt
    flags_new: Annotated[StrictStr, Field(pattern=r"^[0-9]+$")]


class DiscordUnavailableGuild(Model):
    id: Snowflake
    unavailable: StrictBoolLiteral[Literal[True]]


def _valid_shard(value: tuple[int, int]) -> tuple[int, int]:
    if value[0] >= value[1]:
        msg = "Discord shard id must be smaller than shard count"
        raise ValueError(msg)
    return value


type DiscordShard = Annotated[
    tuple[NonNegativeInt, PositiveInt],
    AfterValidator(_valid_shard),
]


class DiscordReady(Model):
    v: StrictIntLiteral[Literal[10]]
    user: DiscordUser
    guilds: list[DiscordUnavailableGuild]
    session_id: Annotated[StrictStr, Field(min_length=1)]
    resume_gateway_url: DiscordWebsocketUrl
    shard: DiscordShard | None = None
    application: DiscordApplication


class DiscordMessageDelete(Model):
    id: Snowflake
    channel_id: Snowflake
    guild_id: Snowflake | None = None


class DiscordMessageDeleteBulk(Model):
    ids: Annotated[list[Snowflake], Field(min_length=1)]
    channel_id: Snowflake
    guild_id: Snowflake | None = None


class DiscordGuildDelete(Model):
    id: Snowflake
    unavailable: StrictBool | None = None


class DiscordGuildMemberEvent(DiscordMember):
    user: DiscordUser
    guild_id: Snowflake


class DiscordGuildMemberRemove(Model):
    guild_id: Snowflake
    user: DiscordUser


class DiscordGuildMember(DiscordMember):
    user: DiscordUser


class DiscordSessionStartLimit(Model):
    total: NonNegativeInt
    remaining: NonNegativeInt
    reset_after: NonNegativeInt
    max_concurrency: PositiveInt


class DiscordGatewayBotInfo(Model):
    url: DiscordWebsocketUrl
    shards: PositiveInt
    session_start_limit: DiscordSessionStartLimit


class DiscordNoContent(Model):
    pass


class DiscordPayload(RootModel[JsonValue]):
    model_config = ConfigDict(allow_inf_nan=False)

    def __repr_args__(  # ruff: ignore[bad-dunder-method-name] - arbitrary API data can contain credentials
        self,
    ) -> list[tuple[str | None, object]]:
        return []


class DiscordBytes(RootModel[WireBytes]):
    def __repr_args__(  # ruff: ignore[bad-dunder-method-name] - arbitrary API data can contain credentials
        self,
    ) -> list[tuple[str | None, object]]:
        return []


class DiscordAPIError(RuntimeError):
    def __init__(
        self,
        status: int,
        *,
        code: int | None = None,
        message: str | None = None,
        errors: JsonValue = None,
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.errors = errors
        detail = f"Discord API request failed with HTTP {status}"
        if code is not None:
            detail += f", code={code}"
        if message:
            detail += f": {message}"
        super().__init__(detail)


type MultipartName = Annotated[
    StrictStr,
    Field(min_length=1, pattern=r"^[^\x00-\x1f\x7f]+$"),
]


class DiscordFile(DiscordRequestModel):
    field: MultipartName | None = None
    filename: MultipartName
    data: WireBytes = Field(repr=False)
    content_type: MultipartName = "application/octet-stream"


def _relative_api_path(value: str) -> str:
    parsed = urlsplit(value)
    decoded = unquote(value)
    parts = decoded.split("/")[1:]
    invalid_location = (
        not value.startswith("/")
        or value.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
    )
    invalid_path = (
        "\\" in value
        or _INVALID_PERCENT_ESCAPE.search(value) is not None
        or any(
            ord(character) <= _ASCII_SPACE or ord(character) == _ASCII_DELETE
            for character in decoded
        )
        or any(not part or part in {".", ".."} for part in parts)
    )
    if invalid_location or invalid_path:
        msg = "Discord API path must be an absolute relative path without a query"
        raise ValueError(msg)
    return value


type DiscordApiPath = Annotated[
    StrictStr,
    AfterValidator(_relative_api_path),
]


class DiscordRequest(DiscordRequestModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: DiscordApiPath = Field(repr=False)
    query: dict[StrictStr, DiscordQueryValue] | None = Field(None, repr=False)
    json_: JsonValue = Field(None, alias="json", repr=False)
    files: Annotated[list[DiscordFile], Field(min_length=1)] | None = Field(
        None,
        repr=False,
    )
    multipart: Literal["payload_json", "form_fields"] = "payload_json"
    auth: StrictBool = True
    response_type: Literal["json", "bytes"] = "json"
    reason: Annotated[StrictStr, Field(min_length=1)] | None = Field(
        None,
        repr=False,
    )

    @model_validator(mode="after")
    def request_shape(self) -> Self:
        if self.files is not None and self.method not in {"POST", "PUT", "PATCH"}:
            msg = "Discord multipart requests require POST, PUT, or PATCH"
            raise ValueError(msg)
        if self.multipart == "form_fields":
            if self.files is None:
                msg = "Discord form-fields multipart requires at least one file"
                raise ValueError(msg)
            if self.json_ is not None and (
                not isinstance(self.json_, dict)
                or any(isinstance(value, dict | list) for value in self.json_.values())
            ):
                msg = "Discord form-fields multipart requires a flat JSON object"
                raise ValueError(msg)
        if self.files is not None:
            part_names = {"payload_json"} if self.multipart == "payload_json" else set()
            if self.multipart == "form_fields" and isinstance(self.json_, dict):
                part_names.update(self.json_)
            for index, file in enumerate(self.files):
                name = file.field or f"files[{index}]"
                if name in part_names:
                    msg = "Discord multipart part names must be unique"
                    raise ValueError(msg)
                part_names.add(name)
        if (
            self.reason is not None
            and len(quote(self.reason, safe="")) > _MAX_AUDIT_REASON_LENGTH
        ):
            msg = "Discord audit-log reason is longer than 512 encoded characters"
            raise ValueError(msg)
        return self


@dataclass(slots=True)
class _DiscordRateBucket:
    lock: Lock = field(default_factory=Lock)
    ready_at: float = 0.0


type _DiscordResponse = DiscordPayload | DiscordBytes | DiscordNoContent


@dataclass(slots=True)
class _DiscordInteractionCallback:
    default_payload: JsonValue
    deadline: float
    ready: AsyncEvent = field(default_factory=AsyncEvent)
    request: DiscordRequest | None = None
    outcome: Future[_DiscordResponse] | None = None
    task: Task[None] | None = None
    claimed: bool = False


class DiscordRestClient:
    def __init__(
        self,
        token: SecretStr | str,
        *,
        http_pool: AsyncPoolManager,
        base_url: str = DISCORD_API_BASE_URL,
    ) -> None:
        token_value = (
            token.get_secret_value() if isinstance(token, SecretStr) else token
        )
        if (
            not isinstance(token_value, str)
            or not token_value
            or any(character.isspace() for character in token_value)
        ):
            msg = "Discord bot token must be non-empty and contain no whitespace"
            raise ValueError(msg)
        self.token = SecretStr(token_value)
        self.base_url = validate_https_base_url(base_url, "Discord")
        self.http_pool = http_pool
        self._rest_lifecycle_lock = Lock()
        self._route_buckets: dict[tuple[str, str], str] = {}
        self._rate_buckets: defaultdict[tuple[str, str, str], _DiscordRateBucket] = (
            defaultdict(_DiscordRateBucket)
        )
        self._global_ready_at = dict.fromkeys(("authless", "bot", "interaction"), 0.0)
        self._global_send_lock = Lock()
        self._global_send_times = {
            "authless": deque[float](),
            "bot": deque[float](),
        }
        self._closed = False
        self._accepting_requests = True
        self._rate_limit_interrupt = AsyncEvent()
        self._unauthorized = False
        self._inflight_requests: set[AsyncEvent] = set()
        self._interaction_callbacks: dict[str, _DiscordInteractionCallback] = {}

    async def request_discord(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, object] | None = None,
        json: JsonValue = None,
        files: list[object] | None = None,
        multipart: Literal["payload_json", "form_fields"] = "payload_json",
        auth: bool = True,
        reason: str | None = None,
        response_type: Literal["json", "bytes"] = "json",
    ) -> DiscordPayload | DiscordBytes | DiscordNoContent:
        request = DiscordRequest.model_validate({
            "method": method,
            "path": path,
            "query": query,
            "json": json,
            "files": files,
            "multipart": multipart,
            "auth": auth,
            "reason": reason,
            "response_type": response_type,
        })
        return await self._request_discord(request)

    async def _request_discord(
        self,
        request: DiscordRequest,
    ) -> DiscordPayload | DiscordBytes | DiscordNoContent:
        canonical_path = unquote(request.path)
        interaction_callback = _INTERACTION_CALLBACK_PATH.fullmatch(canonical_path)
        callback = request.method == "POST" and interaction_callback is not None
        if request.auth and (
            interaction_callback or _WEBHOOK_TOKEN_PATH.fullmatch(canonical_path)
        ):
            request = request.model_copy(update={"auth": False})
        if self._closed or not self._accepting_requests:
            msg = "Discord REST client is closed"
            raise RuntimeError(msg)
        pending = self._interaction_callbacks.get(canonical_path) if callback else None
        if pending is not None:
            return await self._submit_interaction_callback(pending, request)
        return await self._perform_tracked_request(request)

    async def _perform_tracked_request(
        self,
        request: DiscordRequest,
    ) -> _DiscordResponse:
        completed = AsyncEvent()
        self._inflight_requests.add(completed)
        try:
            return await self._perform_request(request)
        finally:
            completed.set()
            self._inflight_requests.discard(completed)

    @staticmethod
    async def _submit_interaction_callback(
        pending: _DiscordInteractionCallback,
        request: DiscordRequest,
    ) -> _DiscordResponse:
        if pending.claimed or pending.request is not None:
            msg = "Discord interaction callback was already claimed"
            raise RuntimeError(msg)
        pending.request = request
        pending.outcome = Future()
        pending.ready.set()
        return await pending.outcome

    async def _perform_request(  # ruff: ignore[complex-structure] - one loop owns bucket rebinding and 429 retries
        self,
        request: DiscordRequest,
    ) -> DiscordPayload | DiscordBytes | DiscordNoContent:
        canonical_path = unquote(request.path)
        lane: Literal["authless", "bot", "interaction"] = (
            "interaction"
            if request.method == "POST"
            and _INTERACTION_CALLBACK_PATH.fullmatch(canonical_path)
            else "bot"
            if request.auth
            else "authless"
        )
        for attempt in range(_MAX_REST_ATTEMPTS):
            while True:
                route, major, bucket = self._rate_bucket(request)
                async with bucket.lock:
                    if self._rate_bucket(request)[2] is not bucket:
                        continue
                    self._ensure_available(request)
                    await self._wait_for_rate_limit(bucket, lane=lane)
                    self._ensure_available(request)
                    await self._wait_for_global_limit(lane)
                    self._ensure_available(request)
                    try:
                        async with timeout(_API_TIMEOUT):
                            response, data = await self._request(request)
                    except HTTPError, TimeoutError:
                        msg = "Discord API transport failed"
                        raise ConnectionError(msg) from None
                    payload = (
                        self._parse_payload(data, response.status)
                        if request.response_type == "json"
                        or not HTTPStatus.OK
                        <= response.status
                        < HTTPStatus.MULTIPLE_CHOICES
                        else None
                    )
                    retry_after = (
                        self._retry_after(payload, response)
                        if response.status == HTTPStatus.TOO_MANY_REQUESTS
                        else None
                    )
                    bound_bucket = self._bind_rate_bucket(
                        response,
                        route=route,
                        major=major,
                        bucket=bucket,
                    )
                    self._record_rate_limit(
                        response,
                        payload,
                        retry_after=retry_after,
                        bucket=bucket,
                        bound_bucket=bound_bucket,
                        lane=lane,
                    )
                    if response.status == HTTPStatus.UNAUTHORIZED and request.auth:
                        self._unauthorized = True
                    break
            if response.status == HTTPStatus.TOO_MANY_REQUESTS:
                if retry_after is not None and attempt < _MAX_REST_ATTEMPTS - 1:
                    continue
                raise self._api_error(response.status, payload)
            if response.status == HTTPStatus.BAD_GATEWAY:
                if attempt < _MAX_REST_ATTEMPTS - 1:
                    delay = _RECONNECT_DELAYS[min(attempt, len(_RECONNECT_DELAYS) - 1)]
                    with suppress(TimeoutError):
                        async with timeout(delay):
                            await self._rate_limit_interrupt.wait()
                    continue
                raise self._api_error(response.status, payload)
            if response.status == HTTPStatus.NO_CONTENT:
                return DiscordNoContent()
            if not HTTPStatus.OK <= response.status < HTTPStatus.MULTIPLE_CHOICES:
                raise self._api_error(response.status, payload)
            if request.response_type == "bytes":
                return DiscordBytes(data)
            return DiscordPayload(payload)
        msg = "Discord REST retry loop exhausted"
        raise AssertionError(msg)

    async def close(self) -> None:
        async with self._rest_lifecycle_lock:
            if self._closed:
                return
            self._accepting_requests = False
            self._rate_limit_interrupt.set()
            pending = tuple(self._inflight_requests)
            finishing = create_task(
                self._finish_close(pending),
                name="discord-rest-close",
            )
            await await_cleanup(finishing)

    async def _finish_close(self, pending: tuple[AsyncEvent, ...]) -> None:
        try:
            await gather(*(completed.wait() for completed in pending))
            self._closed = True
        finally:
            self._route_buckets.clear()
            self._rate_buckets.clear()
            self._global_ready_at = dict.fromkeys(
                ("authless", "bot", "interaction"), 0.0
            )
            for send_times in self._global_send_times.values():
                send_times.clear()
            self._interaction_callbacks.clear()

    async def start(self) -> None:
        async with self._rest_lifecycle_lock:
            if not self._closed:
                if self._accepting_requests:
                    return
                await self._finish_close(tuple(self._inflight_requests))
            self._closed = False
            self._accepting_requests = True
            self._rate_limit_interrupt.clear()
            self._unauthorized = False

    def _ensure_available(self, request: DiscordRequest) -> None:
        if self._closed or not self._accepting_requests:
            msg = "Discord REST client is unavailable"
            raise RuntimeError(msg)
        if self._unauthorized and request.auth:
            msg = "Discord REST client stopped after an authentication failure"
            raise RuntimeError(msg)

    def _rate_bucket(
        self,
        request: DiscordRequest,
    ) -> tuple[tuple[str, str], str, _DiscordRateBucket]:
        now = get_running_loop().time()
        if len(self._rate_buckets) >= _RATE_BUCKET_PRUNE_THRESHOLD:
            stale = next(
                (
                    key
                    for key, bucket in self._rate_buckets.items()
                    if not bucket.lock.locked() and bucket.ready_at <= now
                ),
                None,
            )
            if stale is not None:
                self._rate_buckets.pop(stale)
        route, major = self._rate_route(request)
        bucket_id = self._route_buckets.get(route)
        key = (
            "bucket" if bucket_id is not None else "route",
            bucket_id
            if bucket_id is not None
            else f"{route[0]} /{route[1].split('/', 2)[1]}",
            major,
        )
        bucket = self._rate_buckets[key]
        return route, major, bucket

    @staticmethod
    def _rate_route(
        request: DiscordRequest,
    ) -> tuple[tuple[str, str], str]:
        parts = unquote(request.path).split("/")[1:]
        normalized = parts.copy()
        major = ""
        if len(parts) > 1 and parts[0] in {"channels", "guilds"}:
            major = f"{parts[0]}:{parts[1]}"
            normalized[1] = ":id"
        elif len(parts) > 1 and parts[0] in {"interactions", "webhooks"}:
            major = f"webhooks:{parts[1]}"
            normalized[1] = ":id"
            token_index = 2
            if len(parts) > token_index:
                major = f"{major}:{parts[token_index]}"
                normalized[token_index] = ":token"
        for index, part in enumerate(normalized):
            if part.isdecimal():
                normalized[index] = ":id"
        return (request.method, f"/{'/'.join(normalized)}"), major

    def _bind_rate_bucket(
        self,
        response: AsyncHTTPResponse,
        *,
        route: tuple[str, str],
        major: str,
        bucket: _DiscordRateBucket,
    ) -> _DiscordRateBucket:
        bucket_id = header_value(response.headers, "X-RateLimit-Bucket")
        if not bucket_id:
            return bucket
        self._route_buckets[route] = bucket_id
        if len(self._route_buckets) > _RATE_BUCKET_PRUNE_THRESHOLD:
            self._route_buckets.pop(next(iter(self._route_buckets)))
        key = ("bucket", bucket_id, major)
        bound = self._rate_buckets.get(key)
        if bound is None:
            self._rate_buckets[key] = bound = _DiscordRateBucket()
        bound.ready_at = max(bound.ready_at, bucket.ready_at)
        return bound

    async def _wait_for_rate_limit(
        self,
        bucket: _DiscordRateBucket,
        *,
        lane: Literal["authless", "bot", "interaction"],
    ) -> None:
        while True:
            ready_at = max(bucket.ready_at, self._global_ready_at[lane])
            delay = ready_at - get_running_loop().time()
            if delay <= 0:
                return
            with suppress(TimeoutError):
                async with timeout(delay):
                    await self._rate_limit_interrupt.wait()
                    return

    async def _wait_for_global_limit(
        self,
        lane: Literal["authless", "bot", "interaction"],
    ) -> None:
        if lane == "interaction":
            return
        send_times = self._global_send_times[lane]
        while True:
            async with self._global_send_lock:
                now = get_running_loop().time()
                while send_times and now - send_times[0] >= _GLOBAL_REST_WINDOW_SECONDS:
                    send_times.popleft()
                delay = self._global_ready_at[lane] - now
                if len(send_times) >= _MAX_GLOBAL_REST_REQUESTS:
                    delay = max(
                        delay,
                        _GLOBAL_REST_WINDOW_SECONDS - (now - send_times[0]),
                    )
                if delay <= 0:
                    send_times.append(now)
                    return
            with suppress(TimeoutError):
                async with timeout(delay):
                    await self._rate_limit_interrupt.wait()
                    return

    async def _request(
        self,
        request: DiscordRequest,
    ) -> tuple[AsyncHTTPResponse, bytes]:
        url = self._url(request)
        headers = {"User-Agent": _USER_AGENT}
        if request.auth:
            headers["Authorization"] = f"Bot {self.token.get_secret_value()}"
        if request.reason is not None:
            headers["X-Audit-Log-Reason"] = quote(request.reason, safe="")
        if request.files is not None:
            fields: list[tuple[str, MultipartFieldValue]] = []
            if request.multipart == "form_fields" and isinstance(request.json_, dict):
                fields.extend(
                    (
                        name,
                        str(value).lower()
                        if isinstance(value, bool)
                        else ""
                        if value is None
                        else str(value),
                    )
                    for name, value in request.json_.items()
                )
            elif request.json_ is not None:
                fields.append(("payload_json", dumpb(request.json_)))
            fields.extend(
                (
                    file.field or f"files[{index}]",
                    (file.filename, file.data, file.content_type),
                )
                for index, file in enumerate(request.files)
            )
            body, content_type = encode_multipart_formdata(fields)
            headers["Content-Type"] = content_type
            json = None
        elif request.json_ is not None:
            headers["Content-Type"] = "application/json"
            body = None
            json = request.json_
        else:
            body = None
            json = None
        response = await self.http_pool.request(
            request.method,
            url,
            headers=headers,
            body=body,
            json=json,
            preload_content=False,
            redirect=False,
            retries=False,
            timeout=_API_TIMEOUT,
        )
        return response, await read_http_body(response)

    def _url(self, request: DiscordRequest) -> str:
        url = f"{self.base_url}{request.path}"
        if not request.query:
            return url
        pairs: list[tuple[str, str]] = []
        for name, raw in request.query.items():
            values = raw if isinstance(raw, list) else [raw]
            pairs.extend(
                (
                    name,
                    str(value).lower() if isinstance(value, bool) else str(value),
                )
                for value in values
            )
        return f"{url}?{urlencode(pairs)}"

    def _record_rate_limit(
        self,
        response: AsyncHTTPResponse,
        payload: JsonValue,
        *,
        retry_after: float | None,
        bucket: _DiscordRateBucket,
        bound_bucket: _DiscordRateBucket,
        lane: Literal["authless", "bot", "interaction"],
    ) -> None:
        if response.status == HTTPStatus.TOO_MANY_REQUESTS:
            if retry_after is None:
                return
            ready_at = get_running_loop().time() + retry_after
            if self._is_global_rate_limit(response, payload):
                self._global_ready_at[lane] = max(
                    self._global_ready_at[lane],
                    ready_at,
                )
                return
            bucket.ready_at = max(bucket.ready_at, ready_at)
            bound_bucket.ready_at = max(bound_bucket.ready_at, ready_at)
            return
        if header_value(response.headers, "X-RateLimit-Remaining") != "0":
            return
        reset_after = header_value(response.headers, "X-RateLimit-Reset-After")
        if reset_after is None:
            return
        try:
            delay = float(reset_after)
        except ValueError:
            return
        if delay < 0 or not isfinite(delay):
            return
        ready_at = get_running_loop().time() + delay
        bucket.ready_at = max(bucket.ready_at, ready_at)
        bound_bucket.ready_at = max(bound_bucket.ready_at, ready_at)

    @staticmethod
    def _is_global_rate_limit(
        response: AsyncHTTPResponse,
        payload: JsonValue,
    ) -> bool:
        scope = header_value(response.headers, "X-RateLimit-Scope")
        global_header = header_value(response.headers, "X-RateLimit-Global")
        return (
            (scope is not None and scope.lower() == "global")
            or (global_header is not None and global_header.lower() == "true")
            or (isinstance(payload, dict) and payload.get("global") is True)
        )

    @staticmethod
    def _parse_payload(data: bytes, status: int) -> JsonValue:
        try:
            return loads(data) if data else None
        except ValueError as exc:
            if not HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES:
                return None
            msg = "Discord API returned invalid JSON"
            raise RuntimeError(msg) from exc

    @staticmethod
    def _retry_after(
        payload: JsonValue,
        response: AsyncHTTPResponse,
    ) -> float | None:
        value: object = (
            payload.get("retry_after") if isinstance(payload, dict) else None
        )
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or value < 0
            or not isfinite(value)
        ):
            value = header_value(response.headers, "Retry-After")
        if value is None:
            return None
        try:
            delay = float(value)
        except ValueError:
            return None
        return delay if delay >= 0 and isfinite(delay) else None

    @staticmethod
    def _api_error(status: int, payload: JsonValue) -> DiscordAPIError:
        code: int | None = None
        message: str | None = None
        errors: JsonValue = None
        if isinstance(payload, dict):
            raw_code = payload.get("code")
            code = (
                raw_code
                if isinstance(raw_code, int) and not isinstance(raw_code, bool)
                else None
            )
            raw_message = payload.get("message")
            message = raw_message if isinstance(raw_message, str) else None
            errors = cast(JsonValue, payload.get("errors"))
        return DiscordAPIError(status, code=code, message=message, errors=errors)


class DiscordOpcode(IntEnum):
    DISPATCH = 0
    HEARTBEAT = 1
    IDENTIFY = 2
    PRESENCE_UPDATE = 3
    VOICE_STATE_UPDATE = 4
    RESUME = 6
    RECONNECT = 7
    REQUEST_GUILD_MEMBERS = 8
    INVALID_SESSION = 9
    HELLO = 10
    HEARTBEAT_ACK = 11
    REQUEST_SOUNDBOARD_SOUNDS = 31
    REQUEST_CHANNEL_INFO = 43


_IDENTIFY_WINDOW_SECONDS = 5.0
_MAX_PRESENCE_UPDATES = 5
_PRESENCE_WINDOW_SECONDS = 20.0
_FULL_MEMBER_REQUEST_WINDOW_SECONDS = 30.0
_INTERACTION_RESPONSE_WINDOW_SECONDS = 3.0


class DiscordIntent(IntFlag, boundary=STRICT):
    GUILDS = 1 << 0
    GUILD_MEMBERS = 1 << 1
    GUILD_MODERATION = 1 << 2
    GUILD_EXPRESSIONS = 1 << 3
    GUILD_INTEGRATIONS = 1 << 4
    GUILD_WEBHOOKS = 1 << 5
    GUILD_INVITES = 1 << 6
    GUILD_VOICE_STATES = 1 << 7
    GUILD_PRESENCES = 1 << 8
    GUILD_MESSAGES = 1 << 9
    GUILD_MESSAGE_REACTIONS = 1 << 10
    GUILD_MESSAGE_TYPING = 1 << 11
    DIRECT_MESSAGES = 1 << 12
    DIRECT_MESSAGE_REACTIONS = 1 << 13
    DIRECT_MESSAGE_TYPING = 1 << 14
    MESSAGE_CONTENT = 1 << 15
    GUILD_SCHEDULED_EVENTS = 1 << 16
    AUTO_MODERATION_CONFIGURATION = 1 << 20
    AUTO_MODERATION_EXECUTION = 1 << 21
    GUILD_MESSAGE_POLLS = 1 << 24
    DIRECT_MESSAGE_POLLS = 1 << 25


DEFAULT_DISCORD_INTENTS = (
    DiscordIntent.GUILDS | DiscordIntent.GUILD_MESSAGES | DiscordIntent.DIRECT_MESSAGES
)


class DiscordGatewayPayload(Model):
    op: StrictIntLiteral[DiscordOpcode]
    d: JsonValue = Field(None, repr=False)
    s: NonNegativeInt | None = None
    t: StrictStr | None = None

    @model_validator(mode="after")
    def envelope_shape(self) -> Self:
        if self.op is DiscordOpcode.DISPATCH:
            if self.s is None or not self.t:
                msg = "Discord dispatch payload requires sequence and event name"
                raise ValueError(msg)
        elif self.s is not None or self.t is not None:
            msg = "Discord non-dispatch payload cannot have sequence or event name"
            raise ValueError(msg)
        return self


class DiscordHelloData(Model):
    heartbeat_interval: PositiveInt


class DiscordActivity(DiscordRequestModel):
    name: StrictStr
    type: StrictIntLiteral[Literal[0, 1, 2, 3, 4, 5]]
    url: StrictStr | None = None
    state: StrictStr | None = None


class DiscordPresenceUpdate(DiscordRequestModel):
    since: NonNegativeInt | None
    activities: list[DiscordActivity]
    status: Literal["online", "dnd", "idle", "invisible", "offline"]
    afk: StrictBool


class DiscordVoiceStateUpdate(DiscordRequestModel):
    guild_id: Snowflake
    channel_id: Snowflake | None
    self_mute: StrictBool
    self_deaf: StrictBool


def _nonce(value: str) -> str:
    if len(value.encode()) > _MAX_NONCE_BYTES:
        msg = "Discord Gateway nonce must be at most 32 UTF-8 bytes"
        raise ValueError(msg)
    return value


type DiscordNonce = Annotated[StrictStr, AfterValidator(_nonce)]


class DiscordRequestGuildMemberRateLimitMetadata(Model):
    guild_id: Snowflake
    nonce: DiscordNonce | None = None


class DiscordRateLimited(Model):
    opcode: StrictIntLiteral[Literal[8]]
    retry_after: Annotated[StrictFloat, Field(ge=0)]
    meta: DiscordRequestGuildMemberRateLimitMetadata


class DiscordRequestGuildMembers(DiscordRequestModel):
    guild_id: Snowflake
    query: StrictStr | None = None
    limit: Annotated[StrictInt, Field(ge=0, le=100)] | None = None
    presences: StrictBool = False
    user_ids: (
        Snowflake
        | Annotated[list[Snowflake], Field(min_length=1, max_length=100)]
        | None
    ) = None
    nonce: DiscordNonce | None = None

    @model_validator(mode="after")
    def query_or_users(self) -> Self:
        if (self.query is None) == (self.user_ids is None):
            msg = "Discord member request requires exactly one of query or user_ids"
            raise ValueError(msg)
        if self.query is not None and self.limit is None:
            msg = "Discord member query requires limit"
            raise ValueError(msg)
        return self


class DiscordRequestSoundboardSounds(DiscordRequestModel):
    guild_ids: Annotated[list[Snowflake], Field(min_length=1)]


class DiscordRequestChannelInfo(DiscordRequestModel):
    guild_id: Snowflake
    fields: list[Literal["status", "voice_start_time"]]


_GATEWAY_COMMAND_MODELS: dict[DiscordOpcode, type[DiscordRequestModel]] = {
    DiscordOpcode.PRESENCE_UPDATE: DiscordPresenceUpdate,
    DiscordOpcode.VOICE_STATE_UPDATE: DiscordVoiceStateUpdate,
    DiscordOpcode.REQUEST_GUILD_MEMBERS: DiscordRequestGuildMembers,
    DiscordOpcode.REQUEST_SOUNDBOARD_SOUNDS: DiscordRequestSoundboardSounds,
    DiscordOpcode.REQUEST_CHANNEL_INFO: DiscordRequestChannelInfo,
}


class DiscordGatewayCommand(DiscordRequestModel):
    opcode: StrictIntLiteral[Literal[3, 4, 8, 31, 43]]
    data: JsonValue = Field(repr=False)

    def payload(self) -> tuple[dict[str, JsonValue], DiscordRequestModel]:
        data = _GATEWAY_COMMAND_MODELS[DiscordOpcode(self.opcode)].model_validate(
            self.data
        )
        return (
            {
                "op": self.opcode,
                "d": cast(
                    JsonValue,
                    data.model_dump(mode="json", exclude_unset=True),
                ),
            },
            data,
        )


_DISPATCH_MODELS: dict[str, type[BaseModel]] = {
    "READY": DiscordReady,
    "RESUMED": Model,
    "RATE_LIMITED": DiscordRateLimited,
    "MESSAGE_CREATE": DiscordMessage,
    "MESSAGE_UPDATE": DiscordPartialMessage,
    "MESSAGE_DELETE": DiscordMessageDelete,
    "MESSAGE_DELETE_BULK": DiscordMessageDeleteBulk,
    "GUILD_CREATE": RootModel[DiscordGuild | DiscordUnavailableGuild],
    "GUILD_UPDATE": DiscordGuild,
    "GUILD_DELETE": DiscordGuildDelete,
    "CHANNEL_CREATE": DiscordChannel,
    "CHANNEL_UPDATE": DiscordChannel,
    "CHANNEL_DELETE": DiscordChannel,
    "GUILD_MEMBER_ADD": DiscordGuildMemberEvent,
    "GUILD_MEMBER_UPDATE": DiscordGuildMemberEvent,
    "GUILD_MEMBER_REMOVE": DiscordGuildMemberRemove,
    "INTERACTION_CREATE": DiscordInteraction,
}


class DiscordConnection(Connection):
    @override
    def _message_action_params(
        self,
        event: MessageEvent,
        msg: MsgInput,
    ) -> dict[str, ActionParamInput]:
        params = super()._message_action_params(event, msg)
        channel_id = getattr(event, "channel_id", None)
        if isinstance(channel_id, str) and channel_id:
            params["channel_id"] = channel_id
        params["message_id"] = event.message_id
        return params


class DiscordGatewayFatalError(ConnectionError):
    pass


class _ReconnectError(ConnectionError):
    def __init__(
        self,
        message: str,
        *,
        reset_session: bool = False,
        delay: float | None = None,
    ) -> None:
        super().__init__(message)
        self.reset_session = reset_session
        self.delay = delay


class DiscordGuildSummary(Model):
    id: Snowflake
    name: DiscordGuildName
    icon: StrictStr | None
    banner: StrictStr | None = None
    owner: StrictBool | None = None
    permissions: Annotated[StrictStr, Field(pattern=r"^[0-9]+$")] | None = None
    features: list[StrictStr]
    approximate_member_count: NonNegativeInt | None = None
    approximate_presence_count: NonNegativeInt | None = None


DiscordGuildList = RootModel[list[DiscordGuildSummary]]
DiscordChannelList = RootModel[Annotated[list[DiscordChannel], Field(max_length=500)]]
DiscordMemberList = RootModel[list[DiscordGuildMember]]


_SUPPORTED_COMMON_ACTIONS = (
    Action.GET_SUPPORTED_ACTIONS,
    Action.GET_STATUS,
    Action.GET_VERSION,
    Action.SEND_MESSAGE,
    Action.DELETE_MESSAGE,
    Action.GET_SELF_INFO,
    Action.GET_USER_INFO,
    Action.GET_GUILD_INFO,
    Action.GET_GUILD_LIST,
    Action.SET_GUILD_NAME,
    Action.GET_GUILD_MEMBER_INFO,
    Action.GET_GUILD_MEMBER_LIST,
    Action.LEAVE_GUILD,
    Action.GET_CHANNEL_INFO,
    Action.GET_CHANNEL_LIST,
    Action.SET_CHANNEL_NAME,
)


class DiscordGateway(Gateway, DiscordRestClient):
    def __init__(
        self,
        bot: Bot,
        *,
        token: SecretStr | str,
        http_pool: AsyncPoolManager,
        intents: int = DEFAULT_DISCORD_INTENTS,
        shard: tuple[int, int] = (0, 1),
        base_url: str = DISCORD_API_BASE_URL,
        websocket_connector: WebSocketConnector | None = None,
    ) -> None:
        Gateway.__init__(self, bot)
        DiscordRestClient.__init__(
            self,
            token,
            base_url=base_url,
            http_pool=http_pool,
        )
        self.intents = TypeAdapter(StrictIntLiteral[DiscordIntent]).validate_python(
            intents
        )
        # One gateway owns one shard; 2,500+ guild bots need a shard coordinator.
        self.shard = TypeAdapter(DiscordShard).validate_python(shard)
        self._websocket_connector = (
            websocket_connector
            if websocket_connector is not None
            else partial(connect_websocket, max_size=None)
        )
        self._task: Task[None] | None = None
        self._lifecycle_lock = Lock()
        self._gateway_send_lock = Lock()
        self._gateway_send_times: deque[float] = deque()
        self._presence_send_times: deque[float] = deque()
        self._full_member_ready_at: dict[str, float] = {}
        self._identify_ready_at = 0.0
        self._websocket: WebSocketConnection | None = None
        self._closing = False
        self._session_id: str | None = None
        self._seq: int | None = None
        self._initial_gateway_url: str | None = None
        self._resume_gateway_url: str | None = None
        self._online = False
        self._self = BotSelf(platform="discord", user_id="0")
        self._retry_count = 0

    @override
    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._task is not None:
                if not self._task.done():
                    return
                await gather(self._task, return_exceptions=True)
                self._task = None
                self._clear_session()
            await DiscordRestClient.start(self)
            self._closing = False
            self._retry_count = 0
            self._task = create_task(self._run_gateway(), name="discord-gateway")

    @override
    async def close(self) -> None:
        async with self._lifecycle_lock:
            self._closing = True
            finishing = create_task(
                self._finish_gateway_close(),
                name="discord-gateway-close",
            )
            await await_cleanup(finishing)

    async def _finish_gateway_close(self) -> None:
        task = self._task
        if task is not None:
            task.cancel()
            try:
                await gather(task, return_exceptions=True)
            finally:
                if self._task is task:
                    self._task = None
        self._clear_session()
        self._gateway_send_times.clear()
        self._presence_send_times.clear()
        self._full_member_ready_at.clear()
        try:
            await self._close_interaction_callbacks()
        finally:
            try:
                await DiscordRestClient.close(self)
            finally:
                self._closing = True

    @override
    def connection_for(self, self_: BotSelf) -> DiscordConnection:
        return DiscordConnection(self, self_)

    @override
    async def request_action(
        self,
        connection: Connection,
        action: str,
        params: ActionParamModel,
    ) -> BaseModel:
        if self._closing:
            msg = "Discord gateway is closed"
            raise RuntimeError(msg)
        if connection.gateway is not self or connection.self_ != self._self:
            msg = "Discord action connection has the wrong BotSelf"
            raise ValueError(msg)
        data = params.model_dump(mode="python", exclude_none=True)
        if action == "discord.request":
            request = DiscordRequest.model_validate(data)
            return await self._request_discord(request)
        if action == "discord.gateway":
            command = DiscordGatewayCommand.model_validate(data)
            payload, command_data = command.payload()
            self._validate_gateway_command(command_data)
            websocket = self._websocket
            if websocket is None or not self._online:
                msg = "Discord Gateway is not connected"
                raise ConnectionError(msg)
            await self._send_gateway(websocket, payload)
            return DiscordNoContent()
        if action == Action.GET_SUPPORTED_ACTIONS:
            return RootModel[list[StrictStr]]([
                *(item.value for item in _SUPPORTED_COMMON_ACTIONS),
                "discord.request",
                "discord.gateway",
            ])
        if action == Action.GET_STATUS:
            return Status(
                good=self._task is not None and not self._task.done(),
                bots=[BotStatus(self_=self._self, online=self._online)],
            )
        if action == Action.GET_VERSION:
            return Version(
                impl="lst-bot.discord",
                version=version("bot"),
                onebot_version="12",
            )
        if action == Action.SEND_MESSAGE:
            return await self._send_message(data)
        return await self._common_action(action, data)

    def _validate_gateway_command(self, data: DiscordRequestModel) -> None:
        if not isinstance(data, DiscordRequestGuildMembers):
            return
        if (
            data.query == ""  # ruff: ignore[compare-to-empty-string] - None means a user-id request.
            and data.limit == 0
            and DiscordIntent.GUILD_MEMBERS not in self.intents
        ):
            msg = "Discord full member requests require the GUILD_MEMBERS intent"
            raise ValueError(msg)
        if data.presences and DiscordIntent.GUILD_PRESENCES not in self.intents:
            msg = "Discord member presences require the GUILD_PRESENCES intent"
            raise ValueError(msg)

    async def _common_action(  # ruff: ignore[complex-structure, too-many-branches]
        self,
        action: str,
        data: dict[str, object],
    ) -> BaseModel:
        try:
            common = Action(action)
        except ValueError as exc:
            msg = f"unsupported Discord action: {action}"
            raise LookupError(msg) from exc
        if common == Action.GET_SELF_INFO:
            return await self._request_model("GET", "/users/@me", DiscordUser)
        if common == Action.GET_USER_INFO:
            user_id = self._id(data, "user_id")
            return await self._request_model("GET", f"/users/{user_id}", DiscordUser)
        if common == Action.GET_GUILD_INFO:
            guild_id = self._id(data, "guild_id")
            return await self._request_model("GET", f"/guilds/{guild_id}", DiscordGuild)
        if common == Action.GET_GUILD_LIST:
            return await self._guild_list()
        if common == Action.SET_GUILD_NAME:
            guild_id = self._id(data, "guild_id")
            name = _GUILD_NAME_ADAPTER.validate_python(data.get("guild_name"))
            return await self._request_model(
                "PATCH", f"/guilds/{guild_id}", DiscordGuild, json={"name": name}
            )
        if common == Action.GET_GUILD_MEMBER_INFO:
            guild_id = self._id(data, "guild_id")
            user_id = self._id(data, "user_id")
            return await self._request_model(
                "GET", f"/guilds/{guild_id}/members/{user_id}", DiscordGuildMember
            )
        if common == Action.GET_GUILD_MEMBER_LIST:
            guild_id = self._id(data, "guild_id")
            return await self._guild_member_list(guild_id)
        if common == Action.LEAVE_GUILD:
            guild_id = self._id(data, "guild_id")
            return await self._request_model(
                "DELETE", f"/users/@me/guilds/{guild_id}", DiscordNoContent
            )
        if common == Action.GET_CHANNEL_INFO:
            channel_id = self._id(data, "channel_id")
            return await self._request_model(
                "GET", f"/channels/{channel_id}", DiscordChannel
            )
        if common == Action.GET_CHANNEL_LIST:
            guild_id = self._id(data, "guild_id")
            return await self._request_model(
                "GET", f"/guilds/{guild_id}/channels", DiscordChannelList
            )
        if common == Action.SET_CHANNEL_NAME:
            channel_id = self._id(data, "channel_id")
            name = _CHANNEL_NAME_ADAPTER.validate_python(data.get("channel_name"))
            return await self._request_model(
                "PATCH",
                f"/channels/{channel_id}",
                DiscordChannel,
                json={"name": name},
            )
        if common == Action.DELETE_MESSAGE:
            channel_id = self._id(data, "channel_id")
            message_id = self._id(data, "message_id")
            return await self._request_model(
                "DELETE",
                f"/channels/{channel_id}/messages/{message_id}",
                DiscordNoContent,
            )
        msg = f"Discord Gateway does not support common action {common.value}"
        raise LookupError(msg)

    async def _guild_list(self) -> DiscordGuildList:
        guilds: list[DiscordGuildSummary] = []
        after: str | None = None
        while True:
            page = await self._request_model(
                "GET",
                "/users/@me/guilds",
                DiscordGuildList,
                query={
                    "limit": _GUILD_PAGE_SIZE,
                    **({"after": after} if after is not None else {}),
                },
            )
            guilds.extend(page.root)
            if len(page.root) < _GUILD_PAGE_SIZE:
                return DiscordGuildList(guilds)
            next_after = page.root[-1].id
            if after is not None and int(next_after) <= int(after):
                msg = "Discord guild pagination did not advance"
                raise RuntimeError(msg)
            after = next_after

    async def _guild_member_list(self, guild_id: str) -> DiscordMemberList:
        members: list[DiscordGuildMember] = []
        after: str | None = None
        while True:
            page = await self._request_model(
                "GET",
                f"/guilds/{guild_id}/members",
                DiscordMemberList,
                query={
                    "limit": _MEMBER_PAGE_SIZE,
                    **({"after": after} if after is not None else {}),
                },
            )
            members.extend(page.root)
            if len(page.root) < _MEMBER_PAGE_SIZE:
                return DiscordMemberList(members)
            next_after = page.root[-1].user.id
            if after is not None and int(next_after) <= int(after):
                msg = "Discord guild member pagination did not advance"
                raise RuntimeError(msg)
            after = next_after

    async def _request_model[T: BaseModel](
        self,
        method: str,
        path: str,
        model: type[T],
        *,
        query: Mapping[str, object] | None = None,
        json: JsonValue = None,
    ) -> T:
        response = await self.request_discord(
            method,
            path,
            query=query,
            json=json,
        )
        if model is DiscordNoContent:
            if not isinstance(response, DiscordNoContent):
                msg = f"Discord API returned content for {method} request"
                raise RuntimeError(msg)
            return model()
        if isinstance(response, DiscordNoContent):
            msg = f"Discord API returned no content for {method} request"
            raise RuntimeError(msg)  # ruff: ignore[type-check-without-type-error] - malformed upstream response.
        try:
            return model.model_validate(response.root)
        except ValidationError as exc:
            msg = f"Discord API returned an invalid response for {method} request"
            raise RuntimeError(msg) from exc

    @staticmethod
    def _id(data: Mapping[str, object], name: str) -> str:
        return _SNOWFLAKE_ADAPTER.validate_python(data.get(name))

    async def _connect_gateway(self, url: str) -> WebSocketConnection:
        try:
            return await self._websocket_connector(url, None)
        except OSError, InvalidHandshake:
            fallback_url = self._initial_gateway_url
            if fallback_url is None or fallback_url == url:
                raise
            return await self._websocket_connector(fallback_url, None)

    async def _run_gateway(self) -> None:
        await self.bot.wait_until_running()
        while not self._closing:
            delay = _RECONNECT_DELAYS[
                min(self._retry_count, len(_RECONNECT_DELAYS) - 1)
            ]
            try:
                websocket = await self._connect_gateway(await self._gateway_url())
                await self._serve_websocket(websocket)
                return  # ruff: ignore[try-consider-else] - keep success path local.
            except DiscordGatewayFatalError:
                self._clear_session()
                logger.exception("Discord Gateway stopped")
                return
            except DiscordAPIError as exc:
                if exc.status == HTTPStatus.UNAUTHORIZED:
                    self._clear_session()
                    logger.exception("Discord Gateway authentication failed")
                    return
                logger.exception(
                    "Discord Gateway discovery failed; retrying in %ss",
                    delay,
                )
            except _ReconnectError as exc:
                if exc.reset_session:
                    self._clear_session()
                if exc.delay is not None:
                    delay = max(exc.delay, delay if self._retry_count else 0.0)
            except Exception:
                logger.exception(
                    "Discord Gateway connection failed; retrying in %ss",
                    delay,
                )
            self._retry_count += 1
            await sleep(delay)

    async def _gateway_url(self) -> str:
        if (
            self._session_id is not None
            and self._seq is not None
            and self._resume_gateway_url is not None
        ):
            return _gateway_url(self._resume_gateway_url)
        while True:
            response = await self.request_discord("GET", "/gateway/bot")
            if isinstance(response, DiscordNoContent):
                msg = "Discord Gateway discovery returned no content"
                raise RuntimeError(msg)  # ruff: ignore[type-check-without-type-error] - malformed upstream response.
            try:
                gateway = DiscordGatewayBotInfo.model_validate(response.root)
            except ValueError as exc:
                msg = "Discord Gateway discovery returned an invalid response"
                raise RuntimeError(msg) from exc
            limit = gateway.session_start_limit
            if limit.remaining:
                await self._wait_to_identify()
                self._initial_gateway_url = _gateway_url(str(gateway.url))
                return self._initial_gateway_url
            await sleep(limit.reset_after / 1000)

    async def _serve_websocket(self, websocket: WebSocketConnection) -> None:
        async with self._gateway_send_lock:
            self._gateway_send_times.clear()
            self._presence_send_times.clear()
        self._websocket = websocket
        close_code = 1000
        try:
            await self._read_websocket(websocket)
        except _ReconnectError as exc:
            if not exc.reset_session:
                close_code = 4000
            raise
        except StopAsyncIteration:
            msg = "Discord Gateway closed normally"
            raise _ReconnectError(msg, reset_session=True) from None
        except ConnectionError as exc:
            reconnect = self._disconnect_error(exc)
            if isinstance(reconnect, _ReconnectError) and not reconnect.reset_session:
                close_code = 4000
            raise reconnect from exc
        except Exception:
            close_code = 4000
            raise
        finally:
            self._online = False
            if self._websocket is websocket:
                self._websocket = None
            with suppress(Exception):
                await websocket.close(close_code)

    async def _read_websocket(  # ruff: ignore[complex-structure]
        self,
        websocket: WebSocketConnection,
    ) -> None:
        async with timeout(_HELLO_TIMEOUT):
            payload = DiscordGatewayPayload.model_validate_json(
                await websocket.receive_text()
            )
            if payload.op is DiscordOpcode.RECONNECT:
                msg = "Discord Gateway requested reconnect before Hello"
                raise _ReconnectError(msg, delay=0.0)
            if payload.op is not DiscordOpcode.HELLO:
                msg = "Discord Gateway expected Hello or Reconnect"
                raise ValueError(msg)
        hello = DiscordHelloData.model_validate(payload.d)
        interval = hello.heartbeat_interval / 1000
        # Discord requires non-cryptographic heartbeat jitter.
        next_heartbeat = get_running_loop().time() + interval * random()  # ruff: ignore[suspicious-non-cryptographic-random-usage]
        heartbeat_pending = False
        await self._authenticate_websocket(websocket)
        while True:
            remaining = max(0.0, next_heartbeat - get_running_loop().time())
            try:
                async with timeout(remaining):
                    payload = DiscordGatewayPayload.model_validate_json(
                        await websocket.receive_text()
                    )
            except TimeoutError:
                if heartbeat_pending:
                    msg = "Discord Gateway heartbeat was not acknowledged"
                    raise ConnectionError(msg) from None
                await self._send_heartbeat(websocket)
                heartbeat_pending = True
                next_heartbeat = get_running_loop().time() + interval
                continue

            if payload.op is DiscordOpcode.HEARTBEAT_ACK:
                heartbeat_pending = False
                continue
            if payload.op is DiscordOpcode.HEARTBEAT:
                await self._send_heartbeat(websocket)
                heartbeat_pending = True
                continue
            if payload.op is DiscordOpcode.RECONNECT:
                msg = "Discord Gateway requested reconnect"
                raise _ReconnectError(msg, delay=0.0)
            if payload.op is DiscordOpcode.INVALID_SESSION:
                if not isinstance(payload.d, bool):
                    msg = "Discord invalid-session payload must be a boolean"
                    raise ValueError(msg)
                msg = "Discord Gateway session is invalid"
                raise _ReconnectError(msg, reset_session=not payload.d)
            if payload.op is DiscordOpcode.DISPATCH:
                await self._receive_dispatch(payload)
                continue
            msg = f"Unexpected Discord Gateway opcode: {payload.op.value}"
            raise ValueError(msg)

    async def _authenticate_websocket(
        self,
        websocket: WebSocketConnection,
    ) -> None:
        token = self.token.get_secret_value()
        if self._session_id is not None and self._seq is not None:
            payload: dict[str, JsonValue] = {
                "op": DiscordOpcode.RESUME,
                "d": {
                    "token": token,
                    "session_id": self._session_id,
                    "seq": self._seq,
                },
            }
        else:
            payload = {
                "op": DiscordOpcode.IDENTIFY,
                "d": {
                    "token": token,
                    "properties": {
                        "os": operating_system,
                        "browser": "lst-bot",
                        "device": "lst-bot",
                    },
                    "large_threshold": 250,
                    "shard": [self.shard[0], self.shard[1]],
                    "intents": int(self.intents),
                },
            }
        await self._send_gateway(websocket, payload, system=True)

    async def _wait_to_identify(self) -> None:
        delay = self._identify_ready_at - get_running_loop().time()
        if delay > 0:
            await sleep(delay)
        self._identify_ready_at = get_running_loop().time() + _IDENTIFY_WINDOW_SECONDS

    async def _send_heartbeat(self, websocket: WebSocketConnection) -> None:
        await self._send_gateway(
            websocket,
            {"op": DiscordOpcode.HEARTBEAT, "d": self._seq},
            system=True,
        )

    async def _send_gateway(  # ruff: ignore[complex-structure, too-many-branches]
        self,
        websocket: WebSocketConnection,
        payload: Mapping[str, JsonValue],
        *,
        system: bool = False,
    ) -> None:
        encoded = dumpb(payload)
        if len(encoded) > _MAX_GATEWAY_PAYLOAD_BYTES:
            msg = "Discord Gateway payload exceeds 4096 bytes"
            raise ValueError(msg)
        opcode = payload.get("op")
        data = payload.get("d")
        full_member_guild: str | None = None
        guild_id = data.get("guild_id") if isinstance(data, Mapping) else None
        if (
            opcode == DiscordOpcode.REQUEST_GUILD_MEMBERS
            and isinstance(data, Mapping)
            and data.get("query") == ""  # ruff: ignore[compare-to-empty-string] - None is not a full-list request.
            and data.get("limit") == 0
            and isinstance(guild_id, str)
        ):
            full_member_guild = guild_id
        while True:
            async with self._gateway_send_lock:
                if not system and websocket is not self._websocket:
                    msg = "Discord Gateway connection changed while sending"
                    raise ConnectionError(msg)
                now = get_running_loop().time()
                while (
                    self._gateway_send_times
                    and now - self._gateway_send_times[0] >= _GATEWAY_WINDOW_SECONDS
                ):
                    self._gateway_send_times.popleft()
                self._full_member_ready_at = {
                    guild_id: ready_at
                    for guild_id, ready_at in self._full_member_ready_at.items()
                    if ready_at > now
                }
                limit = (
                    _MAX_GATEWAY_EVENTS
                    if system
                    else _MAX_GATEWAY_EVENTS - _GATEWAY_SYSTEM_RESERVE
                )
                delay = 0.0
                if len(self._gateway_send_times) >= limit:
                    delay = _GATEWAY_WINDOW_SECONDS - (
                        now - self._gateway_send_times[0]
                    )
                if opcode == DiscordOpcode.PRESENCE_UPDATE:
                    while (
                        self._presence_send_times
                        and now - self._presence_send_times[0]
                        >= _PRESENCE_WINDOW_SECONDS
                    ):
                        self._presence_send_times.popleft()
                    if len(self._presence_send_times) >= _MAX_PRESENCE_UPDATES:
                        delay = max(
                            delay,
                            _PRESENCE_WINDOW_SECONDS
                            - (now - self._presence_send_times[0]),
                        )
                if full_member_guild is not None:
                    delay = max(
                        delay,
                        self._full_member_ready_at.get(full_member_guild, 0.0) - now,
                    )
                if delay <= 0:
                    await websocket.send_text(encoded.decode())
                    sent_at = get_running_loop().time()
                    self._gateway_send_times.append(sent_at)
                    if opcode == DiscordOpcode.PRESENCE_UPDATE:
                        self._presence_send_times.append(sent_at)
                    if full_member_guild is not None:
                        self._full_member_ready_at[full_member_guild] = (
                            sent_at + _FULL_MEMBER_REQUEST_WINDOW_SECONDS
                        )
                    return
            await sleep(delay)

    async def _receive_dispatch(self, payload: DiscordGatewayPayload) -> None:
        received_at = get_running_loop().time()
        sequence = cast(int, payload.s)
        event_type = cast(str, payload.t)
        model = _DISPATCH_MODELS.get(event_type)
        parsed: BaseModel | JsonValue = payload.d
        malformed = False
        if model is not None:
            try:
                parsed = model.model_validate(payload.d)
            except ValidationError as exc:
                if event_type == "READY":
                    msg = "Discord READY payload is invalid"
                    raise _ReconnectError(msg, reset_session=True) from exc
                malformed = True
                logger.warning(
                    "Invalid Discord %s event preserved as raw notice: %s",
                    event_type,
                    exc.errors(include_url=False, include_input=False),
                )
        if (
            event_type == "MESSAGE_CREATE"
            and isinstance(parsed, DiscordMessage)
            and parsed.author.id == self._self.user_id
        ):
            self._seq = sequence
            return
        if isinstance(parsed, DiscordRateLimited):
            ready_at = received_at + parsed.retry_after
            guild_id = parsed.meta.guild_id
            self._full_member_ready_at[guild_id] = max(
                self._full_member_ready_at.get(guild_id, 0.0),
                ready_at,
            )
        if event_type == "INTERACTION_CREATE" and isinstance(
            parsed, DiscordInteraction
        ):
            self._schedule_interaction_callback(parsed, received_at)
        try:
            event = (
                self._raw_event(event_type, sequence, payload.d, raw=True)
                if malformed
                else self._event_from_dispatch(
                    event_type,
                    sequence,
                    parsed,
                    payload.d,
                )
            )
        except ValidationError as exc:
            if event_type == "READY":
                msg = "Discord READY event is invalid"
                raise _ReconnectError(msg, reset_session=True) from exc
            event = self._raw_event(event_type, sequence, payload.d, raw=True)
            logger.warning(
                "Invalid Discord %s event preserved as raw notice: %s",
                event_type,
                exc.errors(include_url=False, include_input=False),
            )
        try:
            self.enqueue_event(event)
        except QueueFull:
            msg = "Discord Gateway event queue is full"
            raise ConnectionError(msg) from None
        self._seq = sequence

    def _schedule_interaction_callback(
        self,
        interaction: DiscordInteraction,
        received_at: float,
    ) -> None:
        payload = _INTERACTION_AUTO_RESPONSES.get(interaction.type)
        if payload is None:
            return
        path = (
            f"/interactions/{interaction.id}/"
            f"{quote(interaction.token, safe='')}/callback"
        )
        if path in self._interaction_callbacks:
            return
        deadline = received_at + _INTERACTION_RESPONSE_WINDOW_SECONDS
        pending = _DiscordInteractionCallback(payload, deadline)
        self._interaction_callbacks[path] = pending
        pending.task = create_task(
            self._run_interaction_callback(path, pending),
            name=f"discord-interaction-{interaction.id}",
        )

    async def _run_interaction_callback(
        self,
        path: str,
        pending: _DiscordInteractionCallback,
    ) -> None:
        try:
            await self._execute_interaction_callback(path, pending)
        except CancelledError:
            if pending.outcome is not None and not pending.outcome.done():
                pending.outcome.cancel()
            raise
        except Exception as exc:
            if pending.outcome is not None and not pending.outcome.done():
                pending.outcome.set_exception(exc)
            logger.exception("Discord interaction auto-acknowledgement failed")
        finally:
            if self._interaction_callbacks.get(path) is pending:
                self._interaction_callbacks.pop(path, None)

    @staticmethod
    def _ensure_interaction_deadline(deadline: float) -> None:
        if get_running_loop().time() >= deadline:
            raise TimeoutError

    async def _execute_interaction_callback(
        self,
        path: str,
        pending: _DiscordInteractionCallback,
    ) -> None:
        send_at = (
            pending.deadline
            - _INTERACTION_RESPONSE_WINDOW_SECONDS
            + _INTERACTION_AUTO_ACK_DELAY
        )
        try:
            async with timeout_at(send_at):
                await pending.ready.wait()
        except TimeoutError:
            pass
        pending.claimed = True
        failure: Exception | None = None
        if pending.request is not None:
            try:
                self._ensure_interaction_deadline(send_at)
                async with timeout_at(send_at):
                    response = await self._perform_tracked_request(pending.request)
            except Exception as exc:
                failure = exc
            else:
                if pending.outcome is not None and not pending.outcome.done():
                    pending.outcome.set_result(response)
                return
        fallback = DiscordRequest.model_validate({
            "method": "POST",
            "path": path,
            "json": pending.default_payload,
            "auth": False,
        })
        try:
            self._ensure_interaction_deadline(pending.deadline)
            async with timeout_at(pending.deadline):
                await self._perform_tracked_request(fallback)
        except Exception as exc:
            if pending.outcome is not None and not pending.outcome.done():
                pending.outcome.set_exception(failure or exc)
            raise
        if (
            failure is not None
            and pending.outcome is not None
            and not pending.outcome.done()
        ):
            pending.outcome.set_exception(failure)

    async def _close_interaction_callbacks(self) -> None:
        tasks = tuple(
            pending.task
            for pending in self._interaction_callbacks.values()
            if pending.task is not None
        )
        for task in tasks:
            task.cancel()
        await gather(*tasks, return_exceptions=True)
        self._interaction_callbacks.clear()

    def _event_from_dispatch(
        self,
        event_type: str,
        sequence: int,
        data: BaseModel | JsonValue,
        raw_data: JsonValue,
    ) -> Event:
        if event_type == "READY" and isinstance(data, DiscordReady):
            self._session_id = data.session_id
            self._resume_gateway_url = str(data.resume_gateway_url)
            self._self = BotSelf(platform="discord", user_id=data.user.id)
            self._online = True
            self._retry_count = 0
            return MetaEvent(
                id=f"discord:READY:{sequence}",
                time=time(),
                self_=self._self,
                detail_type="discord.ready",
                sub_type="",
                discord_event_type=event_type,
                discord_data=raw_data,
                discord_raw=False,
            )
        if event_type == "RESUMED" and isinstance(data, Model):
            self._online = True
            self._retry_count = 0
            return MetaEvent(
                id=f"discord:RESUMED:{sequence}",
                time=time(),
                self_=self._self,
                detail_type="discord.resumed",
                sub_type="",
                discord_event_type=event_type,
                discord_data=raw_data,
                discord_raw=False,
            )
        if event_type == "MESSAGE_CREATE" and isinstance(data, DiscordMessage):
            return self._message_event(data, raw_data)
        return self._raw_event(
            event_type,
            sequence,
            raw_data,
            raw=event_type not in _DISPATCH_MODELS,
        )

    def _message_event(
        self,
        message: DiscordMessage,
        raw_data: JsonValue,
    ) -> MessageEvent:
        reference = _discord_reply_reference(message)
        referenced = message.referenced_message if reference is not None else None
        reply_text = None
        if referenced is not None:
            reply_text = referenced.content
        elif reference is not None and reference.message_id is not None:
            reply_text = ""
        fields = {
            "id": f"discord:MESSAGE_CREATE:{message.id}",
            "time": message.timestamp.timestamp(),
            "self": self._self,
            "sub_type": "",
            "user_id": message.author.id,
            "message_id": message.id,
            "message": _discord_message(message),
            "alt_message": message.content,
            **({"reply_alt_message": reply_text} if reply_text is not None else {}),
            "discord_event_type": "MESSAGE_CREATE",
            "discord_data": raw_data,
            "discord_raw": False,
        }
        if message.guild_id is None:
            return PrivateMessageEvent.model_validate({
                **fields,
                "channel_id": message.channel_id,
            })
        return ChannelMessageEvent.model_validate({
            **fields,
            "guild_id": message.guild_id,
            "channel_id": message.channel_id,
        })

    def _raw_event(
        self,
        event_type: str,
        sequence: int,
        data: JsonValue,
        *,
        raw: bool,
    ) -> NoticeEvent:
        return NoticeEvent(
            id=f"discord:{event_type}:{sequence}",
            time=time(),
            self_=self._self,
            detail_type=f"discord.{event_type.lower()}",
            sub_type="",
            discord_event_type=event_type,
            discord_data=data,
            discord_raw=raw,
        )

    def _clear_session(self) -> None:
        self._session_id = None
        self._seq = None
        self._resume_gateway_url = None
        self._online = False

    def _disconnect_error(self, exc: ConnectionError) -> ConnectionError:
        code = exc.code if isinstance(exc, WebSocketClosedError) else None
        if code in _FATAL_CLOSE_CODES:
            return DiscordGatewayFatalError(
                f"Discord Gateway closed with fatal code {code}"
            )
        if code in _NEW_SESSION_CLOSE_CODES:
            return _ReconnectError(
                f"Discord Gateway session cannot resume (close code {code})",
                reset_session=True,
            )
        if code == _RATE_LIMITED_CLOSE_CODE:
            return _ReconnectError(
                "Discord Gateway rate limited",
                delay=_RECONNECT_DELAYS[-1],
            )
        return _ReconnectError(f"Discord Gateway disconnected (close code {code})")

    async def _send_message(self, params: dict[str, object]) -> DiscordMessage:
        message = Msg.model_validate(params.pop("message"))
        detail_type = TypeAdapter(Literal["private", "channel"]).validate_python(
            params.pop("detail_type")
        )
        body = _discord_send_body(message)
        source_message_id = params.pop("message_id", None)
        if source_message_id is not None:
            source_message_id = _SNOWFLAKE_ADAPTER.validate_python(source_message_id)
            if "message_reference" not in body:
                body["message_reference"] = {
                    "message_id": source_message_id,
                    "fail_if_not_exists": False,
                }
        if detail_type == "channel":
            channel_id = _SNOWFLAKE_ADAPTER.validate_python(
                params.pop("channel_id", None)
            )
            _SNOWFLAKE_ADAPTER.validate_python(params.pop("guild_id", None))
        else:
            user_id = _SNOWFLAKE_ADAPTER.validate_python(params.pop("user_id", None))
            channel = params.pop("channel_id", None)
            if channel is None:
                channel_id = None
            else:
                channel_id = _SNOWFLAKE_ADAPTER.validate_python(channel)
        if params:
            msg = (
                f"unsupported Discord send-message options: {', '.join(sorted(params))}"
            )
            raise ValueError(msg)
        if detail_type == "private" and channel_id is None:
            dm = await self._request_model(
                "POST",
                "/users/@me/channels",
                DiscordChannel,
                json={"recipient_id": user_id},
            )
            channel_id = dm.id
        return await self._request_model(
            "POST",
            f"/channels/{channel_id}/messages",
            DiscordMessage,
            json=cast(JsonValue, body),
        )


_MENTION_PATTERN = compile_regex(r"<@!?(?P<user>[0-9]{1,20})>|@(?P<all>everyone|here)")
_REPLY_MESSAGE_TYPE = 19
_VOICE_MESSAGE_FLAG = 1 << 13


def _discord_reply_reference(
    message: DiscordMessage,
) -> DiscordMessageReference | None:
    reference = message.message_reference
    return (
        reference
        if message.type == _REPLY_MESSAGE_TYPE
        and reference is not None
        and reference.type in {None, 0}
        else None
    )


def _discord_message(message: DiscordMessage) -> Msg:  # ruff: ignore[complex-structure, too-many-branches]
    segments: list[dict[str, object]] = []
    reference = _discord_reply_reference(message)
    referenced = message.referenced_message if reference is not None else None
    if referenced is not None:
        segments.append({
            "type": "reply",
            "data": {
                "message_id": referenced.id,
                "user_id": referenced.author.id,
            },
        })
    elif reference is not None and reference.message_id is not None:
        segments.append({
            "type": "reply",
            "data": {"message_id": reference.message_id},
        })
    content_start = len(segments)

    position = 0
    mentioned_users = {user.id for user in message.mentions}
    for match in _MENTION_PATTERN.finditer(message.content):
        if match.start() > position:
            segments.append({
                "type": "text",
                "data": {"text": message.content[position : match.start()]},
            })
        user_id = match.group("user")
        if user_id is not None:
            segments.append(
                {"type": "mention", "data": {"user_id": user_id}}
                if user_id in mentioned_users
                else {"type": "text", "data": {"text": match.group()}}
            )
        elif message.mention_everyone:
            segments.append({"type": "mention_all", "data": {}})
        else:
            segments.append({"type": "text", "data": {"text": match.group()}})
        position = match.end()
    if position < len(message.content):
        segments.append({
            "type": "text",
            "data": {"text": message.content[position:]},
        })

    for attachment in message.attachments:
        content_type = (
            attachment.content_type or guess_type(attachment.filename)[0] or ""
        ).casefold()
        if content_type.startswith("image/"):
            segment_type = "image"
        elif content_type.startswith("audio/"):
            segment_type = (
                "voice" if (message.flags or 0) & _VOICE_MESSAGE_FLAG else "audio"
            )
        elif content_type.startswith("video/"):
            segment_type = "video"
        else:
            segment_type = "file"
        segments.append({
            "type": segment_type,
            "data": {"file_id": attachment.url},
        })
    if len(segments) == content_start:
        segments.append({
            "type": "discord.message",
            "data": {"raw": message.model_dump(mode="json")},
        })
    return Msg.model_validate(segments)


def _discord_send_body(message: Msg) -> dict[str, JsonValue]:  # ruff: ignore[complex-structure]
    content: list[str] = []
    users: list[str] = []
    parse: list[str] = []
    reference: dict[str, JsonValue] | None = None
    for segment in message:
        if isinstance(segment, TextSegment):
            content.append(segment.data.text)
        elif isinstance(segment, MentionSegment):
            user_id = _SNOWFLAKE_ADAPTER.validate_python(segment.data.user_id)
            content.append(f"<@{user_id}>")
            if user_id not in users:
                users.append(user_id)
        elif isinstance(segment, MentionAllSegment):
            content.append("@everyone")
            parse.append("everyone")
        elif isinstance(segment, ReplySegment):
            if reference is not None:
                msg = "Discord sends at most one message reference"
                raise ValueError(msg)
            message_id = _SNOWFLAKE_ADAPTER.validate_python(segment.data.message_id)
            reference = {
                "message_id": message_id,
                "fail_if_not_exists": False,
            }
        else:
            msg = f"Discord common messages do not support segment {segment.type!s}"
            raise TypeError(msg)
    text = "".join(content)
    if not text:
        msg = "Discord messages require content"
        raise ValueError(msg)
    if len(text) > _MAX_MESSAGE_LENGTH:
        msg = "Discord message content exceeds 2000 characters"
        raise ValueError(msg)
    if len(users) > _MAX_ALLOWED_MENTIONS:
        msg = "Discord allowed mentions support at most 100 users"
        raise ValueError(msg)
    return {
        "content": text,
        "allowed_mentions": {
            "parse": parse,
            "users": users,
            "replied_user": False,
        },
        **({"message_reference": reference} if reference is not None else {}),
    }


def _gateway_url(value: str) -> str:
    try:
        parsed = TypeAdapter(DiscordWebsocketUrl).validate_python(value)
    except ValueError as exc:
        msg = "Discord Gateway URL must be an absolute WSS URL"
        raise ValueError(msg) from exc
    if parsed.fragment or parsed.username is not None or parsed.password is not None:
        msg = "Discord Gateway URL cannot contain a fragment or credentials"
        raise ValueError(msg)
    parts = urlsplit(str(parsed))
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.pop("compress", None)
    query.update({"v": "10", "encoding": "json"})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
