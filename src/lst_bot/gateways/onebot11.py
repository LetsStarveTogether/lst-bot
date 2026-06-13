from __future__ import annotations

from asyncio import CancelledError, Task, create_task, sleep
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from http import HTTPMethod, HTTPStatus
from math import isfinite
from typing import Annotated, Any, Literal, cast, override

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
)
from robyn import Request, Response, Robyn, WebSocketDisconnect
from ulid import ULID
from urllib3_future import AsyncPoolManager
from urllib3_future.util import parse_url

from lst_bot.core import Bot
from lst_bot.protocol.actions import (
    ActionParamModel,
    ActionResponse,
    SendGroupMsgParams,
    SendPrivateMsgParams,
)
from lst_bot.protocol.base import Model
from lst_bot.protocol.common import BotSelf, BotStatus, Status
from lst_bot.protocol.enums import Action, ApiStatus, Retcode
from lst_bot.protocol.events import (
    Event,
    EventPayload,
    FriendRequestEvent,
    GroupRequestEvent,
)
from lst_bot.protocol.msg import (
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
from lst_bot.protocol.returns import ReturnAction

from .base import (
    AccessToken,
    Connection,
    Gateway,
    WebSocketActionManager,
    WebSocketActionSession,
    WebSocketConnection,
    access_token_value,
    bearer_or_query_token,
    empty_response,
    json_response,
    text_response,
    token_matches,
)
from .onebot12 import _connect_websockets_client

type WebSocketConnector = Callable[
    [str, dict[str, str] | None],
    Awaitable[WebSocketConnection],
]

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
_OB11_MEDIA_SEGMENT_TYPES: Mapping[type[MsgSegment], str] = {
    ImageSegment: "image",
    VideoSegment: "video",
    VoiceSegment: "record",
    AudioSegment: "record",
}

type OneBot11Id = StrictInt | StrictStr
type OneBot11Time = StrictInt | StrictFloat


def _field_value(value: object, key: str) -> object:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value).get(key)
    return getattr(value, key, None)


def _event_payload_tag(value: object) -> str:
    post_type = _field_value(value, "post_type")
    return post_type if isinstance(post_type, str) else ""


class OneBot11ActionRequest(Model):
    action: StrictStr
    params: SerializeAsAny[BaseModel] = Field(default_factory=Model)
    echo: JsonValue = Field(default=None, exclude_if=lambda value: value is None)


class OneBot11ActionResponse(Model):
    status: StrictStr
    retcode: StrictInt | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
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


class OneBot11RequestEvent(OneBot11Event):
    post_type: Literal["request"] = "request"
    request_type: StrictStr
    user_id: OneBot11Id
    comment: StrictStr
    flag: StrictStr


class OneBot11MetaEvent(OneBot11Event):
    post_type: Literal["meta_event"] = "meta_event"
    meta_event_type: StrictStr
    status: JsonValue = None


type OneBot11EventVariant = Annotated[
    Annotated[OneBot11MessageEvent, Tag("message")]
    | Annotated[OneBot11NoticeEvent, Tag("notice")]
    | Annotated[OneBot11RequestEvent, Tag("request")]
    | Annotated[OneBot11MetaEvent, Tag("meta_event")],
    Discriminator(_event_payload_tag),
]


class OneBot11EventPayload(RootModel[OneBot11EventVariant]):
    pass


@dataclass(frozen=True, slots=True)
class HttpWebhook:
    path: str = "/onebot/v11/http"
    quick_response: bool = True


@dataclass(frozen=True, slots=True)
class ReverseWebSocket:
    path: str = "/onebot/v11/ws"


@dataclass(frozen=True, slots=True)
class ForwardWebSocket:
    url: str
    reconnect_interval: float = 3.0


@dataclass(slots=True)
class HttpAction:
    base_url: str | None = None
    quick_response: bool = True
    http_pool: AsyncPoolManager | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class WebSocketAction:
    timeout: float = 30.0
    quick_response: bool = True


type Ingress = HttpWebhook | ReverseWebSocket | ForwardWebSocket
type ActionBackend = HttpAction | WebSocketAction

