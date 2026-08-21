from asyncio import Event, to_thread
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Self, cast

from bot.json import dumpb, loads
from pydantic import JsonValue
from urllib3_future import AsyncHTTPResponse

_DEFAULT_ACTION_RESPONSE: JsonValue = {
    "status": "ok",
    "retcode": 0,
    "data": {"message_id": 1},
}


def response(
    status: int,
    payload: JsonValue = None,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> AsyncHTTPResponse:
    return AsyncHTTPResponse(
        body=(b"" if payload is None else dumpb(payload)) if body is None else body,
        status=status,
        headers=headers or {"Content-Type": "application/json"},
    )


class HangingBodyResponse(AsyncHTTPResponse):
    def __init__(self) -> None:
        super().__init__(status=HTTPStatus.OK)
        self.cancelled = Event()

    @property
    async def data(self) -> bytes:
        try:
            await Event().wait()
            return b""
        finally:
            self.cancelled.set()


class Pool:
    def __init__(self, *items: JsonValue | AsyncHTTPResponse) -> None:
        self.responses = [
            item if isinstance(item, AsyncHTTPResponse) else response(200, item)
            for item in items
        ]
        self.requests: list[tuple[str, str, dict[str, object]]] = []
        self.cleared = False

    async def request(
        self,
        method: str,
        url: str,
        **kwargs: object,
    ) -> AsyncHTTPResponse:
        self.requests.append((method, url, kwargs))
        return self.responses.pop(0)

    async def clear(self) -> None:
        self.cleared = True


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
