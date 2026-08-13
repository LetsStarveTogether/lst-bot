from __future__ import annotations

from asyncio import to_thread
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import cast

import orjson
import pytest
from bot import ActionResponse, ApiStatus, Bot
from bot.gateways.onebot12 import HttpAction, OneBot12Gateway
from pydantic import JsonValue
from urllib3_future import AsyncPoolManager

from .support import SELF

type RequestRecord = tuple[str, dict[str, str], dict[str, JsonValue]]
AUTH = "test-value"


@asynccontextmanager
async def action_server(
    *,
    status: HTTPStatus = HTTPStatus.OK,
    content_type: str = "application/json",
    payload: JsonValue = None,
) -> AsyncIterator[tuple[str, list[RequestRecord]]]:
    response_payload = (
        {"status": "ok", "retcode": 0, "data": None, "message": ""}
        if payload is None
        else payload
    )
    requests: list[RequestRecord] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = cast(dict[str, JsonValue], orjson.loads(self.rfile.read(length)))
            requests.append((self.path, dict(self.headers.items()), body))
            encoded = orjson.dumps(response_payload)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            _ = format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = cast(tuple[str, int], server.server_address)[1]
    try:
        yield f"http://127.0.0.1:{port}/action?source=test", requests
    finally:
        await to_thread(server.shutdown)
        server.server_close()
        await to_thread(thread.join)


async def test_http_action_preserves_wire_envelope_self_and_null() -> None:
    async with action_server() as (url, requests):
        bot = Bot()
        gateway = OneBot12Gateway(
            bot,
            action=HttpAction(url),
            access_token=AUTH,
        )
        bot.add_gateway(gateway)

        async with bot:
            response = cast(
                ActionResponse,
                await gateway.connection_for(SELF).action(
                    "vendor.test",
                    optional=None,
                ),
            )

    assert response.status == ApiStatus.OK
    assert response.data is None
    assert len(requests) == 1
    path, headers, body = requests[0]
    assert path == "/action?source=test"
    assert headers["Authorization"] == "Bearer test-value"
    assert body == {
        "action": "vendor.test",
        "params": {"optional": None},
        "self": {"platform": "qq", "user_id": "10000"},
    }


@pytest.mark.parametrize(
    ("status", "content_type", "message"),
    [
        pytest.param(
            HTTPStatus.UNAUTHORIZED,
            "application/json",
            "HTTP 401",
            id="non-200-status",
        ),
        pytest.param(
            HTTPStatus.OK,
            "text/plain",
            "unsupported Content-Type",
            id="non-json-content-type",
        ),
    ],
)
async def test_http_action_rejects_transport_contract_violations(
    status: HTTPStatus,
    content_type: str,
    message: str,
) -> None:
    async with action_server(status=status, content_type=content_type) as (url, _):
        bot = Bot()
        gateway = OneBot12Gateway(bot, action=HttpAction(url))
        bot.add_gateway(gateway)

        async with bot:
            with pytest.raises(RuntimeError, match=message):
                await gateway.connection_for(SELF).action("get_version")


async def test_gateway_does_not_close_borrowed_http_pool() -> None:
    async with action_server() as (url, requests):
        pool = AsyncPoolManager()
        bot = Bot()
        gateway = OneBot12Gateway(
            bot,
            action=HttpAction(url, http_pool=pool),
        )
        bot.add_gateway(gateway)
        try:
            async with bot:
                await gateway.connection_for(SELF).action("get_version")

            response = await pool.request("POST", url, json={"still": "open"})
            assert response.status == HTTPStatus.OK
            await response.data
        finally:
            await pool.clear()

    assert len(requests) == 2


async def test_closed_gateway_rejects_actions() -> None:
    async with action_server() as (url, _):
        bot = Bot()
        gateway = OneBot12Gateway(bot, action=HttpAction(url))
        bot.add_gateway(gateway)
        connection = gateway.connection_for(SELF)

        async with bot:
            pass

        with pytest.raises(RuntimeError, match="gateway is closed"):
            await connection.action("get_version")