_HTTP_QUICK_OPERATIONS: ContextVar[list[OneBot11QuickOperation] | None] = ContextVar(
    "lst_bot_onebot11_http_quick_operations",
    default=None,
)


def _payload_kind(data: Mapping[str, JsonValue]) -> str:
    if "post_type" in data:
        return f"event:{data.get('post_type')}"
    if "action" in data:
        return "action"
    if "status" in data and "retcode" in data:
        return "action_response"
    return "unknown"


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
        self.action_backend = action
        self.access_token = access_token_value(access_token)
        self.http_pool = (
            action.http_pool
            if isinstance(action, HttpAction) and action.http_pool is not None
            else AsyncPoolManager()
            if isinstance(action, HttpAction)
            else None
        )
        self._ws_actions = (
            WebSocketActionManager(action.timeout)
            if isinstance(action, WebSocketAction)
            else None
        )
        self._websocket_connector = websocket_connector or _connect_websockets_client
        self._forward_tasks: dict[ForwardWebSocket, Task[None]] = {}
        self._closing = False

    @property
    def authorization_headers(self) -> dict[str, str] | None:
        if self.access_token is None:
            return None
        return {"Authorization": f"Bearer {self.access_token}"}

    @override
    async def start(self) -> None:
        await super().start()
        self._closing = False
        for ingress in self.ingress:
            if isinstance(ingress, ForwardWebSocket) and (
                ingress not in self._forward_tasks
                or self._forward_tasks[ingress].done()
            ):
                task = create_task(self._run_forward_websocket(ingress))
                self._forward_tasks[ingress] = task

    @override
    async def close(self) -> None:
        self._closing = True
        for task in self._forward_tasks.values():
            task.cancel()
        for task in self._forward_tasks.values():
            with suppress(CancelledError):
                await task
        self._forward_tasks.clear()
        if self._ws_actions is not None:
            self._ws_actions.fail_all()
        if self.http_pool is not None:
            await self.http_pool.clear()
        await super().close()

    def mount(self, server: Robyn) -> Robyn:
        if not self._mount_server_once(server):
            return server

        for ingress in self.ingress:
            if isinstance(ingress, HttpWebhook):
                self._mount_http_webhook(server, ingress)
            elif isinstance(ingress, ReverseWebSocket):
                self._mount_reverse_websocket(server, ingress)
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
        token = _HTTP_QUICK_OPERATIONS.set([] if quick_response else None)
        try:
            try:
                await self.handle_payload(payload)
            except (TypeError, ValueError, ValidationError) as exc:
                error = str(exc)
                logger.warning(
                    "reject OneBot 11 HTTP payload: {kind} ({error})",
                    kind=_payload_kind(_model_dump_object(payload)),
                    error=f"{type(exc).__name__}: {error}"
                    if error
                    else type(exc).__name__,
                )
                return text_response(HTTPStatus.BAD_REQUEST, str(exc))
            quick_operations = _HTTP_QUICK_OPERATIONS.get() or []
        finally:
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
        if action.kind != "request":
            return await super().execute_return_action(connection, event, action)

        operation = _request_quick_operation(event, action)
        quick_operations = _HTTP_QUICK_OPERATIONS.get()
        if quick_operations is not None and not quick_operations:
            if __debug__:
                logger.trace(
                    "queue OneBot 11 request quick operation : {operation} {event}",
                    operation=operation,
                    event=event,
                )
            quick_operations.append(operation)
            return operation

        action_name, params = _request_response_action(event, action)
        return await self.request_action(connection, action_name, params)

    async def handle_ws(
        self,
        payload: BaseModel,
        *,
        session: WebSocketActionSession | None = None,
    ) -> ActionResponse | None:
        if __debug__:
            logger.trace(
                "handle OneBot 11 WebSocket payload : {payload}",
                payload=payload,
            )
        try:
            return await self.handle_payload(payload, session=session)
        except (TypeError, ValueError, ValidationError) as exc:
            error = str(exc)
            logger.warning(
                "reject OneBot 11 WebSocket payload: {kind} ({error})",
                kind=_payload_kind(_model_dump_object(payload)),
                error=f"{type(exc).__name__}: {error}" if error else type(exc).__name__,
            )
            return ActionResponse(
                status=ApiStatus.FAILED,
                retcode=1400,
                data=None,
                message=str(exc),
            )

    async def handle_payload(
        self,
        payload: BaseModel,
        *,
        session: WebSocketActionSession | None = None,
    ) -> ActionResponse | None:
        data = _model_dump_object(payload)
        if __debug__:
            logger.trace(
                "handle OneBot 11 payload data : {data}",
                data=data,
            )
        if "post_type" in data:
            event = _event_from_payload(OneBot11EventPayload.model_validate(data).root)
            if __debug__:
                logger.trace(
                    "dispatch OneBot 11 event : {event}",
                    event=event,
                )
            connection = (
                self.connection_for(event.self_) if event.self_ is not None else None
            )
            if (
                session is not None
                and self._ws_actions is not None
                and event.self_ is not None
            ):
                self._ws_actions.bind_self(session, event.self_)
            await self.bot.dispatch(connection, event, gateway=self)
            return None
        if "action" in data:
            msg = "Inbound OneBot 11 action requests are not accepted"
            raise ValueError(msg)
        if "status" in data and "retcode" in data:
            response = _action_response_from_payload(data)
            if self._ws_actions is not None:
                matched = self._ws_actions.receive(response)
                if __debug__:
                    logger.trace(
                        "process OneBot 11 action response : {response} {matched}",
                        response=response,
                        matched=matched,
                    )
            return None

        msg = "OneBot 11 payload must be an event or action response"
        raise ValueError(msg)

    def quick_reply_message(self, msg: MsgInput) -> OneBot11Message:
        return _dump_ob11_message(msg)

    @override
    async def request_action(
        self,
        connection: Connection,
        action: str,
        params: ActionParamModel,
    ) -> BaseModel:
        action_name, payload = self._normalize_action(action, params)
        quick_operations = _HTTP_QUICK_OPERATIONS.get()
        if (
            quick_operations is not None
            and not quick_operations
            and isinstance(
                payload,
                OneBot11SendPrivateMsgParams | OneBot11SendGroupMsgParams,
            )
        ):
            operation = OneBot11QuickOperation(
                reply=payload.message,
                at_sender=False,
            )
            quick_operations.append(operation)
            if __debug__:
                logger.trace(
                    "queue OneBot 11 quick reply : {operation} {action} {connection}",
                    operation=operation,
                    action=action_name,
                    connection=connection,
                )
            return operation

        if isinstance(self.action_backend, HttpAction):
            return await self._request_http_action(
                self.action_backend,
                action_name,
                payload,
            )
        if self._ws_actions is not None:
            return await self._ws_actions.request(
                connection.self_,
                lambda echo: (
                    OneBot11ActionRequest(
                        action=action_name,
                        params=payload,
                        echo=echo,
                    )
                    .model_dump_json(
                        by_alias=True,
                        exclude_none=True,
                    )
                    .encode()
                ),
            )

        msg = f"{action_name} is not supported without an action backend"
        raise LookupError(msg)

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
        if backend.base_url is None:
            msg = "HTTP action backend requires a base URL"
            raise LookupError(msg)
        if self.http_pool is None:
            msg = "HTTP action pool is closed"
            raise RuntimeError(msg)

        parsed_url = parse_url(backend.base_url)
        base_url = parsed_url._replace(
            path=(parsed_url.path or "").rstrip("/") or None,
        ).url
        response = await self.http_pool.request(
            HTTPMethod.POST,
            f"{base_url}/{action}",
            headers=self.authorization_headers,
            json=params.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
            ),
        )
        action_response = _action_response_from_payload(
            orjson.loads(await response.data)
        )
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
            if not self._authenticate(request):
                logger.warning(
                    "reject OneBot 11 HTTP webhook token: {path}",
                    path=ingress.path,
                )
                return empty_response(HTTPStatus.UNAUTHORIZED)
            payload = Model.model_validate_json(request.body)
            return cast(
                Response,
                await self._run_on_server_task(
                    payload,
                    lambda item: self.handle_http(
                        item,
                        quick_response=ingress.quick_response,
                    ),
                ),
            )

        server.post(ingress.path)(handle)

    def _mount_reverse_websocket(
        self,
        server: Robyn,
        ingress: ReverseWebSocket,
    ) -> None:
        async def handle(websocket: WebSocketConnection) -> None:
            try:
                if not self._authenticate(websocket):
                    logger.warning(
                        "reject OneBot 11 reverse WebSocket token: {path}",
                        path=ingress.path,
                    )
                    await websocket.close()
                    return
                await self._serve_websocket(websocket)
            except CancelledError:
                raise
            except Exception as exc:
                error = str(exc)
                logger.exception(
                    "OneBot 11 reverse WebSocket failed: {path} ({error})",
                    path=ingress.path,
                    error=f"{type(exc).__name__}: {error}"
                    if error
                    else type(exc).__name__,
                )

        server.websocket(ingress.path)(handle)

    async def _serve_websocket(self, websocket: WebSocketConnection) -> None:
        session = (
            self._ws_actions.register(websocket)
            if self._ws_actions is not None
            else None
        )
        tasks: set[Task[None]] = set()
        graceful_close = False
        try:
            while True:
                try:
                    payload = Model.model_validate_json(await websocket.receive_bytes())
                except StopAsyncIteration:
                    graceful_close = True
                    break
                except WebSocketDisconnect:
                    graceful_close = True
                    break

                data = _model_dump_object(payload)
                if "status" in data and "retcode" in data:
                    response = _action_response_from_payload(data)
                    if self._ws_actions is not None:
                        self._ws_actions.receive(response)
                    continue

                self._start_ws_payload_task(websocket, tasks, payload, session)
        finally:
            if session is not None and self._ws_actions is not None:
                self._ws_actions.unregister(session)
            if not graceful_close:
                for task in tuple(tasks):
                    task.cancel()
            for task in tuple(tasks):
                with suppress(CancelledError, Exception):
                    await task
            await self._close_websocket(websocket)

    def _start_ws_payload_task(
        self,
        websocket: WebSocketConnection,
        tasks: set[Task[None]],
        payload: BaseModel,
        session: WebSocketActionSession | None,
    ) -> None:
        if __debug__:
            logger.trace(
                "receive OneBot 11 WebSocket payload : {payload}",
                payload=payload,
            )
        task = create_task(self._process_ws_payload(payload, session=session))
        tasks.add(task)
        task.add_done_callback(
            lambda done: self._close_ws_on_payload_failure(
                websocket,
                tasks,
                done,
            )
        )

    async def _process_ws_payload(
        self,
        payload: BaseModel,
        *,
        session: WebSocketActionSession | None,
    ) -> None:
        try:
            await self._run_on_server_task(
                payload,
                lambda item: self.handle_payload(item, session=session),
            )
        except Exception as exc:
            error = str(exc)
            logger.exception(
                "handle OneBot 11 WebSocket payload failed: {kind} ({error})",
                kind=_payload_kind(_model_dump_object(payload)),
                error=f"{type(exc).__name__}: {error}" if error else type(exc).__name__,
            )
            raise

    def _close_ws_on_payload_failure(
        self,
        websocket: WebSocketConnection,
        tasks: set[Task[None]],
        task: Task[None],
    ) -> None:
        tasks.discard(task)
        if task.cancelled() or task.exception() is None:
            return
        close_task = create_task(self._close_websocket(websocket))
        close_task.add_done_callback(self._consume_close_task_result)

    def _consume_close_task_result(self, task: Task[None]) -> None:
        with suppress(CancelledError, Exception):
            task.result()

    async def _close_websocket(self, websocket: WebSocketConnection) -> None:
        with suppress(Exception):
            await websocket.close()

    async def _run_forward_websocket(self, ingress: ForwardWebSocket) -> None:
        while not self._closing:
            try:
                websocket = await self._connect_forward_websocket(ingress)
                await self._serve_websocket(websocket)
                if not self._closing:
                    await self._sleep_before_forward_reconnect(ingress)
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

    async def _connect_forward_websocket(
        self,
        ingress: ForwardWebSocket,
    ) -> WebSocketConnection:
        return await self._websocket_connector(
            ingress.url,
            self.authorization_headers,
        )

    async def _sleep_before_forward_reconnect(self, ingress: ForwardWebSocket) -> None:
        await sleep(ingress.reconnect_interval)

    def _authenticate(self, source: object) -> bool:
        return token_matches(self.access_token, bearer_or_query_token(source))


