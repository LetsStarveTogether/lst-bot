from __future__ import annotations

from asyncio import (
    CancelledError,
    Lock,
    QueueFull,
    Task,
    create_task,
    sleep,
)
from collections.abc import Mapping, Sequence
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from hashlib import sha1
from hmac import compare_digest, new
from http import HTTPMethod, HTTPStatus
from math import isfinite
from typing import Annotated, Any, Literal, Self, cast, override
from urllib.parse import quote, urlsplit, urlunsplit

import orjson
from logbook import Logger
from pydantic import (
    BaseModel,
    Discriminator,
    Field,
    JsonValue,
    RootModel,
    SerializeAsAny,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    Tag,
    ValidationError,
    model_validator,
)
from robyn import Request, Response, Robyn, WebSocketDisconnect
from ulid import ULID
from urllib3_future import AsyncPoolManager
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.http11 import Request as WebSocketRequest
from websockets.http11 import Response as WebSocketResponse

from bot.core import Bot
from bot.protocol.actions import (
    ActionParamModel,
    ActionResponse,
    SendGroupMsgParams,
    SendPrivateMsgParams,
)
from bot.protocol.base import Model
from bot.protocol.common import BotSelf, BotStatus, Status
from bot.protocol.enums import Action, ApiStatus
from bot.protocol.events import (
    Event,
    EventPayload,
    FriendRequestEvent,
    GroupRequestEvent,
    MessageEvent,
    NoticeEvent,
)
from bot.protocol.msg import (
    AudioSegment,
    ExtensionSegment,
    FileSegment,
    ImageSegment,
    LocationSegment,
    MentionAllSegment,
    MentionSegment,
    Msg,
    MsgInput,
    MsgSegment,
    ReplySegment,
    TextSegment,
    VideoSegment,
    VoiceSegment,
)
from bot.protocol.returns import ReturnAction

from .base import (
    AccessToken,
    Connection,
    Gateway,
    HttpAction,
    WebSocketAction,
    WebSocketActionManager,
    WebSocketActionSession,
    WebSocketConnection,
    WebSocketConnector,
    WebsocketsConnection,
    access_token_value,
    bearer_or_query_token,
    connect_websocket,
    empty_response,
    header_value,
    json_response,
    request_target_path,
    text_response,
    token_matches,
)

logger = Logger(__name__)

_INTERNAL_ACTIONS = frozenset(Action)
_ACTION_MAP = {
    Action.DELETE_MESSAGE: "delete_msg",
    Action.GET_SELF_INFO: "get_login_info",
    Action.GET_USER_INFO: "get_stranger_info",
    Action.GET_FRIEND_LIST: "get_friend_list",
    Action.GET_GROUP_INFO: "get_group_info",
    Action.GET_GROUP_LIST: "get_group_list",
    Action.GET_GROUP_MEMBER_INFO: "get_group_member_info",
    Action.GET_GROUP_MEMBER_LIST: "get_group_member_list",
    Action.SET_GROUP_NAME: "set_group_name",
    Action.LEAVE_GROUP: "set_group_leave",
    Action.GET_STATUS: "get_status",
    Action.GET_VERSION: "get_version_info",
}
_OB11_NUMBER_PARAM_KEYS = frozenset({
    "delay",
    "duration",
    "group_id",
    "message_id",
    "self_id",
    "times",
    "user_id",
})
_EVENT_ID_FIELDS = frozenset({
    "group_id",
    "message_id",
    "operator_id",
    "self_id",
    "target_id",
    "user_id",
})
_NOTICE_DETAIL_TYPES = {
    "friend_add": "friend_increase",
    "friend_recall": "private_message_delete",
    "group_decrease": "group_member_decrease",
    "group_increase": "group_member_increase",
    "group_recall": "group_message_delete",
}
_NOTICE_SUB_TYPES = {
    ("group_decrease", "kick_me"): "kick",
    ("group_increase", "approve"): "join",
}
_OB11_MEDIA_SEGMENT_TYPES: Mapping[type[MsgSegment], str] = {
    ImageSegment: "image",
    VideoSegment: "video",
    VoiceSegment: "record",
    AudioSegment: "record",
}

type OneBot11Id = StrictInt | StrictStr
type OneBot11Time = StrictInt | StrictFloat
type WebSocketRole = Literal["api", "event", "universal"]

_ACTION_ROLES = frozenset({"api", "universal"})
_EVENT_ROLES = frozenset({"event", "universal"})
_MAX_PORT = 65535


def _field_value(value: object, key: str) -> object:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value).get(key)
    return getattr(value, key, None)


def _signature_matches(
    secret: str | None,
    body: str | bytes,
    signature: str | None,
) -> bool:
    if secret is None:
        return True
    if signature is None:
        return False
    payload = body.encode() if isinstance(body, str) else body
    expected = "sha1=" + new(secret.encode(), payload, sha1).hexdigest()
    return compare_digest(signature, expected)


def _websocket_role(value: str | None) -> WebSocketRole | None:
    if value is None:
        return None
    role = value.lower()
    return cast(WebSocketRole, role) if role in _ACTION_ROLES | _EVENT_ROLES else None


def _event_self(data: Mapping[str, JsonValue]) -> BotSelf:
    self_id = data.get("self_id")
    if isinstance(self_id, bool) or not isinstance(self_id, int | str):
        msg = "OneBot 11 event self_id must be an integer or string"
        raise TypeError(msg)
    return _qq_self(str(self_id))


def _qq_self(user_id: str) -> BotSelf:
    if not user_id.isdecimal():
        msg = "OneBot 11 self ID must be a decimal integer"
        raise ValueError(msg)
    return BotSelf(platform="qq", user_id=user_id)


def _event_payload_tag(value: object) -> str:
    post_type = _field_value(value, "post_type")
    return post_type if isinstance(post_type, str) else ""


class OneBot11ActionRequest(Model):
    action: StrictStr
    params: SerializeAsAny[BaseModel] = Field(default_factory=Model)
    echo: JsonValue = Field(default=None, exclude_if=lambda value: value is None)


