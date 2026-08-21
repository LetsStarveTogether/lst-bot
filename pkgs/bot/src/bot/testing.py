from asyncio import Event, Queue
from typing import override

from pydantic import JsonValue

from bot import (
    ActionCall,
    ActionResponse,
    Bot,
    BotSelf,
    Connection,
    Gateway,
    PrivateMessageEvent,
)
from bot.json import dumpb
from bot.protocol.actions import ActionParamModel


def private_message_event(
    text: str,
    *,
    user_id: str = "42",
    event_id: str = "evt-1",
    self_id: str = "bot",
) -> PrivateMessageEvent:
    return PrivateMessageEvent.model_validate({
        "id": event_id,
        "self": {"platform": "test", "user_id": self_id},
        "time": 1.0,
        "sub_type": "",
        "message_id": f"{event_id}-message",
        "message": [{"type": "text", "data": {"text": text}}],
        "alt_message": text,
        "user_id": user_id,
    })


class RecordingGateway(Gateway):
    def __init__(
        self,
        bot: Bot,
        *,
        responses: dict[str, ActionResponse] | None = None,
    ) -> None:
        super().__init__(bot)
        self.actions: list[ActionCall] = []
        self.responses = responses or {}

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
        return self.responses.get(action, ActionResponse.ok({"status": "ok"}))


def recording_gateway(bot: Bot) -> RecordingGateway:
    gateway = RecordingGateway(bot)
    bot.add_gateway(gateway)
    return gateway


class ScriptedWebSocket:
    def __init__(self, *incoming: JsonValue | BaseException) -> None:
        self.incoming: Queue[str | BaseException] = Queue()
        self.sent: Queue[str] = Queue()
        self.receiving = Event()
        self.send_allowed = Event()
        self.send_allowed.set()
        self.closed = Event()
        self.close_code: int | None = None
        for item in incoming:
            self.feed(item)

    def feed(self, item: JsonValue | BaseException) -> None:
        value = item if isinstance(item, BaseException) else dumpb(item).decode()
        self.incoming.put_nowait(value)

    def finish(self) -> None:
        self.feed(StopAsyncIteration())

    async def receive_text(self) -> str:
        self.receiving.set()
        try:
            value = await self.incoming.get()
        finally:
            self.receiving.clear()
        if isinstance(value, BaseException):
            raise value
        return value

    async def send_text(self, payload: str) -> None:
        await self.send_allowed.wait()
        await self.sent.put(payload)

    async def close(self, code: int = 1000) -> None:
        self.close_code = code
        self.closed.set()


__all__ = [
    "RecordingGateway",
    "ScriptedWebSocket",
    "private_message_event",
    "recording_gateway",
]
