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
from http import HTTPMethod, HTTPStatus
from math import isfinite
from typing import cast, override

from logbook import Logger
from pydantic import BaseModel, JsonValue, ValidationError
from robyn import Request, Response, Robyn
from urllib3_future import AsyncPoolManager
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.headers import parse_subprotocol
from websockets.http11 import Request as WebSocketRequest
from websockets.http11 import Response as WebSocketResponse
from websockets.typing import Subprotocol

from bot.core import Bot
from bot.protocol.actions import ActionParamModel, ActionRequest, ActionResponse
from bot.protocol.base import Model
from bot.protocol.common import BotSelf
from bot.protocol.constants import NAME_PATTERN
from bot.protocol.events import (
    ConnectMetaEvent,
    EventPayload,
    MetaEvent,
    StatusUpdateMetaEvent,
)

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
    token_matches,
)

logger = Logger(__name__)

_MAX_PORT = 2**16 - 1


@dataclass(frozen=True, slots=True)
class HttpWebhook:
    path: str = "/onebot/v12/http"
    quick_response: bool = True

    def __post_init__(self) -> None:
        _validate_ingress_path(self.path)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReverseWebSocket:
    host: str = "127.0.0.1"
    port: int = 8082
    path: str = "/onebot/v12/ws"

    def __post_init__(self) -> None:
        if isinstance(self.port, bool) or not 0 <= self.port <= _MAX_PORT:
            msg = "OneBot 12 reverse WebSocket port must be between 0 and 65535"
            raise ValueError(msg)
        _validate_ingress_path(self.path)


@dataclass(frozen=True, slots=True)
class ForwardWebSocket:
    url: str
    reconnect_interval: float = 3.0

    def __post_init__(self) -> None:
        if not isfinite(self.reconnect_interval) or self.reconnect_interval <= 0:
            msg = "OneBot 12 reconnect interval must be positive"
            raise ValueError(msg)


type Ingress = HttpWebhook | ReverseWebSocket | ForwardWebSocket
type ActionBackend = HttpAction | WebSocketAction


@dataclass(slots=True)
class _HttpQuickActions:
    actions: list[ActionRequest] = field(default_factory=list)
    active: bool = True


_HTTP_QUICK_ACTIONS: ContextVar[_HttpQuickActions | None] = ContextVar(
    "bot_onebot12_http_quick_actions",
    default=None,
)