class OneBot11ActionResponse(Model):
    status: Literal["ok", "async", "failed"]
    retcode: StrictInt
    data: JsonValue = None
    message: StrictStr | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    msg: StrictStr | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    echo: JsonValue = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def match_status_and_retcode(self) -> Self:
        if self.status == "ok" and self.retcode != 0:
            msg = "OneBot 11 ok action response must use retcode 0"
            raise ValueError(msg)
        if self.status == "async" and self.retcode != 1:
            msg = "OneBot 11 async action response must use retcode 1"
            raise ValueError(msg)
        if self.status == "failed" and self.retcode in {0, 1}:
            msg = "OneBot 11 failed action response must not use retcode 0 or 1"
            raise ValueError(msg)
        return self


class OneBot11SegmentData(Model):
    pass


class OneBot11MessageSegment(Model):
    type: StrictStr
    data: OneBot11SegmentData = Field(default_factory=OneBot11SegmentData)


class OneBot11Message(RootModel[list[OneBot11MessageSegment]]):
    root: list[OneBot11MessageSegment] = Field(default_factory=list)


class OneBot11QuickOperation(Model):
    reply: OneBot11Message | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    at_sender: StrictBool | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    approve: StrictBool | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    remark: StrictStr | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    reason: StrictStr | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class OneBot11SendPrivateMsgParams(Model):
    user_id: StrictInt
    message: OneBot11Message


class OneBot11SendGroupMsgParams(Model):
    group_id: StrictInt
    message: OneBot11Message


class OneBot11GenericActionParams(Model):
    pass


class OneBot11Event(Model):
    time: OneBot11Time
    self_id: OneBot11Id
    post_type: StrictStr
    sub_type: StrictStr = ""


class OneBot11MessageEvent(OneBot11Event):
    post_type: Literal["message"] = "message"
    message_type: StrictStr
    message_id: OneBot11Id
    user_id: OneBot11Id
    message: JsonValue = ""
    raw_message: StrictStr | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class OneBot11NoticeEvent(OneBot11Event):
    post_type: Literal["notice"] = "notice"
    notice_type: StrictStr


class OneBot11GroupUploadFile(Model):
    id: StrictStr
    name: StrictStr
    size: StrictInt
    busid: StrictInt


class OneBot11GroupUploadNotice(OneBot11NoticeEvent):
    notice_type: Literal["group_upload"] = "group_upload"
    group_id: OneBot11Id
    user_id: OneBot11Id
    file: OneBot11GroupUploadFile


class OneBot11GroupAdminNotice(OneBot11NoticeEvent):
    notice_type: Literal["group_admin"] = "group_admin"
    sub_type: Literal["set", "unset"]
    group_id: OneBot11Id
    user_id: OneBot11Id


class OneBot11GroupBanNotice(OneBot11NoticeEvent):
    notice_type: Literal["group_ban"] = "group_ban"
    sub_type: Literal["ban", "lift_ban"]
    group_id: OneBot11Id
    operator_id: OneBot11Id
    user_id: OneBot11Id
    duration: StrictInt


class OneBot11NotifyNotice(OneBot11NoticeEvent):
    notice_type: Literal["notify"] = "notify"
    sub_type: Literal["poke", "lucky_king", "honor"]
    group_id: OneBot11Id
    user_id: OneBot11Id
    target_id: OneBot11Id | None = None
    honor_type: StrictStr | None = None

    @model_validator(mode="after")
    def require_subtype_fields(self) -> Self:
        if self.sub_type in {"poke", "lucky_king"} and self.target_id is None:
            msg = f"OneBot 11 notify.{self.sub_type} requires target_id"
            raise ValueError(msg)
        if self.sub_type == "honor" and self.honor_type is None:
            msg = "OneBot 11 notify.honor requires honor_type"
            raise ValueError(msg)
        return self


class OneBot11RequestEvent(OneBot11Event):
    post_type: Literal["request"] = "request"
    request_type: StrictStr
    user_id: OneBot11Id
    comment: StrictStr
    flag: StrictStr


class OneBot11GroupRequestEvent(OneBot11RequestEvent):
    request_type: Literal["group"] = "group"
    sub_type: Literal["add", "invite"]
    group_id: OneBot11Id


class OneBot11Status(Model):
    good: StrictBool
    online: StrictBool | None = None


class OneBot11MetaEvent(OneBot11Event):
    post_type: Literal["meta_event"] = "meta_event"
    meta_event_type: StrictStr
    status: JsonValue = None


class OneBot11HeartbeatEvent(OneBot11MetaEvent):
    meta_event_type: Literal["heartbeat"] = "heartbeat"
    status: OneBot11Status
    interval: StrictInt


_NOTICE_EVENT_MODELS: Mapping[str, type[OneBot11NoticeEvent]] = {
    "group_admin": OneBot11GroupAdminNotice,
    "group_ban": OneBot11GroupBanNotice,
    "group_upload": OneBot11GroupUploadNotice,
    "notify": OneBot11NotifyNotice,
}


type OneBot11EventVariant = Annotated[
    Annotated[OneBot11MessageEvent, Tag("message")]
    | Annotated[OneBot11NoticeEvent, Tag("notice")]
    | Annotated[OneBot11RequestEvent, Tag("request")]
    | Annotated[OneBot11MetaEvent, Tag("meta_event")],
    Discriminator(_event_payload_tag),
]


class OneBot11EventPayload(RootModel[OneBot11EventVariant]):
    pass


def _validate_ob11_event(data: Mapping[str, JsonValue]) -> OneBot11Event:
    event = OneBot11EventPayload.model_validate(data).root
    if isinstance(event, OneBot11NoticeEvent):
        model = _NOTICE_EVENT_MODELS.get(event.notice_type)
        if model is not None:
            event = model.model_validate(data)
    elif isinstance(event, OneBot11RequestEvent) and event.request_type == "group":
        event = OneBot11GroupRequestEvent.model_validate(data)
    elif isinstance(event, OneBot11MetaEvent) and event.meta_event_type == "heartbeat":
        event = OneBot11HeartbeatEvent.model_validate(data)
    return event


def decode_event(payload: BaseModel | Mapping[str, JsonValue]) -> Event:
    """Validate and convert a OneBot 11 event payload."""
    data = _json_object(payload)
    return _event_from_payload(_validate_ob11_event(data))


