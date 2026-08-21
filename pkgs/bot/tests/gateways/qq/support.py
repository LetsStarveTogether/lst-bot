from typing import cast

from bot import Bot
from bot.gateways.base import WebSocketConnector
from bot.gateways.qq import QQGateway
from bot.gateways.qq_api import QQRestClient
from pydantic import JsonValue
from urllib3_future import AsyncHTTPResponse, AsyncPoolManager

from tests.gateways.support import response

CREDENTIAL = "secret"


class Pool:
    def __init__(self, *responses: JsonValue | AsyncHTTPResponse) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, str, dict[str, object]]] = []

    async def request(
        self,
        method: str,
        url: str,
        **kwargs: object,
    ) -> AsyncHTTPResponse:
        self.requests.append((method, url, kwargs))
        item = self.responses.pop(0)
        return item if isinstance(item, AsyncHTTPResponse) else response(200, item)


def client(pool: object) -> QQRestClient:
    return QQRestClient(
        "app",
        CREDENTIAL,
        base_url="https://qq.example",
        http_pool=cast(AsyncPoolManager, pool),
    )


def gateway(
    pool: Pool | None = None,
    *,
    online: bool = False,
    websocket_connector: WebSocketConnector | None = None,
) -> QQGateway:
    gateway = QQGateway(
        Bot(),
        app_id="app",
        client_secret=CREDENTIAL,
        base_url="https://qq.example",
        http_pool=cast(AsyncPoolManager, pool or Pool()),
        websocket_connector=websocket_connector,
    )
    gateway._online = online  # ruff: ignore[private-member-access]
    return gateway