def _normalize_ob11_params(
    params: ActionParamModel,
    *,
    strict_ids: bool,
) -> OneBot11GenericActionParams:
    payload: dict[str, JsonValue | BaseModel] = dict(_model_dump_object(params))
    if "message" in payload and not isinstance(payload["message"], str):
        payload["message"] = _dump_ob11_message(cast(MsgInput, payload["message"]))
    for key in _OB11_NUMBER_PARAM_KEYS:
        if key in payload:
            payload[key] = _ob11_number(
                cast(JsonValue, payload[key]), strict=strict_ids
            )
    return OneBot11GenericActionParams.model_validate(payload)


def _event_from_payload(event: OneBot11Event) -> Event:
    self_ = BotSelf(platform="qq", user_id=_id_string(event.self_id))
    payload: dict[str, JsonValue] = _model_dump_object(event)
    payload.update({
        "id": str(ULID()),
        "self": cast(
            JsonValue,
            self_.model_dump(mode="json", by_alias=True),
        ),
        "time": event.time,
        "type": _event_type(event),
        "detail_type": _event_detail_type(event),
    })
    for key in _EVENT_ID_FIELDS:
        if key in payload:
            payload[key] = _id_string(cast(JsonValue, payload[key]))

    if isinstance(event, OneBot11MessageEvent):
        message = _load_ob11_message(event.message)
        payload["message"] = cast(
            JsonValue,
            message.model_dump(mode="json", by_alias=True),
        )
        payload["alt_message"] = event.raw_message or str(message)
    elif isinstance(event, OneBot11MetaEvent) and event.meta_event_type == "heartbeat":
        payload["status"] = cast(
            JsonValue,
            _status_payload(event.status, self_).model_dump(
                mode="json",
                by_alias=True,
            ),
        )

    return EventPayload.model_validate(payload).root


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
        data[key] = _unescape_text(value).replace("&#44;", ",") if separator else ""
    return _load_ob11_segment({"type": segment_type, "data": data})