@dataclass(frozen=True, slots=True)
class HttpWebhook:
    path: str = "/onebot/v11/http"
    secret: AccessToken = None
    quick_response: bool = True

    def __post_init__(self) -> None:
        if not self.path.startswith("/"):
            msg = "OneBot 11 HTTP webhook path must start with /"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReverseWebSocket:
    host: str = "127.0.0.1"
    port: int = 8081
    path: str = "/onebot/v11/ws"

    def __post_init__(self) -> None:
        if not self.host or not 0 <= self.port <= _MAX_PORT:
            msg = "OneBot 11 reverse WebSocket address is invalid"
            raise ValueError(msg)
        if not self.path.startswith("/"):
            msg = "OneBot 11 reverse WebSocket path must start with /"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ForwardWebSocket:
    url: str
    role: WebSocketRole
    self_: BotSelf | None = None
    reconnect_interval: float = 3.0

    def __post_init__(self) -> None:
        endpoint = (urlsplit(self.url).path or "/").rstrip("/") or "/"
        expected = {"api": "/api", "event": "/event", "universal": "/"}[self.role]
        if endpoint != expected:
            msg = f"OneBot 11 {self.role} WebSocket must use the {expected} endpoint"
            raise ValueError(msg)
        if self.role in _ACTION_ROLES and self.self_ is None:
            msg = f"OneBot 11 {self.role} WebSocket requires a bot identity"
            raise ValueError(msg)
        if self.self_ is not None and (
            self.self_.platform != "qq" or not self.self_.user_id.isdecimal()
        ):
            msg = "OneBot 11 WebSocket identity must be a decimal qq account"
            raise ValueError(msg)
        if self.reconnect_interval <= 0:
            msg = "OneBot 11 reconnect interval must be positive"
            raise ValueError(msg)


type Ingress = HttpWebhook | ReverseWebSocket | ForwardWebSocket
type ActionBackend = HttpAction | WebSocketAction


def _ingress_resource(ingress: Ingress) -> tuple[object, ...]:
    if isinstance(ingress, HttpWebhook):
        return (HttpWebhook, ingress.path)
    if isinstance(ingress, ReverseWebSocket):
        return (ReverseWebSocket, ingress.host, ingress.port)
    return (ForwardWebSocket, ingress.url)


@dataclass(slots=True)
class _QuickOperations:
    active: bool = True
    values: list[OneBot11QuickOperation] = field(default_factory=list)


_HTTP_QUICK_OPERATIONS: ContextVar[_QuickOperations | None] = ContextVar(
    "bot_onebot11_http_quick_operations",
    default=None,
)


