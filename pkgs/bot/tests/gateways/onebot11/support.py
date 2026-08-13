from __future__ import annotations

from asyncio import to_thread
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Self, cast

import orjson
from bot import Retcode
from pydantic import JsonValue

_DEFAULT_RESPONSE_DATA = object()
_DEFAULT_ACTION_RESPONSE = object()


def private_msg_payload(message: JsonValue = "hello") -> dict[str, JsonValue]:
    return {
        "time": 1632847927,
        "self_id": 10000,
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": 12,
        "user_id": 42,
        "message": message,
        "raw_message": str(message) if isinstance(message, str) else "",
        "font": 0,
        "sender": {
            "user_id": 42,
            "nickname": "tester",
            "sex": "unknown",
            "age": 18,
        },
    }


def group_msg_payload(message: JsonValue = "hello") -> dict[str, JsonValue]:
    return {
        **private_msg_payload(message),
        "message_type": "group",
        "sub_type": "normal",
        "message_id": 13,
        "group_id": 20000,
        "anonymous": None,
    }


def friend_request_payload() -> dict[str, JsonValue]:
    return {
        "time": 1632847927,
        "self_id": 10000,
        "post_type": "request",
        "request_type": "friend",
        "sub_type": "",
        "user_id": 42,
        "comment": "hello",
        "flag": "friend-flag",
    }


def group_request_payload() -> dict[str, JsonValue]:
    return {
        **friend_request_payload(),
        "request_type": "group",
        "sub_type": "add",
        "group_id": 20000,
        "comment": "join",
        "flag": "group-flag",
    }


def action_response_payload(
    data: JsonValue | object = _DEFAULT_RESPONSE_DATA,
    *,
    echo: str | None = None,
) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "status": "ok",
        "retcode": Retcode.OK,
        "data": (
            {"message_id": 1}
            if data is _DEFAULT_RESPONSE_DATA
            else cast(JsonValue, data)
        ),
    }
    if echo is not None:
        payload["echo"] = echo
    return payload


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    path: str
    headers: dict[str, str]
    json: JsonValue


class ActionServer(AbstractAsyncContextManager["ActionServer"]):
    def __init__(
        self,
        payload: JsonValue | str | bytes | object = _DEFAULT_ACTION_RESPONSE,
        *,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        self.payload = (
            action_response_payload()
            if payload is _DEFAULT_ACTION_RESPONSE
            else cast(JsonValue | str | bytes, payload)
        )
        self.status = status
        self.requests: list[RecordedRequest] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None

    @property
    def base_url(self) -> str:
        server = self._server
        if server is None:
            msg = "ActionServer is not running"
            raise RuntimeError(msg)
        _, port = cast(tuple[str, int], server.server_address)
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
                    payload
                    if isinstance(payload, bytes)
                    else payload.encode()
                    if isinstance(payload, str)
                    else orjson.dumps(payload)
                )
                self.send_response(owner.status)
                self.send_header("Content-Type", "application/json")
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
        server = self._server
        thread = self._thread
        if server is not None:
            await to_thread(server.shutdown)
            server.server_close()
        if thread is not None:
            thread.join()
        self._server = None
        self._thread = None


__all__ = [
    "ActionServer",
    "RecordedRequest",
    "action_response_payload",
    "friend_request_payload",
    "group_msg_payload",
    "group_request_payload",
    "private_msg_payload",
]
