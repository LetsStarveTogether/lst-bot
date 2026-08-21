from asyncio import (
    CancelledError,
    Lock,
    QueueFull,
    Task,
    create_task,
    get_running_loop,
    sleep,
    timeout,
)
from collections import deque
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime
from enum import STRICT, IntEnum, IntFlag
from html import escape
from importlib.metadata import version
from logging import getLogger
from time import time
from typing import Annotated, Literal, cast, override

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    Field,
    JsonValue,
    RootModel,
    SecretStr,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    model_validator,
)
from urllib3_future import AsyncPoolManager

from bot.core import Bot
from bot.protocol.actions import ActionParamInput, ActionParamModel
from bot.protocol.base import Model, StrictIntLiteral
from bot.protocol.common import BotSelf, BotStatus, Status, Version
from bot.protocol.enums import Action, MsgSegmentType
from bot.protocol.events import (
    ChannelMessageEvent,
    Event,
    FriendDecreaseNoticeEvent,
    FriendIncreaseNoticeEvent,
    GroupMessageEvent,
    GroupRequestEvent,
    MessageEvent,
    MetaEvent,
    NoticeEvent,
    PrivateMessageEvent,
)
from bot.protocol.msg import Msg, MsgInput
from bot.protocol.returns import ReturnAction

from . import qq_api
from .base import (
    Connection,
    Gateway,
    WebSocketClosedError,
    WebSocketConnection,
    WebSocketConnector,
    await_cleanup,
    connect_websocket,
)
from .qq_api import (
    QQ_API_BASE_URL,
    QQID,
    QQAction,
    QQGatewayInfo,
    QQRestClient,
    QQReviewQA,
    QQVerifyInfo,
)

logger = getLogger(__name__)

_HELLO_TIMEOUT = 30.0
_RECONNECT_DELAYS = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
_AUTHENTICATION_FAILED = 4004
_RATE_LIMITED = 4008
_RESUMABLE_SESSION_TIMEOUT = 4009
_APPLICATION_CLOSE_CODES = range(4000, 5000)
_NON_RETRYABLE_ACCESS_TOKEN_CODES = {"10004", "100007", "100016"}
_MESSAGE_SEQUENCE_MODULUS = 1 << 16
_QUOTED_MESSAGE_TYPE = 103

type NonNegativeInt = Annotated[StrictInt, Field(ge=0)]
type PositiveInt = Annotated[StrictInt, Field(gt=0)]
type QQMessageDetailType = Literal["private", "group", "channel"]
type QQMessageTarget = Literal["c2c", "group", "channel", "dm"]

_MESSAGE_TARGET_ADAPTER = TypeAdapter(
    tuple[QQMessageDetailType, QQMessageTarget | None]
)
_DEFAULT_MESSAGE_TARGET: dict[QQMessageDetailType, QQMessageTarget] = {
    "private": "c2c",
    "group": "group",
    "channel": "channel",
}
_MESSAGE_TARGET_DETAIL: dict[QQMessageTarget, QQMessageDetailType] = {
    "c2c": "private",
    "group": "group",
    "channel": "channel",
    "dm": "private",
}
_MEDIA_FILE_TYPES: dict[str, int] = {
    MsgSegmentType.IMAGE: 1,
    MsgSegmentType.VIDEO: 2,
    MsgSegmentType.VOICE: 3,
    MsgSegmentType.AUDIO: 3,
    MsgSegmentType.FILE: 4,
}
_MEDIA_UPLOADS = {
    "c2c": (QQAction.UPLOAD_C2C_FILE, "user_openid", "user_id"),
    "group": (QQAction.UPLOAD_GROUP_FILE, "group_openid", "group_id"),
}
_SEND_ACTIONS = {
    Action.SEND_MESSAGE,
    QQAction.SEND_C2C_MESSAGE,
    QQAction.SEND_C2C_STREAM_MESSAGE,
    QQAction.SEND_GROUP_MESSAGE,
    QQAction.SEND_CHANNEL_MESSAGE,
    QQAction.SEND_DM_MESSAGE,
}
_UPLOAD_ACTIONS = {upload[0] for upload in _MEDIA_UPLOADS.values()}


def _valid_shard(value: tuple[int, int]) -> tuple[int, int]:
    if value[0] >= value[1]:
        msg = "QQ shard id must be smaller than shard count"
        raise ValueError(msg)
    return value


type Shard = Annotated[
    tuple[NonNegativeInt, PositiveInt],
    AfterValidator(_valid_shard),
]


class QQOpcode(IntEnum):
    DISPATCH = 0
    HEARTBEAT = 1
    IDENTIFY = 2
    RESUME = 6
    RECONNECT = 7
    INVALID_SESSION = 9
    HELLO = 10
    HEARTBEAT_ACK = 11


class QQIntent(IntFlag, boundary=STRICT):
    GUILDS = 1 << 0
    GUILD_MEMBERS = 1 << 1
    GUILD_MESSAGES = 1 << 9
    GUILD_MESSAGE_REACTIONS = 1 << 10
    DIRECT_MESSAGES = 1 << 12
    OPEN_FORUM = 1 << 18
    AUDIO_OR_LIVE_CHANNEL_MEMBER = 1 << 19
    ENTER_AIO = 1 << 23
    GROUP_MEMBERS = 1 << 24
    GROUP_AND_C2C = 1 << 25
    INTERACTIONS = 1 << 26
    MESSAGE_AUDIT = 1 << 27
    FORUMS = 1 << 28
    AUDIO = 1 << 29
    PUBLIC_GUILD_MESSAGES = 1 << 30


