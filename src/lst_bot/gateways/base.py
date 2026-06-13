from __future__ import annotations

from asyncio import (
    CancelledError,
    Future,
    Queue,
    Task,
    create_task,
    get_running_loop,
    wait_for,
)
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from hmac import compare_digest
from inspect import isawaitable
from types import TracebackType
from typing import TYPE_CHECKING, Protocol, Self, cast

import orjson
from logbook import Logger
from pydantic import BaseModel, JsonValue, SecretStr
from robyn import Headers, Response
from ulid import ULID

from lst_bot.protocol.actions import (
    ActionCall,
    ActionParamInput,
    ActionParamModel,
    ActionRequest,
    ActionResponse,
)
from lst_bot.protocol.common import BotSelf
from lst_bot.protocol.enums import Action, ApiStatus
from lst_bot.protocol.events import (
    ChannelMessageEvent,
    Event,
    GroupMessageEvent,
    MessageEvent,
)
from lst_bot.protocol.msg import Msg, MsgInput
from lst_bot.protocol.returns import ReturnAction

if TYPE_CHECKING:
    from lst_bot.core import Bot
    from lst_bot.routing import DispatchResult

logger = Logger(__name__)

type AccessToken = SecretStr | str | None
type State = dict[str, object]
type RobynResult = Response | BaseModel | None
type RobynPayloadHandler = Callable[[BaseModel], RobynResult | Awaitable[RobynResult]]


class RobynServer(Protocol):
    def startup_handler(self, handler: Callable[[], object]) -> None: ...

    def shutdown_handler(self, handler: Callable[[], object]) -> None: ...


class WebSocketConnection(Protocol):
    async def receive_bytes(self) -> bytes: ...

    async def send_bytes(self, payload: bytes) -> None: ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class _QueuedRobynPayload:
    payload: BaseModel
    handler: RobynPayloadHandler
    response: Future[RobynResult]


type _QueuedRobynItem = _QueuedRobynPayload | None

