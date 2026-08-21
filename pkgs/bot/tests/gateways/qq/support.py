from typing import cast

from bot import Bot
from bot.gateways.base import WebSocketConnector
from bot.gateways.qq import QQGateway
from bot.gateways.qq_api import QQRestClient
from urllib3_future import AsyncPoolManager

from tests.gateways.support import Pool

CREDENTIAL = "secret"


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
        http_pool=cast(AsyncPoolManager, pool if pool is not None else Pool()),
        websocket_connector=websocket_connector,
    )
    gateway._online = online  # ruff: ignore[private-member-access]
    return gateway