_COMMON_ACTION_MAP = {
    Action.GET_SELF_INFO: QQAction.GET_BOT,
    Action.GET_GROUP_INFO: QQAction.GET_GROUP_INFO,
    Action.GET_GUILD_INFO: QQAction.GET_GUILD,
    Action.GET_GUILD_LIST: QQAction.LIST_BOT_GUILDS,
    Action.GET_GUILD_MEMBER_INFO: QQAction.GET_GUILD_MEMBER,
    Action.GET_GUILD_MEMBER_LIST: QQAction.LIST_GUILD_MEMBERS,
    Action.GET_CHANNEL_INFO: QQAction.GET_CHANNEL,
    Action.GET_CHANNEL_LIST: QQAction.LIST_GUILD_CHANNELS,
    Action.SET_CHANNEL_NAME: QQAction.UPDATE_CHANNEL,
}
_SUPPORTED_COMMON_ACTIONS = (
    Action.GET_SUPPORTED_ACTIONS,
    Action.GET_STATUS,
    Action.GET_VERSION,
    Action.SEND_MESSAGE,
    *_COMMON_ACTION_MAP,
)


class QQUser(qq_api.QQUser):
    member_role: Literal["member", "admin", "owner"] | None = None


class QQMessageScene(Model):
    source: StrictStr | None = None
    ext: list[StrictStr] = Field(default_factory=list)


class QQAttachment(Model):
    url: StrictStr
    content_type: StrictStr
    filename: StrictStr | None = None
    width: NonNegativeInt | None = None
    height: NonNegativeInt | None = None
    size: NonNegativeInt | None = None
    voice_wav_url: StrictStr | None = None
    asr_refer_text: StrictStr | None = None


class QQArkData(Model):
    prompt: StrictStr | None = None
    ark_type: StrictStr | None = None
    ark_name: StrictStr | None = None
    fields: dict[StrictStr, JsonValue] | None = None


class QQMessageElement(Model):
    msg_idx: StrictStr | None = None
    author: QQUser | None = None
    message_type: StrictIntLiteral[Literal[0, 3, 101, 102, 103]] | None = None
    content: StrictStr | None = None
    attachments: list[QQAttachment] = Field(default_factory=list)
    ark_data: QQArkData | None = None
    msg_elements: list[QQMessageElement] = Field(default_factory=list)


class QQC2CMessage(Model):
    id: StrictStr
    author: QQUser
    content: StrictStr
    timestamp: AwareDatetime
    message_type: StrictIntLiteral[Literal[0, 3, 101, 102, 103]] | None = None
    message_scene: QQMessageScene | None = None
    attachments: list[QQAttachment] = Field(default_factory=list)
    ark_data: QQArkData | None = None
    msg_elements: list[QQMessageElement] = Field(default_factory=list)


class QQGroupMessage(QQC2CMessage):
    group_openid: StrictStr
    mentions: list[QQUser] = Field(default_factory=list)


class QQLegacyChannelMessage(Model):
    id: StrictStr
    channel_id: StrictStr
    guild_id: StrictStr
    content: StrictStr
    timestamp: AwareDatetime
    author: QQUser
    attachments: list[QQAttachment] = Field(default_factory=list)
    mentions: list[QQUser] = Field(default_factory=list)


class QQC2CStatus(Model):
    timestamp: NonNegativeInt
    openid: StrictStr


class QQGroupStatus(Model):
    timestamp: NonNegativeInt
    group_openid: StrictStr
    op_member_openid: StrictStr


class QQFriendAdd(QQC2CStatus):
    scene: StrictInt | None = None
    scene_param: StrictStr | None = None
    author: QQUser | None = None
    short_code: StrictStr | None = None


class QQFriendDelete(QQC2CStatus):
    author: QQUser | None = None


class QQGroupMember(Model):
    timestamp: NonNegativeInt
    group_openid: StrictStr
    member_openid: StrictStr
    user_openid: StrictStr


class QQSubscribeResult(Model):
    template_id: StrictInt
    custom_template_id: StrictStr
    op: StrictIntLiteral[Literal[1, 2]]
    subscribe_id: StrictStr
    subscribe_ts: NonNegativeInt
    update_ts: NonNegativeInt


class QQSubscribeMessageStatus(Model):
    group_openid: StrictStr | None = None
    openid: StrictStr | None = None
    result: list[QQSubscribeResult]


class QQJoinVerification(QQVerifyInfo):
    review_qa_list: list[QQReviewQA] = Field(default_factory=list)


class QQGroupJoinRequest(qq_api.QQJoinRequest):
    group_openid: QQID
    verify_info: QQJoinVerification | None = None


class QQGuildEvent(qq_api.QQGuild):
    op_user_id: StrictStr | None = None


class QQChannelEvent(qq_api.QQChannel):
    op_user_id: StrictStr | None = None


class QQAuthorizeData(Model):
    opt_scene: Literal["setting", "dialog"]
    scope: Literal["c2c_push", "group_push"]


class QQInteractionMessageScene(Model):
    ext: list[StrictStr] = Field(default_factory=list)


class QQInteractionResolved(Model):
    button_data: StrictStr | None = None
    button_id: StrictStr | None = None
    user_id: StrictStr | None = None
    feature_id: StrictStr | None = None
    message_id: StrictStr | None = None
    feedback_opt: Literal["LIKE", "UNLIKE"] | None = None
    checked: StrictInt | None = None
    action: StrictStr | None = None
    message_scene: QQInteractionMessageScene | None = None
    authorize_data: QQAuthorizeData | None = None


class QQInteractionData(Model):
    type: StrictInt | None = None
    resolved: QQInteractionResolved


