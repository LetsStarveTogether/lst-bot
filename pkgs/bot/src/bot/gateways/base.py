from __future__ import annotations

from asyncio import (
    CancelledError,
    Future,
    Task,
    get_running_loop,
    shield,
    timeout,
)
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from hmac import compare_digest
from types import TracebackType
from typing import TYPE_CHECKING, Annotated, Never, Protocol, Self
from urllib.parse import parse_qs
from uuid import uuid4

import orjson
from logbook import Logger
from pydantic import (
    BaseModel,
    Field,
    JsonValue,
    SecretStr,
    StrictFloat,
    StrictInt,
    StrictStr,
    TypeAdapter,
)
from robyn import Response
from urllib3_future import AsyncPoolManager
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK

from bot.protocol.actions import (
    ActionCall,
    ActionParamInput,
    ActionParamModel,
    ActionRequest,
    ActionResponse,
)
from bot.protocol.common import BotSelf
from bot.protocol.enums import Action, ApiStatus
from bot.protocol.events import (
    ChannelMessageEvent,
    Event,
    GroupMessageEvent,
    MessageEvent,
)
from bot.protocol.msg import Msg, MsgInput
from bot.protocol.returns import ReturnAction

if TYPE_CHECKING:
    from bot.core import Bot
    from bot.routing import DispatchResult

logger = Logger(__name__)

type AccessToken = SecretStr | str | None
type NonWhitespaceStr = Annotated[StrictStr, Field(pattern=r"^\S+$")]
type PositiveSeconds = Annotated[
    StrictFloat,
    Field(gt=0, allow_inf_nan=False),
]
type TcpPort = Annotated[StrictInt, Field(ge=0, le=65535)]
_POSITIVE_SECONDS_ADAPTER = TypeAdapter(PositiveSeconds)


class RobynServer(Protocol):
    def startup_handler(self, handler: Callable[[], object]) -> None: ...

    def shutdown_handler(self, handler: Callable[[], object]) -> None: ...


class WebSocketConnection(Protocol):
    async def receive_text(self) -> str: ...

    async def send_text(self, payload: str) -> None: ...

    async def close(self, code: int = 1000) -> None: ...


type WebSocketConnector = Callable[
    [str, dict[str, str] | None],
    Awaitable[WebSocketConnection],
]


async def await_cleanup(task: Task[None]) -> None:
    cancelled: CancelledError | None = None
    while not task.done():
        try:
            await shield(task)
        except CancelledError as exc:
            cancelled = exc
    await task
    if cancelled is not None:
        raise cancelled


@dataclass(slots=True)
class HttpAction:
    base_url: str
    timeout: float = 30.0
    http_pool: AsyncPoolManager | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.timeout = _POSITIVE_SECONDS_ADAPTER.validate_python(self.timeout)


@dataclass(frozen=True, slots=True)
class WebSocketAction:
    timeout: float = 30.0


class _NativeWebSocketConnection(Protocol):
    async def recv(self) -> str | bytes: ...

    async def send(self, message: str) -> None: ...

    async def close(self, code: int = 1000) -> None: ...


class WebSocketClosedError(ConnectionError):
    def __init__(self, code: int | None) -> None:
        self.code = code
        detail = "WebSocket connection closed"
        super().__init__(detail if code is None else f"{detail} with code {code}")


class WebsocketsConnection:
    def __init__(self, websocket: _NativeWebSocketConnection) -> None:
        self.websocket = websocket

    async def receive_text(self) -> str:
        try:
            payload = await self.websocket.recv()
        except ConnectionClosed as exc:
            self._raise_closed(exc)
        if isinstance(payload, bytes):
            msg = "WebSocket text frame required"
            raise TypeError(msg)
        return payload

    async def send_text(self, payload: str) -> None:
        try:
            await self.websocket.send(payload)
        except ConnectionClosed as exc:
            self._raise_closed(exc)

    async def close(self, code: int = 1000) -> None:
        await self.websocket.close(code=code)

    @staticmethod
    def _raise_closed(exc: ConnectionClosed) -> Never:
        if isinstance(exc, ConnectionClosedOK):
            raise StopAsyncIteration from exc
        raise WebSocketClosedError(
            exc.rcvd.code if exc.rcvd is not None else None
        ) from exc


