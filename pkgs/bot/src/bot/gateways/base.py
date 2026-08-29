from asyncio import (
    FIRST_COMPLETED,
    CancelledError,
    Future,
    create_task,
    gather,
    get_running_loop,
    timeout,
    wait,
)
from asyncio import Event as AsyncEvent
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from hmac import compare_digest
from logging import getLogger
from types import TracebackType
from typing import TYPE_CHECKING, Annotated, Protocol, Self
from urllib.parse import parse_qs
from uuid import uuid4

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    Field,
    JsonValue,
    SecretStr,
    StrictFloat,
    StrictInt,
    StrictStr,
    TypeAdapter,
    UrlConstraints,
)
from robyn import Response
from urllib3_future import AsyncHTTPResponse, AsyncPoolManager
from urllib3_future.exceptions import HTTPError
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK

from bot._tasks import await_cleanup
from bot.json import dumpb
from bot.protocol.actions import (
    ActionCall,
    ActionParamInput,
    ActionParamModel,
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

logger = getLogger(__name__)

type AccessToken = SecretStr | str | None
type NonWhitespaceStr = Annotated[StrictStr, Field(pattern=r"^\S+$")]
type PositiveSeconds = Annotated[
    StrictFloat,
    Field(gt=0, allow_inf_nan=False),
]
type TcpPort = Annotated[StrictInt, Field(ge=0, le=65535)]
_POSITIVE_SECONDS_ADAPTER = TypeAdapter(PositiveSeconds)
_HTTP_BASE_URL_ADAPTER = TypeAdapter(AnyHttpUrl)
_HTTPS_BASE_URL_ADAPTER = TypeAdapter(
    Annotated[AnyHttpUrl, UrlConstraints(allowed_schemes=["https"])]
)
# ponytail: one shared ceiling; split only for a documented upstream limit.
_MAX_HTTP_RESPONSE_BYTES = 16 * 1024 * 1024


def validate_https_base_url(value: str, platform: str) -> str:
    msg = f"{platform} API base URL must be an absolute HTTPS URL"
    if not isinstance(value, str) or any(character.isspace() for character in value):
        raise ValueError(msg)
    try:
        parsed = _HTTPS_BASE_URL_ADAPTER.validate_python(value)
    except ValueError:
        raise ValueError(msg) from None
    if url_has_credentials(parsed) or parsed.query or parsed.fragment:
        raise ValueError(msg)
    return str(parsed).rstrip("/")


def url_has_credentials(value: object) -> bool:
    return (
        getattr(value, "username", None) is not None
        or getattr(value, "password", None) is not None
    )


async def read_http_body(
    response: AsyncHTTPResponse,
    max_bytes: int = _MAX_HTTP_RESPONSE_BYTES,
) -> bytes:
    try:
        body = await response.read(max_bytes + 1, decode_content=True)
        if len(body) > max_bytes:
            msg = f"HTTP response exceeds the {max_bytes}-byte limit"
            raise HTTPError(msg)
        return body
    finally:

        async def close() -> None:
            with suppress(Exception):
                await response.close()

        await await_cleanup(create_task(close()))


async def run_while_open[T](
    operation: Coroutine[object, object, T],
    closed_event: AsyncEvent,
    ensure_open: Callable[[AsyncEvent], None],
) -> T:
    operation_task = create_task(operation)
    closed_task = create_task(closed_event.wait())
    try:
        await wait((operation_task, closed_task), return_when=FIRST_COMPLETED)
        ensure_open(closed_event)
        result = await operation_task
        ensure_open(closed_event)
        return result
    finally:
        closed_task.cancel()
        operation_task.cancel()
        await await_cleanup(gather(closed_task, operation_task, return_exceptions=True))


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


@dataclass(slots=True)
class HttpAction:
    base_url: str
    timeout: float = 30.0
    http_pool: AsyncPoolManager | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        msg = "HTTP action base URL must be an absolute HTTP(S) URL"
        if not isinstance(self.base_url, str) or any(
            character.isspace() for character in self.base_url
        ):
            raise ValueError(msg)
        try:
            parsed = _HTTP_BASE_URL_ADAPTER.validate_python(self.base_url)
        except ValueError:
            raise ValueError(msg) from None
        if parsed.fragment is not None or url_has_credentials(parsed):
            raise ValueError(msg)
        self.base_url = str(parsed)
        self.timeout = _POSITIVE_SECONDS_ADAPTER.validate_python(self.timeout)


@dataclass(frozen=True, slots=True)
class WebSocketAction:
    timeout: float = 30.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "timeout",
            _POSITIVE_SECONDS_ADAPTER.validate_python(self.timeout),
        )


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
        except ConnectionClosedOK as exc:
            raise StopAsyncIteration from exc
        except ConnectionClosed as exc:
            raise WebSocketClosedError(
                exc.rcvd.code if exc.rcvd is not None else None
            ) from exc
        if isinstance(payload, bytes):
            msg = "WebSocket text frame required"
            raise TypeError(msg)
        return payload

    async def send_text(self, payload: str) -> None:
        try:
            await self.websocket.send(payload)
        except CancelledError:

            async def close() -> None:
                with suppress(Exception):
                    await self.websocket.close()

            await await_cleanup(create_task(close()))
            raise
        except ConnectionClosed as exc:
            raise WebSocketClosedError(
                exc.rcvd.code if exc.rcvd is not None else None
            ) from exc

    async def close(self, code: int = 1000) -> None:
        await self.websocket.close(code=code)


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


