import re
from asyncio import (
    Lock,
    QueueFull,
    Task,
    create_task,
    current_task,
    gather,
    sleep,
    timeout,
)
from collections.abc import Sequence
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from http import HTTPMethod, HTTPStatus
from logging import getLogger
from typing import Annotated, override

from pydantic import (
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    TypeAdapter,
    ValidationError,
)
from pydantic.dataclasses import dataclass as validated_dataclass
from robyn import Request, Response, Robyn
from urllib3_future import AsyncPoolManager
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import InvalidURI
from websockets.headers import parse_subprotocol
from websockets.http11 import Request as WebSocketRequest
from websockets.http11 import Response as WebSocketResponse
from websockets.uri import parse_uri

from bot.core import Bot
from bot.protocol.actions import ActionParamModel, ActionRequest, ActionResponse
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
    NonWhitespaceStr,
    PositiveSeconds,
    TcpPort,
    WebSocketAction,
    WebSocketActionManager,
    WebSocketActionSession,
    WebSocketConnection,
    WebSocketConnector,
    WebsocketsConnection,
    access_token_value,
    await_cleanup,
    bearer_or_query_token,
    connect_websocket,
    empty_response,
    header_value,
    json_response,
    request_target_path,
    token_matches,
)

logger = getLogger(__name__)

_WS_PAYLOAD_ADAPTER = TypeAdapter(EventPayload | ActionResponse)
_DATACLASS_CONFIG = ConfigDict(strict=True, validate_default=True)
type _IngressPath = Annotated[
    StrictStr,
    Field(pattern=re.compile(r"^/(?!/)[\x21-\x22\x24-\x3e\x40-\x7e]*\Z")),
]


@validated_dataclass(frozen=True, slots=True, config=_DATACLASS_CONFIG)
class HttpWebhook:
    path: _IngressPath = "/onebot/v12/http"
    quick_response: StrictBool = True


@validated_dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=_DATACLASS_CONFIG,
)
class ReverseWebSocket:
    host: NonWhitespaceStr = "127.0.0.1"
    port: TcpPort = 8082
    path: _IngressPath = "/onebot/v12/ws"


@validated_dataclass(frozen=True, slots=True, config=_DATACLASS_CONFIG)
class ForwardWebSocket:
    url: StrictStr
    reconnect_interval: PositiveSeconds = 3.0

    def __post_init__(self) -> None:
        try:
            parsed = parse_uri(self.url)
        except (InvalidURI, ValueError) as exc:
            msg = "OneBot 12 forward WebSocket URL must use ws or wss"
            raise ValueError(msg) from exc
        if not parsed.host.strip() or any(
            character.isspace() for character in self.url
        ):
            msg = "OneBot 12 forward WebSocket URL must not contain whitespace"
            raise ValueError(msg)


type Ingress = HttpWebhook | ReverseWebSocket | ForwardWebSocket
type ActionBackend = HttpAction | WebSocketAction


