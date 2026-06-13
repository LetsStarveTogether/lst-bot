from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, override

import pytest

from lst_bot import (
    ActionCall,
    ActionResponse,
    Bot,
    BotSelf,
    Connection,
    EventPayload,
    Gateway,
    PrivateMessageEvent,
)
from lst_bot.protocol.actions import ActionParamModel


class RecordingGateway(Gateway):
    @override
    def __init__(self, bot: Bot) -> None:
        super().__init__(bot)
        self.actions: list[ActionCall] = []

    @property
    def connection(self) -> Connection:
        return self.connection_for(BotSelf(platform="test", user_id="bot"))

    @override
    async def request_action(
        self,
        connection: Connection,
        action: str,
        params: ActionParamModel,
    ) -> ActionResponse:
        _ = connection
        self.actions.append(
            ActionCall.model_validate({"action": action, "params": params})
        )
        return ActionResponse.ok({"status": "ok"})


type RecordingGatewayFactory = Callable[[Bot], RecordingGateway]


class EventFactory(Protocol):
    def __call__(
        self,
        text: str,
        *,
        user_id: str = "42",
        event_id: str = "evt-1",
    ) -> PrivateMessageEvent: ...


@pytest.fixture
def recording_gateway() -> RecordingGatewayFactory:
    def create(bot: Bot) -> RecordingGateway:
        gateway = RecordingGateway(bot)
        bot.add_gateway(gateway)
        return gateway

    return create


@pytest.fixture
def recording_gateway_cls() -> type[RecordingGateway]:
    return RecordingGateway


@pytest.fixture
def make_event() -> EventFactory:
    def create(
        text: str,
        *,
        user_id: str = "42",
        event_id: str = "evt-1",
    ) -> PrivateMessageEvent:
        event = EventPayload.model_validate({
            "id": event_id,
            "self": {"platform": "test", "user_id": "bot"},
            "time": 1.0,
            "type": "message",
            "detail_type": "private",
            "sub_type": "",
            "message_id": f"{event_id}-message",
            "message": [{"type": "text", "data": {"text": text}}],
            "alt_message": text,
            "user_id": user_id,
        }).root
        if not isinstance(event, PrivateMessageEvent):
            msg = "event factory must build private message events"
            raise TypeError(msg)
        return event

    return create