@dataclass(frozen=True, slots=True)
class Connection:
    gateway: Gateway
    self_: BotSelf

    def __str__(self) -> str:
        return f"{self.gateway}@{self.self_}"

    async def action(
        self,
        action: str | Action,
        **params: ActionParamInput,
    ) -> BaseModel:
        action_call = ActionCall.model_validate({
            "action": action,
            "params": params,
        })
        return await self.request_action(action_call.action, action_call.params)

    async def send_msg(
        self,
        msg: MsgInput,
        **params: ActionParamInput,
    ) -> BaseModel:
        if "detail_type" not in params:
            targets = [
                detail_type
                for detail_type, fields in (
                    ("channel", ("guild_id", "channel_id")),
                    ("group", ("group_id",)),
                    ("private", ("user_id",)),
                )
                if all(field in params for field in fields)
            ]
            if len(targets) == 1:
                params["detail_type"] = targets[0]
        return await self.action(
            Action.SEND_MESSAGE,
            message=Msg.from_input(msg),
            **params,
        )

    async def execute_message_action(
        self,
        event: MessageEvent,
        msg: MsgInput,
    ) -> BaseModel:
        return await self.action(
            Action.SEND_MESSAGE,
            **self._message_action_params(event, msg),
        )

    async def request_action(
        self,
        action: str,
        params: ActionParamModel,
    ) -> BaseModel:
        logger.info(
            "execute action: %s @ %s",
            action,
            self.self_,
        )
        response = await self.gateway.request_action(self, action, params)
        response_text = (
            f"{response.status}:{response.retcode}"
            if isinstance(response, ActionResponse)
            else type(response).__name__
        )
        logger.info(
            "action done: %s @ %s = %s",
            action,
            self.self_,
            response_text,
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
        self._mounted = False

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
        return Connection(self, self_)

    async def dispatch_event(self, event: Event) -> None:
        connection = (
            self.connection_for(event.self_) if event.self_ is not None else None
        )
        await self.bot.dispatch(connection, event, gateway=self)

    def enqueue_event(self, event: Event) -> None:
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
            call = action.action_call
            if call is None:
                msg = "Call return action requires an action call"
                raise TypeError(msg)
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
        self.bot.mount_server(server)
        if self._mounted:
            return False
        self._mounted = True
        return True


@dataclass(slots=True)
class WebSocketActionSession:
    websocket: WebSocketConnection
    selfs: set[tuple[str, str]] = field(default_factory=set)


class WebSocketActionManager:
    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
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
        session.selfs.add((self_.platform, self_.user_id))

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
                logger.debug("send WebSocket action request: %s @ %s", echo, self_)
                await session.websocket.send_text(build_payload(echo))
                return await future
        finally:
            self._pending.pop(echo, None)
            future.cancel()
            if not future.cancelled():
                future.exception()

    def receive(
        self,
        session: WebSocketActionSession,
        response: ActionResponse,
    ) -> bool:
        echo = response.echo
        if not isinstance(echo, str):
            logger.warning(
                "WebSocket action response missing echo: %s:%s",
                response.status,
                response.retcode,
            )
            return False
        pending = self._pending.get(echo)
        if pending is None:
            logger.warning(
                "unmatched WebSocket action response: echo=%s",
                echo,
            )
            return False
        pending_session, future = pending
        if pending_session is not session:
            logger.warning(
                "mismatched WebSocket action response source: echo=%s",
                echo,
            )
            return False
        if future.done():
            return False
        future.set_result(response)
        logger.debug(
            "receive WebSocket action response: echo=%s %s:%s",
            echo,
            response.status,
            response.retcode,
        )
        return True

    def fail_all(self) -> None:
        exc = ConnectionError("WebSocket action backend closed")
        for _, future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()
        self._sessions.clear()

    def _session_for(self, self_: BotSelf) -> WebSocketActionSession | None:
        key = (self_.platform, self_.user_id)
        return next(
            (session for session in reversed(self._sessions) if key in session.selfs),
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
        body = dumpb(payload)
    return Response(status, {"Content-Type": "application/json"}, body=body)


def text_response(status: int, text: str) -> Response:
    return Response(status, {"Content-Type": "text/plain; charset=utf-8"}, body=text)


def empty_response(status: int) -> Response:
    return Response(status, {}, body="")


def access_token_value(access_token: AccessToken) -> str | None:
    if isinstance(access_token, SecretStr):
        access_token = access_token.get_secret_value()
    if access_token is not None and not isinstance(access_token, str):
        msg = "access token must be a string"
        raise TypeError(msg)
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
        scheme, separator, token = authorizations[0].partition(" ")
        return token if separator and scheme.casefold() == "bearer" else None

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