async def connect_websocket(
    url: str,
    headers: dict[str, str] | None,
    *,
    proxy: str | None = None,
    max_size: int | None = 2**20,
) -> WebsocketsConnection:
    websocket = await connect(
        url,
        additional_headers=headers,
        proxy=proxy,
        max_size=max_size,
    )
    return WebsocketsConnection(websocket)


class Connection:
    def __init__(
        self,
        gateway: Gateway,
        self_: BotSelf,
    ) -> None:
        self.gateway = gateway
        self.self_ = self_

    def __str__(self) -> str:
        return f"{self.gateway}@{self.self_}"

    @property
    def bot(self) -> Bot:
        return self.gateway.bot

    async def action(
        self,
        action: str | Action,
        **params: ActionParamInput,
    ) -> BaseModel:
        action_call = ActionCall.model_validate({
            "action": action,
            "params": params,
        })
        call = action_call.root
        return await self.request_action(call.action, call.params)

    async def send_msg(
        self,
        msg: MsgInput,
        **params: ActionParamInput,
    ) -> BaseModel:
        return await self.action(
            Action.SEND_MESSAGE,
            message=Msg.from_input(msg),
            **params,
        )

    async def execute_return_action(
        self,
        event: Event | None,
        action: ReturnAction,
    ) -> BaseModel:
        return await self.gateway.execute_return_action(self, event, action)

    async def execute_message_action(
        self,
        event: MessageEvent,
        msg: MsgInput,
    ) -> BaseModel:
        action_call = ActionCall.model_validate({
            "action": Action.SEND_MESSAGE,
            "params": self._message_action_params(event, msg),
        })
        call = action_call.root
        return await self.request_action(call.action, call.params)

    async def request_action(
        self,
        action: str,
        params: ActionParamModel,
    ) -> BaseModel:
        params_text = str(params)
        action_text = action if params_text == "-" else f"{action} {params_text}"
        logger.info(
            "execute action: {action} @ {self_}",
            action=action_text,
            self_=self.self_,
        )
        if __debug__:
            logger.debug(
                "request action: {action} @ {self_}",
                action=action,
                self_=self.self_,
            )
        response = await self.gateway.request_action(self, action, params)
        response_text = (
            str(response)
            if isinstance(response, ActionRequest | ActionResponse)
            else type(response).__name__
        )
        logger.info(
            "action done: {action} @ {self_} = {response}",
            action=action,
            self_=self.self_,
            response=response_text,
        )
        if __debug__:
            logger.debug(
                "action returned: {action} @ {self_} = {response}",
                action=action,
                self_=self.self_,
                response=response_text,
            )
        self._raise_for_failed_action_response(response)
        return response

    @staticmethod
    def _message_action_params(
        event: MessageEvent,
        msg: MsgInput,
    ) -> dict[str, ActionParamInput]:
        params: dict[str, ActionParamInput] = {
            "detail_type": event.detail_type,
            "message": Msg.from_input(msg),
        }
        if isinstance(event, GroupMessageEvent):
            params["group_id"] = event.group_id
        elif isinstance(event, ChannelMessageEvent):
            params["guild_id"] = event.guild_id
            params["channel_id"] = event.channel_id
        else:
            params["user_id"] = event.user_id
        return params

    @staticmethod
    def _raise_for_failed_action_response(response: BaseModel) -> None:
        if (
            not isinstance(response, ActionResponse)
            or response.status != ApiStatus.FAILED
        ):
            return

        detail = f": {response.message}" if response.message else ""
        msg = f"Action failed with retcode {response.retcode}{detail}"
        raise RuntimeError(msg)