class OneBot11Gateway(Gateway):
    def __init__(
        self,
        bot: Bot,
        *,
        ingress: Sequence[Ingress] = (),
        action: ActionBackend | None = None,
        access_token: AccessToken = None,
        websocket_connector: WebSocketConnector | None = None,
    ) -> None:
        super().__init__(bot)
        self.ingress = tuple(ingress)
        resources = [_ingress_resource(item) for item in self.ingress]
        if len(set(resources)) != len(resources):
            msg = "OneBot 11 ingress resources must be unique"
            raise ValueError(msg)
        self.action_backend = action
        self.access_token = access_token_value(access_token)
        self.http_pool = (
            action.http_pool or AsyncPoolManager()
            if isinstance(action, HttpAction)
            else None
        )
        self._owns_http_pool = (
            isinstance(action, HttpAction) and action.http_pool is None
        )
        self._ws_actions = (
            WebSocketActionManager(action.timeout)
            if isinstance(action, WebSocketAction)
            else None
        )
        self._websocket_connector = websocket_connector or connect_websocket
        self._forward_tasks: dict[ForwardWebSocket, Task[None]] = {}
        self._reverse_servers: list[Server] = []
        self._lifecycle_lock = Lock()
        self._started = False
        self._closing = False

    @property
    def authorization_headers(self) -> dict[str, str] | None:
        if self.access_token is None:
            return None
        return {"Authorization": f"Bearer {self.access_token}"}

    @property
    def reverse_websocket_ports(self) -> tuple[int, ...]:
        return tuple(
            cast(tuple[str, int], server.sockets[0].getsockname())[1]
            for server in self._reverse_servers
        )

    @override
    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._started:
                return
            await super().start()
            self._closing = False
            try:
                for ingress in self.ingress:
                    if isinstance(ingress, ReverseWebSocket):
                        self._reverse_servers.append(
                            await self._start_reverse_websocket(ingress)
                        )
                for ingress in self.ingress:
                    if isinstance(ingress, ForwardWebSocket):
                        self._forward_tasks[ingress] = create_task(
                            self._run_forward_websocket(ingress)
                        )
            except BaseException:
                await self._close_transports()
                await self._close_http_pool()
                raise
            if self._owns_http_pool and self.http_pool is None:
                self.http_pool = AsyncPoolManager()
            self._started = True

    @override
    async def close(self) -> None:
        async with self._lifecycle_lock:
            await self._close_transports()
            await self._close_http_pool()
            await super().close()
            self._started = False

    async def _close_http_pool(self) -> None:
        if self.http_pool is not None and self._owns_http_pool:
            await self.http_pool.clear()
            self.http_pool = None

    async def _close_transports(self) -> None:
        self._closing = True
        forward_tasks = tuple(self._forward_tasks.values())
        reverse_servers = tuple(self._reverse_servers)
        for task in forward_tasks:
            task.cancel()
        for server in reverse_servers:
            server.close()
        if self._ws_actions is not None:
            self._ws_actions.fail_all()
        for task in forward_tasks:
            with suppress(CancelledError):
                await task
        for server in reverse_servers:
            await server.wait_closed()
        self._forward_tasks.clear()
        self._reverse_servers.clear()

    def mount(self, server: Robyn) -> Robyn:
        if not self._mount_server_once(server):
            return server

        for ingress in self.ingress:
            if isinstance(ingress, HttpWebhook):
                self._mount_http_webhook(server, ingress)
        return server

    async def handle_http(
        self,
        payload: BaseModel,
        *,
        quick_response: bool = True,
    ) -> Response:
        if __debug__:
            logger.trace(
                "handle OneBot 11 HTTP payload : {payload} {quick}",
                payload=payload,
                quick=quick_response,
            )
        collector = _QuickOperations() if quick_response else None
        token = _HTTP_QUICK_OPERATIONS.set(collector)
        try:
            try:
                data = _model_dump_object(payload)
                await self.dispatch_event(decode_event(data))
            except QueueFull:
                return empty_response(HTTPStatus.SERVICE_UNAVAILABLE)
            except (TypeError, ValueError, ValidationError) as exc:
                error = str(exc)
                logger.warning(
                    "reject OneBot 11 HTTP payload ({error})",
                    error=f"{type(exc).__name__}: {error}"
                    if error
                    else type(exc).__name__,
                )
                return text_response(HTTPStatus.BAD_REQUEST, str(exc))
            quick_operations = collector.values if collector is not None else []
        finally:
            if collector is not None:
                collector.active = False
            _HTTP_QUICK_OPERATIONS.reset(token)

        if quick_operations:
            if __debug__:
                logger.trace(
                    "return OneBot 11 quick operation : {operation} {payload}",
                    operation=quick_operations[0],
                    payload=payload,
                )
            return json_response(HTTPStatus.OK, quick_operations[0])
        return empty_response(HTTPStatus.NO_CONTENT)

    @override
    async def execute_return_action(
        self,
        connection: Connection,
        event: Event | None,
        action: ReturnAction,
    ) -> BaseModel:
        if action.kind == "message":
            if action.msg is None:
                msg = "Message return action requires a message"
                raise TypeError(msg)
            if not isinstance(event, MessageEvent):
                msg = "Message return values require a message event"
                raise TypeError(msg)
            quick_operations = _HTTP_QUICK_OPERATIONS.get()
            if (
                quick_operations is not None
                and quick_operations.active
                and not quick_operations.values
            ):
                operation = OneBot11QuickOperation(
                    reply=_dump_ob11_message(action.msg),
                    at_sender=False,
                )
                quick_operations.values.append(operation)
                return operation
            return await super().execute_return_action(connection, event, action)

        if action.kind != "request":
            return await super().execute_return_action(connection, event, action)

        operation = _request_quick_operation(event, action)
        quick_operations = _HTTP_QUICK_OPERATIONS.get()
        if (
            quick_operations is not None
            and quick_operations.active
            and not quick_operations.values
        ):
            if __debug__:
                logger.trace(
                    "queue OneBot 11 request quick operation : {operation} {event}",
                    operation=operation,
                    event=event,
                )
            quick_operations.values.append(operation)
            return operation

        action_name, params = _request_response_action(event, action)
        return await self.request_action(connection, action_name, params)

    @override
    async def request_action(
        self,
        connection: Connection,
        action: str,
        params: ActionParamModel,
    ) -> BaseModel:
        if self._closing:
            msg = "OneBot 11 gateway is closed"
            raise RuntimeError(msg)
        action_name, payload = self._normalize_action(action, params)
        if isinstance(self.action_backend, HttpAction):
            response = await self._request_http_action(
                self.action_backend,
                action_name,
                payload,
            )
        elif self._ws_actions is not None:
            response = await self._ws_actions.request(
                connection.self_,
                lambda echo: OneBot11ActionRequest(
                    action=action_name,
                    params=payload,
                    echo=echo,
                ).model_dump_json(
                    by_alias=True,
                    exclude_unset=True,
                ),
            )
        else:
            msg = f"{action_name} is not supported without an action backend"
            raise LookupError(msg)

        return adapt_action_response(action, response, connection.self_)

    def _normalize_action(
        self,
        action: str,
        params: ActionParamModel,
    ) -> tuple[str, BaseModel]:
        if action == Action.SEND_MESSAGE:
            if isinstance(params, SendPrivateMsgParams):
                return "send_private_msg", OneBot11SendPrivateMsgParams(
                    user_id=_ob11_int(params.user_id),
                    message=_dump_ob11_message(params.message),
                )
            if isinstance(params, SendGroupMsgParams):
                return "send_group_msg", OneBot11SendGroupMsgParams(
                    group_id=_ob11_int(params.group_id),
                    message=_dump_ob11_message(params.message),
                )

            msg = "OneBot 11 does not support channel messages"
            raise LookupError(msg)

        mapped_action = _ACTION_MAP.get(action)
        if mapped_action is not None:
            return mapped_action, _normalize_ob11_params(params, strict_ids=True)

        if action in _INTERNAL_ACTIONS:
            msg = f"{action} is not supported by OneBot 11"
            raise LookupError(msg)

        return action, _normalize_ob11_params(params, strict_ids=False)

    async def _request_http_action(
        self,
        backend: HttpAction,
        action: str,
        params: BaseModel,
    ) -> ActionResponse:
        if self.http_pool is None:
            msg = "OneBot 11 HTTP action backend is closed"
            raise RuntimeError(msg)

        parsed_url = urlsplit(backend.base_url)
        action_url = urlunsplit((
            parsed_url.scheme,
            parsed_url.netloc,
            f"{parsed_url.path.rstrip('/')}/{quote(action, safe='')}",
            parsed_url.query,
            "",
        ))
        response = await self.http_pool.request(
            HTTPMethod.POST,
            action_url,
            headers=self.authorization_headers,
            json=params.model_dump(
                mode="json",
                by_alias=True,
                exclude_unset=True,
            ),
        )
        status = getattr(response, "status", HTTPStatus.OK)
        if not HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES:
            msg = f"OneBot 11 action request failed with HTTP {status}"
            raise RuntimeError(msg)
        action_response = decode_action_response(orjson.loads(await response.data))
        if __debug__:
            logger.debug(
                "OneBot 11 HTTP action returned: {action} = {status}/{retcode}",
                action=action,
                status=action_response.status,
                retcode=action_response.retcode,
            )
            logger.trace(
                "OneBot 11 HTTP action response : {action} {response}",
                action=action,
                response=action_response,
            )
        return action_response

    def _mount_http_webhook(self, server: Robyn, ingress: HttpWebhook) -> None:
        async def handle(request: Request) -> Response:
            content_type = header_value(request.headers, "Content-Type")
            media_type = (
                content_type.split(";", 1)[0].strip().lower() if content_type else ""
            )
            if media_type != "application/json":
                return empty_response(HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            if not _signature_matches(
                access_token_value(ingress.secret),
                request.body,
                header_value(request.headers, "X-Signature"),
            ):
                logger.warning(
                    "reject OneBot 11 HTTP webhook signature: {path}",
                    path=ingress.path,
                )
                return empty_response(HTTPStatus.UNAUTHORIZED)
            try:
                payload = Model.model_validate_json(request.body)
            except ValidationError as exc:
                return text_response(HTTPStatus.BAD_REQUEST, str(exc))
            data = _model_dump_object(payload)
            self_id = header_value(request.headers, "X-Self-ID")
            try:
                header_self = _qq_self(self_id) if self_id is not None else None
                event_self = _event_self(data)
            except (TypeError, ValueError) as exc:
                return text_response(HTTPStatus.BAD_REQUEST, str(exc))
            if header_self != event_self:
                return text_response(
                    HTTPStatus.BAD_REQUEST,
                    "OneBot 11 HTTP X-Self-ID must match the event",
                )
            await self.bot.wait_until_running()
            return await self.handle_http(
                payload,
                quick_response=ingress.quick_response,
            )

        server.post(ingress.path)(handle)

    async def _start_reverse_websocket(self, ingress: ReverseWebSocket) -> Server:
        def authenticate(
            websocket: ServerConnection,
            request: WebSocketRequest,
        ) -> WebSocketResponse | None:
            path = request_target_path(request)
            if path is None:
                return websocket.respond(HTTPStatus.BAD_REQUEST, "Bad path\n")
            if path != ingress.path:
                return websocket.respond(HTTPStatus.NOT_FOUND, "Not found\n")
            if not token_matches(
                self.access_token,
                bearer_or_query_token(request),
            ):
                return websocket.respond(HTTPStatus.UNAUTHORIZED, "Unauthorized\n")
            role = _websocket_role(header_value(request.headers, "X-Client-Role"))
            self_id = header_value(request.headers, "X-Self-ID")
            if role is None or not self_id:
                return websocket.respond(
                    HTTPStatus.BAD_REQUEST, "Missing OneBot headers\n"
                )
            try:
                _qq_self(self_id)
            except ValueError:
                return websocket.respond(
                    HTTPStatus.BAD_REQUEST, "Invalid OneBot self ID\n"
                )
            return None

        async def handle(websocket: ServerConnection) -> None:
            request = websocket.request
            if request is None:
                msg = "OneBot 11 reverse WebSocket handshake is missing"
                raise ConnectionError(msg)
            role = _websocket_role(header_value(request.headers, "X-Client-Role"))
            self_id = header_value(request.headers, "X-Self-ID")
            if role is None or self_id is None:
                msg = "OneBot 11 reverse WebSocket headers are missing"
                raise ConnectionError(msg)
            self_ = _qq_self(self_id)
            await self._serve_websocket(WebsocketsConnection(websocket), role, self_)

        return await serve(
            handle,
            ingress.host,
            ingress.port,
            process_request=authenticate,
        )

    async def _serve_websocket(
        self,
        websocket: WebSocketConnection,
        role: WebSocketRole,
        self_: BotSelf | None = None,
    ) -> None:
        await self.bot.wait_until_running()
        expected_self = self_
        session = (
            self._ws_actions.register(websocket)
            if self._ws_actions is not None and role in _ACTION_ROLES
            else None
        )
        if session is not None and self_ is not None and self._ws_actions is not None:
            self._ws_actions.bind_self(session, self_)
        try:
            while True:
                try:
                    payload = Model.model_validate_json(await websocket.receive_text())
                except StopAsyncIteration, WebSocketDisconnect:
                    break
                expected_self = self._queue_ws_payload(
                    payload,
                    role,
                    session,
                    expected_self,
                )
        finally:
            if session is not None and self._ws_actions is not None:
                self._ws_actions.unregister(session)
            with suppress(Exception):
                await websocket.close()

    def _queue_ws_payload(
        self,
        payload: BaseModel | Mapping[str, JsonValue],
        role: WebSocketRole,
        session: WebSocketActionSession | None,
        expected_self: BotSelf | None,
    ) -> BotSelf | None:
        data = _json_object(payload)
        if "status" in data and "retcode" in data:
            self._receive_ws_action_response(session, data)
            return expected_self
        if role not in _EVENT_ROLES:
            msg = "OneBot 11 API WebSocket received an event"
            raise ValueError(msg)
        event_self = _event_self(data)
        if expected_self is not None and event_self != expected_self:
            msg = "OneBot 11 WebSocket event self_id changed"
            raise ValueError(msg)
        event = decode_event(data)
        if (
            session is not None
            and self._ws_actions is not None
            and event.self_ is not None
        ):
            self._ws_actions.bind_self(session, event.self_)
        try:
            self.enqueue_event(event)
        except QueueFull:
            msg = "OneBot 11 event queue is full"
            raise ConnectionError(msg) from None
        return expected_self or event_self

    def _receive_ws_action_response(
        self,
        session: WebSocketActionSession | None,
        data: Mapping[str, JsonValue],
    ) -> None:
        if session is None:
            msg = "OneBot 11 event WebSocket returned an action response"
            raise ValueError(msg)
        response = decode_action_response(data)
        if self._ws_actions is not None:
            self._ws_actions.receive(session, response)

    async def _run_forward_websocket(self, ingress: ForwardWebSocket) -> None:
        while not self._closing:
            try:
                websocket = await self._websocket_connector(
                    ingress.url,
                    self.authorization_headers,
                )
                await self._serve_websocket(websocket, ingress.role, ingress.self_)
                if not self._closing:
                    await sleep(ingress.reconnect_interval)
            except CancelledError:
                raise
            except Exception as exc:
                if self._closing:
                    return
                error = str(exc)
                logger.exception(
                    "OneBot 11 forward WebSocket failed: {url} retry={seconds}s "
                    "({error})",
                    url=ingress.url,
                    seconds=ingress.reconnect_interval,
                    error=f"{type(exc).__name__}: {error}"
                    if error
                    else type(exc).__name__,
                )
                await sleep(ingress.reconnect_interval)


def _normalize_ob11_params(
    params: ActionParamModel,
    *,
    strict_ids: bool,
) -> OneBot11GenericActionParams:
    payload = cast(
        dict[str, JsonValue | BaseModel],
        params.model_dump(mode="json", by_alias=True, exclude_unset=True),
    )
    for key in _OB11_NUMBER_PARAM_KEYS:
        if key in payload:
            payload[key] = _ob11_number(
                cast(JsonValue, payload[key]), strict=strict_ids
            )
    return OneBot11GenericActionParams.model_validate(payload)


def _event_from_payload(event: OneBot11Event) -> Event:
    self_ = _qq_self(_id_string(event.self_id))
    detail_type = _event_detail_type(event)
    payload: dict[str, JsonValue] = _model_dump_object(event)
    payload.update({
        "id": str(ULID()),
        "self": cast(
            JsonValue,
            self_.model_dump(mode="json", by_alias=True),
        ),
        "time": event.time,
        "type": _event_type(event),
        "detail_type": detail_type,
        "sub_type": event.sub_type,
    })
    for key in _EVENT_ID_FIELDS:
        if key in payload:
            payload[key] = _id_string(cast(JsonValue, payload[key]))

    if isinstance(event, OneBot11MessageEvent):
        _normalize_nested_id(payload, "sender", "user_id")
        _normalize_nested_id(payload, "anonymous", "id")
        message = _load_ob11_message(event.message)
        payload["message"] = cast(
            JsonValue,
            message.model_dump(mode="json", by_alias=True),
        )
        payload["alt_message"] = event.raw_message or str(message)
    elif isinstance(event, OneBot11NoticeEvent):
        payload["sub_type"] = _NOTICE_SUB_TYPES.get(
            (event.notice_type, event.sub_type),
            event.sub_type,
        )
    elif isinstance(event, OneBot11HeartbeatEvent):
        payload["status"] = cast(
            JsonValue,
            _status_payload(event.status, self_).model_dump(
                mode="json",
                by_alias=True,
            ),
        )

    if isinstance(event, OneBot11NoticeEvent) and detail_type.startswith("qq."):
        return NoticeEvent.model_validate(payload)
    return EventPayload.model_validate(payload).root


def _normalize_nested_id(
    payload: dict[str, JsonValue],
    field: str,
    key: str,
) -> None:
    value = payload.get(field)
    if value is None:
        return
    if not isinstance(value, Mapping):
        msg = f"OneBot 11 {field} must be an object or null"
        raise TypeError(msg)
    nested = _json_object(value)
    if key in nested:
        nested[key] = _id_string(nested[key])
    payload[field] = nested


def _event_type(event: OneBot11Event) -> str:
    if isinstance(event, OneBot11MetaEvent):
        return "meta"
    return event.post_type


def _event_detail_type(event: OneBot11Event) -> str:
    if isinstance(event, OneBot11MessageEvent):
        return event.message_type
    if isinstance(event, OneBot11NoticeEvent):
        return _NOTICE_DETAIL_TYPES.get(event.notice_type, f"qq.{event.notice_type}")
    if isinstance(event, OneBot11RequestEvent):
        return event.request_type
    if isinstance(event, OneBot11MetaEvent):
        if event.meta_event_type == "heartbeat":
            return "heartbeat"
        return f"qq.{event.meta_event_type}"

    msg = f"unsupported OneBot 11 post_type: {event.post_type}"
    raise ValueError(msg)


def _request_quick_operation(
    event: Event | None,
    action: ReturnAction,
) -> OneBot11QuickOperation:
    approve = _request_approve(action)
    if isinstance(event, FriendRequestEvent):
        if action.reason:
            msg = "Friend request rejections do not support reason"
            raise TypeError(msg)
        return OneBot11QuickOperation(approve=approve, remark=action.remark)
    if isinstance(event, GroupRequestEvent):
        if action.remark:
            msg = "Group request approvals do not support remark"
            raise TypeError(msg)
        return OneBot11QuickOperation(approve=approve, reason=action.reason)

    msg = "Request response return values require a supported request event"
    raise TypeError(msg)


def _request_response_action(
    event: Event | None,
    action: ReturnAction,
) -> tuple[str, ActionParamModel]:
    approve = _request_approve(action)
    if isinstance(event, FriendRequestEvent):
        if action.reason:
            msg = "Friend request rejections do not support reason"
            raise TypeError(msg)
        return "set_friend_add_request", ActionParamModel.model_validate({
            "flag": event.flag,
            "approve": approve,
            "remark": action.remark,
        })
    if isinstance(event, GroupRequestEvent):
        if action.remark:
            msg = "Group request approvals do not support remark"
            raise TypeError(msg)
        return "set_group_add_request", ActionParamModel.model_validate({
            "flag": event.flag,
            "sub_type": event.sub_type,
            "approve": approve,
            "reason": action.reason,
        })

    msg = "Request response return values require a supported request event"
    raise TypeError(msg)


def _request_approve(action: ReturnAction) -> bool:
    if action.approve is None:
        msg = "Request response return action requires approve"
        raise TypeError(msg)
    return action.approve


def _dump_ob11_message(value: MsgInput) -> OneBot11Message:
    return OneBot11Message(
        root=[_dump_ob11_segment(segment) for segment in Msg.from_input(value)]
    )


def _dump_ob11_segment(segment: MsgSegment) -> OneBot11MessageSegment:
    if isinstance(segment, TextSegment):
        return _ob11_segment("text", {"text": segment.data.text})
    if isinstance(segment, MentionSegment):
        return _ob11_segment("at", {"qq": segment.data.user_id})
    if isinstance(segment, MentionAllSegment):
        return _ob11_segment("at", {"qq": "all"})
    if isinstance(segment, ImageSegment | VoiceSegment | AudioSegment | VideoSegment):
        return _ob11_segment(
            _OB11_MEDIA_SEGMENT_TYPES[type(segment)],
            _file_segment_data(segment.data),
        )
    if isinstance(segment, LocationSegment):
        data = segment.data.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        )
        return _ob11_segment(
            "location",
            _segment_data({
                "lat": data["latitude"],
                "lon": data["longitude"],
                "title": data["title"],
                "content": data["content"],
            }),
        )
    if isinstance(segment, ReplySegment):
        return _ob11_segment("reply", {"id": segment.data.message_id})
    if isinstance(segment, ExtensionSegment):
        restored = _model_dump_object(segment.data)
        ob11_type = restored.pop("ob11_type", None)
        if ob11_type is not None and "type" not in restored:
            restored["type"] = ob11_type
        return _ob11_segment(segment.type, _segment_data(restored))
    if isinstance(segment, FileSegment):
        msg = "OneBot 11 does not define a file message segment"
        raise TypeError(msg)

    msg = f"{segment.type} is not supported by OneBot 11"
    raise TypeError(msg)