def _ingress_resource(ingress: Ingress) -> tuple[object, ...]:
    if isinstance(ingress, HttpWebhook):
        return HttpWebhook, ingress.path
    if isinstance(ingress, ReverseWebSocket):
        return ReverseWebSocket, ingress.host, ingress.port
    return ForwardWebSocket, ingress.url


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
        resources = [_ingress_resource(item) for item in self.ingress]
        if len(set(resources)) != len(resources):
            msg = "OneBot 12 ingress resources must be unique"
            raise ValueError(msg)
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
        self._forward_tasks: list[Task[None]] = []
        self._reverse_servers: list[Server] = []
        self._reverse_tasks: set[Task[None]] = set()
        self._lifecycle_lock = Lock()
        self._started = False
        self._closing = False

    @override
    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._started:
                return
            self._closing = False
            try:
                for ingress in self.ingress:
                    if isinstance(ingress, ReverseWebSocket):
                        self._reverse_servers.append(
                            await self._start_reverse_websocket(ingress)
                        )
                for ingress in self.ingress:
                    if isinstance(ingress, ForwardWebSocket):
                        self._forward_tasks.append(
                            create_task(self._run_forward_websocket(ingress))
                        )
            except BaseException as startup_error:
                try:
                    await self._finish_close()
                except BaseException as cleanup_error:
                    msg = "OneBot 12 startup and cleanup failed"
                    raise BaseExceptionGroup(
                        msg,
                        [startup_error, cleanup_error],
                    ) from None
                raise
            self._started = True

    @override
    async def close(self) -> None:
        async with self._lifecycle_lock:
            finishing = create_task(
                self._finish_close(),
                name="onebot12-gateway-close",
            )
            await await_cleanup(finishing)

    async def _finish_close(self) -> None:
        self._closing = True
        try:
            try:
                await self._close_transports()
            finally:
                await self._close_http_pool()
        finally:
            self._started = False

    async def _close_http_pool(self) -> None:
        if self._owns_http_pool and self.http_pool is not None:
            await self.http_pool.clear()
            self.http_pool = None

    async def _close_transports(self) -> None:
        self._closing = True
        servers = tuple(self._reverse_servers)
        for server in servers:
            server.close()
        tasks = (*self._forward_tasks, *self._reverse_tasks)
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        if self._ws_actions is not None:
            self._ws_actions.fail_all()
        await gather(*tasks, return_exceptions=True)
        await gather(*(server.wait_closed() for server in servers))
        self._forward_tasks.clear()
        self._reverse_servers.clear()
        self._reverse_tasks.clear()

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
        collector = _HttpQuickActions() if quick_response else None
        with _HTTP_QUICK_ACTIONS.set(collector):
            try:
                try:
                    await self.dispatch_event(event.root)
                except QueueFull:
                    return empty_response(HTTPStatus.SERVICE_UNAVAILABLE)
                actions = collector.actions if collector is not None else []
            finally:
                if collector is not None:
                    collector.active = False

        if actions:
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
        if self._closing:
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
        async with timeout(backend.timeout):
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
            body = await response.data
        return ActionResponse.model_validate_json(body)

    @property
    def _authorization_headers(self) -> dict[str, str] | None:
        if self.access_token is None:
            return None
        return {"Authorization": f"Bearer {self.access_token}"}

    @property
    def reverse_websocket_ports(self) -> tuple[int, ...]:
        return tuple(
            dict.fromkeys(
                socket.getsockname()[1]
                for server in self._reverse_servers
                for socket in server.sockets
            )
        )

    def _mount_http_webhook(self, server: Robyn, ingress: HttpWebhook) -> None:
        async def handle(request: Request) -> Response:
            if not self._authenticate(request):
                logger.warning(
                    "reject OneBot 12 HTTP webhook token: %s",
                    ingress.path,
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
                    "reject OneBot 12 HTTP webhook payload: %s",
                    exc.errors(include_url=False, include_input=False),
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

        return await serve(
            self._handle_reverse_websocket,
            ingress.host,
            ingress.port,
            select_subprotocol=lambda _websocket, protocols: next(
                filter(_is_onebot12_subprotocol, protocols),
                None,
            ),
            process_request=authenticate,
        )

    async def _handle_reverse_websocket(
        self,
        websocket: ServerConnection,
    ) -> None:
        task = current_task()
        assert task is not None  # ruff: ignore[assert]
        self._reverse_tasks.add(task)
        try:
            if self._closing:
                return
            protocol = websocket.subprotocol
            if protocol is None:
                msg = "OneBot 12 reverse WebSocket subprotocol was not negotiated"
                raise ValueError(msg)
            await self._serve_websocket(
                WebsocketsConnection(websocket),
                expected_impl=protocol.removeprefix("12."),
            )
        finally:
            self._reverse_tasks.discard(task)

    async def _serve_websocket(
        self,
        websocket: WebSocketConnection,
        *,
        expected_impl: str | None = None,
    ) -> None:
        session: WebSocketActionSession | None = None
        try:
            await self.bot.wait_until_running()
            if self._closing:
                return
            if self._ws_actions is not None:
                session = self._ws_actions.register(websocket)
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
                payload = _WS_PAYLOAD_ADAPTER.validate_json(
                    await websocket.receive_text()
                )
            except StopAsyncIteration:
                return

            if not connected:
                if not isinstance(payload, EventPayload) or not isinstance(
                    payload.root,
                    ConnectMetaEvent,
                ):
                    msg = "OneBot 12 WebSocket must start with meta.connect"
                    raise ValueError(msg)
                if (
                    expected_impl is not None
                    and payload.root.version.impl != expected_impl
                ):
                    msg = "OneBot 12 implementation does not match its subprotocol"
                    raise ValueError(msg)
                connected = True
                self._enqueue_ws_event(payload)
                continue
            if isinstance(payload, EventPayload):
                match payload.root:
                    case ConnectMetaEvent():
                        msg = (
                            "OneBot 12 WebSocket meta.connect must appear exactly once"
                        )
                        raise ValueError(msg)
                self._bind_session_event(session, payload)
                self._enqueue_ws_event(payload)
                continue
            if session is None:
                msg = "OneBot 12 WebSocket action response requires an action session"
                raise ValueError(msg)
            if self._ws_actions is not None:
                self._ws_actions.receive(session, payload)

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
            except Exception as exc:
                if not self._closing:
                    logger.warning(
                        "OneBot 12 forward WebSocket failed: %s retry=%ss (%s)",
                        ingress.url,
                        ingress.reconnect_interval,
                        (
                            exc.errors(include_url=False, include_input=False)
                            if isinstance(exc, ValidationError)
                            else type(exc).__name__
                        ),
                    )
            if not self._closing:
                await sleep(ingress.reconnect_interval)

    def _authenticate(self, source: object) -> bool:
        return token_matches(self.access_token, bearer_or_query_token(source))


def _is_onebot12_subprotocol(protocol: str) -> bool:
    return protocol.startswith("12.") and (
        NAME_PATTERN.fullmatch(protocol.removeprefix("12.")) is not None
    )
