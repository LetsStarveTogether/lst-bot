from __future__ import annotations

from typing import override

from pydantic import JsonValue

from lst_bot import ActionResponse, Bot, BotSelf
from lst_bot.gateways.onebot12 import HttpAction, OneBot12Gateway
from lst_bot.protocol.actions import ActionParamModel
from tests.gateways.support import (
    FakePool,
    QueuedRequest,
    QueuedWebSocket,
    RobynServer,
    response_body,
)


class CaptureOneBot12Gateway(OneBot12Gateway):
    @override
    def __init__(self, bot: Bot) -> None:
        super().__init__(bot, action=HttpAction(base_url="https://capture.invalid"))
        self.calls: list[tuple[str, dict[str, JsonValue], BotSelf | None]] = []

    @override
    async def _request_http_action(
        self,
        backend: HttpAction,
        action: str,
        params: ActionParamModel,
        self_: BotSelf | None,
    ) -> ActionResponse:
        _ = backend
        self.calls.append((
            action,
            params.model_dump(mode="json", by_alias=True),
            self_,
        ))
        return ActionResponse.ok({"message_id": "out-1", "time": 1.0})


__all__ = [
    "CaptureOneBot12Gateway",
    "FakePool",
    "QueuedRequest",
    "QueuedWebSocket",
    "RobynServer",
    "response_body",
]
