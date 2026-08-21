from typing import cast

from bot import Bot
from bot.gateways.discord import (
    DiscordGateway,
    DiscordGatewayPayload,
    DiscordRestClient,
)
from pydantic import JsonValue
from urllib3_future import AsyncHTTPResponse, AsyncPoolManager

CREDENTIAL = "token"


class Pool:
    def __init__(self, *responses: AsyncHTTPResponse) -> None:
        self.responses = list(responses)
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


def client(pool: Pool) -> DiscordRestClient:
    return DiscordRestClient(
        CREDENTIAL,
        base_url="https://discord.example/api/v10",
        http_pool=cast(AsyncPoolManager, pool),
    )


def gateway(pool: Pool | None = None) -> DiscordGateway:
    return DiscordGateway(
        Bot(),
        token=CREDENTIAL,
        base_url="https://discord.example/api/v10",
        http_pool=cast(AsyncPoolManager, pool or Pool()),
    )


def interaction(*, sequence: int = 1) -> DiscordGatewayPayload:
    return DiscordGatewayPayload.model_validate({
        "op": 0,
        "s": sequence,
        "t": "INTERACTION_CREATE",
        "d": {
            "id": "10",
            "application_id": "11",
            "type": 2,
            "data": {"id": "12", "name": "test", "type": 1},
            "token": CREDENTIAL,
            "version": 1,
        },
    })


def user(user_id: str = "2") -> dict[str, JsonValue]:
    return {
        "id": user_id,
        "username": "user",
        "discriminator": "0",
        "global_name": None,
        "avatar": None,
    }


def ready_payload(session_id: str = "session") -> dict[str, JsonValue]:
    return {
        "v": 10,
        "user": {**user("1"), "bot": True},
        "guilds": [],
        "session_id": session_id,
        "resume_gateway_url": "wss://resume.discord.example",
        "application": {"id": "4", "flags": 0},
    }


def message(
    *,
    message_id: str = "10",
    channel_id: str = "20",
    guild_id: str | None = None,
    content: str = "hello <@3>",
) -> dict[str, JsonValue]:
    return {
        "id": message_id,
        "channel_id": channel_id,
        "author": user(),
        "content": content,
        "timestamp": "2026-08-19T00:00:00Z",
        "edited_timestamp": None,
        "tts": False,
        "mention_everyone": False,
        "mentions": [user("3")],
        "mention_roles": [],
        "attachments": [],
        "embeds": [],
        "pinned": False,
        "type": 0,
        **({"guild_id": guild_id} if guild_id is not None else {}),
    }