def _file_segment_data(data: BaseModel) -> OneBot11SegmentData:
    dumped = _model_dump_object(data)
    file_id = dumped.pop("file_id")
    dumped["file"] = file_id
    return _segment_data(dumped)


def _segment_data(data: Mapping[str, JsonValue]) -> OneBot11SegmentData:
    return OneBot11SegmentData.model_validate({
        key: _segment_data_value(value)
        for key, value in data.items()
        if value is not None
    })


def _segment_data_value(value: JsonValue) -> JsonValue:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int | float):
        return str(value)
    return value


def _ob11_int(value: JsonValue) -> int:
    number = _ob11_number(value, strict=True)
    if isinstance(number, int):
        return number
    msg = "OneBot 11 numeric id must be an integer"
    raise TypeError(msg)


def _ob11_segment(
    segment_type: str,
    data: OneBot11SegmentData | Mapping[str, JsonValue],
) -> OneBot11MessageSegment:
    segment_data = (
        data
        if isinstance(data, OneBot11SegmentData)
        else OneBot11SegmentData.model_validate(data)
    )
    return OneBot11MessageSegment(type=segment_type, data=segment_data)


def _load_ob11_message(value: JsonValue) -> Msg:
    if isinstance(value, str):
        return Msg.model_validate(_parse_cq_message(value).model_dump(mode="json"))
    if isinstance(value, Mapping):
        return Msg.model_validate([_load_ob11_segment(value).model_dump(mode="json")])
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return Msg.model_validate([
            _load_ob11_segment(cast(JsonValue, segment)).model_dump(mode="json")
            for segment in value
        ])

    msg = "OneBot 11 message must be a string or segment array"
    raise TypeError(msg)


