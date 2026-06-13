from __future__ import annotations

from asyncio import Queue, sleep, wait_for
from collections.abc import Awaitable, Callable, Mapping

import orjson
from pydantic import JsonValue
from robyn import Response as RobynResponse

from lst_bot import Retcode

type RobynRouteHandler = Callable[..., Awaitable[RobynResponse | None]]
type RobynRouteDecorator = Callable[[RobynRouteHandler], RobynRouteHandler]
type LifecycleHandler = Callable[[], Awaitable[None]]


class FakeResponse:
    def __init__(self, payload: JsonValue) -> None:
        self.payload = payload

    @property
    def data(self) -> Awaitable[bytes]:
        return sleep(0, result=orjson.dumps(self.payload))


class FakePool:
    def __init__(self, payload: JsonValue = None) -> None:
        self.payload = (
            payload
            if payload is not None
            else {
                "status": "ok",
                "retcode": Retcode.OK,
                "data": {"message_id": "out-1", "time": 1.0},
                "message": "",
            }
        )
        self.calls: list[dict[str, bytes | Mapping[str, str] | JsonValue]] = []
        self.cleared = False

    async def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | str | None = None,
        headers: Mapping[str, str] | None = None,
        json: JsonValue = None,
        multiplexed: bool = False,
        **kwargs: JsonValue,
    ) -> FakeResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "body": body,
                "headers": headers,
                "json": json,
                "multiplexed": multiplexed,
                "kwargs": kwargs,
            },
        )
        return FakeResponse(self.payload)

    async def clear(self) -> None:
        self.cleared = True


class RobynServer:
    def __init__(self) -> None:
        self.startup: LifecycleHandler | None = None
        self.shutdown: LifecycleHandler | None = None
        self.routes: list[tuple[str, str, RobynRouteHandler]] = []

    def post(self, endpoint: str) -> RobynRouteDecorator:
        def decorate(func: RobynRouteHandler) -> RobynRouteHandler:
            self.routes.append(("POST", endpoint, func))
            return func

        return decorate

    def websocket(self, endpoint: str) -> RobynRouteDecorator:
        def decorate(func: RobynRouteHandler) -> RobynRouteHandler:
            self.routes.append(("WS", endpoint, func))
            return func

        return decorate

    def startup_handler(self, handler: LifecycleHandler) -> None:
        self.startup = handler

    def shutdown_handler(self, handler: LifecycleHandler) -> None:
        self.shutdown = handler


class QueuedRequest:
    def __init__(
        self,
        payload: JsonValue,
        *,
        headers: Mapping[str, str] | None = None,
        query_params: Mapping[str, str] | None = None,
    ) -> None:
        self.payload = payload
        self.headers = dict(headers or {})
        self.query_params = dict(query_params or {})

    @property
    def body(self) -> bytes:
        return orjson.dumps(self.payload)


class QueuedWebSocket:
    def __init__(
        self,
        *payloads: JsonValue,
        headers: Mapping[str, str] | None = None,
        query_params: Mapping[str, str] | None = None,
    ) -> None:
        self.payloads = list(payloads)
        self.headers = dict(headers or {})
        self.query_params = dict(query_params or {})
        self.sent: list[bytes] = []
        self.closed = False

    async def receive_bytes(self) -> bytes:
        if not self.payloads:
            raise StopAsyncIteration
        return orjson.dumps(self.payloads.pop(0))

    async def send_bytes(self, payload: bytes) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True


class EchoResponseWebSocket:
    def __init__(
        self,
        first_payload: JsonValue,
        *,
        response_data: JsonValue = None,
    ) -> None:
        self.incoming: Queue[JsonValue | None] = Queue()
        self.incoming.put_nowait(first_payload)
        self.response_data = (
            response_data if response_data is not None else {"user_id": "42"}
        )
        self.sent: list[bytes] = []
        self.closed = False

    async def receive_bytes(self) -> bytes:
        payload = await wait_for(self.incoming.get(), timeout=1)
        if payload is None:
            raise StopAsyncIteration
        return orjson.dumps(payload)

    async def send_bytes(self, payload: bytes) -> None:
        self.sent.append(payload)
        request = orjson.loads(payload)
        self.incoming.put_nowait({
            "status": "ok",
            "retcode": Retcode.OK,
            "data": self.response_data,
            "message": "",
            "echo": request["echo"],
        })
        self.incoming.put_nowait(None)

    async def close(self) -> None:
        self.closed = True


class FailingReceiveWebSocket:
    def __init__(self) -> None:
        self.closed = False

    async def receive_bytes(self) -> bytes:
        msg = "receive failed"
        raise ConnectionError(msg)

    async def send_bytes(self, payload: bytes) -> None:
        _ = payload

    async def close(self) -> None:
        self.closed = True


def response_body(response: RobynResponse) -> dict[str, JsonValue] | list[JsonValue]:
    return orjson.loads(response.description)


__all__ = [
    "EchoResponseWebSocket",
    "FailingReceiveWebSocket",
    "FakePool",
    "FakeResponse",
    "QueuedRequest",
    "QueuedWebSocket",
    "RobynServer",
    "response_body",
]
