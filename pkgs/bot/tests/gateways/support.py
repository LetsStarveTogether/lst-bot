from asyncio import Event, to_thread
from collections.abc import AsyncIterator
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Self, cast, override

from bot import Bot
from bot.json import dumpb, loads
from httpx2 import (
    AsyncByteStream,
    AsyncClient,
    ByteStream,
    MockTransport,
    Request,
    Response,
)
from pydantic import JsonValue

_DEFAULT_ACTION_RESPONSE: JsonValue = {
    "status": "ok",
    "retcode": 0,
    "data": {"message_id": 1},
}


class ObservableReadinessBot(Bot):
    def __init__(self) -> None:
        super().__init__()
        self.waiting = Event()

    @override
    async def wait_until_running(self) -> None:
        self.waiting.set()
        await super().wait_until_running()


def response(
    status: int,
    payload: JsonValue = None,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> Response:
    content = (b"" if payload is None else dumpb(payload)) if body is None else body
    return Response(
        status,
        stream=ByteStream(content),
        headers=({"Content-Type": "application/json"} if headers is None else headers),
    )


class HangingBodyStream(AsyncByteStream):
    def __init__(self) -> None:
        self.cancelled = Event()
        self.close_called = Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            await Event().wait()
            yield b""
        finally:
            self.cancelled.set()

    async def aclose(self) -> None:
        self.close_called.set()


class HttpMock:
    def __init__(self, *items: JsonValue | Response) -> None:
        self.responses = [
            item if isinstance(item, Response) else response(200, item)
            for item in items
        ]
        self.requests: list[Request] = []
        self.http_client = AsyncClient(
            # Resolve the handler at dispatch time so tests can replace it.
            transport=MockTransport(lambda request: self.handle(request)),  # ruff: ignore[unnecessary-lambda]
            trust_env=False,
        )

    async def handle(self, request: Request) -> Response:
        self.requests.append(request)
        return self.responses.pop(0)


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    path: str
    headers: dict[str, str]
    json: JsonValue


class ActionServer:
    def __init__(
        self,
        payload: JsonValue | str = _DEFAULT_ACTION_RESPONSE,
        *,
        status: HTTPStatus = HTTPStatus.OK,
        content_type: str = "application/json",
    ) -> None:
        self.payload = payload
        self.status = status
        self.content_type = content_type
        self.requests: list[RecordedRequest] = []
        self.http_client = AsyncClient(trust_env=False)
        self._server: ThreadingHTTPServer
        self._thread: Thread

    @property
    def base_url(self) -> str:
        _, port = cast(tuple[str, int], self._server.server_address)
        return f"http://127.0.0.1:{port}"

    async def __aenter__(self) -> Self:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                size = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(size)
                owner.requests.append(
                    RecordedRequest(
                        path=self.path,
                        headers=dict(self.headers.items()),
                        json=cast(JsonValue, loads(body)),
                    )
                )
                payload = owner.payload
                response = (
                    payload.encode() if isinstance(payload, str) else dumpb(payload)
                )
                self.send_response(owner.status)
                self.send_header("Content-Type", owner.content_type)
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, *args: object, **kwargs: object) -> None:
                _ = args, kwargs

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self._thread.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        _ = exc_type, exc, traceback
        await to_thread(self._server.shutdown)
        self._server.server_close()
        self._thread.join()
        await self.http_client.aclose()
