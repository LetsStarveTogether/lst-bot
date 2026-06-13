from __future__ import annotations

from asyncio import CancelledError, Task, create_task, sleep
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from http import HTTPMethod, HTTPStatus
from typing import cast, override

from logbook import Logger
from pydantic import BaseModel, JsonValue, ValidationError
from robyn import Request, Response, Robyn, WebSocketDisconnect
from urllib3_future import AsyncPoolManager
from urllib3_future.util import parse_url
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK

from lst_bot.core import Bot
from lst_bot.protocol.actions import ActionParamModel, ActionRequest, ActionResponse
from lst_bot.protocol.base import Model
from lst_bot.protocol.common import BotSelf
from lst_bot.protocol.enums import Retcode
from lst_bot.protocol.events import EventPayload

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
    token_matches,
)

type WebSocketConnector = Callable[
    [str, dict[str, str] | None],
    Awaitable[WebSocketConnection],
]

logger = Logger(__name__)


@dataclass(frozen=True, slots=True)
class HttpWebhook:
    path: str = "/onebot/v12/http"
    quick_response: bool = True


@dataclass(frozen=True, slots=True)
class ReverseWebSocket:
    path: str = "/onebot/v12/ws"


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

_HTTP_QUICK_ACTIONS: ContextVar[list[ActionRequest] | None] = ContextVar(
    "lst_bot_onebot12_http_quick_actions",
    default=None,
)