def _load_ob11_segment(value: JsonValue) -> OneBot11MessageSegment:
    if not isinstance(value, Mapping):
        msg = "OneBot 11 segment must be an object"
        raise TypeError(msg)
    value = _json_object(value)

    segment_type = _required_str(value.get("type"), "type")
    raw_data = value.get("data") or {}
    if not isinstance(raw_data, Mapping):
        msg = "OneBot 11 segment data must be an object or null"
        raise TypeError(msg)
    data = _json_object(raw_data)

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


def _status_payload(value: JsonValue | None, self_: BotSelf) -> Status:
    data = _json_object(value) if isinstance(value, Mapping) else {}
    good = data.get("good")
    if not isinstance(good, bool):
        online = data.get("online")
        good = online if isinstance(online, bool) else True

    bots: list[BotStatus] = []
    online = data.get("online")
    if isinstance(online, bool):
        bots.append(BotStatus(self=self_, online=online))
    return Status(good=good, bots=bots)


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


def _action_response_from_payload(
    payload: BaseModel | Mapping[str, JsonValue],
) -> ActionResponse:
    response = OneBot11ActionResponse.model_validate(_json_object(payload))
    echo = response.echo if isinstance(response.echo, str) else None
    if response.status in {"ok", "async"}:
        return ActionResponse.ok(response.data, echo=echo)

    retcode_value = response.retcode
    retcode = (
        retcode_value
        if isinstance(retcode_value, int)
        and not isinstance(retcode_value, bool)
        and retcode_value != 0
        else Retcode.INTERNAL_HANDLER_ERROR
    )
    message = response.message or response.msg or ""
    return ActionResponse(
        status=ApiStatus.FAILED,
        retcode=retcode,
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
            exclude_none=True,
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
            exclude_none=True,
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
]
