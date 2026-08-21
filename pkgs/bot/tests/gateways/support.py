from __future__ import annotations

from asyncio import to_thread
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Self, cast

import orjson
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
        body=(b"" if payload is None else orjson.dumps(payload))
        if body is None
        else body,
        status=status,
        headers=headers or {"Content-Type": "application/json"},
    )


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
                        json=cast(JsonValue, orjson.loads(body)),
                    )
                )
                payload = owner.payload
                response = (
                    payload.encode()
                    if isinstance(payload, str)
                    else orjson.dumps(payload)
                )
                self.send_response(owner.status)
                self.send_header("Content-Type", owner.content_type)
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, format: str, *args: object) -> None:
                _ = format, args

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