class Gateway:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self._mounted_servers: list[RobynServer] = []

    def __str__(self) -> str:
        return type(self).__name__

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        _ = exc_type, exc, exc_tb
        await self.close()

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    def connection_for(self, self_: BotSelf) -> Connection:
        connection = Connection(self, self_)
        if __debug__:
            logger.trace(
                "create connection : {connection}",
                connection=connection,
            )
        return connection

    async def dispatch_event(self, event: Event) -> list[DispatchResult]:
        if __debug__:
            logger.trace(
                "gateway dispatch event : {gateway} {event}",
                gateway=self,
                event=event,
            )
        connection = (
            self.connection_for(event.self_) if event.self_ is not None else None
        )
        return await self.bot.dispatch(connection, event, gateway=self)

    def enqueue_event(self, event: Event) -> None:
        if __debug__:
            logger.trace(
                "gateway enqueue event : {gateway} {event}",
                gateway=self,
                event=event,
            )
        connection = (
            self.connection_for(event.self_) if event.self_ is not None else None
        )
        self.bot.enqueue_event(connection, event, gateway=self)

    async def request_action(
        self,
        connection: Connection,
        action: str,
        params: ActionParamModel,
    ) -> BaseModel:
        _ = connection, action, params
        msg = "Gateway action backend is not configured"
        raise NotImplementedError(msg)

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
            return await connection.execute_message_action(event, action.msg)

        if action.kind == "call":
            if action.action_call is None:
                msg = "Call return action requires an action call"
                raise TypeError(msg)
            call = action.action_call.root
            target_connection = connection
            if action.self_ is not None:
                target_connection = self.connection_for(action.self_)
            elif event is not None and event.self_ is not None:
                target_connection = self.connection_for(event.self_)
            return await target_connection.request_action(call.action, call.params)

        if action.kind == "request":
            msg = "Request response return values are not supported by this gateway"
            raise TypeError(msg)

        msg = f"Unsupported return action kind: {action.kind}"
        raise TypeError(msg)

    def _mount_server_once(self, server: RobynServer) -> bool:
        if any(mounted is server for mounted in self._mounted_servers):
            if __debug__:
                logger.debug(
                    "gateway already mounted: {gateway}@{server}",
                    gateway=type(self).__name__,
                    server=type(server).__name__,
                )
            return False

        self.bot.mount_server(server)
        self._mounted_servers.append(server)
        if __debug__:
            logger.debug(
                "mount bot lifecycle hooks: {gateway}@{server}",
                gateway=type(self).__name__,
                server=type(server).__name__,
            )
        return True


@dataclass(slots=True)
class WebSocketActionSession:
    websocket: WebSocketConnection
    selfs: set[BotSelf] = field(default_factory=set)


