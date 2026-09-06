from bot import Bot
from bot.gateways.discord import (
    DiscordGateway,
    DiscordGatewayPayload,
    DiscordRestClient,
)
from pydantic import JsonValue

from tests.gateways.support import HttpMock

CREDENTIAL = "token"


def client(mock: HttpMock) -> DiscordRestClient:
    return DiscordRestClient(
        CREDENTIAL,
        base_url="https://discord.example/api/v10",
        http_client=mock.http_client,
    )


def gateway(mock: HttpMock | None = None) -> DiscordGateway:
    return DiscordGateway(
        Bot(),
        token=CREDENTIAL,
        base_url="https://discord.example/api/v10",
        http_client=(mock if mock is not None else HttpMock()).http_client,
    )


def interaction() -> DiscordGatewayPayload:
    return DiscordGatewayPayload.model_validate({
        "op": 0,
        "s": 1,
        "t": "INTERACTION_CREATE",
        "d": {
            "id": "10",
            "application_id": "11",
            "type": 2,
            "data": {"id": "12", "name": "test", "type": 1},
            "token": CREDENTIAL,
            "version": 1,
            "app_permissions": "0",
            "entitlements": [],
            "authorizing_integration_owners": {"0": "0"},
            "attachment_size_limit": 10_000_000,
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
        "application": {"id": "4", "flags": 0, "flags_new": "0"},
    }


def message(
    *,
    message_id: str = "10",
    guild_id: str | None = None,
    content: str = "hello <@3>",
) -> dict[str, JsonValue]:
    return {
        "id": message_id,
        "channel_id": "20",
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
