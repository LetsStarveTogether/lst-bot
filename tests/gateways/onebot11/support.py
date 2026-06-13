from __future__ import annotations

from collections.abc import Mapping
from typing import override

from pydantic import BaseModel, JsonValue

from lst_bot import ActionResponse, Bot, BotSelf, Retcode
from lst_bot.gateways.onebot11 import HttpAction, OneBot11Gateway
from tests.gateways.support import (
    FakePool,
    QueuedRequest,
    QueuedWebSocket,
    RobynServer,
    response_body,
)


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


class CaptureOneBot11Gateway(OneBot11Gateway):
    @override
    def __init__(self, bot: Bot) -> None:
        super().__init__(bot, action=HttpAction(base_url="https://capture.invalid"))
        self.calls: list[tuple[str, dict[str, JsonValue], BotSelf | None]] = []

    @override
    async def _request_http_action(
        self,
        backend: HttpAction,
        action: str,
        params: BaseModel,
    ) -> ActionResponse:
        _ = backend
        self.calls.append((action, params.model_dump(mode="json", by_alias=True), None))
        return ActionResponse.ok({"message_id": 1})


def action_response_payload(
    data: Mapping[str, JsonValue] | None = None,
    *,
    echo: str | None = None,
) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "status": "ok",
        "retcode": Retcode.OK,
        "data": dict(data or {"message_id": 1}),
    }
    if echo is not None:
        payload["echo"] = echo
    return payload


__all__ = [
    "CaptureOneBot11Gateway",
    "FakePool",
    "QueuedRequest",
    "QueuedWebSocket",
    "RobynServer",
    "action_response_payload",
    "friend_request_payload",
    "group_msg_payload",
    "group_request_payload",
    "private_msg_payload",
    "response_body",
]