def _parse_cq_message(message: str) -> OneBot11Message:
    segments: list[OneBot11MessageSegment] = []
    index = 0
    while index < len(message):
        start = message.find("[CQ:", index)
        if start < 0:
            _append_text_segment(segments, message[index:])
            break
        if start > index:
            _append_text_segment(segments, message[index:start])

        end = message.find("]", start + 4)
        if end < 0:
            _append_text_segment(segments, message[start:])
            break

        segment = _parse_cq_segment(message[start + 4 : end])
        if segment is None:
            _append_text_segment(segments, message[start : end + 1])
        else:
            segments.append(segment)
        index = end + 1
    return OneBot11Message(root=segments)


def _append_text_segment(segments: list[OneBot11MessageSegment], text: str) -> None:
    if text:
        segments.append(_ob11_segment("text", {"text": _unescape_text(text)}))


def _parse_cq_segment(body: str) -> OneBot11MessageSegment | None:
    parts = body.split(",")
    segment_type = parts[0]
    if not segment_type:
        return None
    data: dict[str, JsonValue] = {}
    for part in parts[1:]:
        if not part:
            continue
        key, separator, value = part.partition("=")
        data[key] = _unescape_text(value.replace("&#44;", ",")) if separator else ""
    return _load_ob11_segment({"type": segment_type, "data": data})