_MOUNTED_LIFECYCLES: set[tuple[int, int]] = set()


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
            logger.trace(
                "request action payload : {action} {params!r} {connection}",
                action=action,
                params=params,
                connection=self,
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
            logger.trace(
                "action returned payload : {response!r} {connection}",
                response=response,
                connection=self,
            )
        self._raise_for_failed_action_response(response)
        return response

    @staticmethod
    def _message_action_params(
        event: MessageEvent,
        msg: MsgInput,
    ) -> dict[str, Msg | str]:
        params: dict[str, Msg | str] = {
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
        self._connections: dict[BotSelf, Connection] = {}
        self._server_queue: Queue[_QueuedRobynItem] = Queue()
        self._server_task: Task[None] | None = None
        self._server_payload_tasks: set[Task[None]] = set()
        self._server_closing = False
        self._mounted_servers: set[int] = set()

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
        if self._server_task is None or self._server_task.done():
            self._server_closing = False
            self._server_task = create_task(self._run_server())

    async def close(self) -> None:
        self._server_closing = True
        task = self._server_task
        if task is not None:
            if not task.done():
                await self._server_queue.put(None)
            await task
            self._server_task = None
        await self._cancel_server_payload_tasks()

    def connection_for(self, self_: BotSelf) -> Connection:
        connection = self._connections.get(self_)
        if connection is None:
            connection = Connection(self, self_)
            self._connections[self_] = connection
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
        server_id = id(server)
        if server_id in self._mounted_servers:
            if __debug__:
                logger.debug(
                    "gateway already mounted: {gateway}@server#{server_id}",
                    gateway=type(self).__name__,
                    server_id=server_id,
                )
            return False
        self._mounted_servers.add(server_id)

        lifecycle_key = (server_id, id(self.bot))
        if lifecycle_key not in _MOUNTED_LIFECYCLES:
            startup_handler = server.startup_handler
            shutdown_handler = server.shutdown_handler
            startup_handler(self.bot.start)
            shutdown_handler(self.bot.close)
            _MOUNTED_LIFECYCLES.add(lifecycle_key)
            if __debug__:
                logger.debug(
                    "mount bot lifecycle hooks: server#{server_id}",
                    server_id=server_id,
                )
        return True

    async def _run_on_server_task(
        self,
        payload: BaseModel,
        handler: RobynPayloadHandler,
    ) -> RobynResult:
        if (
            self._server_closing
            or self._server_task is None
            or self._server_task.done()
        ):
            msg = "Gateway server task is not running"
            raise RuntimeError(msg)

        loop = get_running_loop()
        response: Future[RobynResult] = loop.create_future()
        await self._server_queue.put(
            _QueuedRobynPayload(
                payload=payload,
                handler=handler,
                response=response,
            ),
        )
        return await response

    async def _run_server(self) -> None:
        try:
            while True:
                item = await self._server_queue.get()
                try:
                    if item is None:
                        return
                    task = create_task(self._handle_server_payload(item))
                    self._server_payload_tasks.add(task)
                    task.add_done_callback(self._server_payload_tasks.discard)
                finally:
                    self._server_queue.task_done()
        finally:
            await self._cancel_server_payload_tasks()

    async def _handle_server_payload(self, item: _QueuedRobynPayload) -> None:
        try:
            value = item.handler(item.payload)
            if isawaitable(value):
                value = await value
        except CancelledError:
            if not item.response.done():
                item.response.cancel()
            raise
        except Exception as exc:
            if not item.response.done():
                item.response.set_exception(exc)
        else:
            if not item.response.done():
                item.response.set_result(cast(RobynResult, value))

    async def _cancel_server_payload_tasks(self) -> None:
        tasks = tuple(self._server_payload_tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(CancelledError):
                await task
        self._server_payload_tasks.clear()


@dataclass(slots=True)
class WebSocketActionSession:
    websocket: WebSocketConnection
    selfs: set[BotSelf] = field(default_factory=set)


@dataclass(slots=True)
class _PendingAction:
    session: WebSocketActionSession
    future: Future[ActionResponse]


class WebSocketActionManager:
    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
        self._sessions: list[WebSocketActionSession] = []
        self._pending: dict[str, _PendingAction] = {}

    def register(self, websocket: WebSocketConnection) -> WebSocketActionSession:
        session = WebSocketActionSession(websocket)
        self._sessions.append(session)
        return session

    def bind_self(self, session: WebSocketActionSession, self_: BotSelf) -> None:
        session.selfs.add(self_)
        if __debug__:
            logger.trace(
                "bind WebSocket action session : {session} {bot_self}",
                session=session,
                bot_self=self_,
            )

    def unregister(self, session: WebSocketActionSession) -> None:
        with suppress(ValueError):
            self._sessions.remove(session)
        exc = ConnectionError("WebSocket action connection closed")
        for echo, pending in list(self._pending.items()):
            if pending.session is session:
                if not pending.future.done():
                    pending.future.set_exception(exc)
                self._pending.pop(echo, None)

    async def request(
        self,
        self_: BotSelf,
        build_payload: Callable[[str], bytes],
    ) -> ActionResponse:
        session = self._session_for(self_)
        if session is None:
            msg = "No action-capable WebSocket connection is available"
            raise LookupError(msg)

        echo = str(ULID())
        loop = get_running_loop()
        future: Future[ActionResponse] = loop.create_future()
        self._pending[echo] = _PendingAction(session=session, future=future)
        try:
            if __debug__:
                logger.debug(
                    "send WebSocket action request: {echo} @ {self_}",
                    echo=echo,
                    self_=self_,
                )
            await session.websocket.send_bytes(build_payload(echo))
            return await wait_for(future, timeout=self.timeout)
        finally:
            self._pending.pop(echo, None)

    def receive(self, response: ActionResponse) -> bool:
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
        if not pending.future.done():
            pending.future.set_result(response)
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
        for echo, pending in list(self._pending.items()):
            if not pending.future.done():
                pending.future.set_exception(exc)
            self._pending.pop(echo, None)
        self._sessions.clear()

    def _session_for(self, self_: BotSelf) -> WebSocketActionSession | None:
        for session in self._sessions:
            if self_ in session.selfs:
                return session
        return self._sessions[0] if self._sessions else None


def json_response(status: int, payload: BaseModel | JsonValue) -> Response:
    if isinstance(payload, BaseModel):
        body = payload.model_dump_json(
            by_alias=True,
            exclude_none=False,
            exclude_unset=False,
        )
    else:
        body = orjson.dumps(payload)
    return Response(status, Headers({"Content-Type": "application/json"}), body)


def text_response(status: int, text: str) -> Response:
    return Response(
        status, Headers({"Content-Type": "text/plain; charset=utf-8"}), text
    )


def empty_response(status: int) -> Response:
    return Response(status, Headers({}), "")


def access_token_value(access_token: AccessToken) -> str | None:
    if isinstance(access_token, SecretStr):
        access_token = access_token.get_secret_value()
    return access_token or None


def bearer_or_query_token(source: object) -> str | None:
    authorization = header_value(getattr(source, "headers", None), "Authorization")
    if authorization is None:
        connector = getattr(source, "_connector", None)
        authorization = header_value(
            getattr(connector, "headers", None), "Authorization"
        )
    if authorization is not None:
        prefix = "Bearer "
        if authorization.startswith(prefix):
            return authorization[len(prefix) :]

    query_params = getattr(source, "query_params", None)
    get_first = getattr(query_params, "get_first", None)
    if callable(get_first):
        value = get_first("access_token")
    else:
        get = getattr(query_params, "get", None)
        value = get("access_token", None) if callable(get) else None

    if isinstance(value, str):
        return value
    if isinstance(value, list | tuple) and value and isinstance(value[0], str):
        return value[0]
    return None


def header_value(headers: object, name: str) -> str | None:
    get = getattr(headers, "get", None)
    if callable(get):
        for key in (name, name.lower()):
            value = get(key)
            if isinstance(value, str):
                return value

    if isinstance(headers, Mapping):
        for key, value in headers.items():
            if isinstance(key, str) and key.lower() == name.lower():
                return value if isinstance(value, str) else None
    return None


def token_matches(expected: str | None, actual: str | None) -> bool:
    return expected is None or (actual is not None and compare_digest(actual, expected))


__all__ = [
    "AccessToken",
    "Connection",
    "Gateway",
    "WebSocketActionManager",
    "WebSocketActionSession",
    "WebSocketConnection",
    "access_token_value",
    "bearer_or_query_token",
    "empty_response",
    "header_value",
    "json_response",
    "text_response",
    "token_matches",
]
