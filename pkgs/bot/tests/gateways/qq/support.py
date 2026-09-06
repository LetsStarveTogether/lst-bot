from bot import Bot
from bot.gateways.base import WebSocketConnector
from bot.gateways.qq import QQGateway
from bot.gateways.qq_api import QQRestClient

from tests.gateways.support import HttpMock

CREDENTIAL = "secret"


def client(mock: HttpMock) -> QQRestClient:
    return QQRestClient(
        "app",
        CREDENTIAL,
        base_url="https://qq.example",
        http_client=mock.http_client,
    )


def gateway(
    mock: HttpMock | None = None,
    *,
    online: bool = False,
    websocket_connector: WebSocketConnector | None = None,
) -> QQGateway:
    gateway = QQGateway(
        Bot(),
        app_id="app",
        client_secret=CREDENTIAL,
        base_url="https://qq.example",
        http_client=(mock if mock is not None else HttpMock()).http_client,
        websocket_connector=websocket_connector,
    )
    gateway._online = online  # ruff: ignore[private-member-access]
    return gateway