def _load_ob11_segment(value: JsonValue) -> OneBot11MessageSegment:
    if not isinstance(value, Mapping):
        msg = "OneBot 11 segment must be an object"
        raise TypeError(msg)
    value = _json_object(value)

    segment_type = _required_str(value.get("type"), "type")
    data = _ob11_segment_data(value.get("data"))

    if segment_type == "text":
        return _ob11_segment("text", {"text": str(data.get("text", ""))})
    if segment_type == "at":
        qq = str(data.get("qq", ""))
        if qq == "all":
            return _ob11_segment("mention_all", {})
        return _ob11_segment("mention", {"user_id": qq})
    if segment_type in {"image", "record", "video"}:
        internal_type = {
            "image": "image",
            "record": "voice",
            "video": "video",
        }[segment_type]
        file = data.get("file") or data.get("url")
        payload = {key: item for key, item in data.items() if key != "file"}
        payload["file_id"] = str(file or "")
        return _ob11_segment(internal_type, payload)
    if segment_type == "location":
        return _ob11_segment(
            "location",
            {
                "latitude": _finite_float(data.get("lat")),
                "longitude": _finite_float(data.get("lon")),
                "title": str(data.get("title", "")),
                "content": str(data.get("content", "")),
            },
        )
    if segment_type == "reply":
        return _ob11_segment("reply", {"message_id": str(data.get("id", ""))})
    payload = dict(data)
    ob11_type = payload.pop("type", None)
    if ob11_type is not None:
        payload["ob11_type"] = ob11_type
    return _ob11_segment(segment_type, payload)


def _ob11_segment_data(value: JsonValue | None) -> dict[str, JsonValue]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        msg = "OneBot 11 segment data must be an object or null"
        raise TypeError(msg)
    return _json_object(value)


def _unescape_text(value: str) -> str:
    return value.replace("&#91;", "[").replace("&#93;", "]").replace("&amp;", "&")


def _finite_float(value: JsonValue | None) -> float:
    if isinstance(value, bool):
        msg = "location value must be a number"
        raise TypeError(msg)
    try:
        number = float(cast(Any, value))
    except TypeError, ValueError:
        msg = "location value must be a number"
        raise ValueError(msg) from None
    if not isfinite(number):
        msg = "location value must be finite"
        raise ValueError(msg)
    return number


def _status_payload(value: OneBot11Status, self_: BotSelf) -> Status:
    bots = (
        [BotStatus(self=self_, online=value.online)] if value.online is not None else []
    )
    return Status(good=value.good, bots=bots)


def _required_str(value: JsonValue | None, field: str) -> str:
    if not isinstance(value, str):
        msg = f"OneBot 11 {field} must be a string"
        raise TypeError(msg)
    return value


def _id_string(value: JsonValue | None) -> str:
    if isinstance(value, bool) or value is None:
        msg = "OneBot 11 id fields must be strings or numbers"
        raise TypeError(msg)
    if isinstance(value, int | str):
        return str(value)
    msg = "OneBot 11 id fields must be strings or numbers"
    raise TypeError(msg)


def _ob11_number(value: JsonValue, *, strict: bool) -> JsonValue:
    if isinstance(value, bool):
        msg = "OneBot 11 numeric id must not be a boolean"
        raise TypeError(msg)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdecimal() or (
            stripped.startswith("-") and stripped[1:].isdecimal()
        ):
            return int(stripped)
    if strict:
        msg = "OneBot 11 numeric id must be an integer"
        raise TypeError(msg)
    return value


def adapt_action_response(
    action: str,
    response: ActionResponse,
    self_: BotSelf,
) -> ActionResponse:
    if response.status != ApiStatus.OK:
        return response
    try:
        internal_action = Action(action)
    except ValueError:
        return response
    data = _adapt_action_data(internal_action, response.data, self_)
    return response.model_copy(update={"data": data})