class OneBot12Gateway(Gateway):
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
        self._validate_ingress()
        self.action_backend = action
        self.access_token = access_token_value(access_token)
        self._owns_http_pool = (
            isinstance(action, HttpAction) and action.http_pool is None
        )
        self.http_pool = action.http_pool if isinstance(action, HttpAction) else None
        self._ws_actions = (
            WebSocketActionManager(action.timeout)
            if isinstance(action, WebSocketAction)
            else None
        )
        self._websocket_connector = websocket_connector or connect_websocket
        self._forward_tasks: dict[ForwardWebSocket, Task[None]] = {}
        self._reverse_servers: dict[ReverseWebSocket, Server] = {}
        self._lifecycle_lock = Lock()
        self._started = False
        self._closed = False
        self._closing = False

    @override
    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._started:
                return
            await super().start()
            self._closing = False
            self._closed = False
            try:
                await self._start_transports()
            except BaseException:
                await self._close_transports()
                raise
            self._started = True

    async def _start_transports(self) -> None:
        for ingress in self.ingress:
            if isinstance(ingress, ReverseWebSocket):
                reverse_server = await self._start_reverse_websocket(ingress)
                self._reverse_servers[ingress] = reverse_server
        for ingress in self.ingress:
            if isinstance(ingress, ForwardWebSocket):
                task = create_task(self._run_forward_websocket(ingress))
                self._forward_tasks[ingress] = task

    @override
    async def close(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closing = True
            if self._started:
                await self._close_transports()
            if self._ws_actions is not None:
                self._ws_actions.fail_all()
            if self._owns_http_pool and self.http_pool is not None:
                await self.http_pool.clear()
                self.http_pool = None
            await super().close()
            self._started = False
            self._closed = True

    async def _close_transports(self) -> None:
        self._closing = True
        for task in self._forward_tasks.values():
            task.cancel()
        for task in self._forward_tasks.values():
            with suppress(CancelledError):
                await task
        self._forward_tasks.clear()
        for server in self._reverse_servers.values():
            server.close()
        for server in self._reverse_servers.values():
            await server.wait_closed()
        self._reverse_servers.clear()

    def _validate_ingress(self) -> None:
        keys: set[object] = set()
        for ingress in self.ingress:
            match ingress:
                case HttpWebhook(path=path):
                    key = (HttpWebhook, path)
                case ReverseWebSocket(host=host, port=port):
                    key = (ReverseWebSocket, host, port)
                case ForwardWebSocket(url=url):
                    key = (ForwardWebSocket, url)
            if key in keys:
                msg = "OneBot 12 ingress endpoints must be unique"
                raise ValueError(msg)
            keys.add(key)

    def mount(self, server: Robyn) -> Robyn:
        if not self._mount_server_once(server):
            return server

        for ingress in self.ingress:
            if isinstance(ingress, HttpWebhook):
                self._mount_http_webhook(server, ingress)
        return server

    async def handle_http(
        self,
        event: EventPayload,
        *,
        quick_response: bool = True,
    ) -> Response:
        await self.bot.wait_until_running()
        if __debug__:
            logger.trace(
                "handle OneBot 12 HTTP payload : {payload} {quick}",
                payload=event,
                quick=quick_response,
            )
        collector = _HttpQuickActions() if quick_response else None
        token = _HTTP_QUICK_ACTIONS.set(collector)
        try:
            try:
                await self.dispatch_event(event.root)
            except QueueFull:
                return empty_response(HTTPStatus.SERVICE_UNAVAILABLE)
            actions = collector.actions if collector is not None else []
        finally:
            if collector is not None:
                collector.active = False
            _HTTP_QUICK_ACTIONS.reset(token)

        if actions:
            if __debug__:
                logger.trace(
                    "return OneBot 12 quick actions : {actions}",
                    actions=actions,
                )
            return json_response(
                HTTPStatus.OK,
                [
                    action.model_dump(
                        mode="json",
                        by_alias=True,
                        exclude_unset=True,
                    )
                    for action in actions
                ],
            )
        return empty_response(HTTPStatus.NO_CONTENT)

    @override
    async def request_action(
        self,
        connection: Connection,
        action: str,
        params: ActionParamModel,
    ) -> ActionRequest | ActionResponse:
        if self._closing or self._closed:
            msg = "OneBot 12 gateway is closed"
            raise RuntimeError(msg)
        quick_actions = _HTTP_QUICK_ACTIONS.get()
        if quick_actions is not None and quick_actions.active:
            request = ActionRequest(
                action=action,
                params=params,
                self=connection.self_,
            )
            quick_actions.actions.append(request)
            if __debug__:
                logger.trace(
                    "queue OneBot 12 quick action : {request} {connection}",
                    request=request,
                    connection=connection,
                )
            return request

        if isinstance(self.action_backend, HttpAction):
            return await self._request_http_action(
                self.action_backend,
                action,
                params,
                connection.self_,
            )
        if self._ws_actions is not None:
            return await self._ws_actions.request(
                connection.self_,
                lambda echo: ActionRequest(
                    action=action,
                    params=params,
                    echo=echo,
                    self=connection.self_,
                ).model_dump_json(
                    by_alias=True,
                    exclude_unset=True,
                ),
            )

        msg = f"{action} is not supported without an action backend"
        raise LookupError(msg)

    async def _request_http_action(
        self,
        backend: HttpAction,
        action: str,
        params: ActionParamModel,
        self_: BotSelf | None,
    ) -> ActionResponse:
        if self.http_pool is None:
            if not self._owns_http_pool:
                msg = "OneBot 12 HTTP action pool is unavailable"
                raise RuntimeError(msg)
            self.http_pool = AsyncPoolManager()

        request = ActionRequest(action=action, params=params, self=self_)
        response = await self.http_pool.request(
            HTTPMethod.POST,
            backend.base_url,
            headers=self._authorization_headers,
            json=request.model_dump(
                mode="json",
                by_alias=True,
                exclude_unset=True,
            ),
        )
        if response.status != HTTPStatus.OK:
            msg = f"OneBot 12 action request failed: HTTP {response.status}"
            raise RuntimeError(msg)
        content_type = header_value(response.headers, "Content-Type")
        media_type = (
            content_type.split(";", 1)[0].strip().lower() if content_type else ""
        )
        if media_type != "application/json":
            msg = (
                "OneBot 12 action response has unsupported Content-Type: "
                f"{content_type or '-'}"
            )
            raise RuntimeError(msg)
        action_response = ActionResponse.model_validate_json(await response.data)
        if __debug__:
            logger.debug(
                "OneBot 12 HTTP action returned: {action} = {status}/{retcode}",
                action=action,
                status=action_response.status,
                retcode=action_response.retcode,
            )
            logger.trace(
                "OneBot 12 HTTP action response : {action} {response}",
                action=action,
                response=action_response,
            )
        return action_response

    @property
    def _authorization_headers(self) -> dict[str, str] | None:
        if self.access_token is None:
            return None
        return {"Authorization": f"Bearer {self.access_token}"}

    @property
    def reverse_websocket_ports(self) -> tuple[int, ...]:
        return tuple(
            cast(tuple[str, int], server.sockets[0].getsockname())[1]
            for server in self._reverse_servers.values()
        )

    def _mount_http_webhook(self, server: Robyn, ingress: HttpWebhook) -> None:
        async def handle(request: Request) -> Response:
            if not self._authenticate(request):
                logger.warning(
                    "reject OneBot 12 HTTP webhook token: {path}",
                    path=ingress.path,
                )
                return empty_response(HTTPStatus.UNAUTHORIZED)
            version = header_value(request.headers, "X-OneBot-Version")
            impl = header_value(request.headers, "X-Impl")
            if version != "12" or impl is None or NAME_PATTERN.fullmatch(impl) is None:
                return empty_response(HTTPStatus.BAD_REQUEST)
            content_type = header_value(request.headers, "Content-Type")
            media_type = (
                content_type.split(";", 1)[0].strip().lower() if content_type else ""
            )
            if media_type != "application/json":
                return empty_response(HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            try:
                payload = EventPayload.model_validate_json(request.body)
            except ValidationError as exc:
                logger.warning(
                    "reject OneBot 12 HTTP webhook payload: {error}",
                    error=str(exc),
                )
                return empty_response(HTTPStatus.BAD_REQUEST)
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
            try:
                protocols = [
                    protocol
                    for value in request.headers.get_all("Sec-WebSocket-Protocol")
                    for protocol in parse_subprotocol(value)
                ]
            except ValueError:
                return websocket.respond(HTTPStatus.BAD_REQUEST, "Bad subprotocol\n")
            if not any(_is_onebot12_subprotocol(protocol) for protocol in protocols):
                return websocket.respond(HTTPStatus.BAD_REQUEST, "Bad subprotocol\n")
            return None

        async def handle(websocket: ServerConnection) -> None:
            protocol = websocket.subprotocol
            if protocol is None:
                msg = "OneBot 12 reverse WebSocket subprotocol was not negotiated"
                raise ValueError(msg)
            await self._serve_websocket(
                WebsocketsConnection(websocket),
                expected_impl=protocol.removeprefix("12."),
            )

        def select_subprotocol(
            _websocket: ServerConnection,
            protocols: Sequence[Subprotocol],
        ) -> Subprotocol | None:
            return next(
                (
                    protocol
                    for protocol in protocols
                    if _is_onebot12_subprotocol(protocol)
                ),
                None,
            )

        return await serve(
            handle,
            ingress.host,
            ingress.port,
            select_subprotocol=select_subprotocol,
            process_request=authenticate,
        )

    async def _serve_websocket(
        self,
        websocket: WebSocketConnection,
        *,
        expected_impl: str | None = None,
    ) -> None:
        await self.bot.wait_until_running()
        session = (
            self._ws_actions.register(websocket)
            if self._ws_actions is not None
            else None
        )
        try:
            await self._read_ws_payloads(
                websocket,
                session=session,
                expected_impl=expected_impl,
            )
        finally:
            if session is not None and self._ws_actions is not None:
                self._ws_actions.unregister(session)
            with suppress(Exception):
                await websocket.close()

    async def _read_ws_payloads(
        self,
        websocket: WebSocketConnection,
        *,
        session: WebSocketActionSession | None,
        expected_impl: str | None,
    ) -> None:
        connected = False
        while True:
            try:
                payload = Model.model_validate_json(await websocket.receive_text())
            except StopAsyncIteration:
                return

            data = _model_dump_object(payload)
            if not connected:
                event = _connect_event(data, expected_impl=expected_impl)
                connected = True
                self._enqueue_ws_event(event)
                continue
            if "type" in data and "detail_type" in data:
                event = EventPayload.model_validate(data)
                match event.root:
                    case ConnectMetaEvent():
                        msg = (
                            "OneBot 12 WebSocket meta.connect must appear exactly once"
                        )
                        raise ValueError(msg)
                self._bind_session_event(session, event)
                self._enqueue_ws_event(event)
                continue
            if "status" in data and "retcode" in data:
                if session is None:
                    msg = (
                        "OneBot 12 WebSocket action response requires an action session"
                    )
                    raise ValueError(msg)
                response = ActionResponse.model_validate(data)
                if self._ws_actions is not None:
                    self._ws_actions.receive(session, response)
                continue

            EventPayload.model_validate(data)

    def _enqueue_ws_event(self, payload: EventPayload) -> None:
        try:
            self.enqueue_event(payload.root)
        except QueueFull:
            msg = "OneBot 12 WebSocket event queue is full"
            raise ConnectionError(msg) from None

    def _bind_session_event(
        self,
        session: WebSocketActionSession | None,
        payload: EventPayload,
    ) -> None:
        if session is None or self._ws_actions is None:
            return
        event = payload.root
        if isinstance(event, StatusUpdateMetaEvent):
            for bot in event.status.bots:
                self._ws_actions.bind_self(session, bot.self_)
        elif not isinstance(event, MetaEvent):
            self._ws_actions.bind_self(session, event.self_)

    async def _run_forward_websocket(self, ingress: ForwardWebSocket) -> None:
        while not self._closing:
            try:
                websocket = await self._websocket_connector(
                    ingress.url,
                    self._authorization_headers,
                )
                await self._serve_websocket(websocket)
                if not self._closing:
                    await sleep(ingress.reconnect_interval)
            except CancelledError:
                raise
            except Exception as exc:
                if self._closing:
                    return
                error = str(exc)
                logger.exception(
                    "OneBot 12 forward WebSocket failed: {url} retry={seconds}s "
                    "({error})",
                    url=ingress.url,
                    seconds=ingress.reconnect_interval,
                    error=f"{type(exc).__name__}: {error}"
                    if error
                    else type(exc).__name__,
                )
                await sleep(ingress.reconnect_interval)

    def _authenticate(self, source: object) -> bool:
        return token_matches(self.access_token, bearer_or_query_token(source))


def _model_dump_object(value: BaseModel) -> dict[str, JsonValue]:
    return cast(
        dict[str, JsonValue],
        value.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=False,
        ),
    )


def _connect_event(
    data: Mapping[str, JsonValue],
    *,
    expected_impl: str | None,
) -> EventPayload:
    event = EventPayload.model_validate(data)
    match event.root:
        case ConnectMetaEvent():
            pass
        case _:
            msg = "OneBot 12 WebSocket must start with meta.connect"
            raise ValueError(msg)
    if expected_impl is not None and event.root.version.impl != expected_impl:
        msg = "OneBot 12 implementation does not match its subprotocol"
        raise ValueError(msg)
    return event


def _is_onebot12_subprotocol(protocol: str) -> bool:
    return protocol.startswith("12.") and (
        NAME_PATTERN.fullmatch(protocol.removeprefix("12.")) is not None
    )


def _validate_ingress_path(path: str) -> None:
    if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
        msg = "OneBot 12 ingress path must be an origin-form path"
        raise ValueError(msg)


__all__ = [
    "ForwardWebSocket",
    "HttpAction",
    "HttpWebhook",
    "OneBot12Gateway",
    "ReverseWebSocket",
    "WebSocketAction",
    "WebSocketConnection",
]