class QQInteraction(Model):
    id: StrictStr
    type: StrictIntLiteral[Literal[11, 12, 13, 14, 15, 16, 18, 19, 20]]
    scene: Literal["c2c", "group", "guild"]
    chat_type: StrictIntLiteral[Literal[0, 1, 2]] | None = None
    timestamp: AwareDatetime
    guild_id: StrictStr | None = None
    channel_id: StrictStr | None = None
    user_openid: StrictStr | None = None
    group_openid: StrictStr | None = None
    group_member_openid: StrictStr | None = None
    data: QQInteractionData
    version: StrictInt
    application_id: StrictStr


type QQEventData = (
    QQC2CMessage
    | QQGroupMessage
    | QQLegacyChannelMessage
    | QQC2CStatus
    | QQGroupStatus
    | QQFriendAdd
    | QQFriendDelete
    | QQGroupMember
    | QQSubscribeMessageStatus
    | QQGroupJoinRequest
    | QQGuildEvent
    | QQChannelEvent
    | QQInteraction
    | JsonValue
)

_EVENT_DATA_MODELS: dict[str, type[Model]] = {
    "INTERACTION_CREATE": QQInteraction,
    "SUBSCRIBE_MESSAGE_STATUS": QQSubscribeMessageStatus,
    "FRIEND_ADD": QQFriendAdd,
    "FRIEND_DEL": QQFriendDelete,
    "C2C_MESSAGE_CREATE": QQC2CMessage,
    "C2C_MSG_RECEIVE": QQC2CStatus,
    "C2C_MSG_REJECT": QQC2CStatus,
    "GROUP_ADD_ROBOT": QQGroupStatus,
    "GROUP_DEL_ROBOT": QQGroupStatus,
    "GROUP_JOIN_REQUEST": QQGroupJoinRequest,
    "GROUP_MSG_RECEIVE": QQGroupStatus,
    "GROUP_MSG_REJECT": QQGroupStatus,
    "GROUP_AT_MESSAGE_CREATE": QQGroupMessage,
    "GROUP_MESSAGE_CREATE": QQGroupMessage,
    "GROUP_MEMBER_ADD": QQGroupMember,
    "GROUP_MEMBER_REMOVE": QQGroupMember,
    "GUILD_CREATE": QQGuildEvent,
    "GUILD_UPDATE": QQGuildEvent,
    "GUILD_DELETE": QQGuildEvent,
    "CHANNEL_CREATE": QQChannelEvent,
    "CHANNEL_UPDATE": QQChannelEvent,
    "CHANNEL_DELETE": QQChannelEvent,
    "AT_MESSAGE_CREATE": QQLegacyChannelMessage,
    "MESSAGE_CREATE": QQLegacyChannelMessage,
    "DIRECT_MESSAGE_CREATE": QQLegacyChannelMessage,
}


class QQGatewayPayload(Model):
    id: StrictStr | None = None
    op: StrictIntLiteral[QQOpcode]
    d: JsonValue = None
    s: NonNegativeInt | None = None
    t: StrictStr | None = None