class WebSocketActionManager:
    def __init__(self, timeout: float) -> None:
        self.timeout = _POSITIVE_SECONDS_ADAPTER.validate_python(timeout)
        self._sessions: list[WebSocketActionSession] = []
        self._pending: dict[
            str,
            tuple[WebSocketActionSession, Future[ActionResponse]],
        ] = {}

    def register(self, websocket: WebSocketConnection) -> WebSocketActionSession:
        session = WebSocketActionSession(websocket)
        self._sessions.append(session)
        return session

    def bind_self(self, session: WebSocketActionSession, self_: BotSelf) -> None:
        if not any(current is session for current in self._sessions):
            msg = "WebSocket action session is not registered"
            raise LookupError(msg)
        session.selfs.add(self_)
        if __debug__:
            logger.trace(
                "bind WebSocket action session : {session} {bot_self}",
                session=session,
                bot_self=self_,
            )

    def unregister(self, session: WebSocketActionSession) -> None:
        self._sessions = [
            current for current in self._sessions if current is not session
        ]
        exc = ConnectionError("WebSocket action connection closed")
        for echo, (pending_session, future) in list(self._pending.items()):
            if pending_session is session:
                if not future.done():
                    future.set_exception(exc)
                self._pending.pop(echo, None)

    async def request(
        self,
        self_: BotSelf,
        build_payload: Callable[[str], str],
    ) -> ActionResponse:
        session = self._session_for(self_)
        if session is None:
            msg = "No action-capable WebSocket connection is available"
            raise LookupError(msg)

        echo = str(uuid4())
        loop = get_running_loop()
        future: Future[ActionResponse] = loop.create_future()
        self._pending[echo] = session, future
        try:
            async with timeout(self.timeout):
                if __debug__:
                    logger.debug(
                        "send WebSocket action request: {echo} @ {self_}",
                        echo=echo,
                        self_=self_,
                    )
                await session.websocket.send_text(build_payload(echo))
                return await future
        finally:
            self._pending.pop(echo, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    def receive(
        self,
        session: WebSocketActionSession,
        response: ActionResponse,
    ) -> bool:
        echo = response.echo
        if echo is None:
            logger.warning(
                "WebSocket action response missing echo: {response}",
                response=response,
            )
            return False
        pending = self._pending.get(echo)
        if pending is None:
            logger.warning(
                "unmatched WebSocket action response: echo={echo} {response}",
                echo=echo,
                response=response,
            )
            return False
        pending_session, future = pending
        if pending_session is not session:
            logger.warning(
                "mismatched WebSocket action response source: echo={echo} {response}",
                echo=echo,
                response=response,
            )
            return False
        if future.done():
            return False
        future.set_result(response)
        if __debug__:
            logger.debug(
                "receive WebSocket action response: echo={echo} {response}",
                echo=echo,
                response=response,
            )
            logger.trace(
                "receive WebSocket action response payload : {response!r}",
                response=response,
            )
        return True

    def fail_all(self) -> None:
        exc = ConnectionError("WebSocket action backend closed")
        for echo, (_, future) in list(self._pending.items()):
            if not future.done():
                future.set_exception(exc)
            self._pending.pop(echo, None)
        self._sessions.clear()

    def _session_for(self, self_: BotSelf) -> WebSocketActionSession | None:
        return next(
            (session for session in reversed(self._sessions) if self_ in session.selfs),
            None,
        )


def json_response(status: int, payload: BaseModel | JsonValue) -> Response:
    if isinstance(payload, BaseModel):
        body = payload.model_dump_json(
            by_alias=True,
            exclude_none=False,
            exclude_unset=False,
        )
    else:
        body = orjson.dumps(payload)
    return Response(status, {"Content-Type": "application/json"}, body)


def text_response(status: int, text: str) -> Response:
    return Response(status, {"Content-Type": "text/plain; charset=utf-8"}, text)


def empty_response(status: int) -> Response:
    return Response(status, {}, "")


def access_token_value(access_token: AccessToken) -> str | None:
    if isinstance(access_token, SecretStr):
        access_token = access_token.get_secret_value()
    return access_token or None


def bearer_or_query_token(source: object) -> str | None:
    authorizations = _field_values(
        getattr(source, "headers", None),
        "Authorization",
        case_insensitive=True,
    )
    if authorizations:
        if len(authorizations) != 1 or not isinstance(authorizations[0], str):
            return None
        authorization = authorizations[0]
        prefix = "Bearer "
        return (
            authorization[len(prefix) :] if authorization.startswith(prefix) else None
        )

    query_params = getattr(source, "query_params", None)
    values = _field_values(query_params, "access_token")
    if not values:
        target = _request_target_parts(source)
        if target is None:
            return None
        values = parse_qs(target[1], keep_blank_values=True).get(
            "access_token",
            [],
        )

    return values[0] if len(values) == 1 and isinstance(values[0], str) else None


def request_target_path(source: object) -> str | None:
    target = _request_target_parts(source)
    return target[0] if target is not None else None


def _request_target_parts(source: object) -> tuple[str, str] | None:
    target = getattr(source, "path", None)
    if not isinstance(target, str):
        return None
    path, _, query = target.partition("?")
    if not path.startswith("/") or path.startswith("//") or "#" in target:
        return None
    return path, query


def _field_values(
    fields: object,
    name: str,
    *,
    case_insensitive: bool = False,
) -> list[object]:
    get_all = getattr(fields, "get_all", None)
    if callable(get_all):
        values = get_all(name)
        if not values and case_insensitive and name.lower() != name:
            values = get_all(name.lower())
        if values is None:
            return []
        return list(values) if isinstance(values, list | tuple) else [values]

    if not isinstance(fields, Mapping):
        return []

    matches: list[object] = []
    target = name.casefold() if case_insensitive else name
    for key, value in fields.items():
        if not isinstance(key, str):
            continue
        candidate = key.casefold() if case_insensitive else key
        if candidate != target:
            continue
        if isinstance(value, list | tuple):
            matches.extend(value)
        else:
            matches.append(value)
    return matches


def header_value(headers: object, name: str) -> str | None:
    values = _field_values(headers, name, case_insensitive=True)
    return values[0] if len(values) == 1 and isinstance(values[0], str) else None


def token_matches(expected: str | None, actual: str | None) -> bool:
    return expected is None or (
        actual is not None and compare_digest(actual.encode(), expected.encode())
    )


__all__ = [
    "AccessToken",
    "Connection",
    "Gateway",
    "HttpAction",
    "WebSocketAction",
    "WebSocketActionManager",
    "WebSocketActionSession",
    "WebSocketClosedError",
    "WebSocketConnection",
    "WebSocketConnector",
    "WebsocketsConnection",
    "access_token_value",
    "bearer_or_query_token",
    "connect_websocket",
    "empty_response",
    "header_value",
    "json_response",
    "request_target_path",
    "text_response",
    "token_matches",
]