def _payload_kind(data: Mapping[str, JsonValue]) -> str:
    if "type" in data and "detail_type" in data:
        return f"event:{data.get('type')}:{data.get('detail_type')}"
    if "action" in data:
        return "action"
    if "status" in data and "retcode" in data:
        return "action_response"
    return "unknown"


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
                "handle OneBot 12 HTTP payload : {payload} {quick}",
                payload=payload,
                quick=quick_response,
            )
        token = _HTTP_QUICK_ACTIONS.set([] if quick_response else None)
        try:
            try:
                await self.handle_payload(payload)
            except (TypeError, ValueError, ValidationError) as exc:
                error = str(exc)
                logger.warning(
                    "reject OneBot 12 HTTP payload: {kind} ({error})",
                    kind=_payload_kind(_model_dump_object(payload)),
                    error=f"{type(exc).__name__}: {error}"
                    if error
                    else type(exc).__name__,
                )
                return json_response(
                    HTTPStatus.OK,
                    ActionResponse.failed(Retcode.BAD_REQUEST, str(exc)),
                )
            actions = _HTTP_QUICK_ACTIONS.get() or []
        finally:
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
                        exclude_none=True,
                    )
                    for action in actions
                ],
            )
        return empty_response(HTTPStatus.NO_CONTENT)

    async def handle_ws(
        self,
        payload: BaseModel,
        *,
        session: WebSocketActionSession | None = None,
    ) -> ActionResponse | None:
        if __debug__:
            logger.trace(
                "handle OneBot 12 WebSocket payload : {payload}",
                payload=payload,
            )
        try:
            return await self.handle_payload(payload, session=session)
        except (TypeError, ValueError, ValidationError) as exc:
            error = str(exc)
            logger.warning(
                "reject OneBot 12 WebSocket payload: {kind} ({error})",
                kind=_payload_kind(_model_dump_object(payload)),
                error=f"{type(exc).__name__}: {error}" if error else type(exc).__name__,
            )
            return ActionResponse.failed(Retcode.BAD_REQUEST, str(exc))

    async def handle_payload(
        self,
        payload: BaseModel,
        *,
        session: WebSocketActionSession | None = None,
    ) -> ActionResponse | None:
        data = _model_dump_object(payload)
        if __debug__:
            logger.trace(
                "handle OneBot 12 payload data : {data}",
                data=data,
            )
        if "action" in data:
            msg = "Inbound OneBot 12 action requests are not accepted"
            raise ValueError(msg)
        if "status" in data and "retcode" in data:
            response = ActionResponse.model_validate(data)
            if self._ws_actions is not None:
                matched = self._ws_actions.receive(response)
                if __debug__:
                    logger.trace(
                        "process OneBot 12 action response : {response} {matched}",
                        response=response,
                        matched=matched,
                    )
            return None

        event = EventPayload.model_validate(data).root
        if __debug__:
            logger.trace(
                "dispatch OneBot 12 event : {event}",
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

    @override
    async def request_action(
        self,
        connection: Connection,
        action: str,
        params: ActionParamModel,
    ) -> ActionRequest | ActionResponse:
        quick_actions = _HTTP_QUICK_ACTIONS.get()
        if quick_actions is not None:
            request = ActionRequest(
                action=action,
                params=params,
                self=connection.self_,
            )
            quick_actions.append(request)
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
                lambda echo: (
                    ActionRequest(
                        action=action,
                        params=params,
                        echo=echo,
                        self=connection.self_,
                    )
                    .model_dump_json(
                        by_alias=True,
                        exclude_none=True,
                    )
                    .encode()
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
        if backend.base_url is None:
            msg = "HTTP action backend requires a base URL"
            raise LookupError(msg)
        if self.http_pool is None:
            msg = "HTTP action pool is closed"
            raise RuntimeError(msg)

        parsed_url = parse_url(backend.base_url)
        request = ActionRequest(action=action, params=params, self=self_)
        response = await self.http_pool.request(
            HTTPMethod.POST,
            parsed_url._replace(path=(parsed_url.path or "").rstrip("/") or None).url,
            headers=self._authorization_headers,
            json=request.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
            ),
        )
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

    def _mount_http_webhook(self, server: Robyn, ingress: HttpWebhook) -> None:
        async def handle(request: Request) -> Response:
            if not self._authenticate(request):
                logger.warning(
                    "reject OneBot 12 HTTP webhook token: {path}",
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
                        "reject OneBot 12 reverse WebSocket token: {path}",
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
                    "OneBot 12 reverse WebSocket failed: {path} ({error})",
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
                    response = ActionResponse.model_validate(data)
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
                "receive OneBot 12 WebSocket payload : {payload}",
                payload=payload,
            )
        task = create_task(
            self._process_ws_payload(websocket, payload, session=session)
        )
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
        websocket: WebSocketConnection,
        payload: BaseModel,
        *,
        session: WebSocketActionSession | None,
    ) -> None:
        try:
            response = await self._run_on_server_task(
                payload,
                lambda item: self.handle_ws(item, session=session),
            )
            response = cast(ActionResponse | None, response)
            if response is None:
                return
            await self._send_ws_response(websocket, response)
        except Exception as exc:
            error = str(exc)
            logger.exception(
                "handle OneBot 12 WebSocket payload failed: {kind} ({error})",
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

    async def _send_ws_response(
        self,
        websocket: WebSocketConnection,
        response: BaseModel,
    ) -> None:
        await websocket.send_bytes(response.model_dump_json(by_alias=True).encode())

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
                    "OneBot 12 forward WebSocket failed: {url} retry={seconds}s "
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
            self._authorization_headers,
        )

    async def _sleep_before_forward_reconnect(self, ingress: ForwardWebSocket) -> None:
        await sleep(ingress.reconnect_interval)

    def _authenticate(self, source: object) -> bool:
        return token_matches(self.access_token, bearer_or_query_token(source))


class _WebsocketsClientConnection:
    def __init__(self, websocket: ClientConnection) -> None:
        self._websocket = websocket

    async def receive_bytes(self) -> bytes:
        try:
            payload = await self._websocket.recv()
        except ConnectionClosedOK as exc:
            raise StopAsyncIteration from exc
        except ConnectionClosed as exc:
            msg = "WebSocket connection closed"
            raise ConnectionError(msg) from exc
        if isinstance(payload, str):
            return payload.encode()
        return payload

    async def send_bytes(self, payload: bytes) -> None:
        await self._websocket.send(payload)

    async def close(self) -> None:
        await self._websocket.close()


async def _connect_websockets_client(
    url: str,
    headers: dict[str, str] | None,
) -> WebSocketConnection:
    websocket = await connect(url, additional_headers=headers, proxy=None)
    return _WebsocketsClientConnection(websocket)


def _model_dump_object(value: BaseModel) -> dict[str, JsonValue]:
    return cast(
        dict[str, JsonValue],
        value.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        ),
    )


__all__ = [
    "ForwardWebSocket",
    "HttpAction",
    "HttpWebhook",
    "OneBot12Gateway",
    "ReverseWebSocket",
    "WebSocketAction",
    "WebSocketConnection",
]