class QQDispatch(Model):
    id: StrictStr | None = None
    op: StrictIntLiteral[Literal[QQOpcode.DISPATCH]] = QQOpcode.DISPATCH
    d: QQEventData
    s: NonNegativeInt
    t: StrictStr

    @model_validator(mode="before")
    @classmethod
    def parse_known_data(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        event_type = data.get("t")
        model = (
            _EVENT_DATA_MODELS.get(event_type) if isinstance(event_type, str) else None
        )
        if model is not None:
            data["d"] = model.model_validate(data.get("d"))
        return data


class QQHelloData(Model):
    heartbeat_interval: PositiveInt


class QQReadyData(Model):
    version: StrictInt
    session_id: Annotated[StrictStr, Field(min_length=1)]
    user: QQUser
    shard: tuple[NonNegativeInt, NonNegativeInt]


class QQIdentifyData(qq_api.QQRequest):
    token: StrictStr
    intents: NonNegativeInt
    shard: Shard


class QQIdentify(qq_api.QQRequest):
    op: StrictIntLiteral[Literal[QQOpcode.IDENTIFY]] = QQOpcode.IDENTIFY
    d: QQIdentifyData


class QQResumeData(qq_api.QQRequest):
    token: StrictStr
    session_id: Annotated[StrictStr, Field(min_length=1)]
    seq: NonNegativeInt


class QQResume(qq_api.QQRequest):
    op: StrictIntLiteral[Literal[QQOpcode.RESUME]] = QQOpcode.RESUME
    d: QQResumeData


class QQHeartbeat(qq_api.QQRequest):
    op: StrictIntLiteral[Literal[QQOpcode.HEARTBEAT]] = QQOpcode.HEARTBEAT
    d: NonNegativeInt | None


class QQConnection(Connection):
    @staticmethod
    @override
    def _message_action_params(
        event: MessageEvent,
        msg: MsgInput,
    ) -> dict[str, ActionParamInput]:
        params = Connection._message_action_params(  # ruff: ignore[private-member-access] - reuse base helper
            event, msg
        )
        params["msg_id"] = event.message_id
        for key in ("qq_scene", "guild_id", "channel_id"):
            value = getattr(event, key, None)
            if isinstance(value, str) and value:
                params[key] = value
        return params


class QQGatewayFatalError(ConnectionError):
    pass


class _ReconnectError(ConnectionError):
    def __init__(
        self,
        message: str,
        *,
        reset_session: bool = False,
        reset_token: bool = False,
        delay: float | None = None,
    ) -> None:
        super().__init__(message)
        self.reset_session = reset_session
        self.reset_token = reset_token
        self.delay = delay


class QQGateway(Gateway, QQRestClient):
    def __init__(
        self,
        bot: Bot,
        *,
        app_id: str,
        client_secret: SecretStr | str,
        intents: QQIntent = QQIntent.GROUP_AND_C2C,
        shard: tuple[int, int] = (0, 1),
        base_url: str = QQ_API_BASE_URL,
        http_pool: AsyncPoolManager | None = None,
        websocket_connector: WebSocketConnector | None = None,
    ) -> None:
        Gateway.__init__(self, bot)
        QQRestClient.__init__(
            self,
            app_id,
            client_secret,
            base_url=base_url,
            http_pool=http_pool,
        )
        if isinstance(intents, bool) or not isinstance(intents, int):
            msg = "QQ intents must be an integer flag"
            raise TypeError(msg)
        try:
            self.intents = QQIntent(intents)
        except ValueError:
            msg = "QQ intents contain unknown bits"
            raise ValueError(msg) from None
        self.shard = TypeAdapter(Shard).validate_python(shard)
        self._websocket_connector = (
            connect_websocket if websocket_connector is None else websocket_connector
        )
        self._task: Task[None] | None = None
        self._lifecycle_lock = Lock()
        self._closing = False
        self._session_id: str | None = None
        self._seq: int | None = None
        self._online = False
        self._self = BotSelf(platform="qq", user_id=app_id)
        self._retry_count = 0
        self._message_sequence = 0
        # ponytail: bounded O(n) scan; add a set only if duplicate throughput matters.
        self._recent_messages: deque[tuple[str, ...]] = deque(maxlen=1024)

    @override
    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._closing:
                await self._finish_gateway_close()
            if self._task is not None:
                if not self._task.done():
                    return
                with suppress(CancelledError, Exception):
                    await self._task
                self._task = None
                self._clear_session()
            await QQRestClient.start(self)
            self._closing = False
            self._online = False
            self._retry_count = 0
            self._task = create_task(self._run_gateway(), name="qq-gateway")

    @override
    async def close(self) -> None:
        async with self._lifecycle_lock:
            self._closing = True
            finishing = create_task(
                self._finish_gateway_close(),
                name="qq-gateway-close",
            )
            await await_cleanup(finishing)

    async def _finish_gateway_close(self) -> None:
        task = self._task
        if task is not None:
            task.cancel()
            try:
                with suppress(CancelledError, Exception):
                    await task
            finally:
                if self._task is task:
                    self._task = None
        self._clear_session()
        await QQRestClient.close(self)

    @override
    def connection_for(self, self_: BotSelf) -> QQConnection:
        return QQConnection(self, self_)

    @override
    async def request_action(
        self,
        connection: Connection,
        action: str,
        params: ActionParamModel,
    ) -> BaseModel:
        if self._closing:
            msg = "QQ gateway is closed"
            raise RuntimeError(msg)
        if connection.gateway is not self or connection.self_ != self._self:
            msg = "QQ action connection has the wrong BotSelf"
            raise ValueError(msg)
        data = params.model_dump(mode="python", exclude_none=True)
        if not self._online and (
            action in _SEND_ACTIONS
            or (action in _UPLOAD_ACTIONS and data.get("srv_send_msg") is True)
        ):
            msg = "QQ Gateway is not connected"
            raise ConnectionError(msg)
        if action == Action.SEND_MESSAGE:
            return await self._send_message(data)
        if action == Action.GET_SUPPORTED_ACTIONS:
            return RootModel[list[StrictStr]]([
                *(item.value for item in _SUPPORTED_COMMON_ACTIONS),
                *(item.value for item in QQAction),
            ])
        if action == Action.GET_STATUS:
            return Status(
                good=self._task is not None and not self._task.done(),
                bots=[BotStatus(self_=self._self, online=self._online)],
            )
        if action == Action.GET_VERSION:
            return Version(
                impl="lst-bot.qq",
                version=version("bot"),
                onebot_version="12",
            )
        try:
            common_action = Action(action)
        except ValueError:
            return await self.request_qq(action, **data)
        mapped = _COMMON_ACTION_MAP.get(common_action)
        if mapped is not None:
            return await self.request_qq(
                mapped,
                **_common_action_params(common_action, data),
            )
        msg = f"QQ Gateway does not support common action {common_action.value}"
        raise LookupError(msg)

    @override
    async def execute_return_action(
        self,
        connection: Connection,
        event: Event | None,
        action: ReturnAction,
    ) -> BaseModel:
        if action.kind != "request":
            return await super().execute_return_action(connection, event, action)
        if not isinstance(event, GroupRequestEvent):
            msg = "QQ request responses require a group request event"
            raise TypeError(msg)
        if action.approve is None:
            msg = "QQ request response requires approve"
            raise TypeError(msg)
        if action.remark:
            msg = "QQ group request responses do not support remark"
            raise TypeError(msg)
        if action.approve and action.reason:
            msg = "QQ group request approvals do not support reason"
            raise TypeError(msg)
        return await connection.action(
            QQAction.APPROVE_GROUP_JOIN_REQUEST,
            group_openid=event.group_id,
            member_openid=event.user_id,
            join_request_id=event.flag,
            op="approve" if action.approve else "decline",
            **({"reject_reason": action.reason} if action.reason else {}),
        )

    async def _run_gateway(self) -> None:
        await self.bot.wait_until_running()
        while not self._closing:
            try:
                gateway = cast(
                    QQGatewayInfo,
                    await self.request_qq(QQAction.GET_GATEWAY),
                )
                token = await self.access_token()
                websocket = await self._websocket_connector(str(gateway.url), None)
                await self._serve_websocket(websocket, token)
                return  # ruff: ignore[try-consider-else] - keep success path local
            except _ReconnectError as exc:
                if exc.reset_token:
                    self.invalidate_token()
                if exc.reset_session:
                    self._clear_session()
                delay = (
                    exc.delay
                    if exc.delay is not None
                    else _RECONNECT_DELAYS[
                        min(self._retry_count, len(_RECONNECT_DELAYS) - 1)
                    ]
                )
            except Exception as exc:
                if isinstance(exc, QQGatewayFatalError) or (
                    isinstance(exc, qq_api.QQAccessTokenError)
                    and str(exc.code) in _NON_RETRYABLE_ACCESS_TOKEN_CODES
                ):
                    self._clear_session()
                    logger.exception("QQ Gateway stopped")
                    return
                delay = _RECONNECT_DELAYS[
                    min(self._retry_count, len(_RECONNECT_DELAYS) - 1)
                ]
                logger.exception(
                    "QQ Gateway connection failed; retrying in %ss",
                    delay,
                )
            self._retry_count += 1
            await sleep(delay)

    async def _serve_websocket(
        self,
        websocket: WebSocketConnection,
        token: str,
    ) -> None:
        try:
            await self._read_websocket(websocket, token)
        except _ReconnectError:
            raise
        except StopAsyncIteration:
            msg = "QQ Gateway closed normally"
            raise _ReconnectError(msg) from None
        except ConnectionError as exc:
            raise self._disconnect_error(exc) from exc
        finally:
            self._online = False
            with suppress(Exception):
                await websocket.close()

    async def _read_websocket(
        self,
        websocket: WebSocketConnection,
        token: str,
    ) -> None:
        async with timeout(_HELLO_TIMEOUT):
            hello_text = await websocket.receive_text()
        hello = QQGatewayPayload.model_validate_json(hello_text)
        if hello.op is not QQOpcode.HELLO:
            msg = "QQ Gateway first payload must be Hello"
            raise ValueError(msg)
        hello_data = QQHelloData.model_validate(hello.d)
        await self._authenticate_websocket(websocket, token)

        interval = hello_data.heartbeat_interval / 1000
        next_heartbeat = get_running_loop().time() + interval
        heartbeat_pending = False
        while True:
            remaining = max(0.0, next_heartbeat - get_running_loop().time())
            try:
                async with timeout(remaining):
                    payload = QQGatewayPayload.model_validate_json(
                        await websocket.receive_text()
                    )
            except TimeoutError:
                if heartbeat_pending:
                    msg = "QQ Gateway heartbeat was not acknowledged"
                    raise ConnectionError(msg) from None
                await self._send_heartbeat(websocket)
                heartbeat_pending = True
                next_heartbeat = get_running_loop().time() + interval
                continue

            if payload.op is QQOpcode.HEARTBEAT_ACK:
                heartbeat_pending = False
                self._retry_count = 0
                continue
            if payload.op is QQOpcode.HEARTBEAT:
                await self._send_heartbeat(websocket)
                heartbeat_pending = True
                next_heartbeat = get_running_loop().time() + interval
                continue
            if payload.op is QQOpcode.RECONNECT:
                msg = "QQ Gateway requested reconnect"
                raise _ReconnectError(msg)
            if payload.op is QQOpcode.INVALID_SESSION:
                msg = "QQ Gateway session is invalid"
                resumable = TypeAdapter(StrictBool).validate_python(payload.d)
                raise _ReconnectError(msg, reset_session=not resumable)
            if payload.op is QQOpcode.DISPATCH:
                await self._receive_dispatch(payload)
                continue
            msg = f"Unexpected QQ Gateway opcode: {payload.op.value}"
            raise ValueError(msg)

    async def _authenticate_websocket(
        self,
        websocket: WebSocketConnection,
        token: str,
    ) -> None:
        authorization = f"QQBot {token}"
        if self._session_id is not None and self._seq is not None:
            payload = QQResume(
                d=QQResumeData(
                    token=authorization,
                    session_id=self._session_id,
                    seq=self._seq,
                )
            )
        else:
            payload = QQIdentify(
                d=QQIdentifyData(
                    token=authorization,
                    intents=int(self.intents),
                    shard=self.shard,
                )
            )
        await websocket.send_text(payload.model_dump_json())

    async def _send_heartbeat(self, websocket: WebSocketConnection) -> None:
        await websocket.send_text(QQHeartbeat(d=self._seq).model_dump_json())

    async def _receive_dispatch(  # ruff: ignore[complex-structure, too-many-branches] - one pass preserves sequence and queue ordering
        self, payload: QQGatewayPayload
    ) -> None:
        message_key: tuple[str, ...] | None = None
        try:
            dispatch = QQDispatch.model_validate(payload.model_dump(mode="python"))
        except ValidationError as exc:
            if payload.s is None or payload.t not in _EVENT_DATA_MODELS:
                raise
            event = self._raw_event(payload.t, payload.s, payload.id, payload.d)
            sequence = payload.s
            logger.warning(
                "Invalid QQ %s event preserved as raw notice: %s",
                payload.t,
                exc.errors(include_url=False, include_input=False),
            )
        else:
            sequence = dispatch.s
            if isinstance(dispatch.d, QQC2CMessage):
                message_key = (
                    dispatch.t,
                    dispatch.d.id,
                    *(
                        item
                        for item in (
                            dispatch.d.message_scene.ext
                            if dispatch.d.message_scene is not None
                            else ()
                        )
                        if item.startswith("msg_idx=")
                    ),
                )
                if message_key in self._recent_messages:
                    self._seq = sequence
                    return
            if dispatch.t == "READY":
                ready = QQReadyData.model_validate(dispatch.d)
                if ready.user.id is None:
                    msg = "QQ READY user.id is required"
                    raise ValueError(msg)
                self._session_id = ready.session_id
                self._self = BotSelf(platform="qq", user_id=ready.user.id)
                self._online = True
                event = self._meta_event(dispatch, "qq.ready", ready)
            elif dispatch.t == "RESUMED":
                if not isinstance(dispatch.d, str) or dispatch.d:
                    msg = "QQ RESUMED data must be an empty string"
                    raise ValueError(msg)
                self._online = True
                event = self._meta_event(dispatch, "qq.resumed", dispatch.d)
            else:
                try:
                    event = self._event_from_dispatch(dispatch)
                except ValueError as exc:
                    event = self._raw_event(
                        dispatch.t,
                        dispatch.s,
                        dispatch.id,
                        payload.d,
                    )
                    logger.warning(
                        "Invalid QQ %s event preserved as raw notice: %s",
                        dispatch.t,
                        (
                            exc.errors(include_url=False, include_input=False)
                            if isinstance(exc, ValidationError)
                            else str(exc)
                        ),
                    )
        try:
            self.enqueue_event(event)
        except QueueFull:
            msg = "QQ Gateway event queue is full"
            raise ConnectionError(msg) from None
        if message_key is not None:
            self._recent_messages.append(message_key)
        self._seq = sequence

    def _raw_event(
        self,
        event_type: str,
        sequence: int,
        event_id: str | None,
        data: JsonValue,
    ) -> NoticeEvent:
        return NoticeEvent(
            id=event_id or f"qq:{event_type}:{sequence}",
            time=time(),
            self_=self._self,
            detail_type=f"qq.{event_type.lower()}",
            sub_type="",
            qq_event_type=event_type,
            qq_data=data,
            qq_raw=True,
        )

    def _event_from_dispatch(self, dispatch: QQDispatch) -> Event:
        data = dispatch.d
        message_event = self._message_event_from_dispatch(dispatch)
        if message_event is not None:
            return message_event
        if isinstance(data, QQFriendAdd | QQFriendDelete):
            notice_type = {
                "FRIEND_ADD": FriendIncreaseNoticeEvent,
                "FRIEND_DEL": FriendDecreaseNoticeEvent,
            }[dispatch.t]
            return notice_type.model_validate({
                "id": self._event_id(dispatch),
                "time": self._event_time(data),
                "self": self._self,
                "sub_type": "",
                "user_id": data.openid,
                "qq_event_type": dispatch.t,
                "qq_data": self._event_data_json(data),
                "qq_raw": False,
            })
        if isinstance(data, QQGroupJoinRequest) and data.auto_approved is None:
            verification = data.verify_info
            comment = ""
            if verification is not None:
                comment = verification.verify_message or "\n".join(
                    f"{item.question}: {item.answer}"
                    for item in verification.review_qa_list
                )
            return GroupRequestEvent.model_validate({
                "id": self._event_id(dispatch),
                "time": data.apply_at.timestamp(),
                "self": self._self,
                "sub_type": "add" if data.apply_source == "self_apply" else "invite",
                "user_id": data.member_openid,
                "group_id": data.group_openid,
                "comment": comment,
                "flag": data.join_request_id,
                "qq_event_type": dispatch.t,
                "qq_data": self._event_data_json(data),
                "qq_raw": False,
            })
        return NoticeEvent(
            id=self._event_id(dispatch),
            time=self._event_time(data),
            self_=self._self,
            detail_type=f"qq.{dispatch.t.lower()}",
            sub_type="",
            qq_event_type=dispatch.t,
            qq_data=self._event_data_json(data),
            qq_raw=dispatch.t not in _EVENT_DATA_MODELS,
        )

    def _message_event_from_dispatch(
        self,
        dispatch: QQDispatch,
    ) -> MessageEvent | None:
        data = dispatch.d
        if isinstance(data, QQGroupMessage):
            user_id = data.author.member_openid or data.author.user_openid
            if user_id is None:
                msg = "QQ group message author openid is required"
                raise ValueError(msg)
            return GroupMessageEvent.model_validate({
                **self._message_event_fields(dispatch, data),
                "user_id": user_id,
                "group_id": data.group_openid,
            })
        if isinstance(data, QQC2CMessage):
            user_id = data.author.user_openid or data.author.id
            if user_id is None:
                msg = "QQ C2C message author openid is required"
                raise ValueError(msg)
            return PrivateMessageEvent.model_validate({
                **self._message_event_fields(dispatch, data),
                "user_id": user_id,
                "qq_scene": "c2c",
            })
        if not isinstance(data, QQLegacyChannelMessage):
            return None
        if data.author.id is None:
            msg = "QQ channel message author.id is required"
            raise ValueError(msg)
        fields = {
            **self._message_event_fields(dispatch, data),
            "user_id": data.author.id,
            "guild_id": data.guild_id,
            "channel_id": data.channel_id,
        }
        if dispatch.t == "DIRECT_MESSAGE_CREATE":
            return PrivateMessageEvent.model_validate({
                **fields,
                "qq_scene": "dm",
            })
        return ChannelMessageEvent.model_validate({
            **fields,
            "qq_scene": "channel",
        })

    def _message_event_fields(
        self,
        dispatch: QQDispatch,
        data: QQC2CMessage | QQLegacyChannelMessage,
    ) -> dict[str, object]:
        message = _qq_message(data)
        reply_text = (
            data.msg_elements[0].content
            if isinstance(data, QQC2CMessage)
            and data.message_type == _QUOTED_MESSAGE_TYPE
            and data.msg_elements
            else None
        )
        return {
            "id": self._event_id(dispatch),
            "time": data.timestamp.timestamp(),
            "self_": self._self,
            "sub_type": "",
            "message_id": data.id,
            "message": message,
            "alt_message": data.content,
            **(
                {"reply_alt_message": reply_text}
                if reply_text and reply_text.strip()
                else {}
            ),
            "qq_event_id": dispatch.id,
            "qq_message_type": getattr(data, "message_type", None),
            "qq_message_scene": self._event_data_json(
                getattr(data, "message_scene", None)
            ),
            "qq_data": self._event_data_json(data),
            "qq_raw": False,
        }

    def _meta_event(
        self,
        dispatch: QQDispatch,
        detail_type: str,
        data: BaseModel | JsonValue,
    ) -> MetaEvent:
        return MetaEvent(
            id=self._event_id(dispatch),
            time=time(),
            self_=self._self,
            detail_type=detail_type,
            sub_type="",
            qq_data=self._event_data_json(data),
        )

    @staticmethod
    def _event_id(dispatch: QQDispatch) -> str:
        return dispatch.id or f"qq:{dispatch.t}:{dispatch.s}"

    @staticmethod
    def _event_time(data: object) -> float:
        timestamp = getattr(data, "timestamp", None)
        if isinstance(timestamp, datetime):
            return timestamp.timestamp()
        if isinstance(timestamp, int) and not isinstance(timestamp, bool):
            return float(timestamp)
        apply_at = getattr(data, "apply_at", None)
        return apply_at.timestamp() if isinstance(apply_at, datetime) else time()

    @staticmethod
    def _event_data_json(data: object) -> JsonValue:
        if isinstance(data, BaseModel):
            return cast(JsonValue, data.model_dump(mode="json", exclude_none=True))
        return cast(JsonValue, data)

    def _clear_session(self) -> None:
        self._session_id = None
        self._seq = None
        self._online = False

    def _disconnect_error(self, exc: ConnectionError) -> ConnectionError:
        code = exc.code if isinstance(exc, WebSocketClosedError) else None
        if code in {4001, 4002, 4010, 4011, 4012, 4013, 4014, 4914, 4915}:
            return QQGatewayFatalError(f"QQ Gateway closed with fatal code {code}")
        if code == _AUTHENTICATION_FAILED:
            return _ReconnectError(
                "QQ Gateway authentication failed",
                reset_token=True,
            )
        if code == _RATE_LIMITED:
            return _ReconnectError(
                "QQ Gateway rate limited",
                delay=_RECONNECT_DELAYS[-1],
            )
        if code in _APPLICATION_CLOSE_CODES and code != _RESUMABLE_SESSION_TIMEOUT:
            return _ReconnectError(
                f"QQ Gateway session cannot resume (close code {code})",
                reset_session=True,
            )
        return _ReconnectError(f"QQ Gateway disconnected (close code {code})")

    async def _send_message(self, params: dict[str, object]) -> BaseModel:
        message = params.pop("message")
        if not isinstance(message, Msg):
            message = Msg.model_validate(message)
        detail_type = params.pop("detail_type")
        scene = params.pop("qq_scene", None)
        target = _qq_message_target(detail_type, scene)
        if target not in {"group", "channel"} and any(
            segment.type == MsgSegmentType.MENTION for segment in message
        ):
            msg = "QQ user mentions are only supported in groups and channels"
            raise ValueError(msg)
        if target != "channel" and any(
            segment.type == MsgSegmentType.MENTION_ALL for segment in message
        ):
            msg = "QQ mention-all is only supported in channels"
            raise ValueError(msg)
        body = _qq_send_body(message)
        media = next(
            (segment for segment in message if segment.type in _MEDIA_FILE_TYPES),
            None,
        )
        if (
            media is not None
            and (upload := _MEDIA_UPLOADS.get(target)) is not None
            and (
                file_id := cast(object, media.data).file_id  # ty: ignore[unresolved-attribute]
            )
            .casefold()
            .startswith(("http://", "https://"))
        ):
            upload_action, target_name, source_name = upload
            uploaded = cast(
                qq_api.QQFileInfo,
                await self.request_qq(
                    upload_action,
                    **{target_name: params[source_name]},
                    file_type=_MEDIA_FILE_TYPES[media.type],
                    url=file_id,
                    srv_send_msg=False,
                ),
            )
            body["media"] = {"file_info": uploaded.file_info}
        if (
            target in {"c2c", "group"}
            and "msg_id" in params
            and "msg_seq" not in params
        ):
            self._message_sequence = (
                self._message_sequence + 1
            ) % _MESSAGE_SEQUENCE_MODULUS
            params["msg_seq"] = self._message_sequence
        params.update(body)
        if target == "dm":
            _channel_message_body(params, message)
            params.pop("user_id", None)
            params.pop("channel_id", None)
            return await self.request_qq(QQAction.SEND_DM_MESSAGE, **params)
        if target == "c2c":
            params["user_openid"] = params.pop("user_id")
            params.pop("guild_id", None)
            params.pop("channel_id", None)
            return await self.request_qq(QQAction.SEND_C2C_MESSAGE, **params)
        if target == "group":
            params["group_openid"] = params.pop("group_id")
            return await self.request_qq(QQAction.SEND_GROUP_MESSAGE, **params)
        if target == "channel":
            _channel_message_body(params, message)
            params.pop("guild_id", None)
            return await self.request_qq(QQAction.SEND_CHANNEL_MESSAGE, **params)
        raise AssertionError(target)


def _qq_message_target(detail_type: object, scene: object) -> QQMessageTarget:
    detail_type, requested = _MESSAGE_TARGET_ADAPTER.validate_python((
        detail_type,
        scene,
    ))
    target = requested or _DEFAULT_MESSAGE_TARGET[detail_type]
    if detail_type != _MESSAGE_TARGET_DETAIL[target]:
        msg = f"QQ scene {target!r} does not match detail_type {detail_type!r}"
        raise ValueError(msg)
    return target


def _qq_reply_segments(
    message: QQC2CMessage | QQLegacyChannelMessage | QQMessageElement,
) -> list[dict[str, object]]:
    if not isinstance(message, QQC2CMessage) or (
        message.message_type != _QUOTED_MESSAGE_TYPE
    ):
        return []
    quoted = message.msg_elements[0] if message.msg_elements else None
    prefix = "ref_msg_idx="
    ref_idx = (quoted.msg_idx if quoted is not None else None) or next(
        (
            item.removeprefix(prefix)
            for item in (
                message.message_scene.ext if message.message_scene is not None else ()
            )
            if item.startswith(prefix) and item != prefix
        ),
        None,
    )
    if ref_idx is None:
        return []
    reply_data: dict[str, object] = {"message_id": ref_idx}
    if quoted is not None and quoted.author is not None:
        user_id = (
            quoted.author.member_openid or quoted.author.user_openid or quoted.author.id
        )
        if user_id is not None:
            reply_data["user_id"] = user_id
    return [{"type": "reply", "data": reply_data}]


def _qq_message(  # ruff: ignore[complex-structure] - protocol conversion is intentionally flat
    message: QQC2CMessage | QQLegacyChannelMessage | QQMessageElement,
) -> Msg:
    segments = _qq_reply_segments(message)
    mentions = getattr(message, "mentions", ())
    for mention in mentions:
        user_id = mention.member_openid or mention.user_openid or mention.id
        if user_id is not None:
            segments.append({"type": "mention", "data": {"user_id": user_id}})
    content = message.content or ""
    if content:
        segments.append({"type": "text", "data": {"text": content}})
    for attachment in message.attachments:
        content_type = attachment.content_type.casefold()
        if content_type.startswith("image/"):
            segment_type = "image"
        elif content_type == "voice" or content_type.startswith("audio/"):
            segment_type = "voice"
        elif content_type.startswith("video/"):
            segment_type = "video"
        else:
            segment_type = "file"
        url = attachment.url
        if url.startswith("//"):
            url = f"https:{url}"
        segments.append({"type": segment_type, "data": {"file_id": url}})
    ark_data = getattr(message, "ark_data", None)
    if ark_data is not None:
        segments.append({
            "type": "qq.ark",
            "data": ark_data.model_dump(mode="json", exclude_none=True),
        })
    nested_elements = (
        ()
        if isinstance(message, QQLegacyChannelMessage)
        or message.message_type == _QUOTED_MESSAGE_TYPE
        else message.msg_elements
    )
    for element in nested_elements:
        segments.extend(_qq_message(element).model_dump(mode="python"))
    return Msg.model_validate(segments)


def _qq_send_body(message: Msg) -> dict[str, object]:
    content: list[str] = []
    media: str | None = None
    reply: str | None = None
    for segment in message:
        if segment.type == MsgSegmentType.TEXT:
            content.append(cast(object, segment.data).text)  # ty: ignore[unresolved-attribute]
        elif segment.type == MsgSegmentType.MENTION:
            content.append(
                '<qqbot-at-user id="'
                f"{escape(cast(object, segment.data).user_id, quote=True)}"  # ty: ignore[unresolved-attribute]
                '" />'
            )
        elif segment.type == MsgSegmentType.MENTION_ALL:
            content.append("<qqbot-at-everyone />")
        elif segment.type in _MEDIA_FILE_TYPES:
            if media is not None:
                msg = "QQ sends at most one media resource per message"
                raise ValueError(msg)
            media = cast(object, segment.data).file_id  # ty: ignore[unresolved-attribute]
        elif segment.type == MsgSegmentType.REPLY:
            reply = cast(object, segment.data).message_id  # ty: ignore[unresolved-attribute]
        else:
            msg = f"QQ does not support message segment {segment.type!s}"
            raise ValueError(msg)
    text = "".join(content)
    body: dict[str, object] = {
        "msg_type": 7 if media is not None else 0,
        **({"content": text} if text else {}),
    }
    if media is not None:
        body["media"] = {"file_info": media}
    if reply is not None:
        body["message_reference"] = {"message_id": reply}
    return body


def _channel_message_body(params: dict[str, object], message: Msg) -> None:
    unsupported = {
        MsgSegmentType.VOICE,
        MsgSegmentType.AUDIO,
        MsgSegmentType.VIDEO,
        MsgSegmentType.FILE,
    }
    if any(segment.type in unsupported for segment in message):
        msg = "QQ channel and DM messages only support image media"
        raise ValueError(msg)
    params.pop("msg_type", None)
    media = params.pop("media", None)
    if isinstance(media, Mapping):
        params["image"] = media.get("file_info")


def _common_action_params(
    action: Action,
    params: dict[str, object],
) -> dict[str, object]:
    if action in {Action.GET_SELF_INFO, Action.GET_GUILD_LIST}:
        return {}
    if action == Action.GET_GROUP_INFO:
        params["group_openid"] = params.pop("group_id")
    elif action == Action.GET_CHANNEL_INFO:
        params.pop("guild_id", None)
    elif action == Action.GET_CHANNEL_LIST:
        params.pop("joined_only", None)
    elif action == Action.SET_CHANNEL_NAME:
        params.pop("guild_id", None)
        params["name"] = params.pop("channel_name")
    return params