def _adapt_action_data(
    action: Action,
    value: JsonValue,
    self_: BotSelf,
) -> JsonValue:
    if action == Action.SEND_MESSAGE:
        data = _action_data_object(value, action)
        result: dict[str, JsonValue] = {
            "message_id": _id_string(data.get("message_id")),
        }
        if "time" in data:
            result["time"] = _action_time(data["time"])
        return result
    if action in {Action.DELETE_MESSAGE, Action.SET_GROUP_NAME, Action.LEAVE_GROUP}:
        if value is not None:
            msg = f"OneBot 11 {action} response data must be null"
            raise TypeError(msg)
        return None
    return _adapt_query_action_data(action, value, self_)


def _adapt_query_action_data(
    action: Action,
    value: JsonValue,
    self_: BotSelf,
) -> JsonValue:
    if action == Action.GET_SELF_INFO:
        data = _action_data_object(value, action)
        return {
            "user_id": _id_string(data.get("user_id")),
            "user_name": _required_str(data.get("nickname"), "nickname"),
            "user_displayname": "",
        }
    if action == Action.GET_USER_INFO:
        return _adapt_user_data(value, action)
    if action == Action.GET_FRIEND_LIST:
        return [
            _adapt_user_data(item, action, remark_field="remark")
            for item in _action_data_list(value, action)
        ]
    if action == Action.GET_GROUP_INFO:
        return _adapt_group_data(value, action)
    if action == Action.GET_GROUP_LIST:
        return [
            _adapt_group_data(item, action) for item in _action_data_list(value, action)
        ]
    if action == Action.GET_GROUP_MEMBER_INFO:
        return _adapt_group_member_data(value, action)
    if action == Action.GET_GROUP_MEMBER_LIST:
        return [
            _adapt_group_member_data(item, action)
            for item in _action_data_list(value, action)
        ]
    if action == Action.GET_STATUS:
        status = OneBot11Status.model_validate(_action_data_object(value, action))
        return cast(
            JsonValue,
            _status_payload(status, self_).model_dump(mode="json", by_alias=True),
        )
    if action == Action.GET_VERSION:
        data = _action_data_object(value, action)
        _required_str(data.get("protocol_version"), "protocol_version")
        return {
            "impl": _required_str(data.get("app_name"), "app_name"),
            "version": _required_str(data.get("app_version"), "app_version"),
            "onebot_version": "12",
        }
    return value


def _adapt_user_data(
    value: JsonValue,
    action: Action,
    *,
    remark_field: str | None = None,
) -> dict[str, JsonValue]:
    data = _action_data_object(value, action)
    user_remark = (
        _required_str(data.get(remark_field), remark_field)
        if remark_field is not None
        else ""
    )
    return {
        "user_id": _id_string(data.get("user_id")),
        "user_name": _required_str(data.get("nickname"), "nickname"),
        "user_displayname": "",
        "user_remark": user_remark,
    }


def _adapt_group_data(
    value: JsonValue,
    action: Action,
) -> dict[str, JsonValue]:
    data = _action_data_object(value, action)
    return {
        "group_id": _id_string(data.get("group_id")),
        "group_name": _required_str(data.get("group_name"), "group_name"),
    }


def _adapt_group_member_data(
    value: JsonValue,
    action: Action,
) -> dict[str, JsonValue]:
    data = _action_data_object(value, action)
    return {
        "user_id": _id_string(data.get("user_id")),
        "user_name": _required_str(data.get("nickname"), "nickname"),
        "user_displayname": _required_str(data.get("card"), "card"),
    }


def _action_data_object(value: JsonValue, action: Action) -> dict[str, JsonValue]:
    if not isinstance(value, Mapping):
        msg = f"OneBot 11 {action} response data must be an object"
        raise TypeError(msg)
    return _json_object(value)


def _action_data_list(value: JsonValue, action: Action) -> list[JsonValue]:
    if not isinstance(value, list):
        msg = f"OneBot 11 {action} response data must be an array"
        raise TypeError(msg)
    return value


def _action_time(value: JsonValue) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = "OneBot 11 send_message response time must be a number"
        raise TypeError(msg)
    if not isfinite(value):
        msg = "OneBot 11 send_message response time must be finite"
        raise ValueError(msg)
    return float(value)


def decode_action_response(
    payload: BaseModel | Mapping[str, JsonValue],
) -> ActionResponse:
    response = OneBot11ActionResponse.model_validate(_json_object(payload))
    if response.echo is not None and not isinstance(response.echo, str):
        msg = "OneBot 11 action response echo must be a string or null"
        raise TypeError(msg)
    echo = response.echo
    if response.status == "ok":
        return ActionResponse.ok(response.data, echo=echo)
    if response.status == "async":
        msg = "OneBot 11 async action responses cannot be represented"
        raise ValueError(msg)
    message = response.message or response.msg or ""
    return ActionResponse(
        status=ApiStatus.FAILED,
        retcode=response.retcode,
        data=response.data,
        message=message,
        echo=echo,
    )


def _model_dump_object(value: BaseModel) -> dict[str, JsonValue]:
    return cast(
        dict[str, JsonValue],
        value.model_dump(
            mode="json",
            by_alias=True,
            exclude_unset=True,
        ),
    )


def _json_object(value: object) -> dict[str, JsonValue]:
    if isinstance(value, BaseModel):
        return _model_dump_object(value)
    if not isinstance(value, Mapping):
        msg = "JSON value must be an object"
        raise TypeError(msg)
    return {str(key): _json_value(item) for key, item in value.items()}


def _json_value(value: object) -> JsonValue:
    if isinstance(value, BaseModel):
        return value.model_dump(
            mode="json",
            by_alias=True,
            exclude_unset=True,
        )
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_json_value(item) for item in value]
    return cast(JsonValue, value)


__all__ = [
    "ForwardWebSocket",
    "HttpAction",
    "HttpWebhook",
    "MsgInput",
    "OneBot11ActionRequest",
    "OneBot11ActionResponse",
    "OneBot11EventPayload",
    "OneBot11Gateway",
    "OneBot11Message",
    "OneBot11MessageSegment",
    "OneBot11QuickOperation",
    "ReverseWebSocket",
    "WebSocketAction",
    "WebSocketConnection",
    "adapt_action_response",
    "decode_action_response",
    "decode_event",
]
